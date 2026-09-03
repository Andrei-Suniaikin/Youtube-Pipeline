"""Adaptive temporal overlay detection for donor footage.

The detector deliberately separates evidence gathering from policy.  Pixel
analysis emits frame-indexed observations; deterministic tracking then decides
whether a stable edge mark can be removed with a minimal crop or whether the
affected frames must be excluded/reselected.  Central salient objects are never
turned into an automatic crop.
"""

from __future__ import annotations

from collections import defaultdict
import csv
import io
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Mapping, Sequence

from .broad_lazy import (
    BroadLazyError,
    build_donor_overlay_profile,
    evaluate_candidate_overlay_profile,
    validate_donor_overlay_profile,
)


DETECTOR_NAME = "semantic-temporal-overlay"
DETECTOR_VERSION = "2.1.0"
PROFILE_DETECTOR_VERSION = "separated-context-overlay-v2"

# Candidate-local title evidence is intentionally stricter than a raw pixel
# component.  Two-frame glints and one OCR sample are common in water, grass,
# branches and letterbox boundaries; neither is proof of screen-fixed text.
MIN_TRANSIENT_TITLE_FRAMES = 3
MAX_TRANSIENT_SCENE_CHANGE_RATIO = 0.10
MIN_TRANSIENT_WIDTH_RATIO = 0.12
MIN_TRANSIENT_HEIGHT_RATIO = 0.025


class OverlayDetectorError(ValueError):
    """Overlay evidence is malformed or cannot be classified safely."""


def _number(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise OverlayDetectorError(f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise OverlayDetectorError(f"{name} must be finite")
    return result


def _bbox(value: Any, width: int, height: int, name: str) -> tuple[int, int, int, int]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
        raise OverlayDetectorError(f"{name} must be [x1, y1, x2, y2]")
    try:
        x1, y1, x2, y2 = (int(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise OverlayDetectorError(f"{name} must contain integers") from exc
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        raise OverlayDetectorError(f"{name} is outside the frame")
    return x1, y1, x2, y2


def _iou(first: Sequence[int], second: Sequence[int]) -> float:
    ax1, ay1, ax2, ay2 = first
    bx1, by1, bx2, by2 = second
    left, top = max(ax1, bx1), max(ay1, by1)
    right, bottom = min(ax2, bx2), min(ay2, by2)
    intersection = max(0, right - left) * max(0, bottom - top)
    if intersection <= 0:
        return 0.0
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - intersection
    return intersection / union if union else 0.0


def _ocr_tokens_compatible(first: str | None, second: str | None) -> bool:
    if not first or not second:
        return first == second
    return first == second or first in second or second in first


def _contiguous_ranges(frames: Sequence[int], max_gap: int) -> list[tuple[int, int]]:
    if not frames:
        return []
    ordered = sorted(set(int(item) for item in frames))
    output: list[tuple[int, int]] = []
    start = previous = ordered[0]
    for frame in ordered[1:]:
        if frame - previous > max_gap + 1:
            output.append((start, previous + 1))
            start = frame
        previous = frame
    output.append((start, previous + 1))
    return output


def track_overlay_observations(
    observations: Sequence[Mapping[str, Any]],
    *,
    frame_count: int,
    width: int,
    height: int,
    max_gap_frames: int = 2,
    min_iou: float = 0.35,
) -> list[dict[str, Any]]:
    """Associate planted/pixel observations into stable temporal tracks."""

    if frame_count <= 0 or width <= 0 or height <= 0:
        raise OverlayDetectorError("frame_count and dimensions must be positive")
    if max_gap_frames < 0 or not 0.0 <= min_iou <= 1.0:
        raise OverlayDetectorError("invalid tracking thresholds")
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(observations):
        if not isinstance(raw, Mapping):
            raise OverlayDetectorError(f"observation {index} must be a mapping")
        try:
            frame = int(raw["frame_index"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OverlayDetectorError(f"observation {index} requires frame_index") from exc
        if isinstance(raw.get("frame_index"), bool) or not 0 <= frame < frame_count:
            raise OverlayDetectorError(f"observation {index} frame_index is outside the clip")
        confidence = _number(raw.get("confidence", 1.0), f"observation {index}.confidence")
        if not 0.0 <= confidence <= 1.0:
            raise OverlayDetectorError(f"observation {index}.confidence must be in [0, 1]")
        item = {
            "frame_index": frame,
            "bbox": _bbox(raw.get("bbox"), width, height, f"observation {index}.bbox"),
            "confidence": confidence,
            "class_hint": str(raw.get("class_hint") or "overlay_candidate"),
        }
        if raw.get("ocr_token") is not None:
            token = re.sub(r"[^a-z0-9]", "", str(raw.get("ocr_token")).casefold())
            if token:
                item["ocr_token"] = token
        if raw.get("ocr_sample_position") is not None:
            try:
                sample_position = int(raw["ocr_sample_position"])
            except (TypeError, ValueError) as exc:
                raise OverlayDetectorError(
                    f"observation {index}.ocr_sample_position must be a non-negative integer"
                ) from exc
            if isinstance(raw.get("ocr_sample_position"), bool) or sample_position < 0:
                raise OverlayDetectorError(
                    f"observation {index}.ocr_sample_position must be a non-negative integer"
                )
            item["ocr_sample_position"] = sample_position
        if raw.get("global_change_ratio") is not None:
            ratio = _number(
                raw.get("global_change_ratio"),
                f"observation {index}.global_change_ratio",
            )
            if not 0.0 <= ratio <= 1.0:
                raise OverlayDetectorError(
                    f"observation {index}.global_change_ratio must be in [0, 1]"
                )
            item["global_change_ratio"] = ratio
        normalized.append(item)
    normalized.sort(key=lambda item: (item["frame_index"], item["bbox"]))

    tracks: list[dict[str, Any]] = []
    for item in normalized:
        best_index = None
        best_score = 0.0
        for index, track in enumerate(tracks):
            gap = item["frame_index"] - track["last_frame"]
            if gap <= 0 or gap > max_gap_frames + 1:
                continue
            if item["class_hint"] != track["last_class_hint"]:
                continue
            if item["class_hint"] == "ocr_text" and not _ocr_tokens_compatible(
                item.get("ocr_token"), track.get("last_ocr_token")
            ):
                continue
            score = _iou(item["bbox"], track["last_bbox"])
            if score >= min_iou and score > best_score:
                best_index, best_score = index, score
        if best_index is None:
            tracks.append(
                {
                    "frames": [item["frame_index"]],
                    "bboxes": [item["bbox"]],
                    "confidences": [item["confidence"]],
                    "class_hints": [item["class_hint"]],
                    "ocr_tokens": [item["ocr_token"]] if item.get("ocr_token") else [],
                    "ocr_sample_positions": (
                        [item["ocr_sample_position"]]
                        if item.get("ocr_sample_position") is not None
                        else []
                    ),
                    "global_change_ratios": (
                        [item["global_change_ratio"]]
                        if item.get("global_change_ratio") is not None
                        else []
                    ),
                    "last_frame": item["frame_index"],
                    "last_bbox": item["bbox"],
                    "last_class_hint": item["class_hint"],
                    "last_ocr_token": item.get("ocr_token"),
                }
            )
            continue
        track = tracks[best_index]
        track["frames"].append(item["frame_index"])
        track["bboxes"].append(item["bbox"])
        track["confidences"].append(item["confidence"])
        track["class_hints"].append(item["class_hint"])
        if item.get("ocr_token"):
            track["ocr_tokens"].append(item["ocr_token"])
        if item.get("ocr_sample_position") is not None:
            track["ocr_sample_positions"].append(item["ocr_sample_position"])
        if item.get("global_change_ratio") is not None:
            track["global_change_ratios"].append(item["global_change_ratio"])
        track["last_frame"] = item["frame_index"]
        track["last_bbox"] = item["bbox"]
        track["last_class_hint"] = item["class_hint"]
        track["last_ocr_token"] = item.get("ocr_token")

    output: list[dict[str, Any]] = []
    for index, track in enumerate(tracks):
        boxes = track["bboxes"]
        union = (
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )
        hints = track["class_hints"]
        hint = max(set(hints), key=lambda item: (hints.count(item), item))
        unique_frames = sorted(set(track["frames"]))
        row = {
                "track_id": f"overlay-{index:04d}",
                "bbox": list(union),
                "frames": unique_frames,
                "frame_ranges": [list(item) for item in _contiguous_ranges(unique_frames, max_gap_frames)],
                "frames_seen": len(unique_frames),
                "persistence": len(unique_frames) / frame_count,
                "confidence": sum(track["confidences"]) / len(track["confidences"]),
                "class_hint": hint,
            }
        if track["ocr_tokens"]:
            row["ocr_tokens"] = sorted(set(track["ocr_tokens"]))
        if track["ocr_sample_positions"]:
            positions = sorted(set(track["ocr_sample_positions"]))
            row["ocr_sample_positions"] = positions
            row["independent_sample_count"] = len(positions)
        if track["global_change_ratios"]:
            row["max_global_change_ratio"] = round(
                max(track["global_change_ratios"]), 9
            )
        output.append(row)
    return output


def _edge_crop_candidate(
    bbox: Sequence[int],
    *,
    width: int,
    height: int,
    padding_pixels: int,
    edge_anchor_percent: float,
    max_crop_percent: float,
) -> tuple[str, float] | None:
    x1, y1, x2, y2 = bbox
    anchor_x = width * edge_anchor_percent
    anchor_y = height * edge_anchor_percent
    candidates: list[tuple[str, float]] = []
    if x1 <= anchor_x:
        candidates.append(("left", (x2 + padding_pixels) / width))
    if width - x2 <= anchor_x:
        candidates.append(("right", (width - x1 + padding_pixels) / width))
    if y1 <= anchor_y:
        candidates.append(("top", (y2 + padding_pixels) / height))
    if height - y2 <= anchor_y:
        candidates.append(("bottom", (height - y1 + padding_pixels) / height))
    eligible = [item for item in candidates if item[1] <= max_crop_percent]
    return min(eligible, key=lambda item: (item[1], item[0])) if eligible else None


def analyze_overlay_observations(
    observations: Sequence[Mapping[str, Any]],
    *,
    frame_count: int,
    width: int,
    height: int,
    fps: float,
    persistent_ratio: float = 0.70,
    min_track_frames: int = 3,
    edge_anchor_percent: float = 0.10,
    max_crop_percent: float = 0.25,
    min_retained_area: float = 0.65,
    padding_pixels: int = 12,
    range_padding_frames: int = 2,
    tracking_max_gap_frames: int = 2,
) -> dict[str, Any]:
    """Classify tracks and return crop/exclusion policy with provenance."""

    if not math.isfinite(float(fps)) or fps <= 0:
        raise OverlayDetectorError("fps must be positive")
    tracks = track_overlay_observations(
        observations,
        frame_count=frame_count,
        width=width,
        height=height,
        max_gap_frames=tracking_max_gap_frames,
    )
    margins = {"left": 0.0, "right": 0.0, "top": 0.0, "bottom": 0.0}
    excluded: list[dict[str, Any]] = []
    inpaint_markers: list[dict[str, Any]] = []
    classified: list[dict[str, Any]] = []
    crop_tracks: list[dict[str, Any]] = []

    for raw in tracks:
        track = dict(raw)
        x1, y1, x2, y2 = track["bbox"]
        area_ratio = ((x2 - x1) * (y2 - y1)) / float(width * height)
        central = x1 < width * 0.80 and x2 > width * 0.20 and y1 < height * 0.80 and y2 > height * 0.20
        subject_like = track["class_hint"] in {"subject", "subject_saliency"} or (
            track["class_hint"] not in {
                "ocr_text",
                "persistent_pixel_region",
                "transient_text_candidate",
            }
            and central
            and area_ratio >= 0.035
        )
        persistent = (
            track["persistence"] >= persistent_ratio
            and track["frames_seen"] >= min_track_frames
        )
        crop = _edge_crop_candidate(
            track["bbox"],
            width=width,
            height=height,
            padding_pixels=padding_pixels,
            edge_anchor_percent=edge_anchor_percent,
            max_crop_percent=max_crop_percent,
        )

        if subject_like:
            track.update({"classification": "subject_saliency", "action": "ignore"})
        elif persistent and crop is not None:
            edge, amount = crop
            track.update(
                {
                    "classification": "persistent_edge_overlay",
                    "action": "crop",
                    "crop_edge": edge,
                    "crop_percent": round(amount, 9),
                }
            )
            margins[edge] = max(margins[edge], amount)
            crop_tracks.append(track)
        elif persistent:
            track.update(
                {
                    "classification": "persistent_central_overlay",
                    "action": "inpaint_required",
                }
            )
            inpaint_markers.append(
                {
                    "track_id": track["track_id"],
                    "bbox": list(track["bbox"]),
                    "start_frame": 0,
                    "end_frame": frame_count,
                    "status": "required",
                }
            )
            excluded.append(
                {
                    "kind": "persistent_overlay",
                    "start_frame": 0,
                    "end_frame": frame_count,
                    "confidence": track["confidence"],
                    "track_id": track["track_id"],
                }
            )
        elif track["frames_seen"] >= min_track_frames:
            track.update(
                {
                    "classification": "transient_overlay",
                    "action": "exclude_reselect",
                }
            )
            for start, end in track["frame_ranges"]:
                excluded.append(
                    {
                        "kind": "transient_overlay",
                        "start_frame": max(0, start - range_padding_frames),
                        "end_frame": min(frame_count, end + range_padding_frames),
                        "confidence": track["confidence"],
                        "track_id": track["track_id"],
                    }
                )
        else:
            track.update({"classification": "insufficient_evidence", "action": "ignore"})
        classified.append(track)

    retained_area = (1.0 - margins["left"] - margins["right"]) * (
        1.0 - margins["top"] - margins["bottom"]
    )
    crop_safe = bool(crop_tracks) and retained_area >= min_retained_area
    if crop_tracks and not crop_safe:
        # A large blanket crop is worse than asking the selector for another
        # clean clip.  Convert the implicated tracks to explicit exclusions.
        margins = {key: 0.0 for key in margins}
        for track in crop_tracks:
            track["classification"] = "edge_overlay_crop_too_aggressive"
            track["action"] = "exclude_reselect"
            for start, end in track["frame_ranges"]:
                excluded.append(
                    {
                        "kind": "edge_overlay_crop_too_aggressive",
                        "start_frame": max(0, start - range_padding_frames),
                        "end_frame": min(frame_count, end + range_padding_frames),
                        "confidence": track["confidence"],
                        "track_id": track["track_id"],
                    }
                )

    excluded.sort(key=lambda item: (item["start_frame"], item["end_frame"], item["kind"]))
    confidences = [float(track["confidence"]) for track in classified if track["action"] != "ignore"]
    if inpaint_markers and crop_safe:
        action = "crop_and_inpaint"
    elif inpaint_markers:
        action = "inpaint_required"
    elif excluded and crop_safe:
        action = "crop_and_exclude"
    elif excluded:
        action = "exclude_reselect"
    elif crop_safe:
        action = "crop"
    else:
        action = "none"
    return {
        "schema_version": 2,
        "detector": {
            "name": DETECTOR_NAME,
            "version": DETECTOR_VERSION,
            "mode": "adaptive_temporal_tracks",
        },
        "confidence": round(sum(confidences) / len(confidences), 6) if confidences else 1.0,
        "frame_count": frame_count,
        "fps": float(fps),
        "dimensions": {"width": width, "height": height},
        "action": action,
        "crop_safe": crop_safe,
        "crop_margins": {key: round(value, 9) for key, value in margins.items()},
        "retained_area": round(
            (1.0 - margins["left"] - margins["right"])
            * (1.0 - margins["top"] - margins["bottom"]),
            6,
        ),
        "excluded_ranges": excluded,
        "inpaint_markers": inpaint_markers,
        "tracks": classified,
        "thresholds": {
            "persistent_ratio": persistent_ratio,
            "min_track_frames": min_track_frames,
            "edge_anchor_percent": edge_anchor_percent,
            "max_crop_percent": max_crop_percent,
            "min_retained_area": min_retained_area,
            "padding_pixels": padding_pixels,
            "range_padding_frames": range_padding_frames,
            "tracking_max_gap_frames": tracking_max_gap_frames,
        },
    }


def _ocr_observations(
    frames: Sequence[Any],
    *,
    tesseract_bin: str = "tesseract",
    max_sample_frames: int = 12,
) -> list[dict[str, Any]]:
    """Detect screen text with the installed OCR engine on temporal samples.

    Each sampled observation is expanded only over that sample's Voronoi cell.
    Persistence therefore means OCR independently saw the same screen-space
    text on multiple samples; a one-frame hallucination cannot become a
    full-clip logo track.
    """

    try:
        import cv2
        import numpy as np
    except ImportError as exc:  # pragma: no cover
        raise OverlayDetectorError("opencv-python and numpy are required") from exc
    binary = shutil.which(str(tesseract_bin))
    if not binary or not frames:
        return []
    tessdata_dir = os.environ.get("SEMANTIC_MONTAGE_TESSDATA_DIR", "").strip()
    languages = ["eng"]
    if tessdata_dir:
        data_root = Path(tessdata_dir).expanduser().resolve()
        languages = [
            language
            for language in ("eng", "rus")
            if (data_root / f"{language}.traineddata").is_file()
        ]
        if not languages:
            return []
    sample_count = min(len(frames), max(3, int(max_sample_frames)))
    sample_indices = sorted(
        {int(round(value)) for value in np.linspace(0, len(frames) - 1, sample_count)}
    )
    boundaries = [0]
    boundaries.extend(
        int((left + right) // 2 + 1)
        for left, right in zip(sample_indices, sample_indices[1:])
    )
    boundaries.append(len(frames))
    sampled: list[list[dict[str, Any]]] = []
    for sample_position, frame_index in enumerate(sample_indices):
        image = np.asarray(frames[frame_index])
        ok, encoded = cv2.imencode(".png", image)
        if not ok:
            sampled.append([])
            continue
        try:
            command = [binary, "stdin", "stdout"]
            if tessdata_dir:
                command.extend(["--tessdata-dir", str(data_root)])
            command.extend(["-l", "+".join(languages), "--psm", "11", "tsv"])
            result = subprocess.run(
                command,
                input=encoded.tobytes(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=12.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            sampled.append([])
            continue
        if result.returncode != 0:
            sampled.append([])
            continue
        text = result.stdout.decode("utf-8", errors="replace")
        rows = csv.DictReader(io.StringIO(text), delimiter="\t")
        detections: list[dict[str, Any]] = []
        for row in rows:
            token = str(row.get("text") or "").strip()
            try:
                confidence = float(row.get("conf") or -1.0)
                x, y = int(row.get("left") or 0), int(row.get("top") or 0)
                width, height = int(row.get("width") or 0), int(row.get("height") or 0)
            except (TypeError, ValueError):
                continue
            normalized_token = re.sub(r"[^A-Za-z0-9]", "", token)
            edge_token = (
                x <= image.shape[1] * 0.20
                or y <= image.shape[0] * 0.20
                or x + width >= image.shape[1] * 0.80
                or y + height >= image.shape[0] * 0.80
            )
            # One-character age marks and broadcaster letters are important,
            # but natural textures frequently hallucinate isolated glyphs.
            if len(normalized_token) < 2 and not (
                len(normalized_token) == 1
                and edge_token
                and (normalized_token.isdigit() or normalized_token.isupper())
            ):
                continue
            threshold = 82.0 if len(normalized_token) == 1 else 58.0
            if confidence < threshold or width < 3 or height < 3:
                continue
            x2, y2 = min(image.shape[1], x + width), min(image.shape[0], y + height)
            if x2 <= x or y2 <= y:
                continue
            detections.append(
                {
                    "bbox": [x, y, x2, y2],
                    "confidence": min(0.99, 0.70 + confidence / 330.0),
                    "token": normalized_token.casefold(),
                }
            )
        sampled.append(detections)

    output: list[dict[str, Any]] = []
    for sample_position, detections in enumerate(sampled):
        others = [item for index, rows in enumerate(sampled) if index != sample_position for item in rows]
        for detection in detections:
            # Screen text must be independently observed at least twice. Large
            # single-sample cards are handled by the rectangular-title path.
            if not any(
                _iou(detection["bbox"], other["bbox"]) >= 0.30
                and (
                    detection["token"] == other["token"]
                    or detection["token"] in other["token"]
                    or other["token"] in detection["token"]
                )
                for other in others
            ):
                continue
            for covered_frame in range(
                boundaries[sample_position], boundaries[sample_position + 1]
            ):
                output.append(
                    {
                        "frame_index": covered_frame,
                        "bbox": detection["bbox"],
                        "confidence": detection["confidence"],
                        "class_hint": "ocr_text",
                        "ocr_token": detection["token"],
                        "ocr_sample_position": sample_position,
                    }
                )
    return output


def detect_overlay_observations(frames: Sequence[Any]) -> list[dict[str, Any]]:
    """Extract conservative persistent and transient overlay observations.

    This pixel stage intentionally favours precision.  Stable compact regions
    are candidates for logos; transient candidates must be bright/dark,
    geometrically title-like, or edge anchored.  Final crop/exclude policy is
    still decided by :func:`analyze_overlay_observations`.
    """

    try:
        import cv2
        import numpy as np
    except ImportError as exc:  # pragma: no cover - dependency error is explicit.
        raise OverlayDetectorError("opencv-python and numpy are required") from exc
    if len(frames) < 3:
        raise OverlayDetectorError("at least three frames are required")
    arrays = [np.asarray(frame) for frame in frames]
    height, width = arrays[0].shape[:2]
    if height <= 0 or width <= 0 or any(frame.shape[:2] != (height, width) for frame in arrays):
        raise OverlayDetectorError("all frames must have equal positive dimensions")
    gray = np.stack(
        [cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame for frame in arrays]
    ).astype(np.float32)
    median = np.median(gray, axis=0).astype(np.uint8)
    temporal_std = np.std(gray, axis=0)
    stable = (temporal_std <= 3.0).astype(np.uint8) * 255
    stable = cv2.morphologyEx(stable, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    stable = cv2.morphologyEx(stable, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))

    observations: list[dict[str, Any]] = []
    persistent_boxes: list[tuple[int, int, int, int]] = []
    image_area = width * height

    # Text and logos are rich in screen-fixed edges even when their fill colour
    # blends with a moving background. Group persistent Canny support into
    # compact top/corner boxes. The deliberately high area/density floor avoids
    # treating isolated branches, grass and water ripples as a broadcaster mark.
    sample_count = min(len(arrays), 24)
    sample_indices = np.linspace(0, len(arrays) - 1, sample_count).round().astype(int)
    persistent_edges = np.stack(
        [cv2.Canny(gray[index].astype(np.uint8), 60, 160) > 0 for index in sample_indices]
    ).mean(axis=0)
    supported = (persistent_edges >= 0.55).astype(np.uint8) * 255
    supported = cv2.morphologyEx(
        supported, cv2.MORPH_CLOSE, np.ones((3, 9), np.uint8)
    )
    supported = cv2.dilate(supported, np.ones((3, 3), np.uint8))
    component_count, _labels, component_stats, _centroids = (
        cv2.connectedComponentsWithStats(supported)
    )
    for label in range(1, component_count):
        x, y, box_width, box_height, area = (
            int(value) for value in component_stats[label]
        )
        ratio = area / image_area
        edge_anchored = (
            x <= width * 0.12
            or y <= height * 0.12
            or x + box_width >= width * 0.88
            or y + box_height >= height * 0.88
        )
        density = float(
            np.mean(persistent_edges[y : y + box_height, x : x + box_width] >= 0.55)
        )
        if (
            not edge_anchored
            or not 0.0015 <= ratio <= 0.08
            or density < 0.08
            or min(box_width, box_height) < 5
            or (box_width < 15 and box_height < 15)
        ):
            continue
        box = (x, y, x + box_width, y + box_height)
        persistent_boxes.append(box)
        for frame_index in range(len(frames)):
            observations.append(
                {
                    "frame_index": frame_index,
                    "bbox": box,
                    "confidence": min(0.98, 0.86 + density * 0.25),
                    "class_hint": "persistent_pixel_region",
                }
            )

    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(stable)
    for label in range(1, count):
        x, y, box_width, box_height, area = (int(value) for value in stats[label])
        ratio = area / image_area
        if not 0.0002 <= ratio <= 0.08:
            continue
        if (
            box_width > width * 0.35
            or box_height > height * 0.35
            or min(box_width, box_height) < 5
        ):
            continue
        box = (x, y, x + box_width, y + box_height)
        edge_anchored = (
            x <= width * 0.12
            or y <= height * 0.12
            or x + box_width >= width * 0.88
            or y + box_height >= height * 0.88
        )
        if not edge_anchored:
            continue
        edges = cv2.Canny(median[y : y + box_height, x : x + box_width], 60, 160)
        edge_density = float(np.mean(edges > 0)) if edges.size else 0.0
        pad = max(4, min(box_width, box_height) // 2)
        rx1, ry1 = max(0, x - pad), max(0, y - pad)
        rx2, ry2 = min(width, x + box_width + pad), min(height, y + box_height + pad)
        ring = temporal_std[ry1:ry2, rx1:rx2]
        inside_std = float(np.mean(temporal_std[y : y + box_height, x : x + box_width]))
        ring_std = float(np.mean(ring)) if ring.size else inside_std
        # Screen-fixed marks remain stable while the surrounding source image
        # changes. Static natural edges are deliberately left to OCR/review.
        if edge_density < 0.075 or ring_std < inside_std + 0.8:
            continue
        persistent_boxes.append(box)
        stability_contrast = max(0.0, ring_std - inside_std)
        confidence = min(
            0.98,
            0.82
            + min(0.10, edge_density * 0.25)
            + min(0.06, stability_contrast * 0.015),
        )
        for frame_index in range(len(frames)):
            observations.append(
                {
                    "frame_index": frame_index,
                    "bbox": box,
                    "confidence": confidence,
                    "class_hint": "persistent_pixel_region",
                }
            )

    # Pixel differencing alone confuses water, grass and moving highlights with
    # transient titles. Keep only large, coherent horizontal rectangles here;
    # OCR supplies ordinary glyph observations.
    kernel = np.ones((3, 3), np.uint8)
    for frame_index, image in enumerate(gray.astype(np.uint8)):
        delta = cv2.absdiff(image, median)
        global_change_ratio = float(np.mean(delta >= 45))
        if global_change_ratio > MAX_TRANSIENT_SCENE_CHANGE_RATIO:
            # A different shot, a dissolve, or a large moving subject makes its
            # own high-contrast regions differ from the clip median.  Those are
            # scene content, not screen-fixed title evidence.
            continue
        extreme = ((image >= 220) | (image <= 30)).astype(np.uint8) * 255
        mask = cv2.bitwise_and((delta >= 45).astype(np.uint8) * 255, extreme)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        component_count, _labels, component_stats, _centroids = (
            cv2.connectedComponentsWithStats(mask)
        )
        for label in range(1, component_count):
            x, y, box_width, box_height, area = (
                int(value) for value in component_stats[label]
            )
            box_area = max(1, box_width * box_height)
            area_ratio = area / image_area
            rectangularity = area / box_area
            roi = arrays[frame_index][y : y + box_height, x : x + box_width]
            colour_std = float(np.std(roi)) if roi.size else 999.0
            if (
                box_width / max(1, box_height) < 2.5
                or area_ratio < 0.0008
                or rectangularity < 0.85
                or box_height > height * 0.18
                or colour_std > 35.0
                or box_width < max(12, int(math.ceil(width * MIN_TRANSIENT_WIDTH_RATIO)))
                or box_height < max(3, int(math.ceil(height * MIN_TRANSIENT_HEIGHT_RATIO)))
            ):
                continue
            box = (x, y, x + box_width, y + box_height)
            if any(_iou(box, existing) >= 0.5 for existing in persistent_boxes):
                continue
            observations.append(
                {
                    "frame_index": frame_index,
                    "bbox": box,
                    "confidence": min(0.94, 0.72 + area_ratio * 8.0),
                    "class_hint": "transient_text_candidate",
                    "global_change_ratio": global_change_ratio,
                }
            )
    observations.extend(_ocr_observations(frames))
    return observations


def _profile_context_tracks(
    context: Mapping[str, Any],
    *,
    context_index: int,
) -> tuple[str, int, int, list[dict[str, Any]]]:
    """Return normalized tracks for one independently sampled donor context."""

    context_id = str(context.get("context_id") or f"context-{context_index:04d}").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", context_id):
        raise OverlayDetectorError("context_id must be a portable identifier")
    frames = context.get("frames")
    if frames is not None:
        if (
            not isinstance(frames, Sequence)
            or isinstance(frames, (str, bytes))
            or len(frames) < 3
        ):
            raise OverlayDetectorError("each overlay context requires at least three frames")
        try:
            height, width = frames[0].shape[:2]
        except (AttributeError, ValueError) as exc:
            raise OverlayDetectorError("context frames must be numpy-like images") from exc
        if any(frame.shape[:2] != (height, width) for frame in frames):
            raise OverlayDetectorError("all context frames must have equal dimensions")
        observations = detect_overlay_observations(frames)
        frame_count = len(frames)
    else:
        try:
            width = int(context["width"])
            height = int(context["height"])
            frame_count = int(context["frame_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OverlayDetectorError(
                "precomputed overlay context requires width, height and frame_count"
            ) from exc
        observations = context.get("observations") or ()
    if width <= 0 or height <= 0 or frame_count < 3:
        raise OverlayDetectorError("overlay context dimensions/frame_count are invalid")
    tracks = track_overlay_observations(
        observations,
        frame_count=frame_count,
        width=width,
        height=height,
    )
    normalized: list[dict[str, Any]] = []
    for track in tracks:
        x1, y1, x2, y2 = track["bbox"]
        normalized.append(
            {
                **track,
                "context_id": context_id,
                "bbox_normalized": [
                    x1 / width,
                    y1 / height,
                    x2 / width,
                    y2 / height,
                ],
            }
        )
    return context_id, width, height, normalized


def _normalized_iou(first: Sequence[float], second: Sequence[float]) -> float:
    return _iou(first, second)


def _normalized_central(bbox: Sequence[float]) -> bool:
    x1, y1, x2, y2 = bbox
    return x1 < 0.80 and x2 > 0.20 and y1 < 0.80 and y2 > 0.20


def _track_has_title_evidence(track: Mapping[str, Any]) -> bool:
    """Require independent temporal proof before declaring screen text.

    Internally generated OCR carries its sampled-frame lineage.  Legacy or
    injected observations without that lineage remain supported, but must form
    a real multi-frame track.  Pixel rectangles additionally have to survive
    the scene-change/geometry gates in :func:`detect_overlay_observations`.
    """

    hint = str(track.get("class_hint") or "")
    frames_seen = int(track.get("frames_seen") or 0)
    if hint == "transient_text_candidate":
        return frames_seen >= MIN_TRANSIENT_TITLE_FRAMES
    if hint != "ocr_text":
        return False
    if "ocr_sample_positions" in track or "independent_sample_count" in track:
        return int(track.get("independent_sample_count") or 0) >= 2
    return frames_seen >= MIN_TRANSIENT_TITLE_FRAMES


def build_separated_context_overlay_profile(
    contexts: Sequence[Mapping[str, Any]],
    *,
    source_asset_id: str,
    source_sha256: str,
    detector_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build donor overlay evidence only from separated source contexts.

    A candidate-local stable edge is deliberately insufficient evidence for a
    logo. Pixel tracks become crop-eligible only when the same screen-space box
    is independently observed in at least two supplied contexts. This is what
    distinguishes a broadcaster mark from a static branch or horizon.
    """

    if not isinstance(contexts, Sequence) or isinstance(contexts, (str, bytes)):
        raise OverlayDetectorError("contexts must be an array")
    context_rows = []
    for index, raw in enumerate(contexts):
        if not isinstance(raw, Mapping):
            raise OverlayDetectorError("each overlay context must be an object")
        context_rows.append(_profile_context_tracks(raw, context_index=index))

    clusters: list[dict[str, Any]] = []
    titles: list[dict[str, Any]] = []
    for context_id, _width, _height, tracks in context_rows:
        for track in tracks:
            bbox = track["bbox_normalized"]
            hint = str(track.get("class_hint") or "overlay_candidate")
            if hint == "subject_saliency":
                continue
            if hint == "transient_text_candidate":
                if _track_has_title_evidence(track):
                    titles.append(
                        {
                            "classification": "transient_title",
                            "bbox": bbox,
                            "context_ids": [context_id],
                            "persistence": float(track["persistence"]),
                            "confidence": float(track["confidence"]),
                        }
                    )
                continue
            if hint == "ocr_text":
                if _track_has_title_evidence(track):
                    titles.append(
                        {
                            "classification": (
                                "central_title"
                                if _normalized_central(bbox)
                                else "transient_title"
                            ),
                            "bbox": bbox,
                            "context_ids": [context_id],
                            "persistence": float(track["persistence"]),
                            "confidence": float(track["confidence"]),
                        }
                    )
                continue
            match = next(
                (
                    cluster
                    for cluster in clusters
                    if context_id not in cluster["context_ids"]
                    and _normalized_iou(bbox, cluster["bbox"]) >= 0.45
                ),
                None,
            )
            if match is None:
                clusters.append(
                    {
                        "bbox": list(bbox),
                        "context_ids": [context_id],
                        "persistences": [float(track["persistence"])],
                        "confidences": [float(track["confidence"])],
                    }
                )
            else:
                match["context_ids"].append(context_id)
                match["persistences"].append(float(track["persistence"]))
                match["confidences"].append(float(track["confidence"]))
                match["bbox"] = [
                    min(match["bbox"][0], bbox[0]),
                    min(match["bbox"][1], bbox[1]),
                    max(match["bbox"][2], bbox[2]),
                    max(match["bbox"][3], bbox[3]),
                ]

    raw_tracks: list[dict[str, Any]] = []
    for index, cluster in enumerate(clusters):
        context_ids = sorted(set(cluster["context_ids"]))
        raw_tracks.append(
            {
                "track_id": f"screen-track-{index:04d}",
                "classification": (
                    "screen_fixed_edge_logo"
                    if len(context_ids) >= 2
                    else "natural_static_edge"
                ),
                "bbox": cluster["bbox"],
                "context_ids": context_ids,
                "persistence": min(cluster["persistences"]),
                "confidence": min(cluster["confidences"]),
            }
        )
    for index, title in enumerate(titles):
        raw_tracks.append({"track_id": f"title-track-{index:04d}", **title})
    try:
        return build_donor_overlay_profile(
            source_asset_id,
            source_sha256,
            raw_tracks,
            detector_policy=detector_policy,
        )
    except BroadLazyError as exc:
        raise OverlayDetectorError(str(exc)) from exc


def _candidate_track_kind(
    track: Mapping[str, Any],
    *,
    bbox: Sequence[float],
    profile: Mapping[str, Any],
) -> tuple[str, str | None]:
    hint = str(track.get("class_hint") or "overlay_candidate")
    if hint == "subject_saliency":
        return "natural_static_edge", None
    if hint == "transient_text_candidate":
        return (
            ("transient_title", None)
            if _track_has_title_evidence(track)
            else ("natural_static_edge", None)
        )
    if hint == "ocr_text":
        if not _track_has_title_evidence(track):
            return "natural_static_edge", None
        if _normalized_central(bbox):
            return "central_title", None
        return "transient_title", None
    candidates = [
        row
        for row in profile.get("tracks") or ()
        if row.get("reported_classification") == "screen_fixed_edge_logo"
        and _normalized_iou(bbox, row.get("bbox") or ()) >= 0.45
    ]
    if candidates:
        best = max(candidates, key=lambda row: _normalized_iou(bbox, row["bbox"]))
        return "profile_track", str(best["track_id"])
    if hint == "persistent_pixel_region":
        return "natural_static_edge", None
    return "unclassified_overlay", None


def analyze_candidate_overlay_with_profile(
    frames: Sequence[Any],
    donor_profile: Mapping[str, Any],
    *,
    candidate_id: str,
    fps: float,
    observations: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve candidate evidence against donor-wide separated contexts."""

    if not frames:
        raise OverlayDetectorError("frames must not be empty")
    try:
        height, width = frames[0].shape[:2]
    except (AttributeError, ValueError) as exc:
        raise OverlayDetectorError("frames must be numpy-like images") from exc
    if any(frame.shape[:2] != (height, width) for frame in frames):
        raise OverlayDetectorError("all frames must have equal dimensions")
    try:
        profile = validate_donor_overlay_profile(donor_profile)
    except BroadLazyError as exc:
        raise OverlayDetectorError(str(exc)) from exc
    detected = list(observations) if observations is not None else detect_overlay_observations(frames)
    tracks = track_overlay_observations(
        detected,
        frame_count=len(frames),
        width=int(width),
        height=int(height),
    )
    candidate_observations: list[dict[str, Any]] = []
    classified_tracks: list[dict[str, Any]] = []
    for index, track in enumerate(tracks):
        x1, y1, x2, y2 = track["bbox"]
        bbox = [x1 / width, y1 / height, x2 / width, y2 / height]
        kind, profile_track_id = _candidate_track_kind(track, bbox=bbox, profile=profile)
        observation = {
            "observation_id": f"candidate-observation-{index:04d}",
            "kind": kind,
        }
        if profile_track_id is not None:
            observation["profile_track_id"] = profile_track_id
        candidate_observations.append(observation)
        classified_tracks.append(
            {
                **track,
                "bbox_normalized": [round(value, 9) for value in bbox],
                "profile_kind": kind,
                "profile_track_id": profile_track_id,
            }
        )
    try:
        decision = evaluate_candidate_overlay_profile(
            profile,
            candidate_id,
            candidate_observations,
        )
    except BroadLazyError as exc:
        raise OverlayDetectorError(str(exc)) from exc
    rejected = decision["status"] == "rejected"
    excluded = (
        [
            {
                "kind": "profile_overlay_reject",
                "start_frame": 0,
                "end_frame": len(frames),
                "confidence": 1.0,
                "reasons": list(decision.get("rejection_reasons") or ()),
            }
        ]
        if rejected
        else []
    )
    return {
        "schema_version": 3,
        "detector": {
            "name": DETECTOR_NAME,
            "version": PROFILE_DETECTOR_VERSION,
            "mode": "donor_profile_candidate_evaluation",
        },
        "status": decision["status"],
        "action": decision["action"],
        "confidence": 1.0,
        "frame_count": len(frames),
        "fps": float(fps),
        "dimensions": {"width": int(width), "height": int(height)},
        "crop_safe": bool(decision["crop_safe"]),
        "crop_margins": dict(decision["crop_margins"]),
        "retained_area": float(decision["retained_area"]),
        "excluded_ranges": excluded,
        "inpaint_markers": [],
        "tracks": classified_tracks,
        "observation_count": len(detected),
        "candidate_profile": decision,
        "crop_provenance": (
            {
                "kind": "detector_profile_track",
                "donor_profile_hash": profile["content_hash"],
                "detector_policy_hash": profile["detector_policy_hash"],
            }
            if decision["action"] == "crop"
            else None
        ),
        "render_constraints": dict(decision["render_constraints"]),
    }


def compute_transition_metrics(frames: Sequence[Any]) -> dict[str, list[float]]:
    """Compute the MAE/HSV/luminance series used by the transition detector."""

    try:
        import cv2
        import numpy as np
    except ImportError as exc:  # pragma: no cover
        raise OverlayDetectorError("opencv-python and numpy are required") from exc
    if len(frames) < 2:
        raise OverlayDetectorError("at least two frames are required")
    arrays = [np.asarray(frame) for frame in frames]
    height, width = arrays[0].shape[:2]
    if any(frame.shape[:2] != (height, width) for frame in arrays):
        raise OverlayDetectorError("all frames must have equal dimensions")
    gray_frames = []
    histograms = []
    luma = []
    for frame in arrays:
        if frame.ndim == 2:
            gray = frame.astype(np.uint8)
            bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        else:
            bgr = frame
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (240, 135), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(cv2.resize(bgr, (240, 135)), cv2.COLOR_BGR2HSV)
        histogram = cv2.calcHist([hsv], [0, 1], None, [24, 16], [0, 180, 0, 256])
        cv2.normalize(histogram, histogram, norm_type=cv2.NORM_L2)
        gray_frames.append(gray.astype(np.float32))
        histograms.append(histogram)
        luma.append(float(gray.mean()))
    mae = [0.0]
    hist_distance = [0.0]
    for index in range(1, len(gray_frames)):
        mae.append(float(np.mean(np.abs(gray_frames[index] - gray_frames[index - 1]))))
        hist_distance.append(
            float(
                cv2.compareHist(
                    histograms[index - 1], histograms[index], cv2.HISTCMP_BHATTACHARYYA
                )
            )
        )
    return {
        "frame_luma": luma,
        "frame_mae": mae,
        "histogram_distance": hist_distance,
    }


def analyze_temporal_overlays(
    frames: Sequence[Any],
    *,
    fps: float,
    **thresholds: Any,
) -> dict[str, Any]:
    """Run pixel evidence extraction and deterministic temporal policy."""

    if not frames:
        raise OverlayDetectorError("frames must not be empty")
    first = frames[0]
    try:
        height, width = first.shape[:2]
    except (AttributeError, ValueError) as exc:
        raise OverlayDetectorError("frames must be numpy-like images") from exc
    observations = detect_overlay_observations(frames)
    analysis = analyze_overlay_observations(
        observations,
        frame_count=len(frames),
        width=int(width),
        height=int(height),
        fps=fps,
        **thresholds,
    )
    analysis["observation_count"] = len(observations)
    return analysis


__all__ = [
    "DETECTOR_NAME",
    "DETECTOR_VERSION",
    "OverlayDetectorError",
    "PROFILE_DETECTOR_VERSION",
    "analyze_candidate_overlay_with_profile",
    "analyze_overlay_observations",
    "analyze_temporal_overlays",
    "build_separated_context_overlay_profile",
    "compute_transition_metrics",
    "detect_overlay_observations",
    "track_overlay_observations",
]
