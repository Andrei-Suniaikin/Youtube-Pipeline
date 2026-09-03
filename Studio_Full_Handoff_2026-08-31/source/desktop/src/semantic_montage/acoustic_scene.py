"""Visual-to-acoustic scene contracts for production sound planning.

The model result is not trusted as a sound request directly.  This module
normalises visual observations, downgrades uncertain species labels, records
hard negatives, and deterministically expands every timeline slot into the
continuous ambience/presence/event lanes used by sound-plan schema v2.
"""

from __future__ import annotations

import copy
import math
import re
from typing import Any, Mapping, Sequence

from .sound import (
    DEFAULT_MIN_EXACT_SPECIES_CONFIDENCE,
    SoundPlanError,
    _timeline_slots,
    make_ambience_region,
    make_event_cue,
    make_presence_layer,
    stable_cache_key,
)


ACOUSTIC_SCENE_SCHEMA_VERSION = 1
CAUSAL_RECONSTRUCTION_POLICY_VERSION = "causal-reconstruction-v2"
CAUSAL_RECONSTRUCTION_RECEIPT_VERSION = 2
PHYSICAL_FOLEY_OBLIGATION_CONTRACT = "physical-foley-obligation-v1"
REQUIRED_CUE_AUDIBILITY_CONTRACT = "required-physical-foley-audibility-v2"
DISTANCES = frozenset({"extreme_close", "close", "medium", "distant", "very_distant"})
POSITIONS = frozenset({"far_left", "left", "center", "right", "far_right"})
BODY_SCALES = frozenset({"small", "medium", "large", "massive", "unknown"})
SURFACE_CONTACTS = frozenset(
    {
        "bare_ground",
        "dry_grass",
        "mud",
        "none",
        "rock",
        "sand",
        "unknown",
        "vegetation",
        "water",
        "wet_grass",
    }
)
DISTANCE_ALIASES = {"n/a": "medium"}
POSITION_ALIASES = {
    "centre": "center",
    "center_left": "left",
    "centre_left": "left",
    "left_center": "left",
    "left/center": "left",
    "center_right": "right",
    "centre_right": "right",
    "right_center": "right",
    "far_back/center": "center",
    "scene_wide": "center",
    "center_left_to_center": "center",
    "center_to_right": "center",
    "left_to_right_movement_across_center": "center",
    "center_moving_to_right": "center",
    "center_>right": "center",
}
BODY_SCALE_ALIASES = {
    "extra_large": "massive",
    "very_large": "massive",
}
SURFACE_CONTACT_ALIASES = {
    "dirt": "bare_ground",
    "dry_ground": "bare_ground",
    "grass_dry": "dry_grass",
    "grass_wet": "wet_grass",
    "no_contact": "none",
    "soil": "bare_ground",
}

# This is a semantic compatibility profile, not an asset id.  It mirrors the
# minimum metadata contract a licensed candidate must satisfy downstream.  V1
# deliberately accepts only medium-scale dry-grass contact; large, massive,
# and unknown bodies remain blocked until the catalog can prove scale.
_DRY_GRASS_WALKING_PROFILE = {
    "profile": "species-neutral-dry-grass-walking-v1",
    "source_kind": "foley",
    "action": "walking",
    "surface_contact": "dry_grass",
    "compatible_body_scales": ["medium"],
    "required_asset_taxonomy": [
        "foley.footsteps",
        "surface.grass",
        "action.walking",
    ],
    "required_asset_hard_negatives": [
        "horse gallop",
        "water splash",
        "music",
        "speech",
    ],
}

_VISIBLE_WATER_MOVEMENT_PROFILES = {
    "water_wading_splash": {
        "profile": "species-neutral-water-wading-splash-v1",
        "source_kind": "foley",
        "action": "water_wading_splash",
        "surface_contact": "water",
        "required_asset_taxonomy": [
            "foley.water_movement",
            "surface.water",
            "action.water_wading_splash",
        ],
        "required_asset_hard_negatives": [
            "animal vocalization",
            "boat wake",
            "human splash",
            "music",
            "speech",
        ],
    },
    "water_light_pour_dribble": {
        "profile": "species-neutral-water-light-pour-dribble-v1",
        "source_kind": "environment",
        "action": "water_light_pour_dribble",
        "surface_contact": "water",
        "required_asset_taxonomy": [
            "environment.water_movement",
            "surface.water",
            "action.water_light_pour_dribble",
        ],
        "required_asset_hard_negatives": [
            "animal vocalization",
            "heavy splash",
            "wading",
            "music",
            "speech",
        ],
    },
    "water_push_splash": {
        "profile": "species-neutral-water-push-splash-v1",
        "source_kind": "environment",
        "action": "water_push_splash",
        "surface_contact": "water",
        "required_asset_taxonomy": [
            "environment.water_movement",
            "surface.water",
            "action.water_push_splash",
        ],
        "required_asset_hard_negatives": [
            "animal vocalization",
            "boat wake",
            "music",
            "speech",
        ],
    },
}

_WATER_MOVEMENT_ORIGIN_KINDS = frozenset(
    {"animal", "animal_group", "water_splash"}
)
_WATER_MOVEMENT_ACTION_WORDS = frozenset(
    {
        "break",
        "breaking",
        "contact",
        "dribble",
        "dribbling",
        "impact",
        "pour",
        "pouring",
        "scoop",
        "scooping",
        "splash",
        "splashing",
        "spray",
        "spraying",
        "step",
        "stepping",
        "swim",
        "swimming",
        "takeoff",
        "wade",
        "wading",
    }
)
_WATER_MOVEMENT_EFFECT_WORDS = frozenset(
    {
        "contact",
        "displacement",
        "disturbance",
        "disturbed",
        "droplet",
        "droplets",
        "emerges",
        "impact",
        "lift",
        "lifting",
        "pour",
        "pouring",
        "push",
        "pushing",
        "ripple",
        "ripples",
        "scoop",
        "scooping",
        "splash",
        "splashes",
        "splashing",
        "spray",
        "step",
        "stepping",
        "stream",
        "submerged",
        "wake",
        "wakes",
    }
)
_VISIBLE_ANIMAL_CAUSE_WORDS = frozenset(
    {
        "animal",
        "bird",
        "body",
        "feet",
        "foot",
        "hoof",
        "hooves",
        "leg",
        "legs",
        "paw",
        "paws",
        "trunk",
    }
)

_WALKING_ACTIONS = frozenset({"patrol", "patrolling", "walk", "walking"})
_VOCAL_ACTIONS = frozenset(
    {
        "bark",
        "barking",
        "call",
        "calling",
        "chirp",
        "chirping",
        "growl",
        "growling",
        "roar",
        "roaring",
        "trumpet",
        "trumpeting",
    }
)
_VISIBLE_CALL_CUES = frozenset(
    {
        "beak",
        "call",
        "calling",
        "jaw",
        "mouth",
        "roar",
        "roaring",
        "throat",
        "trumpet",
        "trumpeting",
        "vocalising",
        "vocalizing",
    }
)
_CONTINUOUS_ENVIRONMENT_EMITTERS = {
    "cascade": "waterfall",
    "cascading": "waterfall",
    "rain": "rain",
    "raining": "rain",
    "waterfall": "waterfall",
}
_LOCAL_CONTACT_ACTIONS = frozenset(
    {
        "branch_snap",
        "contact",
        "footstep",
        "impact",
        "landing",
        "rustle",
        "snap",
        "splash",
        "step",
    }
)
_VISIBLE_CONTACT_CUES = frozenset(
    {
        "bends",
        "breaks",
        "contact",
        "hits",
        "lands",
        "landing",
        "paw",
        "snaps",
        "splash",
        "splashes",
        "step",
        "touches",
    }
)


class AcousticSceneError(SoundPlanError):
    """Visual observations cannot safely drive a natural-sound plan."""


def _nonempty(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise AcousticSceneError(f"{name} must be non-empty")
    return text


def _unit(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise AcousticSceneError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AcousticSceneError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise AcousticSceneError(f"{name} must be in [0, 1]")
    return number


def _strings(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise AcousticSceneError(f"{name} must be a list")
    output: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        text = _nonempty(item, f"{name}[{index}]")
        if text.casefold() not in seen:
            output.append(text)
            seen.add(text.casefold())
    return output


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise AcousticSceneError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AcousticSceneError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise AcousticSceneError(f"{name} must be finite")
    return number


def _canonical_enum(
    value: Any,
    *,
    default: str,
    allowed: frozenset[str],
    aliases: Mapping[str, str] | None,
    label: str,
) -> str:
    """Canonicalise only formatting and explicitly audited semantic aliases."""

    text = str(value or default).strip().casefold()
    token = re.sub(r"[-\s]+", "_", text)
    canonical = (aliases or {}).get(token, token)
    if canonical not in allowed:
        raise AcousticSceneError(f"unsupported source {label}: {text}")
    return canonical


def _canonical_surface_contact(
    value: Any,
    *,
    provider_flag: bool | None,
    provider_flag_supplied: bool,
    provider_flag_name: str,
) -> tuple[str, dict[str, Any] | None]:
    """Fail closed except for an explicitly silent provider observation."""

    try:
        return (
            _canonical_enum(
                value,
                default="unknown",
                allowed=SURFACE_CONTACTS,
                aliases=SURFACE_CONTACT_ALIASES,
                label="surface_contact",
            ),
            None,
        )
    except AcousticSceneError:
        if provider_flag_supplied is not True or provider_flag is not False:
            raise
        raw_value = str(value or "unknown").strip()
        return (
            "unknown",
            {
                "raw_value": raw_value,
                "canonical_value": "unknown",
                "reason": "unsupported_surface_contact_provider_explicitly_silent",
                "provider_flag": provider_flag_name,
            },
        )


def _provider_flag(
    raw: Mapping[str, Any], key: str, *, default: bool, label: str
) -> tuple[bool, bool | None, bool]:
    """Keep the provider decision separate from the effective compiler flag."""

    supplied = key in raw
    provider_value = raw.get(key) if supplied else None
    if supplied and not isinstance(provider_value, bool):
        raise AcousticSceneError(f"{label} must be boolean when supplied")
    effective = bool(provider_value) if supplied else default
    return effective, provider_value, supplied


def _exact_source_bounds(clip: Mapping[str, Any]) -> tuple[float, float]:
    start = _finite(
        clip.get(
            "render_source_in",
            clip.get("safe_source_in", clip.get("safe_source_start", clip.get("source_in"))),
        ),
        "selected_clip.source_in",
    )
    end = _finite(
        clip.get(
            "render_source_out",
            clip.get("safe_source_out", clip.get("safe_source_end", clip.get("source_out"))),
        ),
        "selected_clip.source_out",
    )
    if end <= start:
        raise AcousticSceneError("selected clip has invalid exact video evidence range")
    return start, end


def acoustic_evidence_contract(
    timeline: Mapping[str, Any], slot: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the mount-independent selected-video identity and frame/time range."""

    clip = slot.get("selected_clip")
    if not isinstance(clip, Mapping):
        raise AcousticSceneError("acoustic evidence slot has no selected_clip")
    source_identity = _nonempty(
        clip.get("source_asset_id") or clip.get("source_id"),
        "selected clip portable source identity",
    )
    source_in, source_out = _exact_source_bounds(clip)
    start_frame = clip.get("source_frame_start", clip.get("source_start_frame"))
    end_frame = clip.get("source_frame_end", clip.get("source_end_frame"))
    render_frames = clip.get("render_frames", slot.get("render_frames"))
    fps_value = clip.get("fps", timeline.get("fps"))
    time_base = clip.get("time_base")
    return {
        "source_asset_id": source_identity,
        "source_frame_start": int(start_frame) if start_frame is not None else None,
        "source_frame_end": int(end_frame) if end_frame is not None else None,
        "source_in": round(source_in, 9),
        "source_out": round(source_out, 9),
        "fps": round(_finite(fps_value, "selected_clip.fps"), 9)
        if fps_value is not None
        else None,
        "time_base": {
            "numerator": int(time_base.get("numerator")),
            "denominator": int(time_base.get("denominator")),
        }
        if isinstance(time_base, Mapping)
        and time_base.get("numerator") is not None
        and time_base.get("denominator") is not None
        else None,
        "render_frames": int(render_frames) if render_frames is not None else None,
    }


def acoustic_evidence_cache_key(
    timeline: Mapping[str, Any],
    slot: Mapping[str, Any],
    *,
    samples: int = 8,
    motion_fps: int = 6,
) -> str:
    """Fingerprint visual evidence without any host path or runtime mount."""

    return stable_cache_key(
        "acoustic_video_evidence",
        {
            "selected_clip": acoustic_evidence_contract(timeline, slot),
            "samples": int(samples),
            "motion_fps": int(motion_fps),
        },
        version=2,
    ).rsplit(":", 1)[-1][:16]


def _canonical_scene_material(scene: Mapping[str, Any]) -> dict[str, Any]:
    """Strip transient evidence locations before computing a durable scene id."""

    material = copy.deepcopy(dict(scene))
    material.pop("scene_id", None)
    evidence = material.get("evidence_artifact")
    if isinstance(evidence, dict):
        evidence.pop("source_path", None)
        evidence.pop("path", None)
    return material


def _normalise_environment(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise AcousticSceneError("environment must be a mapping")
    result = copy.deepcopy(dict(raw))
    result["location_type"] = _nonempty(
        result.get("location_type"), "environment.location_type"
    ).casefold()
    defaults = {
        "weather": "unknown",
        "time_of_day": "unknown",
        "water": "none_visible",
        "wind": "unknown",
        "perspective": "medium",
        "enclosure": "outdoor",
    }
    for key, default in defaults.items():
        result[key] = str(result.get(key) or default).strip().casefold()
    result["confidence"] = _unit(
        result.get("confidence", 0.0), "environment.confidence"
    )
    return result


def _normalise_interval(raw: Mapping[str, Any], name: str) -> dict[str, float]:
    start = _unit(raw.get("start_fraction", 0.0), f"{name}.start_fraction")
    end = _unit(raw.get("end_fraction", 1.0), f"{name}.end_fraction")
    if end <= start:
        raise AcousticSceneError(f"{name} interval must have positive duration")
    return {"start_fraction": start, "end_fraction": end}


def _normalise_source(raw: Mapping[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise AcousticSceneError(f"visible_sources[{index}] must be a mapping")
    confidence = _unit(raw.get("confidence"), f"visible_sources[{index}].confidence")
    source_kind = _nonempty(
        raw.get("source_kind"), f"visible_sources[{index}].source_kind"
    ).casefold()
    family = str(raw.get("family") or "").strip().casefold() or None
    species = str(raw.get("species") or "").strip().casefold() or None
    action = str(raw.get("action") or "").strip().casefold() or None
    requested_species = species
    if species and confidence >= DEFAULT_MIN_EXACT_SPECIES_CONFIDENCE:
        specificity = "exact_species"
    elif family:
        species = None
        specificity = "family"
    elif action:
        species = None
        specificity = "neutral_action"
    else:
        species = None
        specificity = "environment"
    distance = _canonical_enum(
        raw.get("distance"),
        default="medium",
        allowed=DISTANCES,
        aliases=DISTANCE_ALIASES,
        label="distance",
    )
    position = _canonical_enum(
        raw.get("horizontal_position"),
        default="center",
        allowed=POSITIONS,
        aliases=POSITION_ALIASES,
        label="horizontal_position",
    )
    body_scale = _canonical_enum(
        raw.get("body_scale"),
        default="unknown",
        allowed=BODY_SCALES,
        aliases=BODY_SCALE_ALIASES,
        label="body_scale",
    )
    audible_presence, provider_audible_presence, provider_flag_supplied = (
        _provider_flag(
            raw,
            "audible_presence",
            default=True,
            label=f"visible_sources[{index}].audible_presence",
        )
    )
    surface_contact, surface_contact_normalization = _canonical_surface_contact(
        raw.get("surface_contact"),
        provider_flag=provider_audible_presence,
        provider_flag_supplied=provider_flag_supplied,
        provider_flag_name="audible_presence",
    )
    result = {
        "source_id": _nonempty(
            raw.get("source_id") or f"source-{index + 1}",
            f"visible_sources[{index}].source_id",
        ),
        "source_kind": source_kind,
        "family": family,
        "species": species,
        "requested_species": requested_species,
        "action": action,
        "specificity": specificity,
        "confidence": confidence,
        "distance": distance,
        "horizontal_position": position,
        "audible_presence": audible_presence,
        "provider_audible_presence": provider_audible_presence,
        "provider_audible_presence_supplied": provider_flag_supplied,
        "body_scale": body_scale,
        "body_scale_evidence": str(raw.get("body_scale_evidence") or "").strip()
        or None,
        "surface_contact": surface_contact,
        "surface_contact_evidence": str(
            raw.get("surface_contact_evidence") or ""
        ).strip()
        or None,
        "visual_evidence": _nonempty(
            raw.get("visual_evidence"),
            f"visible_sources[{index}].visual_evidence",
        ),
        "hard_negatives": _strings(
            raw.get("hard_negatives", []),
            f"visible_sources[{index}].hard_negatives",
        ),
        **_normalise_interval(raw, f"visible_sources[{index}]"),
    }
    if requested_species and species is None:
        result["degradation"] = {
            "from": "exact_species",
            "to": specificity,
            "reason": "visual confidence below exact-species threshold",
        }
    if surface_contact_normalization is not None:
        result["surface_contact_normalization"] = surface_contact_normalization
    return result


def _normalise_event(raw: Mapping[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise AcousticSceneError(f"events[{index}] must be a mapping")
    confidence = _unit(raw.get("confidence"), f"events[{index}].confidence")
    offset = _unit(raw.get("offset_fraction"), f"events[{index}].offset_fraction")
    duration_fraction = _unit(
        raw.get("duration_fraction", 0.1), f"events[{index}].duration_fraction"
    )
    if duration_fraction <= 0 or offset + duration_fraction > 1.000001:
        raise AcousticSceneError(f"events[{index}] interval is outside slot")
    audible_event, provider_audible_event, provider_flag_supplied = _provider_flag(
        raw,
        "audible_event",
        default=True,
        label=f"events[{index}].audible_event",
    )
    surface_contact, surface_contact_normalization = _canonical_surface_contact(
        raw.get("surface_contact"),
        provider_flag=provider_audible_event,
        provider_flag_supplied=provider_flag_supplied,
        provider_flag_name="audible_event",
    )
    source = _normalise_source(
        {
            **raw,
            "source_id": raw.get("source_id") or f"event-{index + 1}",
            "surface_contact": surface_contact,
            "start_fraction": offset,
            "end_fraction": min(1.0, offset + duration_fraction),
            "audible_presence": False,
        },
        index,
    )
    source.update(
        {
            "event_id": _nonempty(
                raw.get("event_id") or f"event-{index + 1}",
                f"events[{index}].event_id",
            ),
            "offset_fraction": offset,
            "duration_fraction": duration_fraction,
            "audible_event": audible_event,
            "provider_audible_event": provider_audible_event,
            "provider_audible_event_supplied": provider_flag_supplied,
        }
    )
    # ``audible_presence=False`` above is an internal event-shape adapter, not
    # a provider observation.
    source["provider_audible_presence"] = None
    source["provider_audible_presence_supplied"] = False
    if surface_contact_normalization is not None:
        source["surface_contact_normalization"] = surface_contact_normalization
    return source


def build_acoustic_scene(
    timeline: Mapping[str, Any],
    *,
    slot_id: str,
    environment: Mapping[str, Any],
    visible_sources: Sequence[Mapping[str, Any]] = (),
    events: Sequence[Mapping[str, Any]] = (),
    hard_negatives: Sequence[str] = (),
    summary: str,
    planner_model: str,
    planner_version: str,
    evidence_artifact: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create one audited acoustic scene for an exact selected video slot."""

    _slots, by_id, _duration, _tolerance = _timeline_slots(timeline)
    slot_id = _nonempty(slot_id, "slot_id")
    if slot_id not in by_id:
        raise AcousticSceneError(f"unknown slot_id: {slot_id}")
    slot = by_id[slot_id]
    clip = slot.get("selected_clip")
    if not isinstance(clip, Mapping):
        raise AcousticSceneError(f"slot {slot_id} has no exact selected_clip")
    source_contract = acoustic_evidence_contract(timeline, slot)
    source_identity = str(source_contract["source_asset_id"])
    artefact = copy.deepcopy(dict(evidence_artifact or {}))
    artefact["sampled_frames"] = int(artefact.get("sampled_frames", 8))
    artefact["motion_preview"] = bool(artefact.get("motion_preview", True))
    if artefact["sampled_frames"] < 8 or not artefact["motion_preview"]:
        raise AcousticSceneError(
            "acoustic scene evidence requires 8 frames and a motion preview"
        )
    exact_start = float(source_contract["source_in"])
    exact_end = float(source_contract["source_out"])
    supplied_identity = str(
        artefact.get("source_asset_id") or artefact.get("source_id") or source_identity
    ).strip()
    if supplied_identity != source_identity:
        raise AcousticSceneError("acoustic evidence references another source asset")
    supplied_contract = artefact.get("frame_time_contract")
    if supplied_contract is not None and supplied_contract != source_contract:
        raise AcousticSceneError("acoustic evidence frame/time contract is stale")
    artefact["source_asset_id"] = source_identity
    artefact["frame_time_contract"] = copy.deepcopy(source_contract)
    artefact.pop("source_id", None)
    artefact.pop("source_path", None)
    artefact.pop("path", None)
    artefact["source_in"] = float(
        exact_start if artefact.get("source_in") is None else artefact["source_in"]
    )
    artefact["source_out"] = float(
        exact_end if artefact.get("source_out") is None else artefact["source_out"]
    )
    if abs(artefact["source_in"] - exact_start) > 1e-6 or abs(artefact["source_out"] - exact_end) > 1e-6:
        raise AcousticSceneError("acoustic evidence range is stale")
    expected_evidence_key = acoustic_evidence_cache_key(timeline, slot)
    supplied_key = str(artefact.get("evidence_key") or expected_evidence_key)
    if supplied_key != expected_evidence_key:
        raise AcousticSceneError("acoustic evidence key is stale")
    artefact["evidence_key"] = expected_evidence_key
    for digest_key in ("contact_sheet_sha256", "motion_preview_sha256"):
        digest = str(artefact.get(digest_key) or "").strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AcousticSceneError(f"acoustic evidence requires valid {digest_key}")
        artefact[digest_key] = digest
    normalised_sources = [
        _normalise_source(raw, index) for index, raw in enumerate(visible_sources)
    ]
    normalised_events = [
        _normalise_event(raw, index) for index, raw in enumerate(events)
    ]
    scene: dict[str, Any] = {
        "schema_version": ACOUSTIC_SCENE_SCHEMA_VERSION,
        "slot_id": slot_id,
        "start": round(float(slot["start"]), 6),
        "end": round(float(slot["end"]), 6),
        "selected_source_id": source_identity,
        "summary": _nonempty(summary, "summary"),
        "environment": _normalise_environment(environment),
        "visible_sources": normalised_sources,
        "events": normalised_events,
        "hard_negatives": _strings(hard_negatives, "hard_negatives"),
        "evidence_artifact": artefact,
        "planner": {
            "model": _nonempty(planner_model, "planner_model"),
            "version": _nonempty(planner_version, "planner_version"),
        },
    }
    scene["scene_id"] = "acoustic-" + stable_cache_key(
        "acoustic_scene",
        _canonical_scene_material(scene),
        version=ACOUSTIC_SCENE_SCHEMA_VERSION,
    ).rsplit(":", 1)[-1][:12]
    return scene


def validate_acoustic_scenes(
    scenes: Sequence[Mapping[str, Any]],
    timeline: Mapping[str, Any],
    *,
    require_compiled: bool = False,
    _skip_compiler_replay: bool = False,
) -> None:
    """Require exactly one current visual analysis for every final slot."""

    slots, by_id, _duration, tolerance = _timeline_slots(timeline)
    if not isinstance(scenes, Sequence) or isinstance(scenes, (str, bytes)):
        raise AcousticSceneError("acoustic scenes must be a list")
    seen: set[str] = set()
    for index, scene in enumerate(scenes):
        if not isinstance(scene, Mapping):
            raise AcousticSceneError(f"acoustic scene {index} must be a mapping")
        if scene.get("schema_version") != ACOUSTIC_SCENE_SCHEMA_VERSION:
            raise AcousticSceneError(f"acoustic scene {index} has unsupported schema")
        slot_id = _nonempty(scene.get("slot_id"), f"acoustic scene {index}.slot_id")
        if slot_id not in by_id or slot_id in seen:
            raise AcousticSceneError(f"unknown or duplicate acoustic slot: {slot_id}")
        seen.add(slot_id)
        slot = by_id[slot_id]
        if abs(float(scene.get("start")) - float(slot["start"])) > tolerance:
            raise AcousticSceneError(f"acoustic scene {slot_id} start is stale")
        if abs(float(scene.get("end")) - float(slot["end"])) > tolerance:
            raise AcousticSceneError(f"acoustic scene {slot_id} end is stale")
        source_contract = acoustic_evidence_contract(timeline, slot)
        expected_source = str(source_contract["source_asset_id"])
        if str(scene.get("selected_source_id")) != expected_source:
            raise AcousticSceneError(f"acoustic scene {slot_id} references stale video")
        evidence = scene.get("evidence_artifact")
        if not isinstance(evidence, Mapping) or int(evidence.get("sampled_frames", 0)) < 8:
            raise AcousticSceneError(f"acoustic scene {slot_id} has insufficient frames")
        if evidence.get("motion_preview") is not True:
            raise AcousticSceneError(f"acoustic scene {slot_id} has no motion preview")
        exact_start = float(source_contract["source_in"])
        exact_end = float(source_contract["source_out"])
        if (
            str(evidence.get("source_asset_id") or "") != expected_source
            or evidence.get("frame_time_contract") != source_contract
            or abs(float(evidence.get("source_in")) - exact_start) > tolerance
            or abs(float(evidence.get("source_out")) - exact_end) > tolerance
        ):
            raise AcousticSceneError(f"acoustic scene {slot_id} evidence range is stale")
        expected_evidence_key = acoustic_evidence_cache_key(timeline, slot)
        if str(evidence.get("evidence_key") or "") != expected_evidence_key:
            raise AcousticSceneError(f"acoustic scene {slot_id} evidence key is stale")
        supplied_scene_id = scene.get("scene_id")
        expected_scene_id = "acoustic-" + stable_cache_key(
            "acoustic_scene",
            _canonical_scene_material(scene),
            version=ACOUSTIC_SCENE_SCHEMA_VERSION,
        ).rsplit(":", 1)[-1][:12]
        if supplied_scene_id != expected_scene_id:
            raise AcousticSceneError(f"acoustic scene {slot_id} fingerprint is stale")
    expected = {str(slot["slot_id"]) for slot in slots}
    if seen != expected:
        missing = sorted(expected - seen)
        raise AcousticSceneError(f"missing acoustic scenes for slots: {missing}")
    markers = [scene.get("causal_compiler_receipt") for scene in scenes]
    if any(markers):
        if not all(
            isinstance(marker, Mapping)
            and marker.get("policy_version")
            == CAUSAL_RECONSTRUCTION_POLICY_VERSION
            for marker in markers
        ):
            raise AcousticSceneError("acoustic scenes have mixed causal compiler state")
        if not _skip_compiler_replay:
            _validate_physical_foley_compilation(scenes, timeline)
    elif require_compiled:
        raise AcousticSceneError("acoustic scenes lack current causal compiler proof")
    elif any(
        isinstance(source, Mapping)
        and (
            source.get("causal_origin_source_id")
            or source.get("physical_foley_obligation")
        )
        for scene in scenes
        for source in scene.get("visible_sources", [])
    ):
        raise AcousticSceneError("acoustic scenes have incomplete causal compiler state")


def _tokens(value: Any) -> set[str]:
    return set(re.findall(r"[a-zа-яё0-9]+", str(value or "").casefold()))


def _canonical_action(value: Any) -> str:
    return re.sub(r"[-\s]+", "_", str(value or "").strip().casefold())


def _is_compound_action(value: Any) -> bool:
    text = str(value or "").strip().casefold()
    if not re.search(r"\s+(?:and|while|with)\s+|[/+,;]", text):
        return False
    parts = {
        _canonical_action(part)
        for part in re.split(r"\s+(?:and|while|with)\s+|[/+,;]", text)
        if str(part).strip()
    }
    return not parts or not parts.issubset(_WALKING_ACTIONS)


def _continuous_environment_emitter(item: Mapping[str, Any]) -> str | None:
    if str(item.get("source_kind") or "").casefold() not in {
        "environment",
        "vegetation",
        "water",
        "weather",
    }:
        return None
    words = _tokens(
        " ".join(
            [
                str(item.get("action") or ""),
                str(item.get("visual_evidence") or ""),
            ]
        )
    )
    for marker, emitter in _CONTINUOUS_ENVIRONMENT_EMITTERS.items():
        if marker in words:
            return emitter
    return None


def _physical_foley_item_projection(item: Mapping[str, Any], *, lane: str) -> dict[str, Any]:
    identity_key = "event_id" if lane == "events" else "source_id"
    keys = (
        identity_key,
        "source_id",
        "source_kind",
        "family",
        "species",
        "requested_species",
        "action",
        "specificity",
        "confidence",
        "distance",
        "horizontal_position",
        "body_scale",
        "body_scale_evidence",
        "surface_contact",
        "surface_contact_evidence",
        "visual_evidence",
        "hard_negatives",
        "start_fraction",
        "end_fraction",
    )
    if lane == "events":
        keys += (
            "offset_fraction",
            "duration_fraction",
            "provider_audible_event",
            "provider_audible_event_supplied",
        )
    else:
        keys += (
            "provider_audible_presence",
            "provider_audible_presence_supplied",
        )
    return {key: copy.deepcopy(item.get(key)) for key in keys}


def _physical_foley_evidence_binding(scene: Mapping[str, Any]) -> dict[str, Any]:
    evidence = scene.get("evidence_artifact")
    if not isinstance(evidence, Mapping):
        raise AcousticSceneError("physical foley obligation has no visual evidence binding")
    return {
        "selected_source_id": str(scene.get("selected_source_id") or ""),
        "evidence_key": str(evidence.get("evidence_key") or ""),
        "contact_sheet_sha256": str(evidence.get("contact_sheet_sha256") or ""),
        "motion_preview_sha256": str(evidence.get("motion_preview_sha256") or ""),
        "frame_time_contract": copy.deepcopy(evidence.get("frame_time_contract")),
    }


def _water_movement_evidence_words(item: Mapping[str, Any]) -> set[str]:
    return _tokens(
        " ".join(
            (
                str(item.get("action") or ""),
                str(item.get("surface_contact") or ""),
                str(item.get("surface_contact_evidence") or ""),
                str(item.get("visual_evidence") or ""),
            )
        )
    )


def _is_visible_water_movement(
    source: Mapping[str, Any], event: Mapping[str, Any]
) -> bool:
    source_kind = str(source.get("source_kind") or "").casefold()
    event_kind = str(event.get("source_kind") or "").casefold()
    if (
        source_kind not in _WATER_MOVEMENT_ORIGIN_KINDS
        or event_kind not in _WATER_MOVEMENT_ORIGIN_KINDS
    ):
        return False
    if float(source.get("confidence") or 0.0) < 0.85 or float(
        event.get("confidence") or 0.0
    ) < 0.80:
        return False
    if source.get("distance") not in {"extreme_close", "close", "medium"}:
        return False
    if event.get("distance") not in {"extreme_close", "close", "medium"}:
        return False
    event_action_words = _tokens(event.get("action"))
    if not event_action_words.intersection(_WATER_MOVEMENT_ACTION_WORDS):
        return False
    source_words = _water_movement_evidence_words(source)
    event_words = _water_movement_evidence_words(event)
    if "water" not in source_words.union(event_words):
        return False
    if not source_words.intersection(_WATER_MOVEMENT_EFFECT_WORDS):
        return False
    if not event_words.intersection(_WATER_MOVEMENT_EFFECT_WORDS):
        return False
    if "water_splash" in {source_kind, event_kind} and not source_words.union(
        event_words
    ).intersection(_VISIBLE_ANIMAL_CAUSE_WORDS):
        return False
    return True


def _water_movement_profile(event: Mapping[str, Any]) -> Mapping[str, Any]:
    words = _tokens(event.get("action"))
    if "takeoff" in words:
        return _VISIBLE_WATER_MOVEMENT_PROFILES["water_push_splash"]
    if words.intersection({"dribble", "dribbling", "pour", "pouring", "scoop", "scooping"}):
        return _VISIBLE_WATER_MOVEMENT_PROFILES[
            "water_light_pour_dribble"
        ]
    return _VISIBLE_WATER_MOVEMENT_PROFILES["water_wading_splash"]


def derive_physical_foley_obligations(scene: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Derive path-free, species-neutral foley duties from exact visual evidence."""

    sources_by_id: dict[str, list[Mapping[str, Any]]] = {}
    for source in scene.get("visible_sources", []):
        if not isinstance(source, Mapping) or source.get("causal_origin_source_id"):
            continue
        source_id = str(source.get("source_id") or "")
        if source_id:
            sources_by_id.setdefault(source_id, []).append(source)
    obligations: list[dict[str, Any]] = []
    for event in scene.get("events", []):
        if not isinstance(event, Mapping):
            continue
        source_id = str(event.get("source_id") or "")
        matches = sources_by_id.get(source_id, [])
        if len(matches) != 1 or not _is_visible_water_movement(matches[0], event):
            continue
        source = matches[0]
        profile = _water_movement_profile(event)
        start_fraction = float(event.get("offset_fraction") or 0.0)
        end_fraction = min(
            1.0, start_fraction + float(event.get("duration_fraction") or 0.0)
        )
        material: dict[str, Any] = {
            "schema_version": 1,
            "contract": PHYSICAL_FOLEY_OBLIGATION_CONTRACT,
            "policy_version": CAUSAL_RECONSTRUCTION_POLICY_VERSION,
            "profile": profile["profile"],
            "slot_id": str(scene.get("slot_id") or ""),
            "lane": "presence",
            "origin_source_id": source_id,
            "origin_event_id": str(event.get("event_id") or ""),
            "start_fraction": start_fraction,
            "end_fraction": end_fraction,
            "taxonomy": {
                "source_kind": profile["source_kind"],
                "family": None,
                "species": None,
                "action": profile["action"],
                "specificity": "neutral_action",
            },
            "required": True,
            "visual_evidence_binding": _physical_foley_evidence_binding(scene),
            "source_binding": _physical_foley_item_projection(
                source, lane="presence"
            ),
            "event_binding": _physical_foley_item_projection(event, lane="events"),
            "asset_compatibility": copy.deepcopy(profile),
        }
        material["obligation_key"] = stable_cache_key(
            "physical_foley_obligation", material, version=1
        )
        material["output_source_id"] = "causal-foley-" + material[
            "obligation_key"
        ].rsplit(":", 1)[-1][:12]
        obligations.append(material)
    obligations.sort(
        key=lambda row: (
            row["start_fraction"],
            row["origin_event_id"],
            row["obligation_key"],
        )
    )
    return obligations


def required_cue_audibility_binding(obligation: Mapping[str, Any]) -> dict[str, Any]:
    material: dict[str, Any] = {
        "contract": REQUIRED_CUE_AUDIBILITY_CONTRACT,
        "obligation_key": str(obligation.get("obligation_key") or ""),
        "lane": str(obligation.get("lane") or ""),
        "measurement": "source_isolated_static_gain_exact_pcm_v1",
        "window_frames": 4_800,
        "peak_floor_dbfs": -24.0,
        "peak_ceiling_dbfs": -6.0,
        "max_window_rms_floor_dbfs": -34.0,
        "core_window_rms_floor_dbfs": -42.0,
        "minimum_core_window_ratio": 0.2,
        "outside_leakage_threshold_dbfs": -60.0,
        "outside_max_run_frames": 12_000,
        "preview_padding_frames": 24_000,
    }
    material["receipt_key"] = stable_cache_key(
        "required_cue_audibility", material, version=1
    )
    return material


def _explicit_visible_vocalisation(source: Mapping[str, Any]) -> bool:
    action = _canonical_action(source.get("action"))
    if (
        source.get("source_kind") != "animal"
        or action not in _VOCAL_ACTIONS
        or _is_compound_action(source.get("action"))
        or source.get("specificity") != "exact_species"
        or not source.get("species")
    ):
        return False
    return bool(_tokens(source.get("visual_evidence")) & _VISIBLE_CALL_CUES)


def _dry_grass_walking_decision(
    source: Mapping[str, Any],
) -> tuple[bool, str]:
    action = _canonical_action(source.get("action"))
    action_tokens = _tokens(action)
    if _is_compound_action(source.get("action")):
        return False, "blocked_compound_sound"
    if source.get("source_kind") != "animal":
        return False, "blocked_not_visible_animal_locomotion"
    if action not in _WALKING_ACTIONS and not action_tokens.intersection(
        _WALKING_ACTIONS
    ):
        return False, "blocked_not_walking_or_patrolling"
    if float(source.get("confidence") or 0.0) < 0.85:
        return False, "blocked_low_visual_confidence"
    if source.get("distance") not in {"extreme_close", "close", "medium"}:
        return False, "blocked_contact_not_visually_resolved"
    body_scale = str(source.get("body_scale") or "unknown")
    if body_scale == "massive":
        return False, "blocked_massive_body_scale"
    if body_scale not in _DRY_GRASS_WALKING_PROFILE["compatible_body_scales"]:
        return False, "blocked_unproved_body_scale_compatibility"
    if not str(source.get("body_scale_evidence") or "").strip():
        return False, "blocked_missing_body_scale_evidence"
    if source.get("surface_contact") != "dry_grass":
        return False, "blocked_non_dry_grass_contact"
    if not str(source.get("surface_contact_evidence") or "").strip():
        return False, "blocked_missing_surface_contact_evidence"
    evidence_words = _tokens(
        " ".join(
            [
                str(source.get("visual_evidence") or ""),
                str(source.get("surface_contact_evidence") or ""),
            ]
        )
    )
    if not {"dry", "grass"}.issubset(evidence_words):
        return False, "blocked_dry_grass_not_visually_evidenced"
    if not evidence_words.intersection(
        {"contact", "contacts", "feet", "foot", "lands", "paw", "steps"}
    ):
        return False, "blocked_surface_contact_not_visually_evidenced"
    return True, "derived_species_neutral_dry_grass_walking"


def _localized_contact_event(
    event: Mapping[str, Any], *, slot_duration: float
) -> tuple[bool, str]:
    if event.get("provider_audible_event_supplied") is not True or event.get(
        "provider_audible_event"
    ) is not True:
        return False, "blocked_provider_silent_event"
    if _is_compound_action(event.get("action")):
        return False, "blocked_compound_sound"
    action = _canonical_action(event.get("action"))
    if action not in _LOCAL_CONTACT_ACTIONS:
        return False, "blocked_event_without_localized_contact_action"
    if event.get("source_kind") != "foley" or event.get("family") or event.get(
        "species"
    ):
        return False, "blocked_event_without_species_neutral_foley_taxonomy"
    duration = slot_duration * float(event.get("duration_fraction") or 0.0)
    if duration > 0.75:
        return False, "blocked_non_local_event_interval"
    if not (_tokens(event.get("visual_evidence")) & _VISIBLE_CONTACT_CUES):
        return False, "blocked_event_without_visible_contact"
    if action in {"footstep", "splash", "step"}:
        if event.get("body_scale") != "medium" or not event.get(
            "body_scale_evidence"
        ):
            return False, "blocked_event_without_body_scale_compatibility"
        if event.get("surface_contact") in {"none", "unknown"} or not event.get(
            "surface_contact_evidence"
        ):
            return False, "blocked_event_without_surface_contact_compatibility"
    return True, "accepted_localized_visible_contact"


def _compiler_receipt(
    *,
    scene: Mapping[str, Any],
    item: Mapping[str, Any],
    lane: str,
    outcome: str,
    reason_code: str,
    output_id: str | None = None,
    asset_compatibility: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    identity_key = "event_id" if lane == "events" else "source_id"
    provider_key = (
        "provider_audible_event" if lane == "events" else "provider_audible_presence"
    )
    supplied_key = provider_key + "_supplied"
    material: dict[str, Any] = {
        "schema_version": CAUSAL_RECONSTRUCTION_RECEIPT_VERSION,
        "policy_version": CAUSAL_RECONSTRUCTION_POLICY_VERSION,
        "slot_id": str(scene.get("slot_id") or ""),
        "lane": lane,
        "visual_source_id": str(item.get(identity_key) or ""),
        "provider_flag": item.get(provider_key),
        "provider_flag_supplied": item.get(supplied_key) is True,
        "source_kind": item.get("source_kind"),
        "family": item.get("family"),
        "species": item.get("species"),
        "action": item.get("action"),
        "body_scale": item.get("body_scale"),
        "body_scale_evidence": item.get("body_scale_evidence"),
        "surface_contact": item.get("surface_contact"),
        "surface_contact_evidence": item.get("surface_contact_evidence"),
        "visual_evidence": item.get("visual_evidence"),
        "outcome": outcome,
        "reason_code": reason_code,
        "output_id": output_id,
        "input_binding": _physical_foley_item_projection(item, lane=lane),
    }
    surface_contact_normalization = item.get("surface_contact_normalization")
    if isinstance(surface_contact_normalization, Mapping):
        material["surface_contact_normalization"] = copy.deepcopy(
            dict(surface_contact_normalization)
        )
    if asset_compatibility is not None:
        material["asset_compatibility"] = copy.deepcopy(dict(asset_compatibility))
    material["receipt_key"] = stable_cache_key(
        "causal_reconstruction_receipt",
        material,
        version=CAUSAL_RECONSTRUCTION_RECEIPT_VERSION,
    )
    return material


def _synthetic_walking_source(
    scene: Mapping[str, Any], source: Mapping[str, Any]
) -> dict[str, Any]:
    identity_material = {
        "policy_version": CAUSAL_RECONSTRUCTION_POLICY_VERSION,
        "slot_id": scene.get("slot_id"),
        "source_id": source.get("source_id"),
        "profile": _DRY_GRASS_WALKING_PROFILE["profile"],
    }
    source_id = "causal-foley-" + stable_cache_key(
        "causal_foley_source", identity_material, version=1
    ).rsplit(":", 1)[-1][:12]
    receipt = _compiler_receipt(
        scene=scene,
        item=source,
        lane="presence",
        outcome="derived_presence",
        reason_code="derived_species_neutral_dry_grass_walking",
        output_id=source_id,
        asset_compatibility=_DRY_GRASS_WALKING_PROFILE,
    )
    hard_negatives = sorted(
        {
            *[str(item) for item in source.get("hard_negatives", [])],
            "animal vocalization",
            "gallop",
            "heavy impact",
            "massive body footsteps",
            "water splash",
            "wet footsteps",
        },
        key=str.casefold,
    )
    return {
        "source_id": source_id,
        "source_kind": "foley",
        "family": None,
        "species": None,
        "requested_species": None,
        "action": "walking",
        "specificity": "neutral_action",
        "confidence": float(source["confidence"]),
        "distance": source["distance"],
        "horizontal_position": source["horizontal_position"],
        "audible_presence": True,
        "provider_audible_presence": None,
        "provider_audible_presence_supplied": False,
        "body_scale": source["body_scale"],
        "body_scale_evidence": source["body_scale_evidence"],
        "surface_contact": source["surface_contact"],
        "surface_contact_evidence": source["surface_contact_evidence"],
        "visual_evidence": source["visual_evidence"],
        "hard_negatives": hard_negatives,
        "start_fraction": float(source["start_fraction"]),
        "end_fraction": float(source["end_fraction"]),
        "gain_db": -27.0,
        "causal_origin_source_id": source["source_id"],
        "asset_compatibility_contract": copy.deepcopy(_DRY_GRASS_WALKING_PROFILE),
        "causal_compiler_receipt": copy.deepcopy(receipt),
    }


def _synthetic_physical_foley_source(
    scene: Mapping[str, Any],
    source: Mapping[str, Any],
    event: Mapping[str, Any],
    obligation: Mapping[str, Any],
) -> dict[str, Any]:
    source_id = str(obligation["output_source_id"])
    compatibility = copy.deepcopy(dict(obligation["asset_compatibility"]))
    receipt = _compiler_receipt(
        scene=scene,
        item=event,
        lane="events",
        outcome="derived_presence",
        reason_code="derived_species_neutral_visible_water_movement",
        output_id=source_id,
        asset_compatibility=compatibility,
    )
    hard_negatives = sorted(
        {
            *[str(item) for item in scene.get("hard_negatives", [])],
            *[str(item) for item in source.get("hard_negatives", [])],
            *[str(item) for item in event.get("hard_negatives", [])],
            *[
                str(item)
                for item in compatibility.get("required_asset_hard_negatives", [])
            ],
            "animal vocalization",
            "boat wake",
            "human splash",
            "music",
            "speech",
        },
        key=str.casefold,
    )
    return {
        "source_id": source_id,
        "source_kind": str(compatibility["source_kind"]),
        "family": None,
        "species": None,
        "requested_species": None,
        "action": str(obligation["taxonomy"]["action"]),
        "specificity": "neutral_action",
        "confidence": min(
            float(source.get("confidence") or 0.0),
            float(event.get("confidence") or 0.0),
        ),
        "distance": event["distance"],
        "horizontal_position": event["horizontal_position"],
        "audible_presence": True,
        "provider_audible_presence": None,
        "provider_audible_presence_supplied": False,
        "body_scale": "unknown",
        "body_scale_evidence": None,
        "surface_contact": "water",
        "surface_contact_evidence": event.get("surface_contact_evidence"),
        "visual_evidence": event["visual_evidence"],
        "hard_negatives": hard_negatives,
        "start_fraction": float(obligation["start_fraction"]),
        "end_fraction": float(obligation["end_fraction"]),
        # Required physical cues are measured after rendering.  A neutral 0 dB
        # static gain avoids the legacy -16 dB attenuation; the exact window
        # RMS/peak contract below still fails closed for an unsuitable asset.
        "gain_db": 0.0,
        "required": True,
        "causal_origin_source_id": str(obligation["origin_source_id"]),
        "causal_origin_event_id": str(obligation["origin_event_id"]),
        "asset_compatibility_contract": compatibility,
        "physical_foley_obligation": copy.deepcopy(dict(obligation)),
        "required_audibility": required_cue_audibility_binding(obligation),
        "causal_compiler_receipt": copy.deepcopy(receipt),
    }


def _decompile_causal_acoustic_scenes(
    scenes: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Recover the normalized paid planner rows before deterministic compilation."""

    raw_scenes: list[dict[str, Any]] = []
    for compiled_scene in scenes:
        scene = copy.deepcopy(dict(compiled_scene))
        scene.pop("causal_compiler_receipt", None)
        scene["visible_sources"] = [
            copy.deepcopy(dict(source))
            for source in scene.get("visible_sources", [])
            if isinstance(source, Mapping)
            and not source.get("causal_origin_source_id")
        ]
        for source in scene["visible_sources"]:
            source.pop("causal_compiler_receipt", None)
            if source.get("provider_audible_presence_supplied") is True:
                source["audible_presence"] = bool(
                    source.get("provider_audible_presence")
                )
            else:
                source["audible_presence"] = True
        scene["events"] = [
            copy.deepcopy(dict(event))
            for event in scene.get("events", [])
            if isinstance(event, Mapping)
        ]
        for event in scene["events"]:
            event.pop("causal_compiler_receipt", None)
            event["audible_presence"] = False
            if event.get("provider_audible_event_supplied") is True:
                event["audible_event"] = bool(event.get("provider_audible_event"))
            else:
                event["audible_event"] = True
        scene["scene_id"] = "acoustic-" + stable_cache_key(
            "acoustic_scene",
            _canonical_scene_material(scene),
            version=ACOUSTIC_SCENE_SCHEMA_VERSION,
        ).rsplit(":", 1)[-1][:12]
        raw_scenes.append(scene)
    return raw_scenes


def _validate_physical_foley_compilation(
    scenes: Sequence[Mapping[str, Any]],
    timeline: Mapping[str, Any],
) -> None:
    for scene in scenes:
        marker = scene.get("causal_compiler_receipt")
        if not isinstance(marker, Mapping):
            raise AcousticSceneError("acoustic scene lacks causal compiler receipt")
        marker_material = copy.deepcopy(dict(marker))
        supplied_marker_key = marker_material.pop("receipt_key", None)
        expected_marker_key = stable_cache_key(
            "causal_scene_compilation",
            marker_material,
            version=CAUSAL_RECONSTRUCTION_RECEIPT_VERSION,
        )
        if supplied_marker_key != expected_marker_key:
            raise AcousticSceneError("causal scene compilation receipt is stale")

        obligations = derive_physical_foley_obligations(scene)
        source_rows = [
            source
            for source in scene.get("visible_sources", [])
            if isinstance(source, Mapping)
            and not source.get("causal_origin_source_id")
        ]
        source_by_id: dict[str, list[Mapping[str, Any]]] = {}
        for source in source_rows:
            source_by_id.setdefault(str(source.get("source_id") or ""), []).append(
                source
            )
        event_by_id = {
            str(event.get("event_id") or ""): event
            for event in scene.get("events", [])
            if isinstance(event, Mapping)
        }
        expected_sources: list[dict[str, Any]] = []
        for obligation in obligations:
            origin_sources = source_by_id.get(
                str(obligation["origin_source_id"]), []
            )
            event = event_by_id.get(str(obligation["origin_event_id"]))
            if len(origin_sources) != 1 or event is None:
                raise AcousticSceneError("physical foley obligation origin is ambiguous")
            expected_source = _synthetic_physical_foley_source(
                scene, origin_sources[0], event, obligation
            )
            expected_sources.append(expected_source)
            if event.get("audible_event") is not False:
                raise AcousticSceneError(
                    "physical foley origin event must remain a silent observation"
                )
            if event.get("causal_compiler_receipt") != expected_source.get(
                "causal_compiler_receipt"
            ):
                raise AcousticSceneError(
                    "physical foley origin event receipt is stale"
                )

        actual_sources = [
            copy.deepcopy(dict(source))
            for source in scene.get("visible_sources", [])
            if isinstance(source, Mapping)
            and isinstance(source.get("physical_foley_obligation"), Mapping)
        ]
        expected_sources.sort(key=lambda row: str(row["source_id"]))
        actual_sources.sort(key=lambda row: str(row.get("source_id") or ""))
        if actual_sources != expected_sources:
            raise AcousticSceneError(
                "physical foley obligations do not match compiled sources"
            )
        expected_keys = [row["obligation_key"] for row in obligations]
        if marker.get("physical_foley_obligation_keys") != expected_keys:
            raise AcousticSceneError("physical foley obligation lineage is stale")
        expected_source_ids = {str(row["source_id"]) for row in expected_sources}
        declared_source_ids = {
            str(value) for value in marker.get("synthetic_source_ids", [])
        }
        if expected_source_ids != declared_source_ids.intersection(
            expected_source_ids
        ):
            raise AcousticSceneError("physical foley synthetic source lineage is stale")
        declared_decisions = list(marker.get("decision_receipt_keys", []))
        for row in expected_sources:
            receipt_key = row["causal_compiler_receipt"]["receipt_key"]
            if declared_decisions.count(receipt_key) != 1:
                raise AcousticSceneError("physical foley decision lineage is stale")

    # Rebuild every compiler-owned field from the normalized paid planner rows.
    # This prevents a self-rehashed marker from deleting an obligation, leaving
    # a stale decision receipt, or duplicating/retargeting a synthetic source.
    expected = compile_causal_acoustic_scenes(
        timeline,
        _decompile_causal_acoustic_scenes(scenes),
    )
    if list(scenes) != expected:
        raise AcousticSceneError("causal acoustic compilation is not deterministic")


def compile_causal_acoustic_scenes(
    timeline: Mapping[str, Any], scenes: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Compile silent visual evidence into conservative, auditable sound causality.

    Provider flags stay available as evidence but are not blindly trusted.  The
    compiler may add a separately identified foley observation; it never
    rewrites an animal observation into another taxonomy.
    """

    validate_acoustic_scenes(scenes, timeline)
    compiled = [copy.deepcopy(dict(scene)) for scene in scenes]
    markers = [scene.get("causal_compiler_receipt") for scene in compiled]
    if any(markers):
        if not all(
            isinstance(marker, Mapping)
            and marker.get("policy_version")
            == CAUSAL_RECONSTRUCTION_POLICY_VERSION
            for marker in markers
        ):
            raise AcousticSceneError("acoustic scenes have mixed causal compiler state")
        return compiled

    _slots, by_id, _duration, _tolerance = _timeline_slots(timeline)
    for scene in compiled:
        slot_id = str(scene["slot_id"])
        slot = by_id[slot_id]
        slot_duration = float(slot["end"]) - float(slot["start"])
        decisions: list[dict[str, Any]] = []
        synthetic_sources: list[dict[str, Any]] = []
        environment_emitters: set[str] = set()
        physical_obligations = derive_physical_foley_obligations(scene)
        obligations_by_event_id = {
            str(row["origin_event_id"]): row for row in physical_obligations
        }
        origin_sources = {
            str(row.get("source_id") or ""): row
            for row in scene.get("visible_sources", [])
            if isinstance(row, Mapping) and not row.get("causal_origin_source_id")
        }

        for source in scene.get("visible_sources", []):
            source["audible_presence"] = False
            emitter = _continuous_environment_emitter(source)
            if emitter is not None:
                environment_emitters.add(emitter)
                receipt = _compiler_receipt(
                    scene=scene,
                    item=source,
                    lane="presence",
                    outcome="routed_to_ambience",
                    reason_code=f"continuous_environment_emitter:{emitter}",
                )
            elif _explicit_visible_vocalisation(source):
                source["audible_presence"] = True
                receipt = _compiler_receipt(
                    scene=scene,
                    item=source,
                    lane="presence",
                    outcome="accepted_presence",
                    reason_code="explicit_visible_vocal_action",
                    output_id=str(source["source_id"]),
                    asset_compatibility={
                        "profile": "exact-visible-vocal-action-v1",
                        "source_kind": source["source_kind"],
                        "family": source.get("family"),
                        "species": source.get("species"),
                        "action": source.get("action"),
                        "specificity": "exact_species",
                    },
                )
            else:
                eligible, reason = _dry_grass_walking_decision(source)
                if eligible:
                    synthetic = _synthetic_walking_source(scene, source)
                    synthetic_sources.append(synthetic)
                    receipt = copy.deepcopy(synthetic["causal_compiler_receipt"])
                else:
                    if _canonical_action(source.get("action")) in _VOCAL_ACTIONS:
                        reason = "blocked_vocal_without_explicit_visible_call_action"
                    receipt = _compiler_receipt(
                        scene=scene,
                        item=source,
                        lane="presence",
                        outcome="blocked",
                        reason_code=reason,
                    )
            source["causal_compiler_receipt"] = copy.deepcopy(receipt)
            decisions.append(receipt)

        for event in scene.get("events", []):
            event["audible_event"] = False
            obligation = obligations_by_event_id.get(str(event.get("event_id") or ""))
            if obligation is not None:
                origin_source = origin_sources[str(obligation["origin_source_id"])]
                synthetic = _synthetic_physical_foley_source(
                    scene, origin_source, event, obligation
                )
                synthetic_sources.append(synthetic)
                receipt = copy.deepcopy(synthetic["causal_compiler_receipt"])
            else:
                emitter = _continuous_environment_emitter(event)
                if emitter is not None:
                    environment_emitters.add(emitter)
                    receipt = _compiler_receipt(
                        scene=scene,
                        item=event,
                        lane="events",
                        outcome="routed_to_ambience",
                        reason_code=f"continuous_environment_emitter:{emitter}",
                    )
                else:
                    accepted, reason = _localized_contact_event(
                        event, slot_duration=slot_duration
                    )
                    if accepted:
                        event["audible_event"] = True
                        receipt = _compiler_receipt(
                            scene=scene,
                            item=event,
                            lane="events",
                            outcome="accepted_event",
                            reason_code=reason,
                            output_id=str(event["event_id"]),
                        )
                    else:
                        receipt = _compiler_receipt(
                            scene=scene,
                            item=event,
                            lane="events",
                            outcome="blocked",
                            reason_code=reason,
                        )
            event["causal_compiler_receipt"] = copy.deepcopy(receipt)
            decisions.append(receipt)

        scene["visible_sources"].extend(synthetic_sources)

        if environment_emitters:
            scene["environment"]["continuous_emitters"] = sorted(
                environment_emitters
            )
        summary: dict[str, Any] = {
            "schema_version": CAUSAL_RECONSTRUCTION_RECEIPT_VERSION,
            "policy_version": CAUSAL_RECONSTRUCTION_POLICY_VERSION,
            "slot_id": slot_id,
            "decision_receipt_keys": [row["receipt_key"] for row in decisions],
            "synthetic_source_ids": [row["source_id"] for row in synthetic_sources],
            "physical_foley_obligation_keys": [
                row["obligation_key"] for row in physical_obligations
            ],
            "continuous_emitters": sorted(environment_emitters),
        }
        summary["receipt_key"] = stable_cache_key(
            "causal_scene_compilation",
            summary,
            version=CAUSAL_RECONSTRUCTION_RECEIPT_VERSION,
        )
        scene["causal_compiler_receipt"] = summary
        scene["scene_id"] = "acoustic-" + stable_cache_key(
            "acoustic_scene",
            _canonical_scene_material(scene),
            version=ACOUSTIC_SCENE_SCHEMA_VERSION,
        ).rsplit(":", 1)[-1][:12]

    validate_acoustic_scenes(
        compiled,
        timeline,
        _skip_compiler_replay=True,
    )
    return compiled


def acoustically_compatible(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Conservative ambience merge predicate; unknowns never bridge contradictions."""

    left_env, right_env = left.get("environment"), right.get("environment")
    if not isinstance(left_env, Mapping) or not isinstance(right_env, Mapping):
        return False
    keys = (
        "location_type",
        "enclosure",
        "weather",
        "water",
        "wind",
        "perspective",
        "continuous_emitters",
    )
    for key in keys:
        a = str(left_env.get(key) or "unknown").casefold()
        b = str(right_env.get(key) or "unknown").casefold()
        if a != b:
            return False
    return True


def _environment_query(environment: Mapping[str, Any]) -> str:
    parts = [str(environment["location_type"])]
    for key in ("weather", "water", "wind", "time_of_day", "perspective"):
        value = str(environment.get(key) or "unknown")
        if value not in {"unknown", "none", "none_visible"}:
            parts.append(value)
    emitters = environment.get("continuous_emitters", [])
    if isinstance(emitters, Sequence) and not isinstance(emitters, (str, bytes)):
        parts.extend(str(item) for item in emitters)
    return " ".join(parts) + " natural location ambience no music no speech"


def _source_query(source: Mapping[str, Any], environment: Mapping[str, Any]) -> str:
    parts: list[str] = []
    if source.get("species"):
        parts.append(str(source["species"]))
    elif source.get("family"):
        parts.append(str(source["family"]))
    else:
        parts.append(str(source["source_kind"]))
    if source.get("action"):
        parts.append(str(source["action"]))
    if source.get("surface_contact") not in {None, "none", "unknown"}:
        parts.append(str(source["surface_contact"]).replace("_", " "))
    if source.get("body_scale") not in {None, "unknown"}:
        parts.append(str(source["body_scale"]) + " scale")
    if source.get("causal_origin_source_id"):
        parts.append("species neutral subdued foley")
    parts.extend([str(source["distance"]), "natural", "no music", "no speech"])
    return " ".join(parts)


def _pan(position: str) -> float:
    return {
        "far_left": -0.8,
        "left": -0.4,
        "center": 0.0,
        "right": 0.4,
        "far_right": 0.8,
    }[position]


def build_natural_lanes(
    timeline: Mapping[str, Any],
    scenes: Sequence[Mapping[str, Any]],
    *,
    provider: str = "local_licensed",
) -> dict[str, list[dict[str, Any]]]:
    """Expand audited scenes into 100%-covered ambience plus visible sound sources."""

    validate_acoustic_scenes(scenes, timeline)
    slots, by_id, _duration, _tolerance = _timeline_slots(timeline)
    by_slot = {str(scene["slot_id"]): scene for scene in scenes}
    ordered = [by_slot[str(slot["slot_id"])] for slot in slots]

    groups: list[list[Mapping[str, Any]]] = []
    for scene in ordered:
        if groups and acoustically_compatible(groups[-1][-1], scene):
            groups[-1].append(scene)
        else:
            groups.append([scene])
    ambience: list[dict[str, Any]] = []
    for group_index, group in enumerate(groups):
        first, last = group[0], group[-1]
        environment = dict(first["environment"])
        confidence = min(float(scene["environment"]["confidence"]) for scene in group)
        if group_index == 0:
            crossfade_seconds = 0.08
        else:
            previous_environment = groups[group_index - 1][-1]["environment"]
            crossfade_seconds = (
                0.3
                if previous_environment.get("location_type")
                == environment.get("location_type")
                else 0.16
            )
        region = make_ambience_region(
            timeline,
            start_slot_id=str(first["slot_id"]),
            end_slot_id=str(last["slot_id"]),
            environment=environment,
            query_en=_environment_query(environment),
            visual_evidence={
                "description": "; ".join(str(scene["summary"]) for scene in group),
                "sampled_frames": 8,
                "motion_preview": True,
            },
            confidence=confidence,
            hard_negatives=sorted(
                {str(item) for scene in group for item in scene.get("hard_negatives", [])}
            ),
            provider=provider,
            crossfade_seconds=crossfade_seconds,
        )
        region["retrieval"] = {"status": "needs_asset", "degradation_level": "environment"}
        routed_receipts = [
            copy.deepcopy(item["causal_compiler_receipt"])
            for scene in group
            for collection in ("visible_sources", "events")
            for item in scene.get(collection, [])
            if isinstance(item.get("causal_compiler_receipt"), Mapping)
            and item["causal_compiler_receipt"].get("outcome")
            == "routed_to_ambience"
        ]
        if routed_receipts:
            region["causal_compiler_receipts"] = routed_receipts
        ambience.append(region)

    presence: list[dict[str, Any]] = []
    event_cues: list[dict[str, Any]] = []
    for scene in ordered:
        slot_id = str(scene["slot_id"])
        slot = by_id[slot_id]
        slot_duration = float(slot["end"]) - float(slot["start"])
        environment = scene["environment"]
        for source in scene.get("visible_sources", []):
            if not source.get("audible_presence"):
                continue
            taxonomy = {
                "source_kind": source["source_kind"],
                "family": source.get("family"),
                "species": source.get("species"),
                "action": source.get("action"),
                "environment": environment.get("location_type"),
                "specificity": source["specificity"],
            }
            layer = make_presence_layer(
                timeline,
                slot_id=slot_id,
                query_en=_source_query(source, environment),
                visual_evidence={
                    "description": source["visual_evidence"],
                    "sampled_frames": 8,
                    "motion_preview": True,
                },
                taxonomy=taxonomy,
                confidence=float(source["confidence"]),
                hard_negatives=[*scene.get("hard_negatives", []), *source.get("hard_negatives", [])],
                start_offset_seconds=slot_duration * float(source["start_fraction"]),
                end_offset_seconds=slot_duration * float(source["end_fraction"]),
                provider=provider,
                gain_db=float(source.get("gain_db", -21.0)),
                distance=str(source["distance"]),
                horizontal_position=_pan(str(source["horizontal_position"])),
            )
            layer["retrieval"] = {
                "status": "needs_asset",
                "degradation_level": source["specificity"],
                "degradation": source.get("degradation"),
            }
            layer["visual_source_id"] = source["source_id"]
            if isinstance(source.get("causal_compiler_receipt"), Mapping):
                layer["causal_compiler_receipt"] = copy.deepcopy(
                    source["causal_compiler_receipt"]
                )
            if isinstance(source.get("asset_compatibility_contract"), Mapping):
                layer["asset_compatibility_contract"] = copy.deepcopy(
                    source["asset_compatibility_contract"]
                )
            if source.get("required") is True:
                layer["required"] = True
            if isinstance(source.get("physical_foley_obligation"), Mapping):
                layer["physical_foley_obligation"] = copy.deepcopy(
                    source["physical_foley_obligation"]
                )
            if isinstance(source.get("required_audibility"), Mapping):
                layer["required_audibility"] = copy.deepcopy(
                    source["required_audibility"]
                )
            presence.append(layer)
        for event in scene.get("events", []):
            if not event.get("audible_event"):
                continue
            taxonomy = {
                "source_kind": event["source_kind"],
                "family": event.get("family"),
                "species": event.get("species"),
                "action": event.get("action"),
                "environment": environment.get("location_type"),
                "specificity": event["specificity"],
            }
            cue = make_event_cue(
                timeline,
                slot_id=slot_id,
                offset_seconds=slot_duration * float(event["offset_fraction"]),
                duration_seconds=min(8.0, slot_duration * float(event["duration_fraction"])),
                query_en=_source_query(event, environment),
                visual_evidence={
                    "description": event["visual_evidence"],
                    "sampled_frames": 8,
                    "motion_preview": True,
                },
                taxonomy=taxonomy,
                confidence=float(event["confidence"]),
                hard_negatives=[*scene.get("hard_negatives", []), *event.get("hard_negatives", [])],
                provider=provider,
                gain_db=float(event.get("gain_db", -16.0)),
                distance=str(event["distance"]),
                horizontal_position=_pan(str(event["horizontal_position"])),
            )
            cue["retrieval"] = {
                "status": "needs_asset",
                "degradation_level": event["specificity"],
                "degradation": event.get("degradation"),
            }
            cue["visual_source_id"] = event["event_id"]
            if isinstance(event.get("causal_compiler_receipt"), Mapping):
                cue["causal_compiler_receipt"] = copy.deepcopy(
                    event["causal_compiler_receipt"]
                )
            event_cues.append(cue)
    return {
        "ambience_regions": ambience,
        "presence_layers": presence,
        "event_cues": event_cues,
    }


__all__ = [
    "ACOUSTIC_SCENE_SCHEMA_VERSION",
    "CAUSAL_RECONSTRUCTION_POLICY_VERSION",
    "PHYSICAL_FOLEY_OBLIGATION_CONTRACT",
    "REQUIRED_CUE_AUDIBILITY_CONTRACT",
    "AcousticSceneError",
    "acoustic_evidence_cache_key",
    "acoustic_evidence_contract",
    "acoustically_compatible",
    "build_acoustic_scene",
    "build_natural_lanes",
    "compile_causal_acoustic_scenes",
    "derive_physical_foley_obligations",
    "required_cue_audibility_binding",
    "validate_acoustic_scenes",
]
