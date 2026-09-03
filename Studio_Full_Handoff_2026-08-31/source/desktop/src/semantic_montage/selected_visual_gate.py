"""Final, provider-free visual gate for an already selected exact source span.

The gate deliberately does not search, repair, crop, inpaint, retime, or mint
identifiers.  It re-decodes the exact persisted integer-PTS selection at the
configured analysis resolution, evaluates the existing overlay/OCR and
transition detectors, and emits a portable content-addressed receipt.

Runtime paths are accepted only as decode inputs and never enter the receipt.
Long selections are decoded in overlapping chunks so each allocation remains
under ``max_decode_bytes`` without reducing the requested resolution.

The hashes detect stale/corrupt receipts; they are not a secret-key MAC.
Production authentication must recompute the exported actionable-profile
binding from the exact runtime donor profile before accepting a cached row.
"""
from __future__ import annotations

from collections import Counter
from fractions import Fraction
import json
import math
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping, Sequence

from .broad_lazy import (
    BroadLazyError,
    EXACT_PTS_CONTRACT,
    build_exact_pts_window_contract,
    evaluate_candidate_overlay_profile,
    validate_donor_overlay_profile,
)
from .broad_lazy_adapter import (
    BroadLazyAdapterError,
    validate_exact_candidate_contract,
)
from .common import canonical_hash
from .donor import DonorError, TRANSITION_DETECTOR_VERSION, detect_transition_zones
from .overlay_detector import (
    DETECTOR_VERSION,
    PROFILE_DETECTOR_VERSION,
    OverlayDetectorError,
    analyze_candidate_overlay_with_profile,
    compute_transition_metrics,
)
from .strict_candidate import (
    DEFAULT_MAX_DECODE_BYTES,
    StrictCandidateError,
    decode_exact_analysis_window,
)


SELECTED_VISUAL_GATE_VERSION = "selected-span-final-visual-v1.5-ffmpeg44-passthrough"
SELECTED_VISUAL_GATE_RECEIPT_CONTRACT = "selected_span_visual_gate_receipt_v1"
SELECTED_VISUAL_ACTIONABLE_PROFILE_BINDING_CONTRACT = (
    "selected_visual_actionable_profile_binding_v1"
)
RENDER_GEOMETRY_PROXY_CONTRACT = "selected_render_geometry_proxy_v1"
RENDER_GEOMETRY_PROXY_ALGORITHM = (
    "persisted_detector_crop_then_documentary_transform_480x270_v1"
)
TRANSITION_CORROBORATOR_VERSION = "selected-transition-corroborator-v2"
NEAR_BLACK_HOLD_DETECTOR_VERSION = "selected-near-black-hold-v1"
NEAR_BLACK_HOLD_MINIMUM_MILLISECONDS = 500
NEAR_BLACK_LUMA_MEAN_MILLI_MAX = 8_000
NEAR_BLACK_LUMA_P95_MAX = 16
NEAR_BLACK_LUMA_P95_P05_SPREAD_MAX = 8
PROFILE_PRESENCE_POLICY_VERSION = "selected-profile-presence-v1"
PROFILE_PERSISTENCE_MIN = 0.95
PROFILE_MIN_SEPARATED_CONTEXTS = 3
PROFILE_MATCH_IOU_MIN = 0.45
PROFILE_MATCH_PROFILE_COVERAGE_MIN = 0.80
PROFILE_MATCH_OBSERVATION_COVERAGE_MIN = 0.20
DEFAULT_ANALYSIS_WIDTH = 480
DEFAULT_ANALYSIS_HEIGHT = 270
DEFAULT_CHUNK_OVERLAP_FRAMES = 24
_CACHE_KEY_PREFIX = "selected-visual-v1.5-"
_CROP_KEYS = ("left", "right", "top", "bottom")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PORTABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_ABSOLUTE_PATH = re.compile(r"^(?:/|~/|file://|[A-Za-z]:[\\/])")
_PATH_FIELD_NAMES = {
    "path",
    "source_path",
    "absolute_path",
    "contact_sheet",
    "thumbnail",
    "original_location",
    "original_locations",
}


class SelectedVisualGateError(ValueError):
    """The selected span cannot produce a trustworthy visual-gate receipt."""


class SelectedVisualGateRejected(SelectedVisualGateError):
    """The exact selected span contains overlay or transition evidence."""

    def __init__(
        self,
        reason_codes: Sequence[str],
        receipt: Mapping[str, Any],
    ) -> None:
        self.reason_codes = tuple(str(value) for value in reason_codes)
        self.receipt = dict(receipt)
        super().__init__(
            "selected visual span rejected: " + ", ".join(self.reason_codes)
        )


def _json_clone(value: Any, name: str) -> Any:
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
        raise SelectedVisualGateError(f"{name} must be finite JSON data") from exc


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise SelectedVisualGateError(f"{name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise SelectedVisualGateError(f"{name} must be a positive integer") from exc
    if result <= 0 or result != value:
        raise SelectedVisualGateError(f"{name} must be a positive integer")
    return result


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise SelectedVisualGateError(f"{name} must be a non-negative integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise SelectedVisualGateError(f"{name} must be a non-negative integer") from exc
    if result < 0 or result != value:
        raise SelectedVisualGateError(f"{name} must be a non-negative integer")
    return result


def _portable_id(value: Any, name: str) -> str:
    result = str(value or "").strip()
    if not _PORTABLE_ID.fullmatch(result):
        raise SelectedVisualGateError(f"{name} must be an existing portable identifier")
    return result


def _sha256(value: Any, name: str) -> str:
    result = str(value or "").strip().lower()
    if not _SHA256.fullmatch(result):
        raise SelectedVisualGateError(f"{name} must be a lowercase SHA-256")
    return result


def _fps_document(value: Any) -> tuple[float, dict[str, int]]:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SelectedVisualGateError("output_fps must be positive and finite") from exc
    if not math.isfinite(result) or result <= 0:
        raise SelectedVisualGateError("output_fps must be positive and finite")
    rate = Fraction(str(result)).limit_denominator(1_000_000)
    if abs(float(rate) - result) > 1e-9:
        raise SelectedVisualGateError("output_fps cannot be represented rationally")
    return result, {"numerator": rate.numerator, "denominator": rate.denominator}


def _assert_path_free(value: Any, name: str = "receipt") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in _PATH_FIELD_NAMES:
                raise SelectedVisualGateError(
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
        raise SelectedVisualGateError(f"{name} contains an absolute host path")


def _validate_exact_pts_document(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SelectedVisualGateError("candidate has no exact integer-PTS lineage")
    row = _json_clone(dict(value), "candidate.frame_contract.exact_pts_contract")
    if (
        row.get("schema_version") != 1
        or row.get("contract") != EXACT_PTS_CONTRACT
        or row.get("addressing") != "integer_pts_half_open"
        or row.get("fps_inference_used") is not False
    ):
        raise SelectedVisualGateError("candidate exact integer-PTS lineage is incompatible")
    supplied_hash = _sha256(row.get("content_hash"), "exact_pts.content_hash")
    base = {
        key: item for key, item in row.items() if key not in {"content_hash", "window_id"}
    }
    if supplied_hash != canonical_hash(base):
        raise SelectedVisualGateError("candidate exact integer-PTS lineage hash mismatch")
    expected_id = f"pts-window-{supplied_hash[:24]}"
    if row.get("window_id") != expected_id:
        raise SelectedVisualGateError("candidate exact integer-PTS lineage id mismatch")
    points = row.get("frame_pts")
    if not isinstance(points, Sequence) or isinstance(
        points, (str, bytes, bytearray)
    ):
        raise SelectedVisualGateError("candidate exact integer-PTS lineage has no frame vector")
    try:
        normalized_points = [int(value) for value in points]
        start_pts = int(row["start_pts"])
        end_pts = int(row["end_pts"])
        frame_count = int(row["frame_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SelectedVisualGateError("candidate exact integer-PTS lineage is malformed") from exc
    if (
        not normalized_points
        or frame_count != len(normalized_points)
        or start_pts != normalized_points[0]
        or end_pts <= normalized_points[-1]
        or any(
            right <= left
            for left, right in zip(normalized_points, normalized_points[1:])
        )
    ):
        raise SelectedVisualGateError("candidate exact integer-PTS frame vector is invalid")
    return row


def _selected_exact_contract(candidate: Mapping[str, Any]) -> dict[str, Any]:
    frame_contract = candidate.get("frame_contract")
    if not isinstance(frame_contract, Mapping):
        raise SelectedVisualGateError("candidate has no frame_contract")
    exact = _validate_exact_pts_document(frame_contract.get("exact_pts_contract"))
    points = [int(value) for value in exact["frame_pts"]]
    start = _non_negative_int(candidate.get("source_frame_start"), "source_frame_start")
    end = _positive_int(candidate.get("source_frame_end"), "source_frame_end")
    if not 0 <= start < end <= len(points):
        raise SelectedVisualGateError("selected source frame range escapes exact PTS lineage")
    start_pts = points[start]
    end_pts = points[end] if end < len(points) else int(exact["end_pts"])
    try:
        selected = build_exact_pts_window_contract(
            points,
            exact.get("time_base"),
            start_pts=start_pts,
            end_pts=end_pts,
            source_end_pts=int(exact["end_pts"]) if end == len(points) else None,
        )
    except BroadLazyError as exc:
        raise SelectedVisualGateError(str(exc)) from exc
    expected_frames = end - start
    if int(selected.get("frame_count") or 0) != expected_frames:
        raise SelectedVisualGateError("selected exact PTS projection lost source frames")
    try:
        source_in = float(candidate.get("source_in"))
        source_out = float(candidate.get("source_out"))
        selected_start = Fraction(
            int(selected["start_time"]["numerator"]),
            int(selected["start_time"]["denominator"]),
        )
        selected_end = Fraction(
            int(selected["end_time"]["numerator"]),
            int(selected["end_time"]["denominator"]),
        )
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise SelectedVisualGateError("candidate selected source timing is malformed") from exc
    if (
        not math.isfinite(source_in)
        or not math.isfinite(source_out)
        or abs(source_in - float(selected_start)) > 1e-9
        or abs(source_out - float(selected_end)) > 1e-9
    ):
        raise SelectedVisualGateError(
            "candidate source bounds disagree with persisted selected integer PTS"
        )
    return selected


def _crop_margins(value: Any, name: str) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise SelectedVisualGateError(f"{name} must contain detector crop margins")
    if set(value) != set(_CROP_KEYS):
        raise SelectedVisualGateError(
            f"{name} must contain exactly left/right/top/bottom"
        )
    output: dict[str, float] = {}
    for key in _CROP_KEYS:
        raw = value.get(key)
        try:
            normalized = float(raw)
        except (TypeError, ValueError) as exc:
            raise SelectedVisualGateError(f"{name}.{key} must be in [0, 0.25]") from exc
        if (
            isinstance(raw, bool)
            or not math.isfinite(normalized)
            or not 0.0 <= normalized <= 0.25
        ):
            raise SelectedVisualGateError(f"{name}.{key} must be in [0, 0.25]")
        output[key] = round(normalized, 9)
    if output["left"] + output["right"] >= 0.5:
        raise SelectedVisualGateError(f"{name} must retain more than half the width")
    if output["top"] + output["bottom"] >= 0.5:
        raise SelectedVisualGateError(f"{name} must retain more than half the height")
    return output


def _zero_crop_margins() -> dict[str, float]:
    return {key: 0.0 for key in _CROP_KEYS}


def _render_transform_contract(value: Any) -> dict[str, Any]:
    """Validate a fully materialized, path-independent renderer transform."""

    if not isinstance(value, Mapping):
        raise SelectedVisualGateError(
            "render_transform must be the explicit normalized renderer transform"
        )
    required = {
        "preset",
        "seed",
        "hflip",
        "scale",
        "x_offset",
        "y_offset",
        "contrast",
        "saturation",
        "brightness",
        "grain",
        "transform_version",
    }
    if set(value) != required:
        raise SelectedVisualGateError(
            "render_transform must contain the complete normalized renderer fields"
        )
    preset = str(value.get("preset") or "").strip()
    transform_version = str(value.get("transform_version") or "").strip()
    if not preset or len(preset) > 128 or not transform_version or len(transform_version) > 64:
        raise SelectedVisualGateError(
            "render_transform preset/version must be non-empty portable strings"
        )
    if _ABSOLUTE_PATH.match(preset) or _ABSOLUTE_PATH.match(transform_version):
        raise SelectedVisualGateError(
            "render_transform preset/version cannot contain a host path"
        )
    seed = _non_negative_int(value.get("seed"), "render_transform.seed")
    if seed > 0x7FFFFFFF:
        raise SelectedVisualGateError("render_transform.seed exceeds renderer bounds")
    if not isinstance(value.get("hflip"), bool):
        raise SelectedVisualGateError("render_transform.hflip must be boolean")

    def bounded_float(name: str, lower: float, upper: float) -> float:
        raw = value.get(name)
        if isinstance(raw, bool):
            raise SelectedVisualGateError(
                f"render_transform.{name} must be finite and in [{lower}, {upper}]"
            )
        try:
            normalized = float(raw)
        except (TypeError, ValueError) as exc:
            raise SelectedVisualGateError(
                f"render_transform.{name} must be finite and in [{lower}, {upper}]"
            ) from exc
        if not math.isfinite(normalized) or not lower <= normalized <= upper:
            raise SelectedVisualGateError(
                f"render_transform.{name} must be finite and in [{lower}, {upper}]"
            )
        return normalized

    return {
        "preset": preset,
        "seed": seed,
        "hflip": value["hflip"],
        "scale": bounded_float("scale", 1.0, 1.08),
        "x_offset": bounded_float("x_offset", -0.25, 0.25),
        "y_offset": bounded_float("y_offset", -0.25, 0.25),
        "contrast": bounded_float("contrast", 0.90, 1.10),
        "saturation": bounded_float("saturation", 0.90, 1.10),
        "brightness": bounded_float("brightness", -0.05, 0.05),
        "grain": bounded_float("grain", 0.0, 0.05),
        "transform_version": transform_version,
    }


def _planned_render_view_contract(
    candidate: Mapping[str, Any],
    profile: Mapping[str, Any],
    render_transform: Mapping[str, Any],
) -> dict[str, Any]:
    overlay = candidate.get("overlay_analysis")
    if not isinstance(overlay, Mapping):
        raise SelectedVisualGateError("candidate has no persisted overlay analysis")
    action = str(overlay.get("action") or "").strip()
    if action not in {"none", "crop"}:
        raise SelectedVisualGateError(
            "selected planned view supports only resolved none/detector-crop actions"
        )
    if str(overlay.get("status") or "") not in {"clean", "resolved"}:
        raise SelectedVisualGateError("candidate persisted overlay analysis is unresolved")
    detector = overlay.get("detector")
    if not isinstance(detector, Mapping):
        raise SelectedVisualGateError("candidate persisted overlay detector is missing")
    if (
        str(detector.get("name") or "") != "semantic-temporal-overlay"
        or str(detector.get("version") or "") != PROFILE_DETECTOR_VERSION
    ):
        raise SelectedVisualGateError("candidate persisted overlay detector is incompatible")
    candidate_profile = overlay.get("candidate_profile")
    if not isinstance(candidate_profile, Mapping):
        raise SelectedVisualGateError("candidate persisted overlay profile is missing")
    profile_hash = _sha256(profile.get("content_hash"), "donor overlay profile hash")
    policy_hash = _sha256(
        profile.get("detector_policy_hash"), "donor overlay detector policy hash"
    )
    if candidate_profile.get("donor_profile_hash") != profile_hash:
        raise SelectedVisualGateError(
            "candidate persisted overlay result uses another donor profile"
        )
    if candidate_profile.get("detector_policy_hash") != policy_hash:
        raise SelectedVisualGateError(
            "candidate persisted overlay result uses another detector policy"
        )
    if str(candidate_profile.get("status") or "") != "resolved":
        raise SelectedVisualGateError("candidate persisted overlay profile is unresolved")
    raw_evidence = candidate_profile.get("evidence")
    if not isinstance(raw_evidence, Sequence) or isinstance(raw_evidence, (str, bytes)):
        raise SelectedVisualGateError("candidate persisted overlay evidence is malformed")
    observations = []
    for index, raw in enumerate(raw_evidence):
        if not isinstance(raw, Mapping):
            raise SelectedVisualGateError(
                f"candidate persisted overlay evidence {index} is malformed"
            )
        observation = {
            "observation_id": str(raw.get("observation_id") or ""),
            "kind": str(raw.get("kind") or ""),
        }
        if raw.get("profile_track_id") is not None:
            observation["profile_track_id"] = str(raw.get("profile_track_id"))
        observations.append(observation)
    try:
        rebuilt_profile = evaluate_candidate_overlay_profile(
            profile,
            str(candidate_profile.get("candidate_id") or ""),
            observations,
        )
    except BroadLazyError as exc:
        raise SelectedVisualGateError(str(exc)) from exc
    if rebuilt_profile != dict(candidate_profile):
        raise SelectedVisualGateError(
            "candidate persisted overlay decision hash/fields are not canonical"
        )
    if str(candidate_profile.get("action") or "") != action:
        raise SelectedVisualGateError(
            "candidate overlay action disagrees with its persisted detector decision"
        )
    render_constraints = candidate_profile.get("render_constraints")
    if not isinstance(render_constraints, Mapping):
        raise SelectedVisualGateError(
            "candidate persisted overlay result has no render constraints"
        )
    if (
        render_constraints.get("allow_manual_crop") is not False
        or render_constraints.get("allow_inpaint") is not False
        or str(render_constraints.get("fill_strategy") or "") != "none"
        or abs(float(render_constraints.get("retime_factor") or 0.0) - 1.0) > 1e-9
    ):
        raise SelectedVisualGateError(
            "candidate persisted overlay result permits manual/fill/inpaint repair"
        )
    top_constraints = overlay.get("render_constraints")
    if isinstance(top_constraints, Mapping) and (
        top_constraints.get("allow_manual_crop") is not False
        or top_constraints.get("allow_inpaint") is not False
        or str(top_constraints.get("fill_strategy") or "") != "none"
        or abs(float(top_constraints.get("retime_factor") or 0.0) - 1.0) > 1e-9
    ):
        raise SelectedVisualGateError(
            "candidate overlay analysis permits manual/fill/inpaint repair"
        )

    if action == "none":
        margins = _crop_margins(
            overlay.get("crop_margins") or _zero_crop_margins(),
            "overlay_analysis.crop_margins",
        )
        if any(margins.values()) or overlay.get("crop_provenance") is not None:
            raise SelectedVisualGateError(
                "overlay action none cannot carry a crop or crop provenance"
            )
        candidate_margins = _crop_margins(
            candidate_profile.get("crop_margins") or _zero_crop_margins(),
            "candidate_profile.crop_margins",
        )
        if any(candidate_margins.values()) or candidate_profile.get("crop_safe") is True:
            raise SelectedVisualGateError(
                "overlay action none disagrees with its persisted crop decision"
            )
        provenance = None
    else:
        if overlay.get("crop_safe") is not True or candidate_profile.get("crop_safe") is not True:
            raise SelectedVisualGateError(
                "persisted detector crop must be explicitly marked safe"
            )
        margins = _crop_margins(
            overlay.get("crop_margins"), "overlay_analysis.crop_margins"
        )
        candidate_margins = _crop_margins(
            candidate_profile.get("crop_margins"), "candidate_profile.crop_margins"
        )
        if not any(margins.values()) or margins != candidate_margins:
            raise SelectedVisualGateError(
                "persisted detector crop margins disagree with detector decision"
            )
        raw_provenance = overlay.get("crop_provenance")
        if not isinstance(raw_provenance, Mapping):
            raise SelectedVisualGateError("persisted detector crop has no provenance")
        provenance = _json_clone(dict(raw_provenance), "crop_provenance")
        if set(provenance) != {
            "kind",
            "donor_profile_hash",
            "detector_policy_hash",
        }:
            raise SelectedVisualGateError("persisted detector crop provenance is incompatible")
        if (
            provenance.get("kind") != "detector_profile_track"
            or provenance.get("donor_profile_hash") != profile_hash
            or provenance.get("detector_policy_hash") != policy_hash
        ):
            raise SelectedVisualGateError(
                "persisted crop is not proven by the exact donor detector profile"
            )
    explicit_cleanup = candidate.get("edge_cleanup_margins")
    if explicit_cleanup is not None and _crop_margins(
        explicit_cleanup, "candidate.edge_cleanup_margins"
    ) != margins:
        raise SelectedVisualGateError(
            "candidate edge cleanup differs from persisted detector crop"
        )
    normalized_transform = _render_transform_contract(render_transform)
    return {
        "contract": RENDER_GEOMETRY_PROXY_CONTRACT,
        "algorithm": RENDER_GEOMETRY_PROXY_ALGORITHM,
        "crop_action": action,
        "crop_margins": margins,
        "crop_provenance": provenance,
        "render_transform": normalized_transform,
        "geometry": {
            "source_crop_rounding": "round_even_pixels_6dp",
            "scale": "renderer_scaled_even_dimensions_aspect_increase_lanczos",
            "framing": "renderer_normalized_offset_crop",
            "hflip": "renderer_post_crop",
            "proxy_resolution": {
                "width": DEFAULT_ANALYSIS_WIDTH,
                "height": DEFAULT_ANALYSIS_HEIGHT,
            },
            "equivalent_for": [
                "persisted_edge_crop",
                "scale",
                "x_offset",
                "y_offset",
                "hflip",
            ],
            "omitted_pixel_effects": [
                "eq",
                "grain",
                "output_cfr_resample",
                "yuv420p_roundtrip",
            ],
            "known_profile_projection": {
                "algorithm": "normalized_bbox_edge_crop_scale_offset_hflip_v1",
                "decisions": ["detector_crop", "reject_on_presence"],
                "presence_proof": {
                    "version": PROFILE_PRESENCE_POLICY_VERSION,
                    "detector_crop": {
                        "separated_context_support": {
                            "minimum_contexts": PROFILE_MIN_SEPARATED_CONTEXTS,
                            "minimum_persistence": PROFILE_PERSISTENCE_MIN,
                        },
                        "current_high_resolution_spatial_match": {
                            "minimum_frames_seen": 3,
                            "iou_minimum": PROFILE_MATCH_IOU_MIN,
                            "containment_branches": [
                                {
                                    "profile_coverage_minimum": PROFILE_MATCH_PROFILE_COVERAGE_MIN,
                                    "observation_coverage_minimum": PROFILE_MATCH_OBSERVATION_COVERAGE_MIN,
                                },
                            ],
                        },
                    },
                    "reject_on_presence": (
                        "current_stable_ocr_only_profile_v1_has_no_template_identity"
                    ),
                },
                "material_visibility": {
                    "min_width_px": 2.0,
                    "min_height_px": 2.0,
                    "min_area_px": 8.0,
                    "min_visible_fraction": 0.02,
                },
            },
        },
    }


def _probe_source_dimensions(
    source_path: str | Path,
    *,
    ffprobe: str,
    timeout: float,
) -> dict[str, int]:
    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise SelectedVisualGateError("selected source media is missing")
    try:
        result = subprocess.run(
            [
                str(ffprobe),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(source),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=float(timeout),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SelectedVisualGateError(f"selected source dimension probe failed: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or "ffprobe failed").strip()[-800:]
        raise SelectedVisualGateError(f"selected source dimension probe rejected: {detail}")
    try:
        payload = json.loads(result.stdout or "{}")
        stream = payload["streams"][0]
        width = int(stream["width"])
        height = int(stream["height"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SelectedVisualGateError(
            "selected source dimension probe returned malformed media facts"
        ) from exc
    if width <= 0 or height <= 0:
        raise SelectedVisualGateError("selected source dimensions are invalid")
    return {"width": width, "height": height}


_ACTIONABLE_PROFILE_BINDING_FIELDS = {
    "contract",
    "policy_version",
    "donor_overlay_profile_hash",
    "observed_profile_context_count",
    "tracks",
    "tracks_sha256",
    "content_hash",
}
_ACTIONABLE_PROFILE_TRACK_FIELDS = {
    "track_id",
    "decision",
    "classification",
    "bbox_normalized",
    "support_contexts",
    "context_ids",
    "persistence",
}


def _normalized_bbox_contract(value: Any, name: str) -> list[float]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) != 4
    ):
        raise SelectedVisualGateError(f"{name} must be a normalized four-value bbox")
    try:
        x1, y1, x2, y2 = [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise SelectedVisualGateError(
            f"{name} must be a normalized four-value bbox"
        ) from exc
    if (
        not all(math.isfinite(item) for item in (x1, y1, x2, y2))
        or not 0.0 <= x1 < x2 <= 1.0
        or not 0.0 <= y1 < y2 <= 1.0
    ):
        raise SelectedVisualGateError(f"{name} must be a normalized four-value bbox")
    return [round(item, 9) for item in (x1, y1, x2, y2)]


def _validate_actionable_profile_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SelectedVisualGateError("actionable donor-profile binding must be an object")
    row = _json_clone(dict(value), "actionable donor-profile binding")
    if set(row) != _ACTIONABLE_PROFILE_BINDING_FIELDS:
        raise SelectedVisualGateError(
            "actionable donor-profile binding fields are incompatible"
        )
    if (
        row.get("contract")
        != SELECTED_VISUAL_ACTIONABLE_PROFILE_BINDING_CONTRACT
        or row.get("policy_version") != PROFILE_PRESENCE_POLICY_VERSION
    ):
        raise SelectedVisualGateError(
            "actionable donor-profile binding contract is incompatible"
        )
    _sha256(
        row.get("donor_overlay_profile_hash"),
        "actionable_profile.donor_overlay_profile_hash",
    )
    observed_contexts = _non_negative_int(
        row.get("observed_profile_context_count"),
        "actionable_profile.observed_profile_context_count",
    )
    raw_tracks = row.get("tracks")
    if not isinstance(raw_tracks, list):
        raise SelectedVisualGateError("actionable donor-profile tracks must be an array")
    normalized_tracks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_track in enumerate(raw_tracks):
        label = f"actionable_profile.tracks[{index}]"
        if not isinstance(raw_track, Mapping) or set(raw_track) != _ACTIONABLE_PROFILE_TRACK_FIELDS:
            raise SelectedVisualGateError(f"{label} fields are incompatible")
        track_id = _portable_id(raw_track.get("track_id"), f"{label}.track_id")
        if track_id in seen:
            raise SelectedVisualGateError(f"{label} duplicates an actionable profile track")
        seen.add(track_id)
        decision = str(raw_track.get("decision") or "")
        if decision not in {"detector_crop", "reject_on_presence"}:
            raise SelectedVisualGateError(f"{label} decision is not actionable")
        classification = str(raw_track.get("classification") or "").strip()
        if not classification:
            raise SelectedVisualGateError(f"{label} classification is missing")
        bbox = _normalized_bbox_contract(
            raw_track.get("bbox_normalized"), f"{label}.bbox_normalized"
        )
        context_ids = raw_track.get("context_ids")
        if not isinstance(context_ids, list):
            raise SelectedVisualGateError(f"{label}.context_ids must be an array")
        normalized_context_ids = [
            _portable_id(item, f"{label}.context_ids") for item in context_ids
        ]
        if normalized_context_ids != sorted(set(normalized_context_ids)):
            raise SelectedVisualGateError(
                f"{label}.context_ids must be sorted and unique"
            )
        support_contexts = _non_negative_int(
            raw_track.get("support_contexts"), f"{label}.support_contexts"
        )
        if support_contexts != len(normalized_context_ids):
            raise SelectedVisualGateError(f"{label} support context count disagrees")
        if observed_contexts < support_contexts:
            raise SelectedVisualGateError(f"{label} context support escapes the profile")
        raw_persistence = raw_track.get("persistence")
        if isinstance(raw_persistence, bool):
            raise SelectedVisualGateError(f"{label}.persistence must be in [0, 1]")
        try:
            persistence = float(raw_persistence)
        except (TypeError, ValueError) as exc:
            raise SelectedVisualGateError(
                f"{label}.persistence must be in [0, 1]"
            ) from exc
        if not math.isfinite(persistence) or not 0.0 <= persistence <= 1.0:
            raise SelectedVisualGateError(f"{label}.persistence must be in [0, 1]")
        normalized_tracks.append(
            {
                "track_id": track_id,
                "decision": decision,
                "classification": classification,
                "bbox_normalized": bbox,
                "support_contexts": support_contexts,
                "context_ids": normalized_context_ids,
                "persistence": round(persistence, 9),
            }
        )
    expected_tracks = sorted(
        normalized_tracks, key=lambda item: (item["track_id"], item["decision"])
    )
    if raw_tracks != expected_tracks:
        raise SelectedVisualGateError(
            "actionable donor-profile tracks are not canonical"
        )
    if _sha256(row.get("tracks_sha256"), "actionable_profile.tracks_sha256") != canonical_hash(
        expected_tracks
    ):
        raise SelectedVisualGateError("actionable donor-profile track hash mismatch")
    supplied_hash = _sha256(
        row.get("content_hash"), "actionable_profile.content_hash"
    )
    base = {key: item for key, item in row.items() if key != "content_hash"}
    if supplied_hash != canonical_hash(base):
        raise SelectedVisualGateError("actionable donor-profile binding hash mismatch")
    return row


def build_selected_visual_actionable_profile_binding(
    donor_overlay_profile: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the portable actionable subset authenticated by a donor profile hash.

    Production receipt authentication recomputes this document from the exact
    runtime donor profile.  This makes omission of an actionable track a binding
    mismatch instead of a self-consistent evidence rewrite.
    """

    try:
        profile = validate_donor_overlay_profile(donor_overlay_profile)
    except BroadLazyError as exc:
        raise SelectedVisualGateError(str(exc)) from exc
    all_context_ids = sorted(
        {
            str(context_id)
            for raw_track in profile.get("tracks") or ()
            if isinstance(raw_track, Mapping)
            for context_id in raw_track.get("context_ids") or ()
            if str(context_id)
        }
    )
    tracks: list[dict[str, Any]] = []
    for raw_track in profile.get("tracks") or ():
        if not isinstance(raw_track, Mapping):
            continue
        decision = str(raw_track.get("decision") or "")
        if decision not in {"detector_crop", "reject_on_presence"}:
            continue
        tracks.append(
            {
                "track_id": _portable_id(
                    raw_track.get("track_id"), "profile actionable track id"
                ),
                "decision": decision,
                "classification": str(
                    raw_track.get("reported_classification") or ""
                ).strip(),
                "bbox_normalized": _normalized_bbox_contract(
                    raw_track.get("bbox"), "profile actionable track bbox"
                ),
                "support_contexts": _non_negative_int(
                    raw_track.get("support_contexts"),
                    "profile actionable track support_contexts",
                ),
                "context_ids": sorted(
                    {
                        _portable_id(value, "profile actionable track context id")
                        for value in raw_track.get("context_ids") or ()
                    }
                ),
                "persistence": round(float(raw_track.get("persistence") or 0.0), 9),
            }
        )
    tracks.sort(key=lambda item: (item["track_id"], item["decision"]))
    base = {
        "contract": SELECTED_VISUAL_ACTIONABLE_PROFILE_BINDING_CONTRACT,
        "policy_version": PROFILE_PRESENCE_POLICY_VERSION,
        "donor_overlay_profile_hash": _sha256(
            profile.get("content_hash"), "donor_overlay_profile.content_hash"
        ),
        "observed_profile_context_count": len(all_context_ids),
        "tracks": tracks,
        "tracks_sha256": canonical_hash(tracks),
    }
    return _validate_actionable_profile_binding(
        {**base, "content_hash": canonical_hash(base)}
    )


def _project_profile_bbox_geometry(
    bbox_normalized: Sequence[float],
    planned_view: Mapping[str, Any],
    source_dimensions: Mapping[str, Any],
    *,
    width: int,
    height: int,
) -> dict[str, Any]:
    """Pure source-bbox -> renderer-geometry-proxy projection."""

    source_width = _positive_int(source_dimensions.get("width"), "source width")
    source_height = _positive_int(source_dimensions.get("height"), "source height")
    width = _positive_int(width, "projection width")
    height = _positive_int(height, "projection height")
    x1, y1, x2, y2 = _normalized_bbox_contract(
        bbox_normalized, "profile projection bbox"
    )
    margins = _crop_margins(
        planned_view.get("crop_margins"), "planned_render_view.crop_margins"
    )
    transform = _render_transform_contract(planned_view.get("render_transform"))
    keep_width = 1.0 - margins["left"] - margins["right"]
    keep_height = 1.0 - margins["top"] - margins["bottom"]
    cropped_width = int(round(source_width * keep_width / 2.0) * 2)
    cropped_height = int(round(source_height * keep_height / 2.0) * 2)
    crop_x = int(round(source_width * margins["left"] / 2.0) * 2)
    crop_y = int(round(source_height * margins["top"] / 2.0) * 2)
    if cropped_width <= 0 or cropped_height <= 0:
        raise SelectedVisualGateError("render geometry proxy crop has no retained pixels")
    scaled_width = int(math.ceil(width * transform["scale"] / 2.0) * 2)
    scaled_height = int(math.ceil(height * transform["scale"] / 2.0) * 2)
    scale_factor = max(
        scaled_width / cropped_width,
        scaled_height / cropped_height,
    )
    scaled_source_width = cropped_width * scale_factor
    scaled_source_height = cropped_height * scale_factor
    viewport_x = (scaled_source_width - width) * (0.5 + transform["x_offset"])
    viewport_y = (scaled_source_height - height) * (0.5 + transform["y_offset"])
    projection_policy = planned_view.get("geometry", {}).get(
        "known_profile_projection", {}
    )
    thresholds = projection_policy.get("material_visibility", {})
    try:
        min_width = float(thresholds["min_width_px"])
        min_height = float(thresholds["min_height_px"])
        min_area = float(thresholds["min_area_px"])
        min_fraction = float(thresholds["min_visible_fraction"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SelectedVisualGateError(
            "known profile projection policy is malformed"
        ) from exc

    source_box = (
        x1 * source_width,
        y1 * source_height,
        x2 * source_width,
        y2 * source_height,
    )
    retained = (
        max(source_box[0], float(crop_x)),
        max(source_box[1], float(crop_y)),
        min(source_box[2], float(crop_x + cropped_width)),
        min(source_box[3], float(crop_y + cropped_height)),
    )
    if retained[0] >= retained[2] or retained[1] >= retained[3]:
        projected_box = None
        visible_box = None
        visible_fraction = 0.0
        visible_width = visible_height = visible_area = 0.0
    else:
        projected = [
            (retained[0] - crop_x) * scale_factor - viewport_x,
            (retained[1] - crop_y) * scale_factor - viewport_y,
            (retained[2] - crop_x) * scale_factor - viewport_x,
            (retained[3] - crop_y) * scale_factor - viewport_y,
        ]
        if transform["hflip"]:
            projected[0], projected[2] = width - projected[2], width - projected[0]
        projected_box = [round(value, 6) for value in projected]
        visible = [
            max(0.0, projected[0]),
            max(0.0, projected[1]),
            min(float(width), projected[2]),
            min(float(height), projected[3]),
        ]
        visible_width = max(0.0, visible[2] - visible[0])
        visible_height = max(0.0, visible[3] - visible[1])
        visible_area = visible_width * visible_height
        projected_area = max(0.0, projected[2] - projected[0]) * max(
            0.0, projected[3] - projected[1]
        )
        visible_fraction = visible_area / projected_area if projected_area else 0.0
        visible_box = (
            [round(value, 6) for value in visible] if visible_area > 0.0 else None
        )
    geometry_materially_visible = bool(
        visible_width >= min_width
        and visible_height >= min_height
        and visible_area >= min_area
        and visible_fraction >= min_fraction
    )
    return {
        "projected_bbox_pixels": projected_box,
        "visible_bbox_pixels": visible_box,
        "visible_width_pixels": round(visible_width, 6),
        "visible_height_pixels": round(visible_height, 6),
        "visible_area_pixels": round(visible_area, 6),
        "visible_fraction": round(visible_fraction, 9),
        "geometry_materially_visible": geometry_materially_visible,
    }


def _project_known_profile_overlays(
    actionable_profile_binding: Mapping[str, Any],
    planned_view: Mapping[str, Any],
    source_dimensions: Mapping[str, Any],
    *,
    width: int,
    height: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Project actionable donor-profile boxes into the rendered geometry proxy."""

    profile_binding = _validate_actionable_profile_binding(actionable_profile_binding)
    observed_profile_context_count = int(
        profile_binding["observed_profile_context_count"]
    )
    output: list[dict[str, Any]] = []
    tracks = profile_binding["tracks"]
    for raw in tracks:
        decision = str(raw["decision"])
        geometry = _project_profile_bbox_geometry(
            raw["bbox_normalized"],
            planned_view,
            source_dimensions,
            width=width,
            height=height,
        )
        track_id = str(raw["track_id"])
        support_contexts = int(raw["support_contexts"])
        persistence = float(raw["persistence"])
        classification = str(raw["classification"])
        separated_context_persistent = (
            decision == "detector_crop"
            and classification == "screen_fixed_edge_logo"
            and support_contexts >= PROFILE_MIN_SEPARATED_CONTEXTS
            and persistence >= PROFILE_PERSISTENCE_MIN
        )
        presence_basis = (
            "separated_context_persistent_profile"
            if separated_context_persistent
            else "donor_profile_geometry_only"
        )
        presence_proven = separated_context_persistent
        material_visible = bool(
            separated_context_persistent
            and geometry["geometry_materially_visible"]
        )
        output.append(
            {
                "track_id": track_id,
                "decision": decision,
                "classification": classification,
                "support_contexts": support_contexts,
                "support_context_ids": sorted(
                    {str(value) for value in raw["context_ids"] if str(value)}
                ),
                "observed_profile_context_count": observed_profile_context_count,
                "minimum_separated_context_support": PROFILE_MIN_SEPARATED_CONTEXTS,
                "persistence": round(persistence, 9),
                "presence_basis": presence_basis,
                "presence_proven": presence_proven,
                "source_bbox_normalized": list(raw["bbox_normalized"]),
                **geometry,
                "materially_visible": material_visible,
            }
        )
    output.sort(key=lambda item: (item["track_id"], item["decision"]))
    return output, []


def _bbox_overlap_metrics(
    profile_bbox: Sequence[float],
    observation_bbox: Sequence[float],
) -> dict[str, float]:
    try:
        px1, py1, px2, py2 = [float(value) for value in profile_bbox]
        ox1, oy1, ox2, oy2 = [float(value) for value in observation_bbox]
    except (TypeError, ValueError) as exc:
        raise SelectedVisualGateError("profile match bbox is malformed") from exc
    if (
        not all(
            math.isfinite(value)
            for value in (px1, py1, px2, py2, ox1, oy1, ox2, oy2)
        )
        or not 0.0 <= px1 < px2 <= 1.0
        or not 0.0 <= py1 < py2 <= 1.0
        or not 0.0 <= ox1 < ox2 <= 1.0
        or not 0.0 <= oy1 < oy2 <= 1.0
    ):
        raise SelectedVisualGateError("profile match bbox is invalid")
    intersection_width = max(0.0, min(px2, ox2) - max(px1, ox1))
    intersection_height = max(0.0, min(py2, oy2) - max(py1, oy1))
    intersection_area = intersection_width * intersection_height
    profile_area = (px2 - px1) * (py2 - py1)
    observation_area = (ox2 - ox1) * (oy2 - oy1)
    union_area = profile_area + observation_area - intersection_area
    return {
        "iou": intersection_area / union_area if union_area else 0.0,
        "profile_coverage": intersection_area / profile_area if profile_area else 0.0,
        "observation_coverage": (
            intersection_area / observation_area if observation_area else 0.0
        ),
    }


def _profile_spatial_match(metrics: Mapping[str, float]) -> bool:
    iou = float(metrics.get("iou") or 0.0)
    profile_coverage = float(metrics.get("profile_coverage") or 0.0)
    observation_coverage = float(metrics.get("observation_coverage") or 0.0)
    return bool(
        iou >= PROFILE_MATCH_IOU_MIN
        or (
            profile_coverage >= PROFILE_MATCH_PROFILE_COVERAGE_MIN
            and observation_coverage >= PROFILE_MATCH_OBSERVATION_COVERAGE_MIN
        )
    )


def _current_profile_spatial_matches(
    analysis: Mapping[str, Any],
    references: Sequence[Mapping[str, Any]],
    *,
    chunk_index: int,
    start_frame: int,
    coordinate_space: str,
) -> list[dict[str, Any]]:
    """Corroborate a known profile box with a persistent current-span track."""

    normalized_references = []
    for reference in references:
        if not isinstance(reference, Mapping):
            continue
        decision = str(reference.get("decision") or "")
        if decision != "detector_crop":
            continue
        bbox = _rounded_bbox(reference.get("bbox_normalized"))
        if bbox is None:
            continue
        normalized_references.append(
            {
                "track_id": _portable_id(
                    reference.get("track_id"), "profile spatial reference id"
                ),
                "decision": decision,
                "bbox_normalized": bbox,
            }
        )

    matches: list[dict[str, Any]] = []
    for observation_index, raw in enumerate(analysis.get("tracks") or ()):
        if not isinstance(raw, Mapping):
            continue
        class_hint = str(raw.get("class_hint") or "")
        profile_kind = str(raw.get("profile_kind") or "")
        frames_seen = int(raw.get("frames_seen") or 0)
        if frames_seen < 3:
            continue
        direct_track_id = (
            str(raw.get("profile_track_id"))
            if raw.get("profile_track_id") is not None
            else None
        )
        observation_bbox = _rounded_bbox(raw.get("bbox_normalized"))
        if observation_bbox is None:
            continue
        for reference in normalized_references:
            if (
                class_hint != "persistent_pixel_region"
                or profile_kind not in {"profile_track", "natural_static_edge"}
            ):
                continue
            metrics = _bbox_overlap_metrics(
                reference["bbox_normalized"], observation_bbox
            )
            if not _profile_spatial_match(metrics):
                continue
            matches.append(
                {
                    "track_id": reference["track_id"],
                    "coordinate_space": coordinate_space,
                    "chunk_index": chunk_index,
                    "start_frame": start_frame,
                    "observation_index": observation_index,
                    "class_hint": class_hint,
                    "profile_kind": profile_kind,
                    "direct_profile_track_id": direct_track_id,
                    "frames_seen": frames_seen,
                    "profile_bbox_normalized": reference["bbox_normalized"],
                    "observation_bbox_normalized": observation_bbox,
                    "overlap": {
                        key: round(value, 9) for key, value in metrics.items()
                    },
                }
            )
    matches.sort(
        key=lambda item: (
            item["track_id"],
            item["chunk_index"],
            item["observation_index"],
        )
    )
    return matches


def _chunk_ranges(
    frame_count: int,
    *,
    width: int,
    height: int,
    max_decode_bytes: int,
    overlap_frames: int,
) -> list[tuple[int, int]]:
    bytes_per_frame = width * height * 3
    capacity = max_decode_bytes // bytes_per_frame
    if capacity < 3:
        raise SelectedVisualGateError(
            "max_decode_bytes cannot hold three frames at the requested resolution"
        )
    if frame_count <= capacity:
        return [(0, frame_count)]
    if overlap_frames < 2 or overlap_frames >= capacity - 1:
        raise SelectedVisualGateError(
            "chunk_overlap_frames must be at least 2 and smaller than chunk capacity"
        )
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < frame_count:
        end = min(frame_count, start + capacity)
        ranges.append((start, end))
        if end == frame_count:
            break
        start = end - overlap_frames
    return ranges


def _exact_subcontract(
    selected: Mapping[str, Any],
    *,
    start_frame: int,
    end_frame: int,
) -> dict[str, Any]:
    points = [int(value) for value in selected["frame_pts"]]
    if not 0 <= start_frame < end_frame <= len(points):
        raise SelectedVisualGateError("visual-gate chunk escapes selected exact PTS range")
    start_pts = points[start_frame]
    end_pts = points[end_frame] if end_frame < len(points) else int(selected["end_pts"])
    try:
        return build_exact_pts_window_contract(
            points,
            selected.get("time_base"),
            start_pts=start_pts,
            end_pts=end_pts,
            source_end_pts=int(selected["end_pts"]) if end_frame == len(points) else None,
        )
    except BroadLazyError as exc:
        raise SelectedVisualGateError(str(exc)) from exc


def _decode_planned_render_view(
    source_path: str | Path,
    exact: Mapping[str, Any],
    planned_view: Mapping[str, Any],
    *,
    width: int,
    height: int,
    ffmpeg: str,
    timeout: float,
    max_decode_bytes: int,
) -> dict[str, Any]:
    """Decode exact PTS through the renderer-equivalent normalized geometry."""

    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - explicit dependency gate.
        raise SelectedVisualGateError("numpy is required for selected visual gate") from exc
    frame_count = _positive_int(exact.get("frame_count"), "chunk.frame_count")
    expected_bytes = frame_count * width * height * 3
    if expected_bytes > max_decode_bytes:
        raise SelectedVisualGateError(
            "planned render-view chunk exceeds max_decode_bytes"
        )
    try:
        start_seconds = float(
            Fraction(
                int(exact["start_time"]["numerator"]),
                int(exact["start_time"]["denominator"]),
            )
        )
        start_pts = int(exact["start_pts"])
        end_pts = int(exact["end_pts"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise SelectedVisualGateError("planned render-view exact PTS is malformed") from exc
    margins = _crop_margins(
        planned_view.get("crop_margins"), "planned_render_view.crop_margins"
    )
    transform = _render_transform_contract(planned_view.get("render_transform"))
    scaled_width = int(math.ceil(width * transform["scale"] / 2.0) * 2)
    scaled_height = int(math.ceil(height * transform["scale"] / 2.0) * 2)
    x_center = 0.5 + transform["x_offset"]
    y_center = 0.5 + transform["y_offset"]
    filters = [f"trim=start_pts={start_pts}:end_pts={end_pts}", "setpts=PTS-STARTPTS"]
    if any(margins.values()):
        keep_width = 1.0 - margins["left"] - margins["right"]
        keep_height = 1.0 - margins["top"] - margins["bottom"]
        filters.append(
            f"crop=round(iw*{keep_width:.6f}/2)*2:"
            f"round(ih*{keep_height:.6f}/2)*2:"
            f"round(iw*{margins['left']:.6f}/2)*2:"
            f"round(ih*{margins['top']:.6f}/2)*2"
        )
    filters.extend(
        [
            (
                f"scale={scaled_width}:{scaled_height}:"
                "force_original_aspect_ratio=increase:flags=lanczos"
            ),
            (
                f"crop={width}:{height}:"
                f"(iw-ow)*{x_center:.6f}:(ih-oh)*{y_center:.6f}"
            ),
        ]
    )
    if transform["hflip"]:
        filters.append("hflip")
    filters.append("setsar=1")
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-copyts",
        "-ss",
        f"{max(0.0, start_seconds - 2.0):.9f}",
        "-i",
        str(Path(source_path).expanduser().resolve()),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        ",".join(filters),
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
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SelectedVisualGateError(f"planned render-view decode failed: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or b"ffmpeg failed")[-1000:].decode(
            "utf-8", errors="replace"
        )
        raise SelectedVisualGateError(f"planned render-view decode rejected: {detail}")
    if len(result.stdout) != expected_bytes:
        raise SelectedVisualGateError(
            "planned render-view frame count disagrees with exact PTS lineage "
            f"({len(result.stdout)} bytes != {expected_bytes})"
        )
    array = np.frombuffer(result.stdout, dtype=np.uint8).reshape(
        (frame_count, height, width, 3)
    )
    if len(array) < 3:
        raise SelectedVisualGateError(
            "planned render-view analysis window has fewer than three frames"
        )
    return {
        "frames": [array[index] for index in range(len(array))],
        "dimensions": {"width": width, "height": height},
        "decoded_bytes": expected_bytes,
        "exact_pts": dict(exact),
    }


def _rounded_bbox(value: Any) -> list[float] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
        return None
    try:
        return [round(float(item), 9) for item in value]
    except (TypeError, ValueError):
        return None


def _overlay_evidence(
    analysis: Mapping[str, Any],
    *,
    chunk_index: int,
    start_frame: int,
    end_frame: int,
) -> tuple[dict[str, Any], list[str]]:
    status = str(analysis.get("status") or "")
    action = str(analysis.get("action") or "")
    reasons: set[str] = set()
    tracks: list[dict[str, Any]] = []
    ocr_tokens: set[str] = set()
    positive_findings: list[dict[str, Any]] = []
    for raw in analysis.get("tracks") or ():
        if not isinstance(raw, Mapping):
            continue
        raw_ranges = raw.get("frame_ranges") or ()
        frame_ranges: list[list[int]] = []
        if isinstance(raw_ranges, Sequence) and not isinstance(raw_ranges, (str, bytes)):
            for value in raw_ranges:
                if (
                    isinstance(value, Sequence)
                    and not isinstance(value, (str, bytes))
                    and len(value) == 2
                ):
                    try:
                        local_start, local_end = int(value[0]), int(value[1])
                    except (TypeError, ValueError):
                        continue
                    if 0 <= local_start < local_end <= end_frame - start_frame:
                        frame_ranges.append(
                            [start_frame + local_start, start_frame + local_end]
                        )
        if not frame_ranges:
            raw_frames = raw.get("frames") or ()
            if isinstance(raw_frames, Sequence) and not isinstance(raw_frames, (str, bytes)):
                try:
                    normalized_frames = sorted(
                        {
                            int(value)
                            for value in raw_frames
                            if 0 <= int(value) < end_frame - start_frame
                        }
                    )
                except (TypeError, ValueError):
                    normalized_frames = []
                if normalized_frames:
                    frame_ranges = [
                        [
                            start_frame + normalized_frames[0],
                            start_frame + normalized_frames[-1] + 1,
                        ]
                    ]
        profile_kind = str(raw.get("profile_kind") or "")
        class_hint = str(raw.get("class_hint") or "")
        frames_seen = int(raw.get("frames_seen") or 0)
        independent_sample_count = int(raw.get("independent_sample_count") or 0)
        item: dict[str, Any] = {
            "profile_kind": profile_kind,
            "class_hint": class_hint,
            "frame_ranges": frame_ranges,
            "frames_seen": frames_seen,
            "independent_sample_count": independent_sample_count,
            "persistence": round(float(raw.get("persistence") or 0.0), 9),
            "confidence": round(float(raw.get("confidence") or 0.0), 9),
        }
        profile_track_id = (
            str(raw.get("profile_track_id"))
            if raw.get("profile_track_id") is not None
            else None
        )
        if profile_track_id is not None:
            item["profile_track_id"] = profile_track_id
        if raw.get("max_global_change_ratio") is not None:
            item["max_global_change_ratio"] = round(
                float(raw.get("max_global_change_ratio")), 9
            )
        bbox = _rounded_bbox(raw.get("bbox_normalized"))
        if bbox is not None:
            item["bbox_normalized"] = bbox
        track_tokens: set[str] = set()
        tokens = raw.get("ocr_tokens") or ()
        if isinstance(tokens, Sequence) and not isinstance(tokens, (str, bytes)):
            for token in tokens:
                normalized = "".join(
                    value
                    for value in str(token).casefold()
                    if value.isalnum()
                )
                if normalized:
                    track_tokens.add(normalized)
        last_token = "".join(
            value
            for value in str(raw.get("last_ocr_token") or "").casefold()
            if value.isalnum()
        )
        if last_token:
            track_tokens.add(last_token)
        ocr_tokens.update(track_tokens)
        meaningful_tokens = sorted(
            token for token in track_tokens if sum(value.isalpha() for value in token) >= 3
        )
        item["ocr_tokens"] = sorted(track_tokens)
        item["meaningful_ocr_tokens"] = meaningful_tokens
        positive_kind = None
        if profile_kind == "profile_track":
            positive_kind = "known_profile_overlay_visible"
        elif profile_kind in {"central_title", "transient_title"}:
            if (
                class_hint == "ocr_text"
                and independent_sample_count >= 2
                and meaningful_tokens
            ):
                positive_kind = "stable_ocr_title"
                reasons.add("overlay_rejected")
            # A pixel-only transient rectangle has neither independently
            # sampled text identity nor a visual template. Keep it diagnostic;
            # persistence and aggregate detector action are not positive proof.
        item["positive_evidence"] = positive_kind
        if positive_kind is not None:
            positive_findings.append(
                {
                    "kind": positive_kind,
                    "profile_kind": profile_kind,
                    "class_hint": class_hint,
                    "profile_track_id": profile_track_id,
                    "frame_ranges": frame_ranges,
                    "bbox_normalized": bbox,
                    "meaningful_ocr_tokens": meaningful_tokens,
                    "independent_sample_count": independent_sample_count,
                }
            )
        tracks.append(item)
    tracks.sort(
        key=lambda item: (
            item["frame_ranges"],
            item["profile_kind"],
            item.get("bbox_normalized") or (),
        )
    )
    profile = analysis.get("candidate_profile")
    profile_reasons = []
    if isinstance(profile, Mapping):
        profile_reasons = sorted(
            {str(value) for value in profile.get("rejection_reasons") or () if str(value)}
        )
    return (
        {
            "chunk_index": chunk_index,
            "start_frame": start_frame,
            "end_frame": end_frame,
            "frame_count": end_frame - start_frame,
            "status": status,
            "action": action,
            "observation_count": int(analysis.get("observation_count") or 0),
            "profile_rejection_reasons": profile_reasons,
            "ocr_tokens": sorted(ocr_tokens),
            "positive_findings": positive_findings,
            "tracks": tracks,
        },
        sorted(reasons),
    )


def _raw_crop_authorization(
    analysis: Mapping[str, Any],
    planned_view: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Compare a new high-resolution raw crop finding to the persisted bound."""

    action = str(analysis.get("action") or "")
    status = str(analysis.get("status") or "")
    persisted = _crop_margins(
        planned_view.get("crop_margins"), "planned_render_view.crop_margins"
    )
    detected = (
        _crop_margins(analysis.get("crop_margins"), "raw_overlay.crop_margins")
        if action == "crop"
        else _zero_crop_margins()
    )
    outside = [
        key for key in _CROP_KEYS if detected[key] > persisted[key] + 1e-9
    ]
    reasons = ["overlay_crop_outside_persisted_bound"] if outside else []
    provenance = analysis.get("crop_provenance")
    expected = planned_view.get("crop_provenance")
    provenance_matches = True
    if action == "crop":
        provenance_matches = (
            isinstance(provenance, Mapping)
            and isinstance(expected, Mapping)
            and provenance.get("kind") == "detector_profile_track"
            and provenance.get("donor_profile_hash")
            == expected.get("donor_profile_hash")
            and provenance.get("detector_policy_hash")
            == expected.get("detector_policy_hash")
        )
        if not provenance_matches:
            reasons.append("overlay_crop_provenance_mismatch")
    return {
        "status": status,
        "action": action,
        "detected_crop_margins": detected,
        "persisted_crop_margins": persisted,
        "outside_bound_edges": outside,
        "detected_crop_provenance": (
            _json_clone(dict(provenance), "detected crop provenance")
            if isinstance(provenance, Mapping)
            else None
        ),
        "persisted_crop_provenance": (
            _json_clone(dict(expected), "persisted crop provenance")
            if isinstance(expected, Mapping)
            else None
        ),
        "provenance_matches": provenance_matches,
    }, reasons


def _derive_transition_corroboration_result(
    kind: str,
    *,
    boundary_present: bool,
    feature_matches: int,
    geometric_inliers: int,
    geometric_inlier_ratio: float,
    matched_quadrants: int,
    histogram_correlation: float,
    median_flow_explained: float,
    luma_min: float,
    luma_max: float,
    material_change_fraction: float,
    directional_uniformity: float,
    directional_quadrants: int,
    hard_cross_mae: float,
    hard_within_mae: float,
    hard_cross_histogram: float,
) -> dict[str, Any]:
    """Pure policy derivation shared by live evaluation and receipt replay."""

    feature_continuity = bool(
        geometric_inliers >= 12
        and geometric_inlier_ratio >= 0.35
        and matched_quadrants >= 2
    )
    appearance_motion_continuity = bool(
        histogram_correlation >= 0.85 and median_flow_explained >= 0.14
    )
    continuity_proven = feature_continuity or appearance_motion_continuity
    hard_boundary_evidence = bool(
        kind == "hard_cut"
        and boundary_present
        and hard_cross_mae >= 15.0
        and hard_cross_histogram >= 0.15
        and hard_cross_mae
        >= max(8.0 + hard_within_mae, 2.5 * hard_within_mae)
    )
    extreme_fade = bool(
        kind == "fade"
        and luma_max - luma_min >= 18.0
        and (luma_min <= 18.0 or luma_max >= 237.0)
        and material_change_fraction >= 0.50
        and directional_uniformity >= 0.45
        and directional_quadrants >= 3
    )
    if hard_boundary_evidence and not continuity_proven:
        classification = "corroborated_hard_cut"
        decision = "reject"
    elif extreme_fade:
        classification = "corroborated_black_or_white_fade"
        decision = "reject"
    elif continuity_proven:
        classification = "continuous_motion"
        decision = "ignore"
    elif histogram_correlation < 0.85 and feature_matches < 16:
        classification = "corroborated_scene_discontinuity"
        decision = "reject"
    else:
        classification = "unresolved_transition"
        decision = "reject"
    return {
        "feature_continuity": feature_continuity,
        "appearance_motion_continuity": appearance_motion_continuity,
        "hard_boundary_evidence": hard_boundary_evidence,
        "classification": classification,
        "decision": decision,
    }


def _transition_zone_corroboration(
    frames: Sequence[Any],
    raw: Mapping[str, Any],
) -> dict[str, Any]:
    """Distinguish a real transition from detector-triggering continuous motion."""

    try:
        import cv2
        import numpy as np
    except ImportError as exc:  # pragma: no cover - explicit dependency gate.
        raise SelectedVisualGateError(
            "opencv-python and numpy are required for transition corroboration"
        ) from exc
    try:
        zone_start = int(raw["start_frame"])
        zone_end = int(raw["end_frame"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SelectedVisualGateError("transition zone frame bounds are malformed") from exc
    if not 0 <= zone_start < zone_end <= len(frames):
        raise SelectedVisualGateError("transition zone escapes decoded frames")
    before_index = max(0, zone_start - 3)
    after_index = min(len(frames) - 1, zone_end + 2)
    before = np.asarray(frames[before_index])
    after = np.asarray(frames[after_index])
    before_gray = cv2.cvtColor(before, cv2.COLOR_BGR2GRAY)
    after_gray = cv2.cvtColor(after, cv2.COLOR_BGR2GRAY)

    orb = cv2.ORB_create(nfeatures=1200, fastThreshold=10)
    before_points, before_descriptors = orb.detectAndCompute(before_gray, None)
    after_points, after_descriptors = orb.detectAndCompute(after_gray, None)
    good_matches = []
    if before_descriptors is not None and after_descriptors is not None:
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        for pair in matcher.knnMatch(before_descriptors, after_descriptors, k=2):
            if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance:
                good_matches.append(pair[0])
    geometric_inlier_indices: list[int] = []
    if len(good_matches) >= 4:
        source_points = np.float32(
            [before_points[match.queryIdx].pt for match in good_matches]
        )
        target_points = np.float32(
            [after_points[match.trainIdx].pt for match in good_matches]
        )
        _homography, inlier_mask = cv2.findHomography(
            source_points,
            target_points,
            cv2.RANSAC,
            3.0,
        )
        if inlier_mask is not None:
            geometric_inlier_indices = [
                index
                for index, value in enumerate(inlier_mask.ravel().tolist())
                if int(value) == 1
            ]
    matched_quadrants = {
        (
            int(
                before_points[good_matches[index].queryIdx].pt[0]
                >= before_gray.shape[1] / 2
            ),
            int(
                before_points[good_matches[index].queryIdx].pt[1]
                >= before_gray.shape[0] / 2
            ),
        )
        for index in geometric_inlier_indices
    }
    geometric_inlier_ratio = (
        len(geometric_inlier_indices) / len(good_matches) if good_matches else 0.0
    )

    before_hist = cv2.calcHist(
        [before], [0, 1], None, [24, 24], [0, 256, 0, 256]
    )
    after_hist = cv2.calcHist(
        [after], [0, 1], None, [24, 24], [0, 256, 0, 256]
    )
    cv2.normalize(before_hist, before_hist)
    cv2.normalize(after_hist, after_hist)
    histogram_correlation = float(
        cv2.compareHist(before_hist, after_hist, cv2.HISTCMP_CORREL)
    )

    flow_explained: list[float] = []
    zone_luma: list[float] = []
    for frame_index in range(zone_start, zone_end):
        gray = cv2.resize(
            cv2.cvtColor(np.asarray(frames[frame_index]), cv2.COLOR_BGR2GRAY),
            (240, 135),
            interpolation=cv2.INTER_AREA,
        )
        zone_luma.append(float(gray.mean()))
        if frame_index == 0:
            continue
        prior = cv2.resize(
            cv2.cvtColor(np.asarray(frames[frame_index - 1]), cv2.COLOR_BGR2GRAY),
            (240, 135),
            interpolation=cv2.INTER_AREA,
        )
        flow = cv2.calcOpticalFlowFarneback(
            prior,
            gray,
            None,
            0.5,
            3,
            15,
            3,
            5,
            1.2,
            0,
        )
        grid_y, grid_x = np.mgrid[0:135, 0:240].astype(np.float32)
        warped = cv2.remap(
            prior.astype(np.float32),
            grid_x - flow[..., 0],
            grid_y - flow[..., 1],
            cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT,
        )
        raw_mae = float(
            np.mean(np.abs(prior.astype(np.float32) - gray.astype(np.float32)))
        )
        warped_mae = float(np.mean(np.abs(warped - gray.astype(np.float32))))
        if raw_mae > 1e-6:
            flow_explained.append(1.0 - warped_mae / raw_mae)

    median_flow_explained = (
        float(np.median(np.asarray(flow_explained, dtype=np.float64)))
        if flow_explained
        else 0.0
    )
    luma_min = min(zone_luma) if zone_luma else 0.0
    luma_max = max(zone_luma) if zone_luma else 0.0
    endpoint_delta = after_gray.astype(np.float32) - before_gray.astype(np.float32)
    endpoint_direction = 1.0 if float(endpoint_delta.mean()) >= 0.0 else -1.0
    material_endpoint_change = np.abs(endpoint_delta) >= 5.0
    material_change_fraction = float(material_endpoint_change.mean())
    directionally_consistent = (
        material_endpoint_change & (endpoint_delta * endpoint_direction > 0.0)
    )
    directional_uniformity = float(directionally_consistent.mean())
    directional_quadrants = 0
    midpoint_y = before_gray.shape[0] // 2
    midpoint_x = before_gray.shape[1] // 2
    for vertical in (slice(0, midpoint_y), slice(midpoint_y, None)):
        for horizontal in (slice(0, midpoint_x), slice(midpoint_x, None)):
            local_material = material_endpoint_change[vertical, horizontal]
            local_consistent = directionally_consistent[vertical, horizontal]
            if float(local_material.mean()) >= 0.35 and float(local_consistent.mean()) >= 0.30:
                directional_quadrants += 1
    kind = str(raw.get("kind") or "transition")
    hard_cross_mae = hard_within_mae = hard_cross_histogram = 0.0
    if kind == "hard_cut" and raw.get("boundary_frame") is not None:
        boundary = int(raw["boundary_frame"])
        if 1 <= boundary < len(frames):
            local_metrics = compute_transition_metrics(
                frames[max(0, boundary - 3) : min(len(frames), boundary + 3)]
            )
            local_boundary = boundary - max(0, boundary - 3)
            hard_cross_mae = float(local_metrics["frame_mae"][local_boundary])
            hard_cross_histogram = float(
                local_metrics["histogram_distance"][local_boundary]
            )
            within_values = [
                float(value)
                for index, value in enumerate(local_metrics["frame_mae"])
                if index not in {0, local_boundary}
            ]
            hard_within_mae = (
                float(np.median(np.asarray(within_values, dtype=np.float64)))
                if within_values
                else 0.0
            )
    canonical_metrics = {
        "feature_matches": len(good_matches),
        "geometric_inliers": len(geometric_inlier_indices),
        "geometric_inlier_ratio": round(geometric_inlier_ratio, 9),
        "matched_quadrants": len(matched_quadrants),
        "histogram_correlation": round(histogram_correlation, 9),
        "median_flow_explained": round(median_flow_explained, 9),
        "luma_min": round(luma_min, 6),
        "luma_max": round(luma_max, 6),
        "material_change_fraction": round(material_change_fraction, 9),
        "directional_uniformity": round(directional_uniformity, 9),
        "directional_quadrants": directional_quadrants,
        "hard_cross_mae": round(hard_cross_mae, 9),
        "hard_within_mae": round(hard_within_mae, 9),
        "hard_cross_histogram": round(hard_cross_histogram, 9),
    }
    derived = _derive_transition_corroboration_result(
        kind,
        boundary_present=raw.get("boundary_frame") is not None,
        **canonical_metrics,
    )
    return {
        "version": TRANSITION_CORROBORATOR_VERSION,
        "decision": derived["decision"],
        "classification": derived["classification"],
        "endpoint_frames": [before_index, after_index],
        "feature_matches": canonical_metrics["feature_matches"],
        "geometric_inliers": canonical_metrics["geometric_inliers"],
        "geometric_inlier_ratio": canonical_metrics["geometric_inlier_ratio"],
        "matched_quadrants": canonical_metrics["matched_quadrants"],
        "histogram_correlation": canonical_metrics["histogram_correlation"],
        "median_flow_explained": canonical_metrics["median_flow_explained"],
        "luma_min": canonical_metrics["luma_min"],
        "luma_max": canonical_metrics["luma_max"],
        "material_endpoint_change_fraction": canonical_metrics[
            "material_change_fraction"
        ],
        "directional_uniformity": canonical_metrics["directional_uniformity"],
        "directional_quadrants": canonical_metrics["directional_quadrants"],
        "hard_cross_mae": canonical_metrics["hard_cross_mae"],
        "hard_within_mae": canonical_metrics["hard_within_mae"],
        "hard_cross_histogram": canonical_metrics["hard_cross_histogram"],
        "hard_boundary_evidence": derived["hard_boundary_evidence"],
        "feature_continuity": derived["feature_continuity"],
        "appearance_motion_continuity": derived["appearance_motion_continuity"],
    }


def _transition_evidence(
    frames: Sequence[Any],
    *,
    chunk_index: int,
    start_frame: int,
) -> list[dict[str, Any]]:
    metrics = compute_transition_metrics(frames)
    zones = detect_transition_zones(
        metrics["frame_luma"],
        frame_mae=metrics["frame_mae"],
        histogram_distance=metrics["histogram_distance"],
    )
    output = []
    for raw in zones:
        item = {
            "chunk_index": chunk_index,
            "kind": str(raw.get("kind") or "transition"),
            "start_frame": start_frame + int(raw["start_frame"]),
            "end_frame": start_frame + int(raw["end_frame"]),
            "confidence": round(float(raw.get("confidence") or 0.0), 9),
        }
        if raw.get("boundary_frame") is not None:
            item["boundary_frame"] = start_frame + int(raw["boundary_frame"])
        item["corroboration"] = _transition_zone_corroboration(frames, raw)
        output.append(item)
    return output


def _source_span_duration_document(exact: Mapping[str, Any]) -> dict[str, int]:
    try:
        start_pts = int(exact["start_pts"])
        end_pts = int(exact["end_pts"])
        time_base = exact["time_base"]
        duration = Fraction(
            (end_pts - start_pts) * int(time_base["numerator"]),
            int(time_base["denominator"]),
        )
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise SelectedVisualGateError("selected source span duration is malformed") from exc
    if duration <= 0:
        raise SelectedVisualGateError("selected source span duration must be positive")
    return {"numerator": duration.numerator, "denominator": duration.denominator}


def _minimum_near_black_run_frames(
    frame_count: int,
    source_span_duration: Mapping[str, Any],
) -> int:
    try:
        duration = Fraction(
            int(source_span_duration["numerator"]),
            int(source_span_duration["denominator"]),
        )
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise SelectedVisualGateError("near-black source duration is malformed") from exc
    if duration <= 0:
        raise SelectedVisualGateError("near-black source duration must be positive")
    required = (
        Fraction(
            frame_count * NEAR_BLACK_HOLD_MINIMUM_MILLISECONDS,
            1_000,
        )
        / duration
    )
    return max(2, (required.numerator + required.denominator - 1) // required.denominator)


def _is_near_black_frame_metric(metric: Mapping[str, Any]) -> bool:
    return bool(
        int(metric["luma_mean_milli"]) <= NEAR_BLACK_LUMA_MEAN_MILLI_MAX
        and int(metric["luma_p95"]) <= NEAR_BLACK_LUMA_P95_MAX
        and int(metric["luma_p95"]) - int(metric["luma_p05"])
        <= NEAR_BLACK_LUMA_P95_P05_SPREAD_MAX
    )


def _near_black_frame_metrics(
    frames: Sequence[Any],
    *,
    start_frame: int,
) -> list[dict[str, Any]]:
    """Return deterministic integer luma evidence for the planned render view."""

    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - explicit dependency gate.
        raise SelectedVisualGateError("numpy is required for selected visual gate") from exc
    output: list[dict[str, Any]] = []
    for local_index, frame in enumerate(frames):
        array = np.asarray(frame)
        if array.ndim != 3 or array.shape[2] != 3 or array.size == 0:
            raise SelectedVisualGateError("near-black analysis frame is malformed")
        bgr = array.astype(np.uint32, copy=False)
        luma = (
            bgr[:, :, 0] * 29
            + bgr[:, :, 1] * 150
            + bgr[:, :, 2] * 77
            + 128
        ) // 256
        histogram = np.bincount(luma.reshape(-1), minlength=256)
        cumulative = np.cumsum(histogram)
        pixel_count = int(luma.size)

        def percentile(percent: int) -> int:
            rank = max(1, (pixel_count * percent + 99) // 100)
            return int(np.searchsorted(cumulative, rank, side="left"))

        metric = {
            "frame_index": start_frame + local_index,
            "luma_mean_milli": (
                int(luma.sum(dtype=np.uint64)) * 1_000 + pixel_count // 2
            )
            // pixel_count,
            "luma_p05": percentile(5),
            "luma_p95": percentile(95),
        }
        metric["near_black"] = _is_near_black_frame_metric(metric)
        output.append(metric)
    return output


def _derive_near_black_runs(
    frame_metrics: Sequence[Mapping[str, Any]],
    *,
    minimum_run_frames: int,
) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    start: int | None = None
    for index in range(len(frame_metrics) + 1):
        near_black = bool(
            frame_metrics[index]["near_black"]
            if index < len(frame_metrics)
            else False
        )
        if near_black and start is None:
            start = index
        elif not near_black and start is not None:
            frame_count = index - start
            runs.append(
                {
                    "start_frame": start,
                    "end_frame": index,
                    "frame_count": frame_count,
                    "rejected": frame_count >= minimum_run_frames,
                }
            )
            start = None
    return runs


def _build_near_black_hold_evidence(
    frame_metrics: Sequence[Mapping[str, Any]],
    *,
    source_span_duration: Mapping[str, Any],
) -> dict[str, Any]:
    metrics = _json_clone(list(frame_metrics), "near-black frame metrics")
    minimum_run_frames = _minimum_near_black_run_frames(
        len(metrics), source_span_duration
    )
    runs = _derive_near_black_runs(
        metrics,
        minimum_run_frames=minimum_run_frames,
    )
    return {
        "version": NEAR_BLACK_HOLD_DETECTOR_VERSION,
        "policy": {
            "minimum_run_milliseconds": NEAR_BLACK_HOLD_MINIMUM_MILLISECONDS,
            "luma_mean_milli_max": NEAR_BLACK_LUMA_MEAN_MILLI_MAX,
            "luma_p95_max": NEAR_BLACK_LUMA_P95_MAX,
            "luma_p95_p05_spread_max": NEAR_BLACK_LUMA_P95_P05_SPREAD_MAX,
        },
        "source_span_duration": _json_clone(
            dict(source_span_duration), "near-black source span duration"
        ),
        "minimum_run_frames": minimum_run_frames,
        "frame_count": len(metrics),
        "frame_metrics": metrics,
        "runs": runs,
        "rejection_count": sum(bool(run["rejected"]) for run in runs),
    }


def _binding(
    candidate: Mapping[str, Any],
    profile: Mapping[str, Any],
    actionable_profile_binding: Mapping[str, Any],
    selected: Mapping[str, Any],
    planned_view: Mapping[str, Any],
    *,
    output_fps: Mapping[str, int],
    render_frames: int,
    width: int,
    height: int,
    max_decode_bytes: int,
    chunk_overlap_frames: int,
) -> dict[str, Any]:
    clip_id = _portable_id(candidate.get("clip_id"), "candidate.clip_id")
    candidate_key = _sha256(candidate.get("candidate_key"), "candidate.candidate_key")
    candidate_range_hash = _sha256(
        candidate.get("candidate_range_hash"), "candidate.candidate_range_hash"
    )
    source_asset_id = _portable_id(
        candidate.get("source_asset_id"), "candidate.source_asset_id"
    )
    source_sha256 = _sha256(candidate.get("source_sha256"), "candidate.source_sha256")
    if profile.get("source_asset_id") != source_asset_id:
        raise SelectedVisualGateError("donor overlay profile source_asset_id mismatch")
    if profile.get("source_sha256") != source_sha256:
        raise SelectedVisualGateError("donor overlay profile source_sha256 mismatch")
    range_binding = {
        "source_in": round(float(candidate["source_in"]), 9),
        "source_out": round(float(candidate["source_out"]), 9),
        "source_frame_start": int(candidate["source_frame_start"]),
        "source_frame_end": int(candidate["source_frame_end"]),
        "source_frames": int(selected["frame_count"]),
        "start_pts": int(selected["start_pts"]),
        "end_pts": int(selected["end_pts"]),
        "time_base": dict(selected["time_base"]),
        "exact_pts_hash": _sha256(selected.get("content_hash"), "selected exact_pts hash"),
    }
    content = {
        "clip_id": clip_id,
        "candidate_key": candidate_key,
        "candidate_range_hash": candidate_range_hash,
        "source_asset_id": source_asset_id,
        "source_sha256": source_sha256,
        "range": range_binding,
        "render_frames": render_frames,
        "output_fps": dict(output_fps),
    }
    return {
        "content": content,
        "content_hash": canonical_hash(content),
        "donor_overlay_profile_hash": _sha256(
            profile.get("content_hash"), "donor_overlay_profile.content_hash"
        ),
        "actionable_profile_binding": _json_clone(
            dict(actionable_profile_binding), "actionable_profile_binding"
        ),
        "detectors": {
            "overlay_observation_version": DETECTOR_VERSION,
            "overlay_profile_version": PROFILE_DETECTOR_VERSION,
            "transition_version": TRANSITION_DETECTOR_VERSION,
            "transition_corroborator_version": TRANSITION_CORROBORATOR_VERSION,
            "near_black_hold_version": NEAR_BLACK_HOLD_DETECTOR_VERSION,
        },
        "render_geometry_proxy": _json_clone(
            dict(planned_view), "planned_render_view"
        ),
        "analysis": {
            "width": width,
            "height": height,
            "pixel_format": "bgr24",
            "max_decode_bytes": max_decode_bytes,
            "chunk_overlap_frames": chunk_overlap_frames,
        },
    }


_BINDING_FIELDS = {
    "content",
    "content_hash",
    "donor_overlay_profile_hash",
    "actionable_profile_binding",
    "detectors",
    "render_geometry_proxy",
    "analysis",
}
_EVIDENCE_FIELDS = {
    "selected_source_frames",
    "render_frames",
    "chunk_count",
    "decoded_bytes_total",
    "analysis_dimensions",
    "analysis_contract",
    "source_dimensions",
    "render_geometry_proxy",
    "actionable_profile_binding",
    "known_profile_projection",
    "current_profile_matches",
    "raw_crop_authorization",
    "overlay_chunks",
    "near_black_hold",
    "transition_count",
    "transition_rejection_count",
    "transitions",
}
_OVERLAY_SUMMARY_FIELDS = {
    "chunk_index",
    "start_frame",
    "end_frame",
    "frame_count",
    "status",
    "action",
    "observation_count",
    "profile_rejection_reasons",
    "ocr_tokens",
    "positive_findings",
    "tracks",
}
_OVERLAY_TRACK_REQUIRED_FIELDS = {
    "profile_kind",
    "class_hint",
    "frame_ranges",
    "frames_seen",
    "independent_sample_count",
    "persistence",
    "confidence",
    "ocr_tokens",
    "meaningful_ocr_tokens",
    "positive_evidence",
}
_OVERLAY_TRACK_OPTIONAL_FIELDS = {
    "profile_track_id",
    "max_global_change_ratio",
    "bbox_normalized",
}
_POSITIVE_FINDING_FIELDS = {
    "kind",
    "profile_kind",
    "class_hint",
    "profile_track_id",
    "frame_ranges",
    "bbox_normalized",
    "meaningful_ocr_tokens",
    "independent_sample_count",
}
_POSITIVE_FINDING_KINDS = {
    "stable_ocr_title",
    "known_profile_overlay_visible",
}
_PROFILE_PROJECTION_FIELDS = {
    "track_id",
    "decision",
    "classification",
    "support_contexts",
    "support_context_ids",
    "observed_profile_context_count",
    "minimum_separated_context_support",
    "persistence",
    "presence_basis",
    "presence_proven",
    "source_bbox_normalized",
    "projected_bbox_pixels",
    "visible_bbox_pixels",
    "visible_width_pixels",
    "visible_height_pixels",
    "visible_area_pixels",
    "visible_fraction",
    "geometry_materially_visible",
    "materially_visible",
    "current_high_resolution_profile_match",
}
_CURRENT_PROFILE_MATCH_FIELDS = {
    "track_id",
    "coordinate_space",
    "chunk_index",
    "start_frame",
    "observation_index",
    "class_hint",
    "profile_kind",
    "direct_profile_track_id",
    "frames_seen",
    "profile_bbox_normalized",
    "observation_bbox_normalized",
    "overlap",
}
_RAW_CROP_ENTRY_FIELDS = {"chunk_index", "overlay", "authorization"}
_RAW_CROP_AUTHORIZATION_FIELDS = {
    "status",
    "action",
    "detected_crop_margins",
    "persisted_crop_margins",
    "outside_bound_edges",
    "detected_crop_provenance",
    "persisted_crop_provenance",
    "provenance_matches",
}
_TRANSITION_REQUIRED_FIELDS = {
    "chunk_index",
    "kind",
    "start_frame",
    "end_frame",
    "confidence",
    "corroboration",
}
_TRANSITION_OPTIONAL_FIELDS = {"boundary_frame"}
_TRANSITION_CORROBORATION_FIELDS = {
    "version",
    "decision",
    "classification",
    "endpoint_frames",
    "feature_matches",
    "geometric_inliers",
    "geometric_inlier_ratio",
    "matched_quadrants",
    "histogram_correlation",
    "median_flow_explained",
    "luma_min",
    "luma_max",
    "material_endpoint_change_fraction",
    "directional_uniformity",
    "directional_quadrants",
    "hard_cross_mae",
    "hard_within_mae",
    "hard_cross_histogram",
    "hard_boundary_evidence",
    "feature_continuity",
    "appearance_motion_continuity",
}
_NEAR_BLACK_HOLD_FIELDS = {
    "version",
    "policy",
    "source_span_duration",
    "minimum_run_frames",
    "frame_count",
    "frame_metrics",
    "runs",
    "rejection_count",
}
_NEAR_BLACK_POLICY_FIELDS = {
    "minimum_run_milliseconds",
    "luma_mean_milli_max",
    "luma_p95_max",
    "luma_p95_p05_spread_max",
}
_NEAR_BLACK_FRAME_METRIC_FIELDS = {
    "frame_index",
    "luma_mean_milli",
    "luma_p05",
    "luma_p95",
    "near_black",
}
_NEAR_BLACK_RUN_FIELDS = {
    "start_frame",
    "end_frame",
    "frame_count",
    "rejected",
}


def _evidence_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SelectedVisualGateError(f"{label} must be an object")
    return value


def _evidence_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise SelectedVisualGateError(f"{label} must be an array")
    return value


def _evidence_exact_fields(
    value: Mapping[str, Any],
    expected: set[str],
    label: str,
) -> None:
    if set(value) != expected:
        raise SelectedVisualGateError(f"{label} fields are incompatible")


def _evidence_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SelectedVisualGateError(f"{label} must be an integer >= {minimum}")
    return value


def _evidence_float(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise SelectedVisualGateError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SelectedVisualGateError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise SelectedVisualGateError(f"{label} must be a finite number")
    return result


def _validate_near_black_hold_evidence(
    value: Any,
    *,
    selected_source_frames: int,
) -> int:
    label = "evidence.near_black_hold"
    row = _evidence_mapping(value, label)
    _evidence_exact_fields(row, _NEAR_BLACK_HOLD_FIELDS, label)
    if row.get("version") != NEAR_BLACK_HOLD_DETECTOR_VERSION:
        raise SelectedVisualGateError(f"{label} version is incompatible")

    policy = _evidence_mapping(row.get("policy"), f"{label}.policy")
    _evidence_exact_fields(policy, _NEAR_BLACK_POLICY_FIELDS, f"{label}.policy")
    expected_policy = {
        "minimum_run_milliseconds": NEAR_BLACK_HOLD_MINIMUM_MILLISECONDS,
        "luma_mean_milli_max": NEAR_BLACK_LUMA_MEAN_MILLI_MAX,
        "luma_p95_max": NEAR_BLACK_LUMA_P95_MAX,
        "luma_p95_p05_spread_max": NEAR_BLACK_LUMA_P95_P05_SPREAD_MAX,
    }
    for key, expected in expected_policy.items():
        if _evidence_int(policy.get(key), f"{label}.policy.{key}") != expected:
            raise SelectedVisualGateError(f"{label} policy is incompatible")

    duration_doc = _evidence_mapping(
        row.get("source_span_duration"), f"{label}.source_span_duration"
    )
    _evidence_exact_fields(
        duration_doc,
        {"numerator", "denominator"},
        f"{label}.source_span_duration",
    )
    duration = Fraction(
        _evidence_int(
            duration_doc.get("numerator"),
            f"{label}.source_span_duration.numerator",
            minimum=1,
        ),
        _evidence_int(
            duration_doc.get("denominator"),
            f"{label}.source_span_duration.denominator",
            minimum=1,
        ),
    )
    canonical_duration = {
        "numerator": duration.numerator,
        "denominator": duration.denominator,
    }
    if dict(duration_doc) != canonical_duration:
        raise SelectedVisualGateError(f"{label} source duration is not canonical")
    expected_minimum = _minimum_near_black_run_frames(
        selected_source_frames,
        canonical_duration,
    )
    if (
        _evidence_int(
            row.get("minimum_run_frames"), f"{label}.minimum_run_frames", minimum=2
        )
        != expected_minimum
    ):
        raise SelectedVisualGateError(f"{label} minimum run length disagrees")
    if (
        _evidence_int(row.get("frame_count"), f"{label}.frame_count", minimum=1)
        != selected_source_frames
    ):
        raise SelectedVisualGateError(f"{label} frame count disagrees")

    raw_metrics = _evidence_list(row.get("frame_metrics"), f"{label}.frame_metrics")
    if len(raw_metrics) != selected_source_frames:
        raise SelectedVisualGateError(f"{label} frame metrics are incomplete")
    metrics: list[dict[str, Any]] = []
    for index, raw_metric in enumerate(raw_metrics):
        metric_label = f"{label}.frame_metrics[{index}]"
        metric = _evidence_mapping(raw_metric, metric_label)
        _evidence_exact_fields(metric, _NEAR_BLACK_FRAME_METRIC_FIELDS, metric_label)
        if (
            _evidence_int(metric.get("frame_index"), f"{metric_label}.frame_index")
            != index
        ):
            raise SelectedVisualGateError(f"{metric_label} ordering disagrees")
        mean_milli = _evidence_int(
            metric.get("luma_mean_milli"), f"{metric_label}.luma_mean_milli"
        )
        p05 = _evidence_int(metric.get("luma_p05"), f"{metric_label}.luma_p05")
        p95 = _evidence_int(metric.get("luma_p95"), f"{metric_label}.luma_p95")
        if mean_milli > 255_000 or p05 > 255 or p95 > 255 or p05 > p95:
            raise SelectedVisualGateError(f"{metric_label} luma metrics are invalid")
        near_black = metric.get("near_black")
        if not isinstance(near_black, bool):
            raise SelectedVisualGateError(
                f"{metric_label}.near_black must be boolean"
            )
        normalized_metric = {
            "frame_index": index,
            "luma_mean_milli": mean_milli,
            "luma_p05": p05,
            "luma_p95": p95,
            "near_black": near_black,
        }
        if near_black is not _is_near_black_frame_metric(normalized_metric):
            raise SelectedVisualGateError(
                f"{metric_label} classification disagrees with raw luma metrics"
            )
        metrics.append(normalized_metric)

    expected_runs = _derive_near_black_runs(
        metrics,
        minimum_run_frames=expected_minimum,
    )
    raw_runs = _evidence_list(row.get("runs"), f"{label}.runs")
    normalized_runs: list[dict[str, Any]] = []
    for index, raw_run in enumerate(raw_runs):
        run_label = f"{label}.runs[{index}]"
        run = _evidence_mapping(raw_run, run_label)
        _evidence_exact_fields(run, _NEAR_BLACK_RUN_FIELDS, run_label)
        start = _evidence_int(run.get("start_frame"), f"{run_label}.start_frame")
        end = _evidence_int(
            run.get("end_frame"), f"{run_label}.end_frame", minimum=1
        )
        frame_count = _evidence_int(
            run.get("frame_count"), f"{run_label}.frame_count", minimum=1
        )
        rejected = run.get("rejected")
        if not isinstance(rejected, bool):
            raise SelectedVisualGateError(f"{run_label}.rejected must be boolean")
        normalized_runs.append(
            {
                "start_frame": start,
                "end_frame": end,
                "frame_count": frame_count,
                "rejected": rejected,
            }
        )
    if normalized_runs != expected_runs:
        raise SelectedVisualGateError(
            f"{label} derived runs disagree with raw frame metrics"
        )
    expected_rejections = sum(bool(run["rejected"]) for run in expected_runs)
    if (
        _evidence_int(row.get("rejection_count"), f"{label}.rejection_count")
        != expected_rejections
    ):
        raise SelectedVisualGateError(f"{label} rejection count disagrees")
    return expected_rejections


def _evidence_string_list(value: Any, label: str) -> list[str]:
    values = _evidence_list(value, label)
    if any(not isinstance(item, str) or not item for item in values):
        raise SelectedVisualGateError(f"{label} must contain non-empty strings")
    if values != sorted(set(values)):
        raise SelectedVisualGateError(f"{label} must be sorted and unique")
    return values


def _evidence_bbox(value: Any, label: str, *, nullable: bool = False) -> Any:
    if nullable and value is None:
        return None
    bbox = _rounded_bbox(value)
    if bbox is None:
        raise SelectedVisualGateError(f"{label} must be a four-value bbox")
    return bbox


def _positive_finding_key(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _validate_overlay_summary(
    value: Any,
    *,
    label: str,
    derive_positive_codes: bool,
) -> set[str]:
    row = _evidence_mapping(value, label)
    _evidence_exact_fields(row, _OVERLAY_SUMMARY_FIELDS, label)
    chunk_index = _evidence_int(row.get("chunk_index"), f"{label}.chunk_index")
    start_frame = _evidence_int(row.get("start_frame"), f"{label}.start_frame")
    end_frame = _evidence_int(row.get("end_frame"), f"{label}.end_frame", minimum=1)
    frame_count = _evidence_int(row.get("frame_count"), f"{label}.frame_count", minimum=1)
    if end_frame <= start_frame or frame_count != end_frame - start_frame:
        raise SelectedVisualGateError(f"{label} frame bounds are inconsistent")
    if not isinstance(row.get("status"), str) or not isinstance(row.get("action"), str):
        raise SelectedVisualGateError(f"{label} status/action are malformed")
    _evidence_int(row.get("observation_count"), f"{label}.observation_count")
    _evidence_string_list(
        row.get("profile_rejection_reasons"),
        f"{label}.profile_rejection_reasons",
    )
    _evidence_string_list(row.get("ocr_tokens"), f"{label}.ocr_tokens")

    track_positive: list[str] = []
    tracks = _evidence_list(row.get("tracks"), f"{label}.tracks")
    for index, raw_track in enumerate(tracks):
        track_label = f"{label}.tracks[{index}]"
        track = _evidence_mapping(raw_track, track_label)
        if (
            not _OVERLAY_TRACK_REQUIRED_FIELDS.issubset(track)
            or not set(track).issubset(
                _OVERLAY_TRACK_REQUIRED_FIELDS | _OVERLAY_TRACK_OPTIONAL_FIELDS
            )
        ):
            raise SelectedVisualGateError(f"{track_label} fields are incompatible")
        _evidence_int(track.get("frames_seen"), f"{track_label}.frames_seen")
        independent = _evidence_int(
            track.get("independent_sample_count"),
            f"{track_label}.independent_sample_count",
        )
        meaningful = _evidence_string_list(
            track.get("meaningful_ocr_tokens"),
            f"{track_label}.meaningful_ocr_tokens",
        )
        track_tokens = _evidence_string_list(
            track.get("ocr_tokens"), f"{track_label}.ocr_tokens"
        )
        expected_meaningful = sorted(
            token
            for token in track_tokens
            if sum(character.isalpha() for character in token) >= 3
        )
        if meaningful != expected_meaningful:
            raise SelectedVisualGateError(
                f"{track_label} meaningful OCR tokens disagree"
            )
        kind = track.get("positive_evidence")
        if kind is not None and kind not in _POSITIVE_FINDING_KINDS:
            raise SelectedVisualGateError(f"{track_label} has an unknown positive finding")
        profile_kind = str(track.get("profile_kind") or "")
        class_hint = str(track.get("class_hint") or "")
        if profile_kind == "profile_track":
            expected_kind = "known_profile_overlay_visible"
        elif (
            profile_kind in {"central_title", "transient_title"}
            and class_hint == "ocr_text"
            and independent >= 2
            and meaningful
        ):
            expected_kind = "stable_ocr_title"
        else:
            expected_kind = None
        if kind != expected_kind:
            raise SelectedVisualGateError(
                f"{track_label} positive evidence disagrees with raw track fields"
            )
        if expected_kind == "known_profile_overlay_visible" and not track.get(
            "profile_track_id"
        ):
            raise SelectedVisualGateError(f"{track_label} profile proof has no track id")
        if kind is not None:
            track_positive.append(
                _positive_finding_key(
                    {
                        "kind": kind,
                        "profile_kind": track.get("profile_kind"),
                        "class_hint": track.get("class_hint"),
                        "profile_track_id": track.get("profile_track_id"),
                        "frame_ranges": track.get("frame_ranges"),
                        "bbox_normalized": track.get("bbox_normalized"),
                        "meaningful_ocr_tokens": meaningful,
                        "independent_sample_count": independent,
                    }
                )
            )

    finding_positive: list[str] = []
    stable_ocr_present = False
    findings = _evidence_list(
        row.get("positive_findings"), f"{label}.positive_findings"
    )
    for index, raw_finding in enumerate(findings):
        finding_label = f"{label}.positive_findings[{index}]"
        finding = _evidence_mapping(raw_finding, finding_label)
        _evidence_exact_fields(finding, _POSITIVE_FINDING_FIELDS, finding_label)
        kind = finding.get("kind")
        if kind not in _POSITIVE_FINDING_KINDS:
            raise SelectedVisualGateError(f"{finding_label} kind is unknown")
        independent = _evidence_int(
            finding.get("independent_sample_count"),
            f"{finding_label}.independent_sample_count",
        )
        meaningful = _evidence_string_list(
            finding.get("meaningful_ocr_tokens"),
            f"{finding_label}.meaningful_ocr_tokens",
        )
        if kind == "stable_ocr_title":
            if independent < 2 or not meaningful:
                raise SelectedVisualGateError(f"{finding_label} stable OCR proof is incomplete")
            stable_ocr_present = True
        elif not finding.get("profile_track_id"):
            raise SelectedVisualGateError(f"{finding_label} profile proof has no track id")
        finding_positive.append(_positive_finding_key(finding))
    if Counter(track_positive) != Counter(finding_positive):
        raise SelectedVisualGateError(
            f"{label} track and positive-finding evidence disagree"
        )
    return {"overlay_rejected"} if derive_positive_codes and stable_ocr_present else set()


def derive_rejection_codes(evidence: Mapping[str, Any]) -> list[str]:
    """Derive final rejection codes solely from canonical visual evidence."""

    if not isinstance(evidence, Mapping):
        raise SelectedVisualGateError("selected visual gate evidence must be an object")
    row = _json_clone(dict(evidence), "selected visual gate evidence")
    _evidence_exact_fields(row, _EVIDENCE_FIELDS, "selected visual gate evidence")
    selected_source_frames = _evidence_int(
        row.get("selected_source_frames"), "evidence.selected_source_frames", minimum=3
    )
    _evidence_int(row.get("render_frames"), "evidence.render_frames", minimum=1)
    chunk_count = _evidence_int(row.get("chunk_count"), "evidence.chunk_count", minimum=1)
    decoded_bytes_total = _evidence_int(
        row.get("decoded_bytes_total"), "evidence.decoded_bytes_total", minimum=1
    )
    for field in ("analysis_dimensions", "source_dimensions"):
        dimensions = _evidence_mapping(row.get(field), f"evidence.{field}")
        _evidence_exact_fields(dimensions, {"width", "height"}, f"evidence.{field}")
        _evidence_int(dimensions.get("width"), f"evidence.{field}.width", minimum=1)
        _evidence_int(dimensions.get("height"), f"evidence.{field}.height", minimum=1)
    analysis_contract = _evidence_mapping(
        row.get("analysis_contract"), "evidence.analysis_contract"
    )
    _evidence_exact_fields(
        analysis_contract,
        {
            "width",
            "height",
            "pixel_format",
            "max_decode_bytes",
            "chunk_overlap_frames",
        },
        "evidence.analysis_contract",
    )
    if (
        analysis_contract.get("width") != row["analysis_dimensions"]["width"]
        or analysis_contract.get("height") != row["analysis_dimensions"]["height"]
        or analysis_contract.get("pixel_format") != "bgr24"
    ):
        raise SelectedVisualGateError("evidence analysis contract disagrees")
    max_decode_bytes = _evidence_int(
        analysis_contract.get("max_decode_bytes"),
        "evidence.analysis_contract.max_decode_bytes",
        minimum=1,
    )
    chunk_overlap_frames = _evidence_int(
        analysis_contract.get("chunk_overlap_frames"),
        "evidence.analysis_contract.chunk_overlap_frames",
    )
    expected_chunk_ranges = _chunk_ranges(
        selected_source_frames,
        width=int(analysis_contract["width"]),
        height=int(analysis_contract["height"]),
        max_decode_bytes=max_decode_bytes,
        overlap_frames=chunk_overlap_frames,
    )
    if chunk_count != len(expected_chunk_ranges):
        raise SelectedVisualGateError("evidence chunk count disagrees with decode policy")
    planned_view = _evidence_mapping(
        row.get("render_geometry_proxy"), "evidence.render_geometry_proxy"
    )
    actionable_profile_binding = _validate_actionable_profile_binding(
        row.get("actionable_profile_binding")
    )
    actionable_tracks = {
        str(item["track_id"]): item
        for item in actionable_profile_binding["tracks"]
    }

    codes: set[str] = set()
    if _validate_near_black_hold_evidence(
        row.get("near_black_hold"),
        selected_source_frames=selected_source_frames,
    ):
        codes.add("sustained_near_black_hold")
    current_matches = _evidence_list(
        row.get("current_profile_matches"), "evidence.current_profile_matches"
    )
    current_track_ids: set[str] = set()
    for index, raw_match in enumerate(current_matches):
        label = f"evidence.current_profile_matches[{index}]"
        match = _evidence_mapping(raw_match, label)
        _evidence_exact_fields(match, _CURRENT_PROFILE_MATCH_FIELDS, label)
        track_id = _portable_id(match.get("track_id"), f"{label}.track_id")
        current_track_ids.add(track_id)
        if match.get("coordinate_space") not in {
            "raw_normalized_analysis_view",
            "render_geometry_proxy_480x270",
        }:
            raise SelectedVisualGateError(f"{label} coordinate space is unknown")
        match_chunk_index = _evidence_int(
            match.get("chunk_index"), f"{label}.chunk_index"
        )
        if match_chunk_index >= chunk_count:
            raise SelectedVisualGateError(f"{label} chunk index escapes evidence")
        _evidence_int(match.get("start_frame"), f"{label}.start_frame")
        _evidence_int(match.get("observation_index"), f"{label}.observation_index")
        _evidence_int(match.get("frames_seen"), f"{label}.frames_seen", minimum=3)
        metrics = _bbox_overlap_metrics(
            _evidence_bbox(match.get("profile_bbox_normalized"), f"{label}.profile_bbox"),
            _evidence_bbox(
                match.get("observation_bbox_normalized"), f"{label}.observation_bbox"
            ),
        )
        expected_overlap = {key: round(value, 9) for key, value in metrics.items()}
        overlap = _evidence_mapping(match.get("overlap"), f"{label}.overlap")
        _evidence_exact_fields(overlap, set(expected_overlap), f"{label}.overlap")
        if dict(overlap) != expected_overlap or not _profile_spatial_match(metrics):
            raise SelectedVisualGateError(f"{label} overlap proof is invalid")

    projections = _evidence_list(
        row.get("known_profile_projection"), "evidence.known_profile_projection"
    )
    geometry_contract = _evidence_mapping(
        planned_view.get("geometry"), "evidence.render_geometry_proxy.geometry"
    )
    projection_contract = _evidence_mapping(
        geometry_contract.get("known_profile_projection"),
        "evidence known-profile projection contract",
    )
    _evidence_mapping(
        projection_contract.get("material_visibility"),
        "evidence known-profile visibility contract",
    )
    proxy_width = int(row["analysis_dimensions"]["width"])
    proxy_height = int(row["analysis_dimensions"]["height"])
    projection_track_ids: set[str] = set()
    projection_geometry_by_id: dict[str, dict[str, Any]] = {}
    if len(projections) != len(actionable_tracks):
        raise SelectedVisualGateError(
            "evidence known-profile projection is incomplete"
        )
    for index, raw_projection in enumerate(projections):
        label = f"evidence.known_profile_projection[{index}]"
        projection = _evidence_mapping(raw_projection, label)
        _evidence_exact_fields(projection, _PROFILE_PROJECTION_FIELDS, label)
        track_id = _portable_id(projection.get("track_id"), f"{label}.track_id")
        if track_id in projection_track_ids:
            raise SelectedVisualGateError(f"{label} duplicates a profile track")
        projection_track_ids.add(track_id)
        expected_track = actionable_tracks.get(track_id)
        if expected_track is None:
            raise SelectedVisualGateError(f"{label} is not bound to an actionable track")
        decision = expected_track["decision"]
        if (
            projection.get("decision") != decision
            or projection.get("classification") != expected_track["classification"]
            or projection.get("support_contexts")
            != expected_track["support_contexts"]
            or projection.get("support_context_ids") != expected_track["context_ids"]
            or projection.get("persistence") != expected_track["persistence"]
            or projection.get("source_bbox_normalized")
            != expected_track["bbox_normalized"]
            or projection.get("observed_profile_context_count")
            != actionable_profile_binding["observed_profile_context_count"]
        ):
            raise SelectedVisualGateError(
                f"{label} disagrees with its actionable profile binding"
            )
        support = _evidence_int(projection.get("support_contexts"), f"{label}.support")
        support_ids = _evidence_string_list(
            projection.get("support_context_ids"), f"{label}.support_context_ids"
        )
        if support != len(support_ids):
            raise SelectedVisualGateError(f"{label} support context count disagrees")
        observed_contexts = _evidence_int(
            projection.get("observed_profile_context_count"),
            f"{label}.observed_profile_context_count",
        )
        if observed_contexts < support:
            raise SelectedVisualGateError(f"{label} observed context count is impossible")
        if (
            projection.get("minimum_separated_context_support")
            != PROFILE_MIN_SEPARATED_CONTEXTS
        ):
            raise SelectedVisualGateError(f"{label} support policy is incompatible")
        current_match = projection.get("current_high_resolution_profile_match")
        if not isinstance(current_match, bool) or current_match != (track_id in current_track_ids):
            raise SelectedVisualGateError(f"{label} current profile match disagrees")
        presence_proven = projection.get("presence_proven")
        geometry_visible = projection.get("geometry_materially_visible")
        materially_visible = projection.get("materially_visible")
        if not all(
            isinstance(value, bool)
            for value in (presence_proven, geometry_visible, materially_visible)
        ):
            raise SelectedVisualGateError(f"{label} visibility proof is malformed")
        separated_context_presence = bool(
            decision == "detector_crop"
            and expected_track["classification"] == "screen_fixed_edge_logo"
            and support >= PROFILE_MIN_SEPARATED_CONTEXTS
            and float(expected_track["persistence"]) >= PROFILE_PERSISTENCE_MIN
        )
        if current_match:
            expected_basis = "current_high_resolution_spatial_profile_match"
        elif separated_context_presence:
            expected_basis = "separated_context_persistent_profile"
        else:
            expected_basis = "donor_profile_geometry_only"
        expected_presence = bool(current_match or separated_context_presence)
        if projection.get("presence_basis") != expected_basis:
            raise SelectedVisualGateError(f"{label} presence basis disagrees")
        if presence_proven != expected_presence:
            raise SelectedVisualGateError(f"{label} presence proof disagrees")
        expected_geometry = _project_profile_bbox_geometry(
            expected_track["bbox_normalized"],
            planned_view,
            row["source_dimensions"],
            width=proxy_width,
            height=proxy_height,
        )
        if any(
            projection.get(field) != expected_value
            for field, expected_value in expected_geometry.items()
        ):
            raise SelectedVisualGateError(
                f"{label} projection geometry disagrees with source bbox/transform"
            )
        projection_geometry_by_id[track_id] = expected_geometry
        expected_geometry_visible = bool(
            expected_geometry["geometry_materially_visible"]
        )
        if geometry_visible != expected_geometry_visible:
            raise SelectedVisualGateError(f"{label} geometry visibility disagrees")
        if materially_visible != bool(expected_presence and expected_geometry_visible):
            raise SelectedVisualGateError(f"{label} material visibility disagrees")
        if materially_visible:
            codes.add("known_profile_overlay_visible")
    expected_projection_order = list(actionable_tracks)
    actual_projection_order = [str(item.get("track_id") or "") for item in projections]
    if actual_projection_order != expected_projection_order:
        raise SelectedVisualGateError(
            "evidence known-profile projection order/completeness disagrees"
        )
    if projection_track_ids != set(actionable_tracks):
        raise SelectedVisualGateError("evidence known-profile projection is incomplete")
    if not current_track_ids.issubset(projection_track_ids):
        raise SelectedVisualGateError("current profile evidence references an unknown track")

    overlays = _evidence_list(row.get("overlay_chunks"), "evidence.overlay_chunks")
    if len(overlays) != chunk_count:
        raise SelectedVisualGateError("evidence overlay chunk count disagrees")
    overlay_intervals: list[tuple[int, int]] = []
    for index, overlay in enumerate(overlays):
        codes.update(
            _validate_overlay_summary(
                overlay,
                label=f"evidence.overlay_chunks[{index}]",
                derive_positive_codes=True,
            )
        )
        overlay_row = _evidence_mapping(overlay, f"evidence.overlay_chunks[{index}]")
        if overlay_row.get("chunk_index") != index:
            raise SelectedVisualGateError("evidence overlay chunk ordering disagrees")
        overlay_intervals.append(
            (int(overlay_row["start_frame"]), int(overlay_row["end_frame"]))
        )
    if overlay_intervals != expected_chunk_ranges:
        raise SelectedVisualGateError(
            "evidence overlay chunks disagree with the decode policy"
        )

    raw_authorizations = _evidence_list(
        row.get("raw_crop_authorization"), "evidence.raw_crop_authorization"
    )
    planned_view = _evidence_mapping(
        row.get("render_geometry_proxy"), "evidence.render_geometry_proxy"
    )
    planned_crop_action = planned_view.get("crop_action")
    if planned_crop_action not in {"none", "crop"}:
        raise SelectedVisualGateError("evidence planned crop action is unknown")
    expected_raw_authorization_count = (
        chunk_count if planned_crop_action == "crop" else 0
    )
    if len(raw_authorizations) != expected_raw_authorization_count:
        raise SelectedVisualGateError(
            "evidence raw crop chunks disagree with the planned crop"
        )
    planned_crop_margins = _crop_margins(
        planned_view.get("crop_margins"), "evidence planned crop margins"
    )
    planned_crop_provenance = planned_view.get("crop_provenance")
    for index, raw_entry in enumerate(raw_authorizations):
        label = f"evidence.raw_crop_authorization[{index}]"
        entry = _evidence_mapping(raw_entry, label)
        _evidence_exact_fields(entry, _RAW_CROP_ENTRY_FIELDS, label)
        if _evidence_int(entry.get("chunk_index"), f"{label}.chunk_index") != index:
            raise SelectedVisualGateError(f"{label} chunk ordering disagrees")
        raw_overlay = _evidence_mapping(entry.get("overlay"), f"{label}.overlay")
        _validate_overlay_summary(
            raw_overlay,
            label=f"{label}.overlay",
            derive_positive_codes=False,
        )
        if (
            raw_overlay.get("chunk_index") != index
            or (
                raw_overlay.get("start_frame"),
                raw_overlay.get("end_frame"),
            )
            != overlay_intervals[index]
        ):
            raise SelectedVisualGateError(f"{label} raw/planned chunk lineage disagrees")
        authorization = _evidence_mapping(
            entry.get("authorization"), f"{label}.authorization"
        )
        _evidence_exact_fields(
            authorization,
            _RAW_CROP_AUTHORIZATION_FIELDS,
            f"{label}.authorization",
        )
        if (
            authorization.get("status") != raw_overlay.get("status")
            or authorization.get("action") != raw_overlay.get("action")
        ):
            raise SelectedVisualGateError(f"{label} authorization summary disagrees")
        detected = _crop_margins(
            authorization.get("detected_crop_margins"),
            f"{label}.detected_crop_margins",
        )
        persisted = _crop_margins(
            authorization.get("persisted_crop_margins"),
            f"{label}.persisted_crop_margins",
        )
        if persisted != planned_crop_margins:
            raise SelectedVisualGateError(
                f"{label} persisted margins disagree with the planned crop"
            )
        expected_outside = [
            key for key in _CROP_KEYS if detected[key] > persisted[key] + 1e-9
        ]
        if authorization.get("outside_bound_edges") != expected_outside:
            raise SelectedVisualGateError(f"{label} crop-bound evidence disagrees")
        if expected_outside:
            codes.add("overlay_crop_outside_persisted_bound")
        detected_provenance = authorization.get("detected_crop_provenance")
        persisted_provenance = authorization.get("persisted_crop_provenance")
        if persisted_provenance != planned_crop_provenance:
            raise SelectedVisualGateError(
                f"{label} persisted provenance disagrees with the planned crop"
            )
        if authorization.get("action") == "crop":
            expected_provenance_match = bool(
                isinstance(detected_provenance, Mapping)
                and isinstance(persisted_provenance, Mapping)
                and detected_provenance.get("kind") == "detector_profile_track"
                and detected_provenance.get("donor_profile_hash")
                == persisted_provenance.get("donor_profile_hash")
                and detected_provenance.get("detector_policy_hash")
                == persisted_provenance.get("detector_policy_hash")
            )
        else:
            if detected_provenance is not None:
                raise SelectedVisualGateError(
                    f"{label} non-crop observation carries crop provenance"
                )
            expected_provenance_match = True
        provenance_matches = authorization.get("provenance_matches")
        if (
            not isinstance(provenance_matches, bool)
            or provenance_matches != expected_provenance_match
        ):
            raise SelectedVisualGateError(f"{label} provenance evidence is malformed")
        if authorization.get("action") == "crop" and not provenance_matches:
            codes.add("overlay_crop_provenance_mismatch")
        elif authorization.get("action") != "crop" and not provenance_matches:
            raise SelectedVisualGateError(f"{label} has impossible provenance evidence")

    profile_track_summaries = list(overlays) + [
        _evidence_mapping(
            entry["overlay"],
            f"evidence.raw_crop_authorization[{index}].overlay",
        )
        for index, entry in enumerate(raw_authorizations)
    ]
    for summary_index, summary in enumerate(profile_track_summaries):
        for track_index, track in enumerate(summary.get("tracks") or ()):
            if str(track.get("profile_kind") or "") != "profile_track":
                continue
            profile_track_id = _portable_id(
                track.get("profile_track_id"),
                f"evidence.profile_track_summaries[{summary_index}]"
                f".tracks[{track_index}].profile_track_id",
            )
            if profile_track_id not in actionable_tracks:
                raise SelectedVisualGateError(
                    "overlay profile-track evidence is absent from the actionable binding"
                )

    raw_profile_references = [
        {
            "track_id": track["track_id"],
            "decision": track["decision"],
            "bbox_normalized": track["bbox_normalized"],
        }
        for track in actionable_profile_binding["tracks"]
    ]
    planned_profile_references = []
    for track in actionable_profile_binding["tracks"]:
        geometry = projection_geometry_by_id[str(track["track_id"])]
        visible_bbox = geometry.get("visible_bbox_pixels")
        if not isinstance(visible_bbox, list) or len(visible_bbox) != 4:
            continue
        planned_profile_references.append(
            {
                "track_id": track["track_id"],
                "decision": track["decision"],
                "bbox_normalized": [
                    float(visible_bbox[0]) / proxy_width,
                    float(visible_bbox[1]) / proxy_height,
                    float(visible_bbox[2]) / proxy_width,
                    float(visible_bbox[3]) / proxy_height,
                ],
            }
        )
    expected_current_matches: list[dict[str, Any]] = []
    for index, overlay in enumerate(overlays):
        start_frame = overlay_intervals[index][0]
        expected_current_matches.extend(
            _current_profile_spatial_matches(
                _evidence_mapping(overlay, f"evidence.overlay_chunks[{index}]"),
                planned_profile_references,
                chunk_index=index,
                start_frame=start_frame,
                coordinate_space="render_geometry_proxy_480x270",
            )
        )
        if raw_authorizations:
            expected_current_matches.extend(
                _current_profile_spatial_matches(
                    _evidence_mapping(
                        raw_authorizations[index]["overlay"],
                        f"evidence.raw_crop_authorization[{index}].overlay",
                    ),
                    raw_profile_references,
                    chunk_index=index,
                    start_frame=start_frame,
                    coordinate_space="raw_normalized_analysis_view",
                )
            )
    expected_current_matches.sort(
        key=lambda item: (
            item["track_id"],
            item["coordinate_space"],
            item["chunk_index"],
            item["observation_index"],
        )
    )
    if expected_current_matches != current_matches:
        raise SelectedVisualGateError(
            "evidence current profile matches disagree with overlay tracks"
        )

    expected_decoded_bytes = (
        sum(end - start for start, end in overlay_intervals)
        * int(row["analysis_dimensions"]["width"])
        * int(row["analysis_dimensions"]["height"])
        * 3
        * (2 if raw_authorizations else 1)
    )
    if decoded_bytes_total != expected_decoded_bytes:
        raise SelectedVisualGateError("evidence decoded byte count disagrees")

    transitions = _evidence_list(row.get("transitions"), "evidence.transitions")
    transition_count = _evidence_int(
        row.get("transition_count"), "evidence.transition_count"
    )
    if transition_count != len(transitions):
        raise SelectedVisualGateError("evidence transition count disagrees")
    transition_rejections = 0
    for index, raw_transition in enumerate(transitions):
        label = f"evidence.transitions[{index}]"
        transition = _evidence_mapping(raw_transition, label)
        if (
            not _TRANSITION_REQUIRED_FIELDS.issubset(transition)
            or not set(transition).issubset(
                _TRANSITION_REQUIRED_FIELDS | _TRANSITION_OPTIONAL_FIELDS
            )
        ):
            raise SelectedVisualGateError(f"{label} fields are incompatible")
        transition_chunk_index = _evidence_int(
            transition.get("chunk_index"), f"{label}.chunk_index"
        )
        if transition_chunk_index >= chunk_count:
            raise SelectedVisualGateError(f"{label} chunk index escapes evidence")
        transition_start = _evidence_int(
            transition.get("start_frame"), f"{label}.start_frame"
        )
        transition_end = _evidence_int(
            transition.get("end_frame"), f"{label}.end_frame", minimum=1
        )
        if not 0 <= transition_start < transition_end <= selected_source_frames:
            raise SelectedVisualGateError(f"{label} frame range escapes evidence")
        if transition.get("kind") not in {"hard_cut", "fade", "dissolve"}:
            raise SelectedVisualGateError(f"{label} kind is unknown")
        corroboration = _evidence_mapping(
            transition.get("corroboration"), f"{label}.corroboration"
        )
        _evidence_exact_fields(
            corroboration,
            _TRANSITION_CORROBORATION_FIELDS,
            f"{label}.corroboration",
        )
        if corroboration.get("version") != TRANSITION_CORROBORATOR_VERSION:
            raise SelectedVisualGateError(f"{label} corroborator version is incompatible")
        feature_matches = _evidence_int(
            corroboration.get("feature_matches"),
            f"{label}.corroboration.feature_matches",
        )
        geometric_inliers = _evidence_int(
            corroboration.get("geometric_inliers"),
            f"{label}.corroboration.geometric_inliers",
        )
        geometric_inlier_ratio = _evidence_float(
            corroboration.get("geometric_inlier_ratio"),
            f"{label}.corroboration.geometric_inlier_ratio",
        )
        matched_quadrants = _evidence_int(
            corroboration.get("matched_quadrants"),
            f"{label}.corroboration.matched_quadrants",
        )
        histogram_correlation = _evidence_float(
            corroboration.get("histogram_correlation"),
            f"{label}.corroboration.histogram_correlation",
        )
        median_flow_explained = _evidence_float(
            corroboration.get("median_flow_explained"),
            f"{label}.corroboration.median_flow_explained",
        )
        luma_min = _evidence_float(
            corroboration.get("luma_min"), f"{label}.corroboration.luma_min"
        )
        luma_max = _evidence_float(
            corroboration.get("luma_max"), f"{label}.corroboration.luma_max"
        )
        material_change_fraction = _evidence_float(
            corroboration.get("material_endpoint_change_fraction"),
            f"{label}.corroboration.material_endpoint_change_fraction",
        )
        directional_uniformity = _evidence_float(
            corroboration.get("directional_uniformity"),
            f"{label}.corroboration.directional_uniformity",
        )
        directional_quadrants = _evidence_int(
            corroboration.get("directional_quadrants"),
            f"{label}.corroboration.directional_quadrants",
        )
        hard_cross_mae = _evidence_float(
            corroboration.get("hard_cross_mae"),
            f"{label}.corroboration.hard_cross_mae",
        )
        hard_within_mae = _evidence_float(
            corroboration.get("hard_within_mae"),
            f"{label}.corroboration.hard_within_mae",
        )
        hard_cross_histogram = _evidence_float(
            corroboration.get("hard_cross_histogram"),
            f"{label}.corroboration.hard_cross_histogram",
        )
        expected = _derive_transition_corroboration_result(
            str(transition.get("kind")),
            boundary_present=transition.get("boundary_frame") is not None,
            feature_matches=feature_matches,
            geometric_inliers=geometric_inliers,
            geometric_inlier_ratio=geometric_inlier_ratio,
            matched_quadrants=matched_quadrants,
            histogram_correlation=histogram_correlation,
            median_flow_explained=median_flow_explained,
            luma_min=luma_min,
            luma_max=luma_max,
            material_change_fraction=material_change_fraction,
            directional_uniformity=directional_uniformity,
            directional_quadrants=directional_quadrants,
            hard_cross_mae=hard_cross_mae,
            hard_within_mae=hard_within_mae,
            hard_cross_histogram=hard_cross_histogram,
        )
        if (
            corroboration.get("feature_continuity")
            is not expected["feature_continuity"]
            or corroboration.get("appearance_motion_continuity")
            is not expected["appearance_motion_continuity"]
            or corroboration.get("hard_boundary_evidence")
            is not expected["hard_boundary_evidence"]
            or corroboration.get("decision") != expected["decision"]
            or corroboration.get("classification") != expected["classification"]
        ):
            raise SelectedVisualGateError(
                f"{label} corroboration result disagrees with raw metrics"
            )
        if expected["decision"] == "reject":
            transition_rejections += 1
    if row.get("transition_rejection_count") != transition_rejections:
        raise SelectedVisualGateError("evidence transition rejection count disagrees")
    if transition_rejections:
        codes.add("transition_detected")

    if selected_source_frames < max(item.get("end_frame", 0) for item in overlays):
        raise SelectedVisualGateError("evidence overlay frames escape the selected span")
    return sorted(codes)


def _build_receipt(
    binding: Mapping[str, Any],
    *,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    normalized_binding = _json_clone(dict(binding), "binding")
    normalized_evidence = _json_clone(dict(evidence), "evidence")
    normalized_codes = derive_rejection_codes(normalized_evidence)
    status = "rejected" if normalized_codes else "passed"
    base = {
        "schema_version": 1,
        "contract": SELECTED_VISUAL_GATE_RECEIPT_CONTRACT,
        "gate_version": SELECTED_VISUAL_GATE_VERSION,
        "status": status,
        "cache_key": _CACHE_KEY_PREFIX + canonical_hash(normalized_binding),
        "binding": normalized_binding,
        "evidence": normalized_evidence,
        "rejection_codes": normalized_codes,
    }
    receipt = {**base, "receipt_sha256": canonical_hash(base)}
    return validate_selected_visual_gate_receipt(receipt)


def validate_selected_visual_gate_receipt(
    receipt: Mapping[str, Any],
    *,
    expected_cache_key: str | None = None,
) -> dict[str, Any]:
    """Validate and return a canonical path-free visual-gate receipt."""

    if not isinstance(receipt, Mapping):
        raise SelectedVisualGateError("selected visual gate receipt must be an object")
    row = _json_clone(dict(receipt), "receipt")
    required = {
        "schema_version",
        "contract",
        "gate_version",
        "status",
        "cache_key",
        "binding",
        "evidence",
        "rejection_codes",
        "receipt_sha256",
    }
    if set(row) != required:
        raise SelectedVisualGateError("selected visual gate receipt fields are incompatible")
    if (
        row.get("schema_version") != 1
        or row.get("contract") != SELECTED_VISUAL_GATE_RECEIPT_CONTRACT
        or row.get("gate_version") != SELECTED_VISUAL_GATE_VERSION
    ):
        raise SelectedVisualGateError("selected visual gate receipt contract is incompatible")
    status = str(row.get("status") or "")
    if status not in {"passed", "rejected"}:
        raise SelectedVisualGateError("selected visual gate receipt status is invalid")
    binding = row.get("binding")
    evidence = row.get("evidence")
    codes = row.get("rejection_codes")
    if not isinstance(binding, Mapping) or not isinstance(evidence, Mapping):
        raise SelectedVisualGateError("selected visual gate receipt payload is malformed")
    if set(binding) != _BINDING_FIELDS:
        raise SelectedVisualGateError("selected visual gate binding fields are incompatible")
    detectors = _evidence_mapping(
        binding.get("detectors"), "selected visual gate binding.detectors"
    )
    expected_detectors = {
        "overlay_observation_version": DETECTOR_VERSION,
        "overlay_profile_version": PROFILE_DETECTOR_VERSION,
        "transition_version": TRANSITION_DETECTOR_VERSION,
        "transition_corroborator_version": TRANSITION_CORROBORATOR_VERSION,
        "near_black_hold_version": NEAR_BLACK_HOLD_DETECTOR_VERSION,
    }
    _evidence_exact_fields(
        detectors,
        set(expected_detectors),
        "selected visual gate binding.detectors",
    )
    if dict(detectors) != expected_detectors:
        raise SelectedVisualGateError(
            "selected visual gate detector binding is incompatible"
        )
    actionable_profile_binding = _validate_actionable_profile_binding(
        binding.get("actionable_profile_binding")
    )
    if (
        _sha256(
            binding.get("donor_overlay_profile_hash"),
            "binding.donor_overlay_profile_hash",
        )
        != actionable_profile_binding["donor_overlay_profile_hash"]
        or evidence.get("actionable_profile_binding")
        != actionable_profile_binding
    ):
        raise SelectedVisualGateError(
            "selected visual gate actionable profile binding mismatch"
        )
    if not isinstance(codes, Sequence) or isinstance(codes, (str, bytes)):
        raise SelectedVisualGateError("selected visual gate rejection_codes must be an array")
    normalized_codes = sorted(set(str(value) for value in codes if str(value)))
    if list(codes) != normalized_codes:
        raise SelectedVisualGateError("selected visual gate rejection_codes are not canonical")
    derived_codes = derive_rejection_codes(evidence)
    derived_status = "rejected" if derived_codes else "passed"
    if normalized_codes != derived_codes or status != derived_status:
        raise SelectedVisualGateError(
            "selected visual gate status/rejection_codes disagree with evidence"
        )
    content = binding.get("content")
    analysis = binding.get("analysis")
    if not isinstance(content, Mapping) or not isinstance(analysis, Mapping):
        raise SelectedVisualGateError("selected visual gate binding is malformed")
    if _sha256(binding.get("content_hash"), "binding.content_hash") != canonical_hash(
        content
    ):
        raise SelectedVisualGateError("selected visual gate binding content hash mismatch")
    content_range = content.get("range")
    expected_source_duration = (
        _source_span_duration_document(content_range)
        if isinstance(content_range, Mapping)
        else None
    )
    black_hold = evidence.get("near_black_hold")
    if (
        not isinstance(content_range, Mapping)
        or evidence.get("selected_source_frames") != content_range.get("source_frames")
        or evidence.get("render_frames") != content.get("render_frames")
        or evidence.get("analysis_dimensions")
        != {"width": analysis.get("width"), "height": analysis.get("height")}
        or evidence.get("analysis_contract") != dict(analysis)
        or evidence.get("render_geometry_proxy") != binding.get("render_geometry_proxy")
        or not isinstance(black_hold, Mapping)
        or black_hold.get("source_span_duration") != expected_source_duration
    ):
        raise SelectedVisualGateError(
            "selected visual gate evidence disagrees with its binding"
        )
    expected_key = _CACHE_KEY_PREFIX + canonical_hash(binding)
    if row.get("cache_key") != expected_key:
        raise SelectedVisualGateError("selected visual gate cache key mismatch")
    if expected_cache_key is not None and expected_key != expected_cache_key:
        raise SelectedVisualGateError("selected visual gate cached binding mismatch")
    supplied_hash = _sha256(row.get("receipt_sha256"), "receipt.receipt_sha256")
    base = {key: value for key, value in row.items() if key != "receipt_sha256"}
    if supplied_hash != canonical_hash(base):
        raise SelectedVisualGateError("selected visual gate receipt hash mismatch")
    _assert_path_free(row)
    return row


def evaluate_selected_visual_span(
    candidate: Mapping[str, Any],
    donor_overlay_profile: Mapping[str, Any],
    *,
    render_transform: Mapping[str, Any],
    source_path: str | Path,
    output_fps: float,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    timeout: float = 180.0,
    width: int = DEFAULT_ANALYSIS_WIDTH,
    height: int = DEFAULT_ANALYSIS_HEIGHT,
    max_decode_bytes: int = DEFAULT_MAX_DECODE_BYTES,
    chunk_overlap_frames: int = DEFAULT_CHUNK_OVERLAP_FRAMES,
    cached_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate one exact selected span or raise :class:`SelectedVisualGateRejected`.

    A matching cached pass is returned without media access.  A matching cached
    rejection is re-raised as the typed rejection.  A mismatched cached receipt
    fails closed instead of being silently reused or overwritten.
    """

    if not isinstance(candidate, Mapping):
        raise SelectedVisualGateError("candidate must be an object")
    if not isinstance(donor_overlay_profile, Mapping):
        raise SelectedVisualGateError("donor_overlay_profile must be an object")
    width = _positive_int(width, "width")
    height = _positive_int(height, "height")
    max_decode_bytes = _positive_int(max_decode_bytes, "max_decode_bytes")
    chunk_overlap_frames = _non_negative_int(
        chunk_overlap_frames, "chunk_overlap_frames"
    )
    if width % 2 or height % 2:
        raise SelectedVisualGateError("analysis width and height must be even")
    if (width, height) != (DEFAULT_ANALYSIS_WIDTH, DEFAULT_ANALYSIS_HEIGHT):
        raise SelectedVisualGateError(
            "selected visual gate resolution is fixed at 480x270"
        )
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError) as exc:
        raise SelectedVisualGateError("timeout must be positive and finite") from exc
    if not math.isfinite(timeout_value) or timeout_value <= 0:
        raise SelectedVisualGateError("timeout must be positive and finite")
    output_fps_value, output_fps_doc = _fps_document(output_fps)
    render_frames = _positive_int(candidate.get("render_frames"), "candidate.render_frames")
    try:
        validate_exact_candidate_contract(
            candidate,
            output_fps=output_fps_value,
            required_render_frames=render_frames,
        )
        profile = validate_donor_overlay_profile(donor_overlay_profile)
    except (BroadLazyAdapterError, BroadLazyError) as exc:
        raise SelectedVisualGateError(str(exc)) from exc
    selected = _selected_exact_contract(candidate)
    actionable_profile_binding = build_selected_visual_actionable_profile_binding(
        profile
    )
    planned_view = _planned_render_view_contract(
        candidate,
        profile,
        render_transform,
    )
    binding = _binding(
        candidate,
        profile,
        actionable_profile_binding,
        selected,
        planned_view,
        output_fps=output_fps_doc,
        render_frames=render_frames,
        width=width,
        height=height,
        max_decode_bytes=max_decode_bytes,
        chunk_overlap_frames=chunk_overlap_frames,
    )
    cache_key = _CACHE_KEY_PREFIX + canonical_hash(binding)
    if cached_receipt is not None:
        cached = validate_selected_visual_gate_receipt(
            cached_receipt,
            expected_cache_key=cache_key,
        )
        if cached["binding"] != binding:
            raise SelectedVisualGateError("selected visual gate cached binding mismatch")
        if cached["status"] == "rejected":
            raise SelectedVisualGateRejected(cached["rejection_codes"], cached)
        return cached

    dimensions = _probe_source_dimensions(
        source_path,
        ffprobe=ffprobe,
        timeout=timeout_value,
    )
    selected_frame_count = int(selected["frame_count"])
    source_span_duration = _source_span_duration_document(selected)
    chunks = _chunk_ranges(
        selected_frame_count,
        width=width,
        height=height,
        max_decode_bytes=max_decode_bytes,
        overlap_frames=chunk_overlap_frames,
    )
    overlay_chunks: list[dict[str, Any]] = []
    raw_crop_authorization: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    near_black_frame_metrics: list[dict[str, Any] | None] = [
        None
    ] * selected_frame_count
    known_profile_projection, projection_internal_rejections = (
        _project_known_profile_overlays(
            actionable_profile_binding,
            planned_view,
            dimensions,
            width=width,
            height=height,
        )
    )
    if projection_internal_rejections:
        raise SelectedVisualGateError(
            "known profile projection emitted non-canonical rejection evidence"
        )
    current_high_resolution_profile_track_ids: set[str] = set()
    current_profile_matches: list[dict[str, Any]] = []
    raw_profile_references = [
        {
            "track_id": row.get("track_id"),
            "decision": row.get("decision"),
            "bbox_normalized": row.get("bbox_normalized"),
        }
        for row in actionable_profile_binding.get("tracks") or ()
        if isinstance(row, Mapping)
    ]
    planned_profile_references = []
    for projection in known_profile_projection:
        visible_bbox = projection.get("visible_bbox_pixels")
        if not isinstance(visible_bbox, Sequence) or len(visible_bbox) != 4:
            continue
        planned_profile_references.append(
            {
                "track_id": projection.get("track_id"),
                "decision": projection.get("decision"),
                "bbox_normalized": [
                    float(visible_bbox[0]) / width,
                    float(visible_bbox[1]) / height,
                    float(visible_bbox[2]) / width,
                    float(visible_bbox[3]) / height,
                ],
            }
        )
    decoded_bytes = 0
    candidate_id = _portable_id(candidate.get("clip_id"), "candidate.clip_id")
    source = Path(source_path).expanduser().resolve()
    for chunk_index, (start_frame, end_frame) in enumerate(chunks):
        exact_chunk = _exact_subcontract(
            selected,
            start_frame=start_frame,
            end_frame=end_frame,
        )
        probe = {
            "source_dimensions": dimensions,
            "exact_pts": exact_chunk,
        }
        if planned_view["crop_action"] == "crop":
            try:
                raw_decoded = decode_exact_analysis_window(
                    source,
                    probe,
                    width=width,
                    height=height,
                    ffmpeg=ffmpeg,
                    timeout=timeout_value,
                    max_decode_bytes=max_decode_bytes,
                )
                raw_overlay = analyze_candidate_overlay_with_profile(
                    raw_decoded["frames"],
                    profile,
                    candidate_id=candidate_id,
                    fps=output_fps_value,
                )
                raw_summary, _ignored_raw_rejections = _overlay_evidence(
                    raw_overlay,
                    chunk_index=chunk_index,
                    start_frame=start_frame,
                    end_frame=end_frame,
                )
                authorization, _ignored_authorization_rejections = _raw_crop_authorization(
                    raw_overlay,
                    planned_view,
                )
                raw_matches = _current_profile_spatial_matches(
                    raw_summary,
                    raw_profile_references,
                    chunk_index=chunk_index,
                    start_frame=start_frame,
                    coordinate_space="raw_normalized_analysis_view",
                )
            except (StrictCandidateError, OverlayDetectorError) as exc:
                raise SelectedVisualGateError(str(exc)) from exc
            decoded_bytes += int(raw_decoded.get("decoded_bytes") or 0)
            raw_crop_authorization.append(
                {
                    "chunk_index": chunk_index,
                    "overlay": raw_summary,
                    "authorization": authorization,
                }
            )
            current_profile_matches.extend(raw_matches)
            current_high_resolution_profile_track_ids.update(
                str(item["track_id"]) for item in raw_matches
            )
        decoded = _decode_planned_render_view(
            source,
            exact_chunk,
            planned_view,
            width=width,
            height=height,
            ffmpeg=ffmpeg,
            timeout=timeout_value,
            max_decode_bytes=max_decode_bytes,
        )
        frames = decoded["frames"]
        if len(frames) != end_frame - start_frame:
            raise SelectedVisualGateError("selected visual gate decoded the wrong frame count")
        decoded_bytes += int(decoded.get("decoded_bytes") or 0)
        for metric in _near_black_frame_metrics(
            frames,
            start_frame=start_frame,
        ):
            frame_index = int(metric["frame_index"])
            existing_metric = near_black_frame_metrics[frame_index]
            if existing_metric is not None and existing_metric != metric:
                raise SelectedVisualGateError(
                    "overlapping near-black frame evidence disagrees"
                )
            near_black_frame_metrics[frame_index] = metric
        try:
            overlay = analyze_candidate_overlay_with_profile(
                frames,
                profile,
                candidate_id=candidate_id,
                fps=output_fps_value,
            )
            overlay_evidence, _ignored_overlay_rejections = _overlay_evidence(
                overlay,
                chunk_index=chunk_index,
                start_frame=start_frame,
                end_frame=end_frame,
            )
            transition_evidence = _transition_evidence(
                frames,
                chunk_index=chunk_index,
                start_frame=start_frame,
            )
        except (OverlayDetectorError, DonorError) as exc:
            raise SelectedVisualGateError(str(exc)) from exc
        overlay_chunks.append(overlay_evidence)
        planned_matches = _current_profile_spatial_matches(
            overlay_evidence,
            planned_profile_references,
            chunk_index=chunk_index,
            start_frame=start_frame,
            coordinate_space="render_geometry_proxy_480x270",
        )
        current_profile_matches.extend(planned_matches)
        current_high_resolution_profile_track_ids.update(
            str(item["track_id"]) for item in planned_matches
        )
        transitions.extend(transition_evidence)

    if any(metric is None for metric in near_black_frame_metrics):
        raise SelectedVisualGateError("near-black frame evidence is incomplete")
    canonical_near_black_metrics = [
        dict(metric)
        for metric in near_black_frame_metrics
        if metric is not None
    ]

    for projection in known_profile_projection:
        track_id = str(projection.get("track_id") or "")
        current_match = track_id in current_high_resolution_profile_track_ids
        projection["current_high_resolution_profile_match"] = current_match
        if current_match:
            projection["presence_basis"] = (
                "current_high_resolution_spatial_profile_match"
            )
            projection["presence_proven"] = True
        projection["materially_visible"] = bool(
            projection.get("presence_proven") is True
            and projection.get("geometry_materially_visible") is True
        )
    transitions.sort(
        key=lambda item: (
            item["start_frame"],
            item["end_frame"],
            item["kind"],
            item["chunk_index"],
        )
    )
    current_profile_matches.sort(
        key=lambda item: (
            item["track_id"],
            item["coordinate_space"],
            item["chunk_index"],
            item["observation_index"],
        )
    )
    evidence = {
        "selected_source_frames": selected_frame_count,
        "render_frames": render_frames,
        "chunk_count": len(chunks),
        "decoded_bytes_total": decoded_bytes,
        "analysis_dimensions": {"width": width, "height": height},
        "analysis_contract": {
            "width": width,
            "height": height,
            "pixel_format": "bgr24",
            "max_decode_bytes": max_decode_bytes,
            "chunk_overlap_frames": chunk_overlap_frames,
        },
        "source_dimensions": dimensions,
        "render_geometry_proxy": _json_clone(
            dict(planned_view), "planned_render_view evidence"
        ),
        "actionable_profile_binding": _json_clone(
            dict(actionable_profile_binding), "actionable_profile_binding evidence"
        ),
        "near_black_hold": _build_near_black_hold_evidence(
            canonical_near_black_metrics,
            source_span_duration=source_span_duration,
        ),
        "known_profile_projection": known_profile_projection,
        "current_profile_matches": current_profile_matches,
        "raw_crop_authorization": raw_crop_authorization,
        "overlay_chunks": overlay_chunks,
        "transition_count": len(transitions),
        "transition_rejection_count": sum(
            item.get("corroboration", {}).get("decision") == "reject"
            for item in transitions
        ),
        "transitions": transitions,
    }
    receipt = _build_receipt(
        binding,
        evidence=evidence,
    )
    if receipt["status"] == "rejected":
        raise SelectedVisualGateRejected(receipt["rejection_codes"], receipt)
    return receipt


__all__ = [
    "DEFAULT_ANALYSIS_HEIGHT",
    "DEFAULT_ANALYSIS_WIDTH",
    "DEFAULT_CHUNK_OVERLAP_FRAMES",
    "NEAR_BLACK_HOLD_DETECTOR_VERSION",
    "RENDER_GEOMETRY_PROXY_CONTRACT",
    "SELECTED_VISUAL_ACTIONABLE_PROFILE_BINDING_CONTRACT",
    "SELECTED_VISUAL_GATE_RECEIPT_CONTRACT",
    "SELECTED_VISUAL_GATE_VERSION",
    "SelectedVisualGateError",
    "SelectedVisualGateRejected",
    "build_selected_visual_actionable_profile_binding",
    "derive_rejection_codes",
    "evaluate_selected_visual_span",
    "validate_selected_visual_gate_receipt",
]
