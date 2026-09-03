"""Fail-closed donor overlay profiles.

Persistent corner marks are removed by a donor-specific source crop.  Clips
that overlap transient titles, warnings, promos, or credits are rejected before
semantic ranking so they cannot silently reach the renderer.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence


class OverlayProfileError(ValueError):
    """Raised when an overlay profile cannot be applied safely."""


_MARGIN_NAMES = ("left", "right", "top", "bottom")
_DETECTOR_ACTIONS = {
    "none",
    "crop",
    "exclude_reselect",
    "crop_and_exclude",
    "inpaint_required",
    "crop_and_inpaint",
}


def _finite_number(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise OverlayProfileError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise OverlayProfileError(f"{field} must be a finite number")
    return number


def normalize_overlay_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize one donor overlay profile.

    Schema::

        {
          "crop_margins": {"left": .1, "right": .1, "top": .2, "bottom": 0},
          "excluded_ranges": [{"start": 0, "end": 9.5, "reason": "rating"}]
        }

    Ranges are half-open: a clip ending exactly at ``start`` is allowed.
    """

    if not isinstance(profile, Mapping):
        raise OverlayProfileError("overlay profile must be a mapping")
    raw_margins = profile.get("crop_margins") or {}
    if not isinstance(raw_margins, Mapping):
        raise OverlayProfileError("crop_margins must be a mapping")
    margins: dict[str, float] = {}
    for name in _MARGIN_NAMES:
        value = _finite_number(raw_margins.get(name, 0.0), f"crop_margins.{name}")
        if not 0.0 <= value <= 0.25:
            raise OverlayProfileError(f"crop_margins.{name} must be in [0, 0.25]")
        margins[name] = value
    if margins["left"] + margins["right"] >= 0.5:
        raise OverlayProfileError("horizontal crop margins remove too much of the frame")
    if margins["top"] + margins["bottom"] >= 0.5:
        raise OverlayProfileError("vertical crop margins remove too much of the frame")

    raw_ranges = profile.get("excluded_ranges") or ()
    if not isinstance(raw_ranges, Sequence) or isinstance(raw_ranges, (str, bytes)):
        raise OverlayProfileError("excluded_ranges must be a list")
    ranges: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_ranges):
        if not isinstance(raw, Mapping):
            raise OverlayProfileError(f"excluded_ranges[{index}] must be a mapping")
        start = _finite_number(raw.get("start"), f"excluded_ranges[{index}].start")
        end = _finite_number(raw.get("end"), f"excluded_ranges[{index}].end")
        if start < 0 or end <= start:
            raise OverlayProfileError(
                f"excluded_ranges[{index}] must have 0 <= start < end"
            )
        reason = str(raw.get("reason") or "transient_overlay").strip()
        ranges.append({"start": start, "end": end, "reason": reason})
    ranges.sort(key=lambda item: (item["start"], item["end"]))
    return {"crop_margins": margins, "excluded_ranges": ranges}


def ranges_overlap(start: float, end: float, blocked_start: float, blocked_end: float) -> bool:
    """Return whether two half-open source ranges intersect."""

    return start < blocked_end and end > blocked_start


def apply_overlay_profile(
    clips: Sequence[Mapping[str, Any]],
    profile: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Copy and annotate donor clips with crop metadata and hard exclusions."""

    normalized = normalize_overlay_profile(profile)
    output: list[dict[str, Any]] = []
    for index, raw_clip in enumerate(clips):
        if not isinstance(raw_clip, Mapping):
            raise OverlayProfileError(f"clips[{index}] must be a mapping")
        clip = dict(raw_clip)
        start = _finite_number(clip.get("source_in"), f"clips[{index}].source_in")
        end = _finite_number(clip.get("source_out"), f"clips[{index}].source_out")
        if start < 0 or end <= start:
            raise OverlayProfileError(f"clips[{index}] must have 0 <= source_in < source_out")
        clip["edge_cleanup_margins"] = dict(normalized["crop_margins"])
        hits = [
            dict(item)
            for item in normalized["excluded_ranges"]
            if ranges_overlap(start, end, float(item["start"]), float(item["end"]))
        ]
        if hits:
            clip["overlay_blocked"] = True
            clip["overlay_blocked_reason"] = "; ".join(
                dict.fromkeys(str(item["reason"]) for item in hits)
            )
            clip["overlay_hits"] = hits
        else:
            # Profiles are often re-applied after a source audit changes.  Do
            # not preserve stale block flags from an older profile.
            clip.pop("overlay_blocked", None)
            clip.pop("overlay_blocked_reason", None)
            clip.pop("overlay_hits", None)
        output.append(clip)
    return output


def normalize_overlay_analysis(analysis: Mapping[str, Any]) -> dict[str, Any]:
    """Validate detector-derived adaptive crop/exclusion metadata."""

    if not isinstance(analysis, Mapping):
        raise OverlayProfileError("overlay analysis must be a mapping")
    detector = analysis.get("detector")
    if not isinstance(detector, Mapping):
        raise OverlayProfileError("overlay analysis requires detector metadata")
    name = str(detector.get("name") or "").strip()
    version = str(detector.get("version") or "").strip()
    if not name or not version:
        raise OverlayProfileError("overlay detector name/version are required")
    confidence = _finite_number(analysis.get("confidence"), "overlay confidence")
    if not 0.0 <= confidence <= 1.0:
        raise OverlayProfileError("overlay confidence must be in [0, 1]")
    action = str(analysis.get("action") or "").strip()
    if action not in _DETECTOR_ACTIONS:
        raise OverlayProfileError(
            f"overlay action must be one of {', '.join(sorted(_DETECTOR_ACTIONS))}"
        )
    margins = normalize_overlay_profile(
        {"crop_margins": analysis.get("crop_margins") or {}}
    )["crop_margins"]
    crop_safe = analysis.get("crop_safe") is True
    crop_action = action in {"crop", "crop_and_exclude", "crop_and_inpaint"}
    if crop_action and (not crop_safe or not any(margins.values())):
        raise OverlayProfileError("crop action requires a non-zero detector-safe crop")
    if not crop_action and any(margins.values()):
        raise OverlayProfileError("non-crop overlay action cannot carry crop margins")

    raw_ranges = analysis.get("excluded_ranges") or ()
    if not isinstance(raw_ranges, Sequence) or isinstance(raw_ranges, (str, bytes)):
        raise OverlayProfileError("overlay excluded_ranges must be a list")
    ranges: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_ranges):
        if not isinstance(raw, Mapping):
            raise OverlayProfileError(f"overlay excluded_ranges[{index}] must be a mapping")
        try:
            start = int(raw["start_frame"])
            end = int(raw["end_frame"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OverlayProfileError(
                f"overlay excluded_ranges[{index}] requires integer frame bounds"
            ) from exc
        if (
            isinstance(raw.get("start_frame"), bool)
            or isinstance(raw.get("end_frame"), bool)
            or start < 0
            or end <= start
        ):
            raise OverlayProfileError(
                f"overlay excluded_ranges[{index}] requires 0 <= start_frame < end_frame"
            )
        item = dict(raw)
        item.update({"start_frame": start, "end_frame": end})
        ranges.append(item)
    normalized = dict(analysis)
    normalized.update(
        {
            "schema_version": int(analysis.get("schema_version", 2)),
            "detector": {**dict(detector), "name": name, "version": version},
            "confidence": confidence,
            "action": action,
            "crop_safe": crop_safe,
            "crop_margins": margins,
            "excluded_ranges": sorted(
                ranges, key=lambda item: (item["start_frame"], item["end_frame"])
            ),
        }
    )
    return normalized


def apply_overlay_analysis(
    clips: Sequence[Mapping[str, Any]],
    analysis: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Attach detector provenance and fail-close clips that hit contamination."""

    normalized = normalize_overlay_analysis(analysis)
    output: list[dict[str, Any]] = []
    for index, raw in enumerate(clips):
        if not isinstance(raw, Mapping):
            raise OverlayProfileError(f"clips[{index}] must be a mapping")
        clip = dict(raw)
        clip["overlay_analysis"] = dict(normalized)
        clip.pop("edge_cleanup_margins", None)
        if normalized["action"] in {"crop", "crop_and_exclude", "crop_and_inpaint"}:
            clip["edge_cleanup_margins"] = dict(normalized["crop_margins"])

        start = clip.get("source_start_frame")
        end = clip.get("source_end_frame")
        hits: list[dict[str, Any]] = []
        if start is not None and end is not None:
            try:
                start_frame, end_frame = int(start), int(end)
            except (TypeError, ValueError) as exc:
                raise OverlayProfileError(
                    f"clips[{index}] source frame bounds must be integers"
                ) from exc
            hits = [
                dict(item)
                for item in normalized["excluded_ranges"]
                if start_frame < int(item["end_frame"])
                and end_frame > int(item["start_frame"])
            ]
        elif normalized["excluded_ranges"]:
            # A frame-indexed detector result cannot be safely projected onto a
            # time-only candidate.  Force re-indexing rather than guessing.
            hits = [
                {
                    "kind": "missing_candidate_frame_bounds",
                    "start_frame": 0,
                    "end_frame": 1,
                }
            ]
        if normalized["action"] in {"inpaint_required", "crop_and_inpaint"} and not clip.get("inpaint_applied"):
            hits.append(
                {"kind": "inpaint_required", "start_frame": 0, "end_frame": 1}
            )
        if hits:
            clip["overlay_blocked"] = True
            clip["overlay_blocked_reason"] = "; ".join(
                dict.fromkeys(str(item.get("kind") or "overlay") for item in hits)
            )
            clip["overlay_hits"] = hits
        else:
            clip.pop("overlay_blocked", None)
            clip.pop("overlay_blocked_reason", None)
            clip.pop("overlay_hits", None)
        output.append(clip)
    return output


__all__ = [
    "OverlayProfileError",
    "apply_overlay_analysis",
    "apply_overlay_profile",
    "normalize_overlay_analysis",
    "normalize_overlay_profile",
    "ranges_overlap",
]
