"""Versioned, JSON-friendly sound-design plan contracts.

Schema v2 separates music from three diegetic lanes: exact full-timeline
``ambience_regions``, sustained ``presence_layers``, and any number of
frame-synchronised ``event_cues``. Every natural layer is bound to selected
video evidence, taxonomy/hard negatives, a retrieval receipt, and commercially
licensed content provenance. A ready plan fails closed on even one 48 kHz
audio-frame coverage gap, an unresolved asset, or an acoustic contradiction.

Legacy schema v1 remains readable/re-renderable for the existing canary, but
can never satisfy a production-v2 release gate.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .common import atomic_write_json


LEGACY_SOUND_PLAN_SCHEMA_VERSION = 1
SOUND_PLAN_SCHEMA_VERSION = 2
PRODUCTION_SOUND_PLAN_SCHEMA_VERSION = SOUND_PLAN_SCHEMA_VERSION
FOREGROUND_SOUND_DISPOSITION_SCHEMA_VERSION = 1
DEFAULT_MIN_SFX_CONFIDENCE = 0.82
DEFAULT_MIN_SFX_GAP_SECONDS = 8.0
DEFAULT_MIN_EXACT_SPECIES_CONFIDENCE = 0.85
MAX_AMBIENCE_GAP_SECONDS = 0.1
PRODUCTION_AUDIO_SAMPLE_RATE = 48_000
AUDIO_FRAME_TOLERANCE_SECONDS = 0.5 / PRODUCTION_AUDIO_SAMPLE_RATE + 1e-9
MIN_MUSIC_SEGMENT_SECONDS = 25.0
MAX_MUSIC_SEGMENT_SECONDS = 90.0
NATURAL_SOUND_DISTANCES = frozenset(
    {"extreme_close", "close", "medium", "distant", "very_distant"}
)


class SoundPlanError(ValueError):
    """A sound plan is ambiguous, unsafe, or inconsistent with its timeline."""


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"value is not JSON serializable: {type(value).__name__}")


def stable_cache_key(namespace: str, payload: Any, *, version: int = 1) -> str:
    """Return a deterministic SHA-256 key for a JSON-compatible request.

    Secrets must never be included in ``payload``.  Provider request helpers in
    :mod:`semantic_montage.providers` intentionally omit authentication headers
    from their cache-key representation.
    """

    namespace = str(namespace).strip()
    if not namespace or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", namespace):
        raise ValueError("cache-key namespace must be lowercase and filesystem-safe")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("cache-key version must be a positive integer")
    try:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=_json_default,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("cache-key payload must be finite JSON data") from exc
    digest = hashlib.sha256(canonical).hexdigest()
    return f"{namespace}:v{version}:{digest}"


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise SoundPlanError(f"{name} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SoundPlanError(f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise SoundPlanError(f"{name} must be finite")
    return result


def _nonempty(value: Any, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise SoundPlanError(f"{name} must be non-empty")
    return result


def foreground_sound_disposition_cache_key(
    disposition: Mapping[str, Any],
) -> str:
    """Return the durable identity of an intentionally omitted foreground sound."""

    if not isinstance(disposition, Mapping):
        raise SoundPlanError("foreground sound disposition must be a mapping")
    material = copy.deepcopy(dict(disposition))
    material.pop("disposition_key", None)
    return stable_cache_key(
        "foreground_sound_disposition",
        material,
        version=FOREGROUND_SOUND_DISPOSITION_SCHEMA_VERSION,
    )


def refresh_sound_plan_cache_ids(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Refresh embedded acoustic-scene ids and the enclosing sound-plan key.

    Runtime asset paths and evidence mounts remain outside the durable identity.
    Resolver/repair code must use this helper after changing scene audibility or
    a foreground disposition instead of rebuilding fingerprints ad hoc.
    """

    if not isinstance(plan, Mapping):
        raise SoundPlanError("sound plan must be a mapping")
    updated = copy.deepcopy(dict(plan))
    scenes = updated.get("acoustic_scenes", [])
    if not isinstance(scenes, Sequence) or isinstance(scenes, (str, bytes)):
        raise SoundPlanError("acoustic_scenes must be a list")
    for index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            raise SoundPlanError(f"acoustic scene {index} must be mutable mapping data")
        version = scene.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise SoundPlanError(f"acoustic scene {index} has invalid schema version")
        material = copy.deepcopy(scene)
        material.pop("scene_id", None)
        evidence = material.get("evidence_artifact")
        if isinstance(evidence, dict):
            evidence.pop("source_path", None)
            evidence.pop("path", None)
        scene["scene_id"] = "acoustic-" + stable_cache_key(
            "acoustic_scene", material, version=version
        ).rsplit(":", 1)[-1][:12]
    updated.pop("plan_cache_key", None)
    updated["plan_cache_key"] = stable_cache_key(
        "sound_plan", updated, version=SOUND_PLAN_SCHEMA_VERSION
    )
    return updated


def _timeline_slots(
    timeline: Mapping[str, Any], *, tolerance: float | None = None
) -> tuple[list[Mapping[str, Any]], dict[str, Mapping[str, Any]], float, float]:
    raw_slots = timeline.get("slots")
    if not isinstance(raw_slots, Sequence) or isinstance(raw_slots, (str, bytes)):
        raise SoundPlanError("timeline.slots must be a non-empty list")
    slots = list(raw_slots)
    if not slots or any(not isinstance(slot, Mapping) for slot in slots):
        raise SoundPlanError("timeline.slots must contain mappings")

    fps = _finite_number(timeline.get("fps", 25.0), "timeline.fps")
    if fps <= 0:
        raise SoundPlanError("timeline.fps must be positive")
    if tolerance is None:
        tolerance = 1.0 / fps + 1e-7

    by_id: dict[str, Mapping[str, Any]] = {}
    previous_end: float | None = None
    for index, slot in enumerate(slots):
        slot_id = _nonempty(slot.get("slot_id"), f"timeline.slots[{index}].slot_id")
        if slot_id in by_id:
            raise SoundPlanError(f"duplicate timeline slot_id: {slot_id}")
        start = _finite_number(slot.get("start"), f"timeline slot {slot_id}.start")
        end = _finite_number(slot.get("end"), f"timeline slot {slot_id}.end")
        if start < -tolerance or end <= start:
            raise SoundPlanError(f"timeline slot {slot_id} has invalid bounds")
        if previous_end is None:
            if abs(start) > tolerance:
                raise SoundPlanError("timeline must start at zero")
        elif abs(start - previous_end) > tolerance:
            raise SoundPlanError(f"timeline gap/overlap before slot {slot_id}")
        by_id[slot_id] = slot
        previous_end = end

    duration = _finite_number(previous_end, "timeline duration")
    declared = timeline.get("narration_duration")
    if declared is not None and abs(
        _finite_number(declared, "timeline.narration_duration") - duration
    ) > tolerance:
        raise SoundPlanError("timeline.narration_duration disagrees with its slots")
    return slots, by_id, duration, tolerance


def timeline_fingerprint(
    timeline: Mapping[str, Any], *, schema_version: int | None = None
) -> str:
    """Fingerprint portable editorial facts, never runtime mount paths.

    ``source_asset_id`` is the canonical donor identity in a portable project.
    Legacy timelines fall back to their content-derived ``source_id``.  Local
    ``source_path``/``path`` values are intentionally excluded: relocating the
    same project between a Mac, Windows worker, or a named runtime mount must
    not invalidate its sound plan.
    """

    slots, _by_id, duration, _tolerance = _timeline_slots(timeline)
    material: list[dict[str, Any]] = []
    for slot in slots:
        clip = slot.get("selected_clip")
        clip_facts: dict[str, Any] | None = None
        if isinstance(clip, Mapping):
            source_identity = str(
                clip.get("source_asset_id") or clip.get("source_id") or ""
            ).strip()
            if not source_identity:
                raise SoundPlanError(
                    "selected_clip requires source_asset_id or source_id for "
                    "a portable timeline fingerprint"
                )

            start_frame = clip.get("source_frame_start", clip.get("source_start_frame"))
            end_frame = clip.get("source_frame_end", clip.get("source_end_frame"))
            source_in = clip.get("source_in", clip.get("in"))
            source_out = clip.get("source_out", clip.get("out"))
            fps_value = clip.get("fps")
            time_base = clip.get("time_base")
            clip_facts = {
                "source_asset_id": source_identity,
                "source_frame_start": (
                    int(start_frame) if start_frame is not None else None
                ),
                "source_frame_end": int(end_frame) if end_frame is not None else None,
                "source_in": (
                    round(_finite_number(source_in, "selected_clip.source_in"), 9)
                    if source_in is not None
                    else None
                ),
                "source_out": (
                    round(_finite_number(source_out, "selected_clip.source_out"), 9)
                    if source_out is not None
                    else None
                ),
                "fps": (
                    round(_finite_number(fps_value, "selected_clip.fps"), 9)
                    if fps_value is not None
                    else None
                ),
                "time_base": (
                    {
                        "numerator": int(time_base.get("numerator")),
                        "denominator": int(time_base.get("denominator")),
                    }
                    if isinstance(time_base, Mapping)
                    and time_base.get("numerator") is not None
                    and time_base.get("denominator") is not None
                    else None
                ),
                "render_frames": (
                    int(clip["render_frames"])
                    if clip.get("render_frames") is not None
                    else None
                ),
            }
        material.append(
            {
                "slot_id": str(slot["slot_id"]),
                "start": round(float(slot["start"]), 6),
                "end": round(float(slot["end"]), 6),
                "text": str(slot.get("text") or ""),
                "selected_clip": clip_facts,
            }
        )
    version = SOUND_PLAN_SCHEMA_VERSION if schema_version is None else int(schema_version)
    return stable_cache_key(
        "sound_timeline",
        {"duration": round(duration, 6), "slots": material},
        version=version,
    )


def _normalise_mood(mood: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(mood, Mapping):
        raise SoundPlanError("mood must be a mapping")
    result = copy.deepcopy(dict(mood))
    result["label"] = _nonempty(result.get("label"), "mood.label")
    valence = _finite_number(result.get("valence"), "mood.valence")
    arousal = _finite_number(result.get("arousal"), "mood.arousal")
    tension = _finite_number(result.get("tension"), "mood.tension")
    if not -1.0 <= valence <= 1.0:
        raise SoundPlanError("mood.valence must be in [-1, 1]")
    if not 0.0 <= arousal <= 1.0 or not 0.0 <= tension <= 1.0:
        raise SoundPlanError("mood.arousal and mood.tension must be in [0, 1]")
    result.update(valence=valence, arousal=arousal, tension=tension)

    keywords = result.get("keywords", [])
    if not isinstance(keywords, Sequence) or isinstance(keywords, (str, bytes)):
        raise SoundPlanError("mood.keywords must be a list")
    result["keywords"] = [
        _nonempty(keyword, "mood keyword") for keyword in keywords
    ]
    if result.get("bpm") is not None:
        bpm = _finite_number(result["bpm"], "mood.bpm")
        if not 35.0 <= bpm <= 220.0:
            raise SoundPlanError("mood.bpm must be in [35, 220]")
        result["bpm"] = bpm
    return result


def build_music_prompt(
    mood: Mapping[str, Any],
    *,
    narrative_context: str = "",
    style: str = "cinematic documentary score",
) -> str:
    """Build a concise deterministic instrumental prompt from mood metadata."""

    normalised = _normalise_mood(mood)
    parts = [
        f"Instrumental {str(style).strip() or 'documentary score'}",
        "no vocals and no spoken words",
        f"mood: {normalised['label']}",
        f"valence {normalised['valence']:.2f}",
        f"arousal {normalised['arousal']:.2f}",
        f"tension {normalised['tension']:.2f}",
    ]
    if normalised["keywords"]:
        parts.append("qualities: " + ", ".join(normalised["keywords"]))
    if normalised.get("bpm") is not None:
        parts.append(f"approximately {normalised['bpm']:.0f} BPM")
    instruments = normalised.get("instruments")
    if isinstance(instruments, Sequence) and not isinstance(instruments, (str, bytes)):
        cleaned = [str(item).strip() for item in instruments if str(item).strip()]
        if cleaned:
            parts.append("palette: " + ", ".join(cleaned))
    context = str(narrative_context or "").strip()
    if context:
        parts.append("narrative context: " + re.sub(r"\s+", " ", context)[:500])
    return "; ".join(parts) + "."


def music_generation_cache_key(
    segment: Mapping[str, Any], *, schema_version: int | None = None
) -> str:
    """Key only the generation semantics, never a downloaded asset or secret."""

    return stable_cache_key(
        "music_generation",
        {
            "provider": segment.get("provider"),
            "prompt": segment.get("prompt"),
            "instrumental": segment.get("instrumental"),
            "duration_hint_seconds": round(
                _finite_number(
                    segment.get("duration", 0.0), "music segment duration"
                ),
                3,
            ),
            "mood": segment.get("mood"),
        },
        version=SOUND_PLAN_SCHEMA_VERSION if schema_version is None else int(schema_version),
    )


def sfx_search_cache_key(
    cue: Mapping[str, Any], *, schema_version: int | None = None
) -> str:
    """Key a stock search independently from license/download state."""

    return stable_cache_key(
        "sfx_search",
        {
            "provider": cue.get("provider"),
            "query_en": str(cue.get("query_en") or "").strip().casefold(),
            "max_duration_seconds": cue.get("max_duration_seconds"),
        },
        version=SOUND_PLAN_SCHEMA_VERSION if schema_version is None else int(schema_version),
    )


def _slot_range(
    timeline: Mapping[str, Any], start_slot_id: str, end_slot_id: str
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    slots, by_id, _duration, _tolerance = _timeline_slots(timeline)
    if start_slot_id not in by_id or end_slot_id not in by_id:
        raise SoundPlanError("music segment references an unknown slot")
    positions = {str(slot["slot_id"]): index for index, slot in enumerate(slots)}
    if positions[start_slot_id] > positions[end_slot_id]:
        raise SoundPlanError("music segment end slot precedes its start slot")
    return by_id[start_slot_id], by_id[end_slot_id]


def make_music_segment(
    timeline: Mapping[str, Any],
    *,
    start_slot_id: str,
    end_slot_id: str,
    mood: Mapping[str, Any],
    prompt: str | None = None,
    narrative_context: str = "",
    provider: str = "suno_official",
    crossfade_seconds: float = 3.0,
    asset: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a music chapter with exact inclusive slot boundaries."""

    start_slot_id = _nonempty(start_slot_id, "start_slot_id")
    end_slot_id = _nonempty(end_slot_id, "end_slot_id")
    first, last = _slot_range(timeline, start_slot_id, end_slot_id)
    start = float(first["start"])
    end = float(last["end"])
    normalised_mood = _normalise_mood(mood)
    result: dict[str, Any] = {
        "start_slot_id": start_slot_id,
        "end_slot_id": end_slot_id,
        "start": round(start, 6),
        "end": round(end, 6),
        "duration": round(end - start, 6),
        "mood": normalised_mood,
        "prompt": _nonempty(
            prompt
            if prompt is not None
            else build_music_prompt(
                normalised_mood, narrative_context=narrative_context
            ),
            "music prompt",
        ),
        "instrumental": True,
        "provider": _nonempty(provider, "music provider"),
        "transition": {
            "kind": "crossfade",
            "seconds": _finite_number(crossfade_seconds, "crossfade_seconds"),
        },
    }
    if asset is not None:
        result["asset"] = copy.deepcopy(dict(asset))
    identity = {key: value for key, value in result.items() if key != "asset"}
    result["segment_id"] = "music-" + stable_cache_key(
        "music_segment", identity, version=SOUND_PLAN_SCHEMA_VERSION
    ).rsplit(":", 1)[-1][:12]
    result["generation_cache_key"] = music_generation_cache_key(result)
    return result


def _selected_clip(slot: Mapping[str, Any], slot_id: str) -> Mapping[str, Any]:
    clip = slot.get("selected_clip")
    if not isinstance(clip, Mapping):
        raise SoundPlanError(f"SFX slot {slot_id} has no selected_clip evidence")
    return clip


def make_sfx_cue(
    timeline: Mapping[str, Any],
    *,
    slot_id: str,
    offset_seconds: float,
    query_en: str,
    visible_evidence: str | Mapping[str, Any],
    confidence: float,
    evidence_method: str = "temporal_vision_judge",
    provider: str = "shutterstock_sfx",
    gain_db: float = -12.0,
    max_duration_seconds: float = 6.0,
    asset: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create one evidence-backed SFX cue inside a selected timeline slot."""

    slot_id = _nonempty(slot_id, "slot_id")
    _slots, by_id, _duration, tolerance = _timeline_slots(timeline)
    if slot_id not in by_id:
        raise SoundPlanError(f"SFX cue references unknown slot {slot_id}")
    slot = by_id[slot_id]
    clip = _selected_clip(slot, slot_id)
    offset = _finite_number(offset_seconds, "SFX offset_seconds")
    slot_duration = float(slot["end"]) - float(slot["start"])
    if offset < -tolerance or offset >= slot_duration - tolerance:
        raise SoundPlanError("SFX offset_seconds must be inside its slot")

    source_in_raw = clip.get("source_in", clip.get("in"))
    source_out_raw = clip.get("source_out", clip.get("out"))
    source_in = _finite_number(source_in_raw, "selected_clip.source_in")
    source_out = _finite_number(source_out_raw, "selected_clip.source_out")
    if source_out <= source_in:
        raise SoundPlanError("selected clip has invalid bounds")

    if isinstance(visible_evidence, Mapping):
        evidence = copy.deepcopy(dict(visible_evidence))
        evidence["description"] = _nonempty(
            evidence.get("description"), "visible_evidence.description"
        )
        evidence.setdefault("method", evidence_method)
    else:
        evidence = {
            "description": _nonempty(
                visible_evidence, "visible_evidence.description"
            ),
            "method": evidence_method,
        }
    source_id = _nonempty(
        clip.get("source_id") or clip.get("source_path") or clip.get("path"),
        "selected_clip source identity",
    )
    expected_source_time = min(source_out, source_in + max(0.0, offset))
    for key, expected in (("slot_id", slot_id), ("source_id", source_id)):
        if evidence.get(key) is not None and str(evidence[key]) != str(expected):
            raise SoundPlanError(f"visible_evidence.{key} conflicts with timeline")
        evidence[key] = expected
    if evidence.get("source_time") is not None and abs(
        _finite_number(evidence["source_time"], "visible_evidence.source_time")
        - expected_source_time
    ) > tolerance:
        raise SoundPlanError("visible_evidence.source_time conflicts with cue offset")
    evidence["source_time"] = round(expected_source_time, 6)
    evidence["method"] = _nonempty(evidence.get("method"), "visible_evidence.method")

    result: dict[str, Any] = {
        "slot_id": slot_id,
        "offset_seconds": round(max(0.0, offset), 6),
        "time": round(float(slot["start"]) + max(0.0, offset), 6),
        "query_en": _nonempty(query_en, "query_en"),
        "visible_evidence": evidence,
        "confidence": _finite_number(confidence, "SFX confidence"),
        "provider": _nonempty(provider, "SFX provider"),
        "gain_db": _finite_number(gain_db, "SFX gain_db"),
        "max_duration_seconds": _finite_number(
            max_duration_seconds, "SFX max_duration_seconds"
        ),
    }
    if asset is not None:
        result["asset"] = copy.deepcopy(dict(asset))
    identity = {key: value for key, value in result.items() if key != "asset"}
    result["cue_id"] = "sfx-" + stable_cache_key(
        "sfx_cue", identity, version=SOUND_PLAN_SCHEMA_VERSION
    ).rsplit(":", 1)[-1][:12]
    result["search_cache_key"] = sfx_search_cache_key(result)
    return result


def _validate_rfc3339(value: Any, name: str) -> str:
    text = _nonempty(value, name)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise SoundPlanError(f"{name} must be an ISO-8601 timestamp") from exc
    return text


def validate_asset_provenance(
    asset: Mapping[str, Any], *, expected_provider: str | None = None
) -> None:
    """Validate a renderable asset and its commercial-use audit envelope."""

    if not isinstance(asset, Mapping):
        raise SoundPlanError("resolved asset must be a mapping")
    if not asset.get("path") and not asset.get("uri"):
        raise SoundPlanError("resolved asset requires path or uri")
    provenance = asset.get("provenance")
    if not isinstance(provenance, Mapping):
        raise SoundPlanError("resolved asset requires provenance")
    provider = _nonempty(provenance.get("provider"), "provenance.provider")
    if expected_provider is not None and provider != expected_provider:
        raise SoundPlanError(
            f"asset provider {provider!r} disagrees with planned provider {expected_provider!r}"
        )
    _nonempty(provenance.get("asset_id"), "provenance.asset_id")
    _nonempty(provenance.get("source_ref"), "provenance.source_ref")
    digest = _nonempty(provenance.get("sha256"), "provenance.sha256")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise SoundPlanError("provenance.sha256 must contain 64 hexadecimal characters")
    _validate_rfc3339(provenance.get("acquired_at"), "provenance.acquired_at")
    license_info = provenance.get("license")
    if not isinstance(license_info, Mapping):
        raise SoundPlanError("provenance.license must be a mapping")
    if license_info.get("commercial_use_allowed") is not True:
        raise SoundPlanError("asset license must explicitly allow commercial use")
    _nonempty(license_info.get("license_type"), "license.license_type")
    _nonempty(license_info.get("license_ref"), "license.license_ref")

    if provider == "shutterstock_sfx":
        _nonempty(license_info.get("license_id"), "Shutterstock license_id")
        _nonempty(license_info.get("subscription_id"), "Shutterstock subscription_id")
    elif provider == "suno_official":
        _nonempty(license_info.get("terms_ref"), "Suno terms_ref")
        _nonempty(license_info.get("account_tier"), "Suno account_tier")
    elif provider == "freesound":
        _nonempty(license_info.get("api_agreement_ref"), "Freesound api_agreement_ref")
        content_license = _nonempty(
            license_info.get("content_license"), "Freesound content_license"
        )
        if "noncommercial" in content_license.casefold():
            raise SoundPlanError("Freesound NonCommercial assets are not renderable")
        if "creative commons 0" not in content_license.casefold() and not str(
            license_info.get("attribution") or ""
        ).strip():
            raise SoundPlanError("attribution is required for non-CC0 Freesound assets")


def _validate_music_segments(
    segments: Sequence[Mapping[str, Any]],
    *,
    slots: Sequence[Mapping[str, Any]],
    by_id: Mapping[str, Mapping[str, Any]],
    duration: float,
    tolerance: float,
    require_assets: bool,
    schema_version: int = SOUND_PLAN_SCHEMA_VERSION,
) -> None:
    if not segments:
        raise SoundPlanError("sound plan requires at least one music segment")
    positions = {str(slot["slot_id"]): index for index, slot in enumerate(slots)}
    previous_end = 0.0
    ids: set[str] = set()
    for index, segment in enumerate(segments):
        prefix = f"music_segments[{index}]"
        segment_id = _nonempty(segment.get("segment_id"), f"{prefix}.segment_id")
        if segment_id in ids:
            raise SoundPlanError(f"duplicate music segment_id: {segment_id}")
        ids.add(segment_id)
        start_slot_id = _nonempty(
            segment.get("start_slot_id"), f"{prefix}.start_slot_id"
        )
        end_slot_id = _nonempty(segment.get("end_slot_id"), f"{prefix}.end_slot_id")
        if start_slot_id not in by_id or end_slot_id not in by_id:
            raise SoundPlanError(f"{prefix} references an unknown slot")
        if positions[start_slot_id] > positions[end_slot_id]:
            raise SoundPlanError(f"{prefix} reverses its slot range")
        start = _finite_number(segment.get("start"), f"{prefix}.start")
        end = _finite_number(segment.get("end"), f"{prefix}.end")
        expected_start = float(by_id[start_slot_id]["start"])
        expected_end = float(by_id[end_slot_id]["end"])
        if abs(start - expected_start) > tolerance or abs(end - expected_end) > tolerance:
            raise SoundPlanError(f"{prefix} boundaries must coincide with slot boundaries")
        if abs(start - previous_end) > tolerance:
            raise SoundPlanError(f"music has a gap/overlap before {segment_id}")
        segment_duration = end - start
        declared_duration = _finite_number(
            segment.get("duration"), f"{prefix}.duration"
        )
        if abs(segment_duration - declared_duration) > tolerance:
            raise SoundPlanError(f"{prefix}.duration disagrees with start/end")
        if duration >= MIN_MUSIC_SEGMENT_SECONDS:
            if segment_duration < MIN_MUSIC_SEGMENT_SECONDS - tolerance:
                raise SoundPlanError(
                    f"{prefix} is shorter than {MIN_MUSIC_SEGMENT_SECONDS:g}s"
                )
            if segment_duration > MAX_MUSIC_SEGMENT_SECONDS + tolerance:
                raise SoundPlanError(
                    f"{prefix} is longer than {MAX_MUSIC_SEGMENT_SECONDS:g}s"
                )
        _normalise_mood(segment.get("mood"))
        _nonempty(segment.get("prompt"), f"{prefix}.prompt")
        if segment.get("instrumental") is not True:
            raise SoundPlanError(f"{prefix} must be explicitly instrumental")
        provider = _nonempty(segment.get("provider"), f"{prefix}.provider")
        transition = segment.get("transition")
        if not isinstance(transition, Mapping) or transition.get("kind") != "crossfade":
            raise SoundPlanError(f"{prefix} requires a crossfade transition")
        crossfade = _finite_number(transition.get("seconds"), f"{prefix}.transition.seconds")
        if not 0.0 <= crossfade <= 5.0 or crossfade > segment_duration / 2:
            raise SoundPlanError(f"{prefix} crossfade is outside safe bounds")
        expected_keys = {
            music_generation_cache_key(segment, schema_version=schema_version)
        }
        if schema_version == LEGACY_SOUND_PLAN_SCHEMA_VERSION:
            # New code may still be asked to construct a legacy render plan.
            # Its segment helpers use the current key version, while persisted
            # v1 manifests retain their original v1 keys.
            expected_keys.add(music_generation_cache_key(segment))
        if segment.get("generation_cache_key") not in expected_keys:
            raise SoundPlanError(f"{prefix} generation_cache_key is stale")
        asset = segment.get("asset")
        if asset is not None:
            validate_asset_provenance(asset, expected_provider=provider)
        elif require_assets:
            raise SoundPlanError(f"{prefix} has no resolved asset")
        previous_end = end
    if abs(previous_end - duration) > tolerance:
        raise SoundPlanError("music does not cover the full timeline")


def _normalised_effect_identity(cue: Mapping[str, Any]) -> str:
    asset = cue.get("asset")
    if isinstance(asset, Mapping):
        provenance = asset.get("provenance")
        if isinstance(provenance, Mapping) and provenance.get("asset_id"):
            return "asset:" + str(provenance["asset_id"]).strip().casefold()
    query = re.sub(r"\s+", " ", str(cue.get("query_en") or "").strip().casefold())
    return "query:" + query


def _validate_sfx_cues(
    cues: Sequence[Mapping[str, Any]],
    *,
    by_id: Mapping[str, Mapping[str, Any]],
    duration: float,
    tolerance: float,
    min_confidence: float,
    min_gap_seconds: float,
    require_assets: bool,
    schema_version: int = SOUND_PLAN_SCHEMA_VERSION,
) -> None:
    ids: set[str] = set()
    used_slots: set[str] = set()
    previous_time: float | None = None
    previous_identity: str | None = None
    for index, cue in enumerate(cues):
        prefix = f"sfx_cues[{index}]"
        cue_id = _nonempty(cue.get("cue_id"), f"{prefix}.cue_id")
        if cue_id in ids:
            raise SoundPlanError(f"duplicate SFX cue_id: {cue_id}")
        ids.add(cue_id)
        slot_id = _nonempty(cue.get("slot_id"), f"{prefix}.slot_id")
        if slot_id not in by_id:
            raise SoundPlanError(f"{prefix} references an unknown slot")
        if slot_id in used_slots:
            raise SoundPlanError(f"more than one SFX cue in slot {slot_id}")
        used_slots.add(slot_id)
        slot = by_id[slot_id]
        clip = _selected_clip(slot, slot_id)

        offset = _finite_number(cue.get("offset_seconds"), f"{prefix}.offset_seconds")
        cue_time = _finite_number(cue.get("time"), f"{prefix}.time")
        slot_start, slot_end = float(slot["start"]), float(slot["end"])
        if offset < -tolerance or offset >= slot_end - slot_start - tolerance:
            raise SoundPlanError(f"{prefix} offset is outside its slot")
        if abs(cue_time - (slot_start + offset)) > tolerance:
            raise SoundPlanError(f"{prefix}.time disagrees with its slot offset")
        if cue_time < -tolerance or cue_time >= duration + tolerance:
            raise SoundPlanError(f"{prefix}.time is outside the timeline")
        if previous_time is not None and cue_time - previous_time < min_gap_seconds - tolerance:
            raise SoundPlanError("SFX cues are too dense for the sparse sound-design contract")

        query = _nonempty(cue.get("query_en"), f"{prefix}.query_en")
        if len(query) > 200:
            raise SoundPlanError(f"{prefix}.query_en is too long")
        confidence = _finite_number(cue.get("confidence"), f"{prefix}.confidence")
        if not min_confidence <= confidence <= 1.0:
            raise SoundPlanError(
                f"{prefix}.confidence must be in [{min_confidence:.2f}, 1]"
            )
        gain_db = _finite_number(cue.get("gain_db"), f"{prefix}.gain_db")
        if not -36.0 <= gain_db <= -3.0:
            raise SoundPlanError(f"{prefix}.gain_db must be in [-36, -3]")
        maximum = _finite_number(
            cue.get("max_duration_seconds"), f"{prefix}.max_duration_seconds"
        )
        if not 0.05 <= maximum <= 8.0:
            raise SoundPlanError(f"{prefix}.max_duration_seconds must be in [0.05, 8]")

        evidence = cue.get("visible_evidence")
        if not isinstance(evidence, Mapping):
            raise SoundPlanError(f"{prefix}.visible_evidence must be a mapping")
        _nonempty(evidence.get("description"), f"{prefix}.visible_evidence.description")
        _nonempty(evidence.get("method"), f"{prefix}.visible_evidence.method")
        if str(evidence.get("slot_id") or "") != slot_id:
            raise SoundPlanError(f"{prefix} evidence references another slot")
        clip_source_id = _nonempty(
            clip.get("source_id") or clip.get("source_path") or clip.get("path"),
            f"{prefix} selected clip source identity",
        )
        if str(evidence.get("source_id") or "") != clip_source_id:
            raise SoundPlanError(f"{prefix} evidence references another donor source")
        source_in = _finite_number(
            clip.get("source_in", clip.get("in")), f"{prefix} selected_clip.source_in"
        )
        source_out = _finite_number(
            clip.get("source_out", clip.get("out")), f"{prefix} selected_clip.source_out"
        )
        source_time = _finite_number(
            evidence.get("source_time"), f"{prefix}.visible_evidence.source_time"
        )
        if source_time < source_in - tolerance or source_time > source_out + tolerance:
            raise SoundPlanError(f"{prefix} evidence time is outside the donor range")

        provider = _nonempty(cue.get("provider"), f"{prefix}.provider")
        expected_keys = {sfx_search_cache_key(cue, schema_version=schema_version)}
        if schema_version == LEGACY_SOUND_PLAN_SCHEMA_VERSION:
            expected_keys.add(sfx_search_cache_key(cue))
        if cue.get("search_cache_key") not in expected_keys:
            raise SoundPlanError(f"{prefix} search_cache_key is stale")
        identity = _normalised_effect_identity(cue)
        if identity == previous_identity:
            raise SoundPlanError("the same SFX asset/query cannot repeat consecutively")
        asset = cue.get("asset")
        if asset is not None:
            validate_asset_provenance(asset, expected_provider=provider)
        elif require_assets:
            raise SoundPlanError(f"{prefix} has no resolved asset")
        previous_identity = identity
        previous_time = cue_time


def _validate_sound_plan_v1(
    plan: Mapping[str, Any],
    timeline: Mapping[str, Any],
    *,
    min_sfx_confidence: float = DEFAULT_MIN_SFX_CONFIDENCE,
    min_sfx_gap_seconds: float = DEFAULT_MIN_SFX_GAP_SECONDS,
    require_resolved_assets: bool | None = None,
) -> None:
    """Fail closed when a plan cannot be rendered or audited safely."""

    if not isinstance(plan, Mapping):
        raise SoundPlanError("sound plan must be a mapping")
    if plan.get("schema_version") != LEGACY_SOUND_PLAN_SCHEMA_VERSION:
        raise SoundPlanError(
            f"unsupported sound plan schema_version: {plan.get('schema_version')!r}"
        )
    slots, by_id, duration, tolerance = _timeline_slots(timeline)
    declared_duration = _finite_number(plan.get("timeline_duration"), "timeline_duration")
    if abs(declared_duration - duration) > tolerance:
        raise SoundPlanError("sound plan duration disagrees with timeline")
    expected_fingerprints = {
        timeline_fingerprint(
            timeline, schema_version=LEGACY_SOUND_PLAN_SCHEMA_VERSION
        ),
        # Compatibility for short-lived canary manifests emitted while the
        # implementation bundle was being upgraded in-place.
        timeline_fingerprint(timeline),
    }
    if plan.get("timeline_fingerprint") not in expected_fingerprints:
        raise SoundPlanError("sound plan was built for a different timeline")
    status = str(plan.get("status") or "")
    if status not in {"planned", "ready"}:
        raise SoundPlanError("sound plan status must be 'planned' or 'ready'")
    if require_resolved_assets is None:
        require_resolved_assets = status == "ready"
    if status == "ready" and not require_resolved_assets:
        raise SoundPlanError("ready plans cannot disable resolved-asset validation")
    min_confidence = _finite_number(min_sfx_confidence, "min_sfx_confidence")
    min_gap = _finite_number(min_sfx_gap_seconds, "min_sfx_gap_seconds")
    if not 0.0 <= min_confidence <= 1.0 or min_gap < 0:
        raise SoundPlanError("SFX validation thresholds are invalid")

    music = plan.get("music_segments")
    sfx = plan.get("sfx_cues", [])
    if not isinstance(music, Sequence) or isinstance(music, (str, bytes)):
        raise SoundPlanError("music_segments must be a list")
    if not isinstance(sfx, Sequence) or isinstance(sfx, (str, bytes)):
        raise SoundPlanError("sfx_cues must be a list")
    if any(not isinstance(item, Mapping) for item in music) or any(
        not isinstance(item, Mapping) for item in sfx
    ):
        raise SoundPlanError("sound plan entries must be mappings")

    _validate_music_segments(
        list(music),
        slots=slots,
        by_id=by_id,
        duration=duration,
        tolerance=tolerance,
        require_assets=bool(require_resolved_assets),
        schema_version=LEGACY_SOUND_PLAN_SCHEMA_VERSION,
    )
    _validate_sfx_cues(
        list(sfx),
        by_id=by_id,
        duration=duration,
        tolerance=tolerance,
        min_confidence=min_confidence,
        min_gap_seconds=min_gap,
        require_assets=bool(require_resolved_assets),
        schema_version=LEGACY_SOUND_PLAN_SCHEMA_VERSION,
    )

    core = copy.deepcopy(dict(plan))
    supplied_key = core.pop("plan_cache_key", None)
    expected_key = stable_cache_key(
        "sound_plan", core, version=LEGACY_SOUND_PLAN_SCHEMA_VERSION
    )
    if supplied_key != expected_key:
        raise SoundPlanError("sound plan cache key is stale")


def _build_sound_plan_v1(
    timeline: Mapping[str, Any],
    music_segments: Sequence[Mapping[str, Any]],
    sfx_cues: Sequence[Mapping[str, Any]] = (),
    *,
    status: str | None = None,
) -> dict[str, Any]:
    """Assemble, sort, key, and validate a versioned sound plan."""

    _slots, _by_id, duration, _tolerance = _timeline_slots(timeline)
    music = [copy.deepcopy(dict(item)) for item in music_segments]
    sfx = [copy.deepcopy(dict(item)) for item in sfx_cues]
    music.sort(key=lambda item: (float(item.get("start", 0.0)), str(item.get("segment_id"))))
    sfx.sort(key=lambda item: (float(item.get("time", 0.0)), str(item.get("cue_id"))))
    all_resolved = all(isinstance(item.get("asset"), Mapping) for item in music + sfx)
    if status is None:
        status = "ready" if all_resolved else "planned"
    plan: dict[str, Any] = {
        "schema_version": LEGACY_SOUND_PLAN_SCHEMA_VERSION,
        "status": str(status),
        "timeline_duration": round(duration, 6),
        "timeline_fingerprint": timeline_fingerprint(
            timeline, schema_version=LEGACY_SOUND_PLAN_SCHEMA_VERSION
        ),
        "music_segments": music,
        "sfx_cues": sfx,
    }
    plan["plan_cache_key"] = stable_cache_key(
        "sound_plan", plan, version=LEGACY_SOUND_PLAN_SCHEMA_VERSION
    )
    _validate_sound_plan_v1(plan, timeline)
    return plan


def _string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SoundPlanError(f"{name} must be a list")
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        text = _nonempty(item, f"{name}[{index}]")
        folded = text.casefold()
        if folded not in seen:
            result.append(text)
            seen.add(folded)
    return result


def _slot_source_identity(slot: Mapping[str, Any], slot_id: str) -> str:
    clip = _selected_clip(slot, slot_id)
    return _nonempty(
        clip.get("source_id") or clip.get("source_path") or clip.get("path"),
        f"timeline slot {slot_id} selected clip source identity",
    )


def _normalise_visual_evidence_v2(
    value: str | Mapping[str, Any],
    *,
    slot_ids: Sequence[str],
    by_id: Mapping[str, Mapping[str, Any]],
    confidence: float,
    method: str = "eight_frame_motion_vision",
) -> dict[str, Any]:
    if isinstance(value, Mapping):
        evidence = copy.deepcopy(dict(value))
        evidence["description"] = _nonempty(
            evidence.get("description"), "visual_evidence.description"
        )
    else:
        evidence = {"description": _nonempty(value, "visual_evidence.description")}
    expected_ids = [_nonempty(item, "visual evidence slot_id") for item in slot_ids]
    supplied_ids = evidence.get("slot_ids")
    if supplied_ids is not None and _string_list(
        supplied_ids, "visual_evidence.slot_ids"
    ) != expected_ids:
        raise SoundPlanError("visual_evidence.slot_ids conflict with layer interval")
    evidence["slot_ids"] = expected_ids
    evidence["method"] = _nonempty(
        evidence.get("method") or method, "visual_evidence.method"
    )
    evidence["confidence"] = _finite_number(
        evidence.get("confidence", confidence), "visual_evidence.confidence"
    )
    if not 0.0 <= evidence["confidence"] <= 1.0:
        raise SoundPlanError("visual_evidence.confidence must be in [0, 1]")
    sampled_frames = evidence.get("sampled_frames", 8)
    if isinstance(sampled_frames, bool) or not isinstance(sampled_frames, int):
        raise SoundPlanError("visual_evidence.sampled_frames must be an integer")
    if sampled_frames < 8:
        raise SoundPlanError("production-v2 visual evidence requires at least 8 frames")
    evidence["sampled_frames"] = sampled_frames
    if evidence.get("motion_preview", True) is not True:
        raise SoundPlanError("production-v2 visual evidence requires a motion preview")
    evidence["motion_preview"] = True

    expected_sources = {
        slot_id: _slot_source_identity(by_id[slot_id], slot_id)
        for slot_id in expected_ids
    }
    supplied_sources = evidence.get("source_ids")
    if supplied_sources is not None:
        if not isinstance(supplied_sources, Mapping):
            raise SoundPlanError("visual_evidence.source_ids must be a mapping")
        if {str(key): str(item) for key, item in supplied_sources.items()} != expected_sources:
            raise SoundPlanError("visual_evidence.source_ids conflict with selected clips")
    evidence["source_ids"] = expected_sources
    return evidence


def _normalise_taxonomy_v2(
    value: Mapping[str, Any],
    *,
    lane: str,
    confidence: float,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SoundPlanError(f"{lane} taxonomy must be a mapping")
    taxonomy = copy.deepcopy(dict(value))
    taxonomy["source_kind"] = _nonempty(
        taxonomy.get("source_kind") or ("environment" if lane == "ambience" else None),
        f"{lane} taxonomy.source_kind",
    ).casefold()
    for key in ("family", "species", "action", "environment"):
        if taxonomy.get(key) is not None:
            taxonomy[key] = _nonempty(taxonomy[key], f"{lane} taxonomy.{key}").casefold()
    specificity = str(taxonomy.get("specificity") or "").strip().casefold()
    if not specificity:
        if taxonomy.get("species"):
            specificity = "exact_species"
        elif taxonomy.get("family"):
            specificity = "family"
        elif taxonomy.get("action"):
            specificity = "neutral_action"
        else:
            specificity = "environment"
    if specificity not in {
        "exact_species",
        "family",
        "neutral_action",
        "environment",
    }:
        raise SoundPlanError(f"unsupported {lane} taxonomy specificity: {specificity}")
    if specificity == "exact_species":
        if not taxonomy.get("species"):
            raise SoundPlanError("exact_species taxonomy requires species")
        if confidence < DEFAULT_MIN_EXACT_SPECIES_CONFIDENCE:
            raise SoundPlanError(
                "exact species sound requires visual confidence >= "
                f"{DEFAULT_MIN_EXACT_SPECIES_CONFIDENCE:.2f}"
            )
    taxonomy["specificity"] = specificity
    return taxonomy


def _slot_ids_for_range(
    timeline: Mapping[str, Any], start_slot_id: str, end_slot_id: str
) -> tuple[list[str], Mapping[str, Mapping[str, Any]]]:
    slots, by_id, _duration, _tolerance = _timeline_slots(timeline)
    positions = {str(slot["slot_id"]): index for index, slot in enumerate(slots)}
    if start_slot_id not in positions or end_slot_id not in positions:
        raise SoundPlanError("natural-sound layer references an unknown slot")
    start_index, end_index = positions[start_slot_id], positions[end_slot_id]
    if end_index < start_index:
        raise SoundPlanError("natural-sound layer reverses its slot range")
    return [str(slot["slot_id"]) for slot in slots[start_index : end_index + 1]], by_id


def natural_sound_search_cache_key(layer: Mapping[str, Any]) -> str:
    """Deterministic retrieval identity without asset/license state."""

    return stable_cache_key(
        "natural_sound_search",
        {
            "lane": layer.get("lane"),
            "provider": layer.get("provider"),
            "query_en": str(layer.get("query_en") or "").strip().casefold(),
            "taxonomy": layer.get("taxonomy"),
            "hard_negatives": layer.get("hard_negatives", []),
            "duration": round(
                _finite_number(layer.get("duration", 0.0), "natural layer duration"),
                3,
            ),
        },
        version=SOUND_PLAN_SCHEMA_VERSION,
    )


def make_ambience_region(
    timeline: Mapping[str, Any],
    *,
    start_slot_id: str,
    end_slot_id: str,
    environment: Mapping[str, Any],
    query_en: str,
    visual_evidence: str | Mapping[str, Any],
    confidence: float,
    hard_negatives: Sequence[str] = (),
    provider: str = "local_licensed",
    gain_db: float = -24.0,
    crossfade_seconds: float = 0.4,
    asset: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a slot-aligned bed. Adjacent beds must later cover the timeline exactly."""

    start_slot_id = _nonempty(start_slot_id, "start_slot_id")
    end_slot_id = _nonempty(end_slot_id, "end_slot_id")
    slot_ids, by_id = _slot_ids_for_range(timeline, start_slot_id, end_slot_id)
    if not isinstance(environment, Mapping):
        raise SoundPlanError("environment must be a mapping")
    environment_copy = copy.deepcopy(dict(environment))
    environment_copy["location_type"] = _nonempty(
        environment_copy.get("location_type"), "environment.location_type"
    ).casefold()
    for key in ("weather", "time_of_day", "water", "wind", "perspective"):
        if environment_copy.get(key) is not None:
            environment_copy[key] = str(environment_copy[key]).strip().casefold()
    confidence_value = _finite_number(confidence, "ambience confidence")
    if not 0.0 <= confidence_value <= 1.0:
        raise SoundPlanError("ambience confidence must be in [0, 1]")
    start, end = float(by_id[start_slot_id]["start"]), float(by_id[end_slot_id]["end"])
    result: dict[str, Any] = {
        "lane": "ambience",
        "start_slot_id": start_slot_id,
        "end_slot_id": end_slot_id,
        "slot_ids": slot_ids,
        "start": round(start, 6),
        "end": round(end, 6),
        "duration": round(end - start, 6),
        "environment": environment_copy,
        "taxonomy": {
            "source_kind": "environment",
            "environment": environment_copy["location_type"],
            "specificity": "environment",
        },
        "query_en": _nonempty(query_en, "ambience query_en"),
        "visual_evidence": _normalise_visual_evidence_v2(
            visual_evidence,
            slot_ids=slot_ids,
            by_id=by_id,
            confidence=confidence_value,
        ),
        "confidence": confidence_value,
        "hard_negatives": _string_list(hard_negatives, "hard_negatives"),
        "provider": _nonempty(provider, "ambience provider"),
        "gain_db": _finite_number(gain_db, "ambience gain_db"),
        "crossfade_seconds": _finite_number(
            crossfade_seconds, "ambience crossfade_seconds"
        ),
    }
    if asset is not None:
        result["asset"] = copy.deepcopy(dict(asset))
    identity = {key: value for key, value in result.items() if key != "asset"}
    result["region_id"] = "amb-" + stable_cache_key(
        "ambience_region", identity, version=SOUND_PLAN_SCHEMA_VERSION
    ).rsplit(":", 1)[-1][:12]
    result["search_cache_key"] = natural_sound_search_cache_key(result)
    return result


def make_presence_layer(
    timeline: Mapping[str, Any],
    *,
    slot_id: str | None = None,
    start_slot_id: str | None = None,
    end_slot_id: str | None = None,
    query_en: str,
    visual_evidence: str | Mapping[str, Any],
    taxonomy: Mapping[str, Any],
    confidence: float,
    hard_negatives: Sequence[str] = (),
    start_offset_seconds: float = 0.0,
    end_offset_seconds: float | None = None,
    provider: str = "local_licensed",
    gain_db: float = -21.0,
    distance: str = "medium",
    horizontal_position: float = 0.0,
    asset: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a sustained audible-presence layer from visible evidence."""

    if slot_id is not None:
        if start_slot_id is not None or end_slot_id is not None:
            raise SoundPlanError("use slot_id or start/end slot ids, not both")
        start_slot_id = end_slot_id = _nonempty(slot_id, "slot_id")
    start_slot_id = _nonempty(start_slot_id, "start_slot_id")
    end_slot_id = _nonempty(end_slot_id or start_slot_id, "end_slot_id")
    slot_ids, by_id = _slot_ids_for_range(timeline, start_slot_id, end_slot_id)
    confidence_value = _finite_number(confidence, "presence confidence")
    if not 0.0 <= confidence_value <= 1.0:
        raise SoundPlanError("presence confidence must be in [0, 1]")
    range_start = float(by_id[start_slot_id]["start"])
    range_end = float(by_id[end_slot_id]["end"])
    start_offset = _finite_number(start_offset_seconds, "presence start_offset_seconds")
    end_offset = (
        range_end - range_start
        if end_offset_seconds is None
        else _finite_number(end_offset_seconds, "presence end_offset_seconds")
    )
    start, end = range_start + start_offset, range_start + end_offset
    if start < range_start or end > range_end or end <= start:
        raise SoundPlanError("presence sync interval is outside its slot range")
    distance_value = _nonempty(distance, "presence distance").casefold()
    if distance_value not in NATURAL_SOUND_DISTANCES:
        raise SoundPlanError(f"unsupported presence distance: {distance_value}")
    pan_value = _finite_number(horizontal_position, "presence horizontal_position")
    if not -1.0 <= pan_value <= 1.0:
        raise SoundPlanError("presence horizontal_position must be in [-1, 1]")
    result: dict[str, Any] = {
        "lane": "presence",
        "start_slot_id": start_slot_id,
        "end_slot_id": end_slot_id,
        "slot_ids": slot_ids,
        "start": round(start, 6),
        "end": round(end, 6),
        "duration": round(end - start, 6),
        "sync_interval": {"start": round(start, 6), "end": round(end, 6)},
        "query_en": _nonempty(query_en, "presence query_en"),
        "taxonomy": _normalise_taxonomy_v2(
            taxonomy, lane="presence", confidence=confidence_value
        ),
        "visual_evidence": _normalise_visual_evidence_v2(
            visual_evidence,
            slot_ids=slot_ids,
            by_id=by_id,
            confidence=confidence_value,
        ),
        "confidence": confidence_value,
        "hard_negatives": _string_list(hard_negatives, "hard_negatives"),
        "provider": _nonempty(provider, "presence provider"),
        "gain_db": _finite_number(gain_db, "presence gain_db"),
        "perspective": {
            "distance": distance_value,
            "horizontal_position": pan_value,
        },
    }
    if asset is not None:
        result["asset"] = copy.deepcopy(dict(asset))
    identity = {key: value for key, value in result.items() if key != "asset"}
    result["layer_id"] = "pre-" + stable_cache_key(
        "presence_layer", identity, version=SOUND_PLAN_SCHEMA_VERSION
    ).rsplit(":", 1)[-1][:12]
    result["search_cache_key"] = natural_sound_search_cache_key(result)
    return result


def make_event_cue(
    timeline: Mapping[str, Any],
    *,
    slot_id: str,
    offset_seconds: float,
    duration_seconds: float,
    query_en: str,
    visual_evidence: str | Mapping[str, Any],
    taxonomy: Mapping[str, Any],
    confidence: float,
    hard_negatives: Sequence[str] = (),
    provider: str = "local_licensed",
    gain_db: float = -16.0,
    distance: str = "medium",
    horizontal_position: float = 0.0,
    asset: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a frame-synchronised event. Multiple cues per slot are valid."""

    slot_id = _nonempty(slot_id, "slot_id")
    _slots, by_id, _duration, tolerance = _timeline_slots(timeline)
    if slot_id not in by_id:
        raise SoundPlanError(f"event cue references unknown slot {slot_id}")
    confidence_value = _finite_number(confidence, "event confidence")
    if not 0.0 <= confidence_value <= 1.0:
        raise SoundPlanError("event confidence must be in [0, 1]")
    offset = _finite_number(offset_seconds, "event offset_seconds")
    event_duration = _finite_number(duration_seconds, "event duration_seconds")
    slot = by_id[slot_id]
    slot_start, slot_end = float(slot["start"]), float(slot["end"])
    start, end = slot_start + offset, slot_start + offset + event_duration
    if event_duration <= 0 or event_duration > 8.0:
        raise SoundPlanError("event duration_seconds must be in (0, 8]")
    if start < slot_start - tolerance or end > slot_end + tolerance:
        raise SoundPlanError("event sync interval is outside its slot")
    distance_value = _nonempty(distance, "event distance").casefold()
    if distance_value not in NATURAL_SOUND_DISTANCES:
        raise SoundPlanError(f"unsupported event distance: {distance_value}")
    pan_value = _finite_number(horizontal_position, "event horizontal_position")
    if not -1.0 <= pan_value <= 1.0:
        raise SoundPlanError("event horizontal_position must be in [-1, 1]")
    result: dict[str, Any] = {
        "lane": "events",
        "slot_id": slot_id,
        "slot_ids": [slot_id],
        "offset_seconds": round(offset, 6),
        "start": round(start, 6),
        "end": round(end, 6),
        "duration": round(event_duration, 6),
        "sync_interval": {"start": round(start, 6), "end": round(end, 6)},
        "query_en": _nonempty(query_en, "event query_en"),
        "taxonomy": _normalise_taxonomy_v2(
            taxonomy, lane="events", confidence=confidence_value
        ),
        "visual_evidence": _normalise_visual_evidence_v2(
            visual_evidence,
            slot_ids=[slot_id],
            by_id=by_id,
            confidence=confidence_value,
        ),
        "confidence": confidence_value,
        "hard_negatives": _string_list(hard_negatives, "hard_negatives"),
        "provider": _nonempty(provider, "event provider"),
        "gain_db": _finite_number(gain_db, "event gain_db"),
        "perspective": {
            "distance": distance_value,
            "horizontal_position": pan_value,
        },
    }
    if asset is not None:
        result["asset"] = copy.deepcopy(dict(asset))
    identity = {key: value for key, value in result.items() if key != "asset"}
    result["cue_id"] = "evt-" + stable_cache_key(
        "event_cue", identity, version=SOUND_PLAN_SCHEMA_VERSION
    ).rsplit(":", 1)[-1][:12]
    result["search_cache_key"] = natural_sound_search_cache_key(result)
    return result


def _asset_taxonomy(asset: Mapping[str, Any]) -> Mapping[str, Any] | None:
    direct = asset.get("taxonomy")
    if isinstance(direct, Mapping):
        return direct
    provenance = asset.get("provenance")
    if isinstance(provenance, Mapping):
        nested = provenance.get("taxonomy")
        if isinstance(nested, Mapping):
            return nested
    return None


_HARD_NEGATIVE_TAXONOMY_SCALAR_FIELDS = (
    "source_kind",
    "family",
    "species",
    "action",
    "environment",
    "distance",
)


def _positive_taxonomy_text(taxonomy: Mapping[str, Any]) -> str:
    """Return only asserted asset identity, never classifier prompt-bank keys."""

    values = [
        value.strip()
        for key in _HARD_NEGATIVE_TAXONOMY_SCALAR_FIELDS
        if isinstance((value := taxonomy.get(key)), str) and value.strip()
    ]
    labels = taxonomy.get("labels")
    if isinstance(labels, Sequence) and not isinstance(labels, (str, bytes)):
        values.extend(str(value).strip() for value in labels if str(value).strip())
    return " ".join(values).casefold()


def _validate_taxonomy_match(
    planned: Mapping[str, Any], asset: Mapping[str, Any], *, prefix: str
) -> None:
    actual = _asset_taxonomy(asset)
    if actual is None:
        # Provenance is still mandatory; a missing classifier result is handled
        # by sound QA and blocks production readiness below.
        raise SoundPlanError(f"{prefix} asset has no taxonomy classification")
    for key in ("species", "family"):
        expected = str(planned.get(key) or "").strip().casefold()
        got = str(actual.get(key) or "").strip().casefold()
        if expected and got and expected != got:
            raise SoundPlanError(f"{prefix} asset has wrong {key}: {got!r}")
        if expected and not got and planned.get("specificity") in {"exact_species", "family"}:
            raise SoundPlanError(f"{prefix} asset cannot prove requested {key}")
    expected_action = str(planned.get("action") or "").strip().casefold()
    got_action = str(actual.get("action") or "").strip().casefold()
    if expected_action and got_action and expected_action != got_action:
        raise SoundPlanError(f"{prefix} asset has wrong action: {got_action!r}")
    if expected_action and not got_action and planned.get("specificity") != "environment":
        raise SoundPlanError(f"{prefix} asset cannot prove requested action")


def _validate_natural_layer_common(
    layer: Mapping[str, Any],
    *,
    lane: str,
    prefix: str,
    by_id: Mapping[str, Mapping[str, Any]],
    duration: float,
    tolerance: float,
    require_assets: bool,
) -> None:
    if layer.get("lane") != lane:
        raise SoundPlanError(f"{prefix}.lane must be {lane!r}")
    start = _finite_number(layer.get("start"), f"{prefix}.start")
    end = _finite_number(layer.get("end"), f"{prefix}.end")
    declared = _finite_number(layer.get("duration"), f"{prefix}.duration")
    if start < -tolerance or end <= start or end > duration + tolerance:
        raise SoundPlanError(f"{prefix} interval is outside timeline")
    if abs((end - start) - declared) > tolerance:
        raise SoundPlanError(f"{prefix}.duration disagrees with start/end")
    slot_ids = _string_list(layer.get("slot_ids"), f"{prefix}.slot_ids")
    if not slot_ids or any(slot_id not in by_id for slot_id in slot_ids):
        raise SoundPlanError(f"{prefix} references an unknown slot")
    confidence = _finite_number(layer.get("confidence"), f"{prefix}.confidence")
    if not 0.0 <= confidence <= 1.0:
        raise SoundPlanError(f"{prefix}.confidence must be in [0, 1]")
    taxonomy = _normalise_taxonomy_v2(
        layer.get("taxonomy"), lane=lane, confidence=confidence
    )
    hard_negatives = _string_list(
        layer.get("hard_negatives"), f"{prefix}.hard_negatives"
    )
    _nonempty(layer.get("query_en"), f"{prefix}.query_en")
    _normalise_visual_evidence_v2(
        layer.get("visual_evidence"),
        slot_ids=slot_ids,
        by_id=by_id,
        confidence=confidence,
    )
    provider = _nonempty(layer.get("provider"), f"{prefix}.provider")
    gain = _finite_number(layer.get("gain_db"), f"{prefix}.gain_db")
    required_physical_foley = (
        layer.get("required") is True
        and isinstance(layer.get("physical_foley_obligation"), Mapping)
        and isinstance(layer.get("required_audibility"), Mapping)
    )
    gain_ceiling = 12.0 if required_physical_foley else -3.0
    if not -48.0 <= gain <= gain_ceiling:
        raise SoundPlanError(
            f"{prefix}.gain_db must be in [-48, {gain_ceiling:g}]"
        )
    if lane in {"presence", "events"}:
        perspective = layer.get("perspective")
        if not isinstance(perspective, Mapping):
            raise SoundPlanError(f"{prefix}.perspective must be a mapping")
        distance = _nonempty(
            perspective.get("distance"), f"{prefix}.perspective.distance"
        ).casefold()
        if distance not in NATURAL_SOUND_DISTANCES:
            raise SoundPlanError(f"{prefix} has unsupported perspective distance")
        horizontal = _finite_number(
            perspective.get("horizontal_position"),
            f"{prefix}.perspective.horizontal_position",
        )
        if not -1.0 <= horizontal <= 1.0:
            raise SoundPlanError(f"{prefix} horizontal position must be in [-1, 1]")
    expected_key = natural_sound_search_cache_key(layer)
    if layer.get("search_cache_key") != expected_key:
        raise SoundPlanError(f"{prefix}.search_cache_key is stale")
    asset = layer.get("asset")
    if asset is not None:
        validate_asset_provenance(asset, expected_provider=provider)
        _validate_taxonomy_match(taxonomy, asset, prefix=prefix)
        actual = _asset_taxonomy(asset) or {}
        searchable = _positive_taxonomy_text(actual)
        for negative in hard_negatives:
            if negative.casefold() in searchable:
                raise SoundPlanError(f"{prefix} asset matches hard negative {negative!r}")
    elif require_assets:
        raise SoundPlanError(f"{prefix} has no resolved asset")
    if require_assets:
        retrieval = layer.get("retrieval")
        if not isinstance(retrieval, Mapping) or retrieval.get("status") != "resolved":
            raise SoundPlanError(f"{prefix} has no resolved retrieval receipt")
        degradation = _nonempty(
            retrieval.get("degradation_level"),
            f"{prefix}.retrieval.degradation_level",
        )
        if degradation not in {
            "exact_species",
            "family",
            "neutral_action",
            "environment",
        }:
            raise SoundPlanError(f"{prefix} has invalid retrieval degradation level")
        if not retrieval.get("index_fingerprint") and not retrieval.get(
            "provider_search_receipt"
        ):
            raise SoundPlanError(f"{prefix} retrieval has no index/provider receipt")
        retriever = retrieval.get("retriever")
        if not isinstance(retriever, Mapping):
            raise SoundPlanError(f"{prefix} retrieval has no retriever version")
        _nonempty(retriever.get("version"), f"{prefix}.retrieval.retriever.version")
        if not isinstance(retrieval.get("candidate_audit"), Sequence) or isinstance(
            retrieval.get("candidate_audit"), (str, bytes)
        ):
            raise SoundPlanError(f"{prefix} retrieval candidate_audit must be a list")
        _nonempty(retrieval.get("retrieval_key"), f"{prefix}.retrieval.retrieval_key")


def _resolved_ambience_for_slot(
    ambience: Sequence[Mapping[str, Any]], slot_id: str
) -> list[tuple[str, str]]:
    matches: list[tuple[str, str]] = []
    for region in ambience:
        if slot_id not in {str(item) for item in region.get("slot_ids", [])}:
            continue
        retrieval = region.get("retrieval")
        asset = region.get("asset")
        if (
            not isinstance(retrieval, Mapping)
            or retrieval.get("status") != "resolved"
            or retrieval.get("degradation_level") != "environment"
            or not isinstance(asset, Mapping)
        ):
            continue
        provenance = asset.get("provenance")
        asset_id = str(
            asset.get("asset_id")
            or (provenance.get("asset_id") if isinstance(provenance, Mapping) else "")
            or ""
        )
        region_id = str(region.get("region_id") or "")
        if region_id and asset_id:
            matches.append((region_id, asset_id))
    return sorted(set(matches))


def _validate_foreground_disposition(
    item: Mapping[str, Any],
    *,
    lane: str,
    slot_id: str,
    source_id_key: str,
    audible_key: str,
    ambience: Sequence[Mapping[str, Any]],
    catalog_fingerprint: str,
) -> None:
    requested = item.get("audibility_requested") is True
    disposition = item.get("sound_disposition")
    if not requested:
        if disposition is not None:
            raise SoundPlanError(
                f"{lane} source {item.get(source_id_key)!r} has an unrequested disposition"
            )
        return
    if item.get(audible_key) is not False:
        raise SoundPlanError(
            f"{lane} source {item.get(source_id_key)!r} disposition requires disabled audibility"
        )
    if not isinstance(disposition, Mapping):
        raise SoundPlanError(
            f"removed notable {lane} source {item.get(source_id_key)!r} has no disposition"
        )
    if disposition.get("schema_version") != FOREGROUND_SOUND_DISPOSITION_SCHEMA_VERSION:
        raise SoundPlanError("foreground sound disposition has unsupported schema")
    if disposition.get("outcome") != "covered_by_exact_ambience":
        raise SoundPlanError("foreground sound disposition has unsafe outcome")
    if disposition.get("lane") != lane:
        raise SoundPlanError("foreground sound disposition lane is stale")
    source_id = _nonempty(item.get(source_id_key), f"{lane} source id")
    if str(disposition.get("visual_source_id") or "") != source_id:
        raise SoundPlanError("foreground sound disposition source is stale")
    disposition_slots = {
        str(value) for value in disposition.get("slot_ids", []) if str(value)
    }
    if slot_id not in disposition_slots:
        raise SoundPlanError("foreground sound disposition does not cover its scene slot")
    _nonempty(disposition.get("original_layer_id"), "disposition.original_layer_id")
    if _nonempty(
        disposition.get("catalog_fingerprint"), "disposition.catalog_fingerprint"
    ) != _nonempty(catalog_fingerprint, "resolution_diagnostics.catalog_fingerprint"):
        raise SoundPlanError("foreground sound disposition catalog fingerprint is stale")
    _nonempty(disposition.get("reason_code"), "disposition.reason_code")
    if disposition.get("wrong_species_used") is not False:
        raise SoundPlanError("foreground sound disposition must prove wrong_species_used=false")
    taxonomy = disposition.get("requested_taxonomy")
    if not isinstance(taxonomy, Mapping):
        raise SoundPlanError("foreground sound disposition requires requested_taxonomy")
    for key in ("source_kind", "family", "species", "action", "specificity"):
        if str(taxonomy.get(key) or "").casefold() != str(item.get(key) or "").casefold():
            raise SoundPlanError(
                f"foreground sound disposition requested taxonomy conflicts on {key}"
            )
    exact_ambience = _resolved_ambience_for_slot(ambience, slot_id)
    if not exact_ambience:
        raise SoundPlanError(
            "foreground sound disposition is not covered by resolved exact ambience"
        )
    declared_regions = {str(value) for value in disposition.get("ambience_region_ids", [])}
    declared_assets = {str(value) for value in disposition.get("ambience_asset_ids", [])}
    if not {region_id for region_id, _asset_id in exact_ambience}.issubset(
        declared_regions
    ) or not {asset_id for _region_id, asset_id in exact_ambience}.issubset(
        declared_assets
    ):
        raise SoundPlanError("foreground sound disposition ambience receipt is stale")
    if disposition.get("disposition_key") != foreground_sound_disposition_cache_key(
        disposition
    ):
        raise SoundPlanError("foreground sound disposition cache key is stale")


def _validate_foreground_dispositions(
    acoustic_scenes: Sequence[Mapping[str, Any]],
    ambience: Sequence[Mapping[str, Any]],
    *,
    catalog_fingerprint: str,
) -> None:
    for scene in acoustic_scenes:
        slot_id = str(scene.get("slot_id") or "")
        for source in scene.get("visible_sources", []):
            if isinstance(source, Mapping):
                _validate_foreground_disposition(
                    source,
                    lane="presence",
                    slot_id=slot_id,
                    source_id_key="source_id",
                    audible_key="audible_presence",
                    ambience=ambience,
                    catalog_fingerprint=catalog_fingerprint,
                )
        for event in scene.get("events", []):
            if isinstance(event, Mapping):
                _validate_foreground_disposition(
                    event,
                    lane="events",
                    slot_id=slot_id,
                    source_id_key="event_id",
                    audible_key="audible_event",
                    ambience=ambience,
                    catalog_fingerprint=catalog_fingerprint,
                )


def _validate_sound_plan_v2(
    plan: Mapping[str, Any],
    timeline: Mapping[str, Any],
    *,
    require_resolved_assets: bool | None = None,
    require_causal_compilation: bool = False,
) -> None:
    if plan.get("schema_version") != SOUND_PLAN_SCHEMA_VERSION:
        raise SoundPlanError(
            f"unsupported sound plan schema_version: {plan.get('schema_version')!r}"
        )
    slots, by_id, duration, tolerance = _timeline_slots(timeline)
    declared_duration = _finite_number(plan.get("timeline_duration"), "timeline_duration")
    if abs(declared_duration - duration) > tolerance:
        raise SoundPlanError("sound plan duration disagrees with timeline")
    if plan.get("timeline_fingerprint") != timeline_fingerprint(timeline):
        raise SoundPlanError("sound plan was built for a different timeline")
    status = str(plan.get("status") or "")
    if status not in {"planned", "needs_sound", "ready"}:
        raise SoundPlanError(
            "production-v2 status must be 'planned', 'needs_sound', or 'ready'"
        )
    if require_resolved_assets is None:
        require_resolved_assets = status == "ready"
    if status == "ready" and not require_resolved_assets:
        raise SoundPlanError("ready plans cannot disable resolved-asset validation")

    lane_names = (
        "music_segments",
        "ambience_regions",
        "presence_layers",
        "event_cues",
    )
    lanes: dict[str, list[Mapping[str, Any]]] = {}
    for name in lane_names:
        raw = plan.get(name, [])
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise SoundPlanError(f"{name} must be a list")
        if any(not isinstance(item, Mapping) for item in raw):
            raise SoundPlanError(f"{name} entries must be mappings")
        lanes[name] = list(raw)

    acoustic_scenes = plan.get("acoustic_scenes", [])
    if not isinstance(acoustic_scenes, Sequence) or isinstance(
        acoustic_scenes, (str, bytes)
    ):
        raise SoundPlanError("acoustic_scenes must be a list")
    if acoustic_scenes:
        # Local import avoids a module cycle: acoustic_scene constructors use
        # the sound-plan lane helpers defined above.
        from .acoustic_scene import (
            CAUSAL_RECONSTRUCTION_POLICY_VERSION,
            AcousticSceneError,
            validate_acoustic_scenes,
        )

        try:
            validate_acoustic_scenes(
                list(acoustic_scenes),
                timeline,
                require_compiled=require_causal_compilation,
            )
        except AcousticSceneError as exc:
            raise SoundPlanError(f"causal acoustic scene validation failed: {exc}") from exc
        if require_causal_compilation and plan.get(
            "causal_reconstruction_policy_version"
        ) != CAUSAL_RECONSTRUCTION_POLICY_VERSION:
            raise SoundPlanError("sound plan lacks current causal reconstruction proof")
    elif require_resolved_assets:
        raise SoundPlanError(
            "production-v2 ready plan requires one acoustic scene per slot"
        )

    _validate_music_segments(
        lanes["music_segments"],
        slots=slots,
        by_id=by_id,
        duration=duration,
        tolerance=tolerance,
        require_assets=bool(require_resolved_assets),
        schema_version=SOUND_PLAN_SCHEMA_VERSION,
    )

    ambience = lanes["ambience_regions"]
    if not ambience:
        raise SoundPlanError("production-v2 requires ambience_regions")
    previous_end = 0.0
    ids: set[str] = set()
    positions = {str(slot["slot_id"]): index for index, slot in enumerate(slots)}
    for index, region in enumerate(ambience):
        prefix = f"ambience_regions[{index}]"
        region_id = _nonempty(region.get("region_id"), f"{prefix}.region_id")
        if region_id in ids:
            raise SoundPlanError(f"duplicate ambience region_id: {region_id}")
        ids.add(region_id)
        _validate_natural_layer_common(
            region,
            lane="ambience",
            prefix=prefix,
            by_id=by_id,
            duration=duration,
            tolerance=tolerance,
            require_assets=bool(require_resolved_assets),
        )
        start_slot_id = _nonempty(
            region.get("start_slot_id"), f"{prefix}.start_slot_id"
        )
        end_slot_id = _nonempty(region.get("end_slot_id"), f"{prefix}.end_slot_id")
        if start_slot_id not in by_id or end_slot_id not in by_id:
            raise SoundPlanError(f"{prefix} references unknown slot bounds")
        if positions[start_slot_id] > positions[end_slot_id]:
            raise SoundPlanError(f"{prefix} reverses slot bounds")
        start, end = float(region["start"]), float(region["end"])
        if abs(start - float(by_id[start_slot_id]["start"])) > tolerance:
            raise SoundPlanError(f"{prefix}.start is not slot-aligned")
        if abs(end - float(by_id[end_slot_id]["end"])) > tolerance:
            raise SoundPlanError(f"{prefix}.end is not slot-aligned")
        if abs(start - previous_end) > AUDIO_FRAME_TOLERANCE_SECONDS:
            raise SoundPlanError(
                f"ambience coverage gap/overlap before {region_id}: "
                f"{start - previous_end:+.6f}s"
            )
        crossfade = _finite_number(
            region.get("crossfade_seconds"), f"{prefix}.crossfade_seconds"
        )
        if not 0.08 <= crossfade <= 0.8:
            raise SoundPlanError(f"{prefix}.crossfade_seconds must be in [0.08, 0.8]")
        environment = region.get("environment")
        if not isinstance(environment, Mapping):
            raise SoundPlanError(f"{prefix}.environment must be a mapping")
        _nonempty(environment.get("location_type"), f"{prefix}.environment.location_type")
        previous_end = end
    if abs(previous_end - duration) > AUDIO_FRAME_TOLERANCE_SECONDS:
        raise SoundPlanError("ambience must cover [0, timeline_duration) exactly")

    if acoustic_scenes:
        diagnostics = plan.get("resolution_diagnostics")
        catalog_fingerprint = (
            str(diagnostics.get("catalog_fingerprint") or "")
            if isinstance(diagnostics, Mapping)
            else ""
        )
        _validate_foreground_dispositions(
            acoustic_scenes,
            ambience,
            catalog_fingerprint=catalog_fingerprint,
        )

    for lane_name, lane_value, id_key, lane_id in (
        ("presence_layers", lanes["presence_layers"], "layer_id", "presence"),
        ("event_cues", lanes["event_cues"], "cue_id", "events"),
    ):
        seen: set[str] = set()
        for index, layer in enumerate(lane_value):
            prefix = f"{lane_name}[{index}]"
            item_id = _nonempty(layer.get(id_key), f"{prefix}.{id_key}")
            if item_id in seen:
                raise SoundPlanError(f"duplicate {lane_name} id: {item_id}")
            seen.add(item_id)
            _validate_natural_layer_common(
                layer,
                lane=lane_id,
                prefix=prefix,
                by_id=by_id,
                duration=duration,
                tolerance=tolerance,
                require_assets=bool(require_resolved_assets),
            )
            sync = layer.get("sync_interval")
            if not isinstance(sync, Mapping):
                raise SoundPlanError(f"{prefix}.sync_interval must be a mapping")
            if abs(_finite_number(sync.get("start"), f"{prefix}.sync start") - float(layer["start"])) > tolerance:
                raise SoundPlanError(f"{prefix}.sync_interval start is stale")
            if abs(_finite_number(sync.get("end"), f"{prefix}.sync end") - float(layer["end"])) > tolerance:
                raise SoundPlanError(f"{prefix}.sync_interval end is stale")

    if acoustic_scenes:
        expected_presence: dict[tuple[str, str], tuple[float, float, Mapping[str, Any]]] = {}
        expected_events: dict[tuple[str, str], tuple[float, float, Mapping[str, Any]]] = {}
        for scene in acoustic_scenes:
            scene_slot_id = str(scene.get("slot_id") or "")
            scene_slot = by_id[scene_slot_id]
            scene_start = float(scene_slot["start"])
            scene_duration = float(scene_slot["end"]) - scene_start
            for source in scene.get("visible_sources", []):
                if source.get("audible_presence"):
                    identity = (
                        scene_slot_id,
                        str(source.get("source_id") or ""),
                    )
                    if identity in expected_presence:
                        raise SoundPlanError(
                            f"duplicate audible visual source identity: {identity}"
                        )
                    expected_presence[identity] = (
                        scene_start
                        + scene_duration * float(source.get("start_fraction", 0.0)),
                        scene_start
                        + scene_duration * float(source.get("end_fraction", 1.0)),
                        source,
                    )
            for event in scene.get("events", []):
                if event.get("audible_event"):
                    identity = (
                        scene_slot_id,
                        str(event.get("event_id") or ""),
                    )
                    if identity in expected_events:
                        raise SoundPlanError(
                            f"duplicate audible event identity: {identity}"
                        )
                    expected_events[identity] = (
                        scene_start
                        + scene_duration * float(event.get("offset_fraction", 0.0)),
                        scene_start
                        + scene_duration
                        * (
                            float(event.get("offset_fraction", 0.0))
                            + float(event.get("duration_fraction", 0.0))
                        ),
                        event,
                    )
        actual_presence: dict[tuple[str, str], Mapping[str, Any]] = {}
        for layer in lanes["presence_layers"]:
            for slot_id in layer.get("slot_ids", []):
                identity = (
                    str(slot_id),
                    str(layer.get("visual_source_id") or ""),
                )
                if identity in actual_presence:
                    raise SoundPlanError(
                        f"duplicate presence layer identity: {identity}"
                    )
                actual_presence[identity] = layer
        actual_events: dict[tuple[str, str], Mapping[str, Any]] = {}
        for layer in lanes["event_cues"]:
            for slot_id in layer.get("slot_ids", []):
                identity = (
                    str(slot_id),
                    str(layer.get("visual_source_id") or ""),
                )
                if identity in actual_events:
                    raise SoundPlanError(
                        f"duplicate event cue identity: {identity}"
                    )
                actual_events[identity] = layer
        if set(actual_presence) != set(expected_presence):
            raise SoundPlanError(
                "presence layers do not exactly match audible visible sources"
            )
        if set(actual_events) != set(expected_events):
            raise SoundPlanError(
                "event cues do not exactly match visually confirmed events"
            )
        for identity, (expected_start, expected_end, source) in expected_presence.items():
            layer = actual_presence[identity]
            if (
                abs(float(layer["start"]) - expected_start)
                > AUDIO_FRAME_TOLERANCE_SECONDS
                or abs(float(layer["end"]) - expected_end)
                > AUDIO_FRAME_TOLERANCE_SECONDS
            ):
                raise SoundPlanError(
                    f"presence layer {identity} is not synced to visual evidence"
                )
            taxonomy = layer.get("taxonomy") or {}
            for key in ("source_kind", "family", "species", "action"):
                if str(taxonomy.get(key) or "").casefold() != str(
                    source.get(key) or ""
                ).casefold():
                    raise SoundPlanError(
                        f"presence layer {identity} taxonomy conflicts with visual evidence"
                    )
            obligation = source.get("physical_foley_obligation")
            if isinstance(obligation, Mapping):
                if layer.get("required") is not True:
                    raise SoundPlanError(
                        f"physical foley presence layer {identity} is not required"
                    )
                if layer.get("physical_foley_obligation") != obligation:
                    raise SoundPlanError(
                        f"physical foley presence layer {identity} obligation is stale"
                    )
                if layer.get("required_audibility") != source.get(
                    "required_audibility"
                ):
                    raise SoundPlanError(
                        f"physical foley presence layer {identity} audibility proof is stale"
                    )
        for identity, (expected_start, expected_end, event) in expected_events.items():
            layer = actual_events[identity]
            if (
                abs(float(layer["start"]) - expected_start)
                > AUDIO_FRAME_TOLERANCE_SECONDS
                or abs(float(layer["end"]) - expected_end)
                > AUDIO_FRAME_TOLERANCE_SECONDS
            ):
                raise SoundPlanError(
                    f"event cue {identity} is not synced to visual evidence"
                )
            taxonomy = layer.get("taxonomy") or {}
            for key in ("source_kind", "family", "species", "action"):
                if str(taxonomy.get(key) or "").casefold() != str(
                    event.get(key) or ""
                ).casefold():
                    raise SoundPlanError(
                        f"event cue {identity} taxonomy conflicts with visual evidence"
                    )

    core = copy.deepcopy(dict(plan))
    supplied_key = core.pop("plan_cache_key", None)
    expected_key = stable_cache_key(
        "sound_plan", core, version=SOUND_PLAN_SCHEMA_VERSION
    )
    if supplied_key != expected_key:
        raise SoundPlanError("sound plan cache key is stale")


def build_sound_plan_v2(
    timeline: Mapping[str, Any],
    music_segments: Sequence[Mapping[str, Any]],
    ambience_regions: Sequence[Mapping[str, Any]],
    presence_layers: Sequence[Mapping[str, Any]] = (),
    event_cues: Sequence[Mapping[str, Any]] = (),
    *,
    acoustic_scenes: Sequence[Mapping[str, Any]] = (),
    status: str | None = None,
) -> dict[str, Any]:
    """Build the production-v2 four-lane sound contract."""

    _slots, _by_id, duration, _tolerance = _timeline_slots(timeline)
    music = [copy.deepcopy(dict(item)) for item in music_segments]
    ambience = [copy.deepcopy(dict(item)) for item in ambience_regions]
    presence = [copy.deepcopy(dict(item)) for item in presence_layers]
    events = [copy.deepcopy(dict(item)) for item in event_cues]
    scenes = [copy.deepcopy(dict(item)) for item in acoustic_scenes]
    music.sort(key=lambda item: (float(item.get("start", 0.0)), str(item.get("segment_id"))))
    ambience.sort(key=lambda item: (float(item.get("start", 0.0)), str(item.get("region_id"))))
    presence.sort(key=lambda item: (float(item.get("start", 0.0)), str(item.get("layer_id"))))
    events.sort(key=lambda item: (float(item.get("start", 0.0)), str(item.get("cue_id"))))
    renderables = music + ambience + presence + events
    all_resolved = bool(renderables) and all(
        isinstance(item.get("asset"), Mapping) for item in renderables
    )
    if status is None:
        status = "ready" if all_resolved else "needs_sound"
    plan: dict[str, Any] = {
        "schema_version": SOUND_PLAN_SCHEMA_VERSION,
        "status": str(status),
        "production_contract": {
            "ambience_coverage_required": 1.0,
            "music_counts_as_natural_sound": False,
            "donor_audio_allowed": False,
            "min_exact_species_confidence": DEFAULT_MIN_EXACT_SPECIES_CONFIDENCE,
            "max_ambience_gap_seconds": MAX_AMBIENCE_GAP_SECONDS,
        },
        "timeline_duration": round(duration, 6),
        "timeline_fingerprint": timeline_fingerprint(timeline),
        "music_segments": music,
        "ambience_regions": ambience,
        "presence_layers": presence,
        "event_cues": events,
        "acoustic_scenes": scenes,
    }
    compiler_markers = [
        scene.get("causal_compiler_receipt")
        for scene in scenes
        if isinstance(scene.get("causal_compiler_receipt"), Mapping)
    ]
    if compiler_markers:
        compiler_policies = {
            str(marker.get("policy_version") or "") for marker in compiler_markers
        }
        if len(compiler_markers) != len(scenes) or len(compiler_policies) != 1:
            raise SoundPlanError("acoustic scenes have mixed causal compiler policies")
        plan["causal_reconstruction_policy_version"] = next(
            iter(compiler_policies)
        )
    plan["plan_cache_key"] = stable_cache_key(
        "sound_plan", plan, version=SOUND_PLAN_SCHEMA_VERSION
    )
    _validate_sound_plan_v2(plan, timeline)
    return plan


def build_sound_plan(
    timeline: Mapping[str, Any],
    music_segments: Sequence[Mapping[str, Any]],
    sfx_cues: Sequence[Mapping[str, Any]] = (),
    *,
    ambience_regions: Sequence[Mapping[str, Any]] | None = None,
    presence_layers: Sequence[Mapping[str, Any]] = (),
    event_cues: Sequence[Mapping[str, Any]] = (),
    acoustic_scenes: Sequence[Mapping[str, Any]] = (),
    status: str | None = None,
    schema_version: int | None = None,
) -> dict[str, Any]:
    """Build v2 when natural lanes are supplied; otherwise preserve the v1 API.

    The positional ``sfx_cues`` argument is intentionally legacy-only. New
    callers must use ``event_cues`` because event sounds are no longer sparse
    and may repeat within a slot.
    """

    wants_v2 = schema_version == SOUND_PLAN_SCHEMA_VERSION or ambience_regions is not None
    if wants_v2:
        if sfx_cues:
            raise SoundPlanError("production-v2 uses event_cues, not legacy sfx_cues")
        if ambience_regions is None:
            raise SoundPlanError("production-v2 requires ambience_regions")
        return build_sound_plan_v2(
            timeline,
            music_segments,
            ambience_regions,
            presence_layers,
            event_cues,
            acoustic_scenes=acoustic_scenes,
            status=status,
        )
    if schema_version not in (None, LEGACY_SOUND_PLAN_SCHEMA_VERSION):
        raise SoundPlanError(f"unsupported sound plan schema_version: {schema_version!r}")
    if presence_layers or event_cues or acoustic_scenes:
        raise SoundPlanError("natural sound lanes require production-v2 schema")
    return _build_sound_plan_v1(
        timeline, music_segments, sfx_cues, status=status
    )


def validate_sound_plan(
    plan: Mapping[str, Any],
    timeline: Mapping[str, Any],
    *,
    min_sfx_confidence: float = DEFAULT_MIN_SFX_CONFIDENCE,
    min_sfx_gap_seconds: float = DEFAULT_MIN_SFX_GAP_SECONDS,
    require_resolved_assets: bool | None = None,
    require_production_v2: bool = False,
    require_causal_compilation: bool = False,
) -> None:
    """Validate v1 for replay, or v2 for autonomous production.

    A legacy v1 manifest can be re-rendered, but it never passes a
    ``require_production_v2`` release gate because it has no continuous
    ambience contract.
    """

    if not isinstance(plan, Mapping):
        raise SoundPlanError("sound plan must be a mapping")
    version = plan.get("schema_version")
    if version == LEGACY_SOUND_PLAN_SCHEMA_VERSION:
        if require_production_v2:
            raise SoundPlanError(
                "legacy sound plan v1 cannot be ready for production-v2"
            )
        return _validate_sound_plan_v1(
            plan,
            timeline,
            min_sfx_confidence=min_sfx_confidence,
            min_sfx_gap_seconds=min_sfx_gap_seconds,
            require_resolved_assets=require_resolved_assets,
        )
    if version == SOUND_PLAN_SCHEMA_VERSION:
        return _validate_sound_plan_v2(
            plan,
            timeline,
            require_resolved_assets=require_resolved_assets,
            require_causal_compilation=require_causal_compilation,
        )
    raise SoundPlanError(f"unsupported sound plan schema_version: {version!r}")


def is_production_v2_ready(
    plan: Mapping[str, Any], timeline: Mapping[str, Any]
) -> bool:
    try:
        validate_sound_plan(
            plan,
            timeline,
            require_resolved_assets=True,
            require_production_v2=True,
            require_causal_compilation=True,
        )
    except SoundPlanError:
        return False
    return str(plan.get("status")) == "ready"


def write_sound_plan_json(
    path: str | Path,
    plan: Mapping[str, Any],
    timeline: Mapping[str, Any],
) -> None:
    """Validate then atomically persist a sound plan manifest."""

    validate_sound_plan(plan, timeline)
    atomic_write_json(path, plan)


__all__ = [
    "DEFAULT_MIN_EXACT_SPECIES_CONFIDENCE",
    "DEFAULT_MIN_SFX_CONFIDENCE",
    "DEFAULT_MIN_SFX_GAP_SECONDS",
    "FOREGROUND_SOUND_DISPOSITION_SCHEMA_VERSION",
    "LEGACY_SOUND_PLAN_SCHEMA_VERSION",
    "AUDIO_FRAME_TOLERANCE_SECONDS",
    "MAX_AMBIENCE_GAP_SECONDS",
    "MAX_MUSIC_SEGMENT_SECONDS",
    "NATURAL_SOUND_DISTANCES",
    "PRODUCTION_AUDIO_SAMPLE_RATE",
    "MIN_MUSIC_SEGMENT_SECONDS",
    "SOUND_PLAN_SCHEMA_VERSION",
    "SoundPlanError",
    "build_music_prompt",
    "build_sound_plan",
    "build_sound_plan_v2",
    "foreground_sound_disposition_cache_key",
    "is_production_v2_ready",
    "make_ambience_region",
    "make_event_cue",
    "make_music_segment",
    "make_presence_layer",
    "make_sfx_cue",
    "music_generation_cache_key",
    "natural_sound_search_cache_key",
    "refresh_sound_plan_cache_ids",
    "sfx_search_cache_key",
    "stable_cache_key",
    "timeline_fingerprint",
    "validate_asset_provenance",
    "validate_sound_plan",
    "write_sound_plan_json",
]
