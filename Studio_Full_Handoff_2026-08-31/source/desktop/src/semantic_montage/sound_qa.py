"""Lane-specific acoustic QA for production-v2 plans and rendered ambience stems."""

from __future__ import annotations

import audioop
import copy
import math
import re
import wave
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from .common import canonical_hash, sha256_file
from .sound import (
    MAX_AMBIENCE_GAP_SECONDS,
    SOUND_PLAN_SCHEMA_VERSION,
    SoundPlanError,
    foreground_sound_disposition_cache_key,
    validate_sound_plan,
)


class SoundQAError(ValueError):
    """Sound QA input is malformed or cannot be measured."""


REQUIRED_CUE_RENDER_RECEIPT_CONTRACT = (
    "required-physical-foley-render-receipt-v3"
)
REQUIRED_CUE_LINEAR_PREMASTER_POLICY = "required-cue-linear-premaster-mix-v1"
REQUIRED_CUE_AB_WINDOW_FRAMES = 19_200
REQUIRED_CUE_AB_DELTA_FLOOR_DBFS = -36.0
REQUIRED_CUE_AB_DELTA_CEILING_DBFS = -30.0
REQUIRED_CUE_AB_TARGET_DBFS = -33.0
REQUIRED_CUE_AB_PROJECTION_MARGIN_DB = 3.0


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise SoundQAError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SoundQAError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise SoundQAError(f"{name} must be finite")
    return number


def _merge_intervals(
    intervals: Sequence[tuple[float, float]], *, tolerance: float = 1e-6
) -> list[tuple[float, float]]:
    ordered = sorted(intervals)
    merged: list[list[float]] = []
    for start, end in ordered:
        if end <= start:
            raise SoundQAError("coverage interval must have positive duration")
        if not merged or start > merged[-1][1] + tolerance:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(row[0], row[1]) for row in merged]


def coverage_report(
    intervals: Sequence[tuple[float, float]], duration: float, *, tolerance: float = 1e-6
) -> dict[str, Any]:
    duration = _finite(duration, "duration")
    if duration <= 0:
        raise SoundQAError("duration must be positive")
    overlaps: list[dict[str, float]] = []
    ordered = sorted(intervals)
    for index in range(1, len(ordered)):
        overlap = ordered[index - 1][1] - ordered[index][0]
        if overlap > tolerance:
            overlaps.append(
                {
                    "start": ordered[index][0],
                    "end": min(ordered[index - 1][1], ordered[index][1]),
                    "duration": min(ordered[index - 1][1], ordered[index][1]) - ordered[index][0],
                }
            )
    merged = _merge_intervals(intervals, tolerance=tolerance) if intervals else []
    gaps: list[dict[str, float]] = []
    cursor = 0.0
    covered = 0.0
    for start, end in merged:
        clipped_start, clipped_end = max(0.0, start), min(duration, end)
        if clipped_end <= clipped_start:
            continue
        if clipped_start > cursor + tolerance:
            gaps.append(
                {"start": cursor, "end": clipped_start, "duration": clipped_start - cursor}
            )
        covered += max(0.0, clipped_end - max(cursor, clipped_start))
        cursor = max(cursor, clipped_end)
    if cursor < duration - tolerance:
        gaps.append({"start": cursor, "end": duration, "duration": duration - cursor})
    return {
        "duration": duration,
        "covered_seconds": min(duration, covered),
        "coverage_ratio": min(1.0, covered / duration),
        "gaps": gaps,
        "overlaps": overlaps,
    }


def _energy_gaps_from_windows(
    windows: Sequence[Mapping[str, Any]],
    *,
    duration: float,
    silence_threshold_dbfs: float,
    max_gap_seconds: float,
) -> list[dict[str, float]]:
    silent: list[tuple[float, float]] = []
    previous_end = 0.0
    for index, window in enumerate(windows):
        if not isinstance(window, Mapping):
            raise SoundQAError(f"energy window {index} must be a mapping")
        start = _finite(window.get("start"), f"energy window {index}.start")
        end = _finite(window.get("end"), f"energy window {index}.end")
        dbfs = _finite(window.get("rms_dbfs"), f"energy window {index}.rms_dbfs")
        if start < previous_end - 1e-6 or end <= start or end > duration + 1e-6:
            raise SoundQAError(f"energy window {index} has invalid ordering/bounds")
        if start > previous_end + 1e-6:
            # Missing measurement is not assumed audible.
            silent.append((previous_end, start))
        if dbfs <= silence_threshold_dbfs:
            silent.append((start, end))
        previous_end = end
    if previous_end < duration - 1e-6:
        silent.append((previous_end, duration))
    merged = _merge_intervals(silent) if silent else []
    return [
        {"start": start, "end": end, "duration": end - start}
        for start, end in merged
        if end - start > max_gap_seconds + 1e-6
    ]


def measure_wav_energy(
    path: str | Path, *, window_seconds: float = 0.05
) -> tuple[list[dict[str, float]], float]:
    """Measure PCM WAV RMS without loading the stem into memory."""

    window_seconds = _finite(window_seconds, "window_seconds")
    if window_seconds <= 0:
        raise SoundQAError("window_seconds must be positive")
    target = Path(path)
    if not target.is_file():
        raise SoundQAError(f"ambience stem does not exist: {target}")
    windows: list[dict[str, float]] = []
    try:
        with wave.open(str(target), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            frames_total = source.getnframes()
            if channels < 1 or sample_width not in {1, 2, 3, 4} or sample_rate <= 0:
                raise SoundQAError("unsupported ambience WAV format")
            frames_per_window = max(1, round(sample_rate * window_seconds))
            cursor = 0
            max_amplitude = float(1 << (sample_width * 8 - 1))
            while cursor < frames_total:
                count = min(frames_per_window, frames_total - cursor)
                payload = source.readframes(count)
                rms = audioop.rms(payload, sample_width) if payload else 0
                dbfs = -120.0 if rms <= 0 else 20.0 * math.log10(rms / max_amplitude)
                start = cursor / sample_rate
                end = (cursor + count) / sample_rate
                windows.append({"start": start, "end": end, "rms_dbfs": dbfs})
                cursor += count
    except (wave.Error, EOFError) as exc:
        raise SoundQAError(f"cannot read ambience WAV: {exc}") from exc
    return windows, frames_total / sample_rate


def detect_ambience_energy_gaps(
    path: str | Path,
    *,
    silence_threshold_dbfs: float = -55.0,
    max_gap_seconds: float = MAX_AMBIENCE_GAP_SECONDS,
    window_seconds: float = 0.05,
) -> list[dict[str, float]]:
    windows, duration = measure_wav_energy(path, window_seconds=window_seconds)
    return _energy_gaps_from_windows(
        windows,
        duration=duration,
        silence_threshold_dbfs=silence_threshold_dbfs,
        max_gap_seconds=max_gap_seconds,
    )


def _asset_taxonomy(asset: Mapping[str, Any]) -> Mapping[str, Any]:
    value = asset.get("taxonomy")
    if isinstance(value, Mapping):
        return value
    provenance = asset.get("provenance")
    if isinstance(provenance, Mapping) and isinstance(provenance.get("taxonomy"), Mapping):
        return provenance["taxonomy"]
    return {}


def _asset_id(asset: Mapping[str, Any]) -> str:
    provenance = asset.get("provenance")
    return str(provenance.get("asset_id") or "") if isinstance(provenance, Mapping) else ""


_HARD_NEGATIVE_TAXONOMY_SCALAR_FIELDS = (
    "source_kind",
    "family",
    "species",
    "action",
    "environment",
    "distance",
)


def _positive_taxonomy_text(taxonomy: Mapping[str, Any]) -> str:
    """Return asserted identity labels, excluding classifier and audit metadata."""

    values = [
        value.strip()
        for key in _HARD_NEGATIVE_TAXONOMY_SCALAR_FIELDS
        if isinstance((value := taxonomy.get(key)), str) and value.strip()
    ]
    labels = taxonomy.get("labels")
    if isinstance(labels, Sequence) and not isinstance(labels, (str, bytes)):
        values.extend(
            value.strip()
            for value in labels
            if isinstance(value, str) and value.strip()
        )
    return " ".join(values).casefold()


def _iter_natural_layers(plan: Mapping[str, Any]) -> Iterable[tuple[str, str, Mapping[str, Any]]]:
    for lane_name, id_key in (
        ("ambience_regions", "region_id"),
        ("presence_layers", "layer_id"),
        ("event_cues", "cue_id"),
    ):
        for layer in plan.get(lane_name, []):
            yield lane_name, str(layer.get(id_key) or ""), layer


def _taxonomy_issues(plan: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    issues = {
        "wrong_species": [],
        "wrong_family": [],
        "wrong_action": [],
        "wrong_environment": [],
        "hard_negative_hits": [],
        "missing_classification": [],
    }
    for lane, layer_id, layer in _iter_natural_layers(plan):
        asset = layer.get("asset")
        if not isinstance(asset, Mapping):
            continue
        actual = _asset_taxonomy(asset)
        expected = layer.get("taxonomy") if isinstance(layer.get("taxonomy"), Mapping) else {}
        if not actual:
            issues["missing_classification"].append(
                {"lane": lane, "layer_id": layer_id, "asset_id": _asset_id(asset)}
            )
            continue
        for key, bucket in (
            ("species", "wrong_species"),
            ("family", "wrong_family"),
            ("action", "wrong_action"),
            ("environment", "wrong_environment"),
        ):
            if key == "environment" and not (
                lane == "ambience_regions"
                or expected.get("specificity") == "environment"
                or (layer.get("retrieval") or {}).get("degradation_level") == "environment"
            ):
                # Environment is context for exact/family/action retrieval, not
                # a required label on an isolated animal or movement recording.
                continue
            requested = str(expected.get(key) or "").strip().casefold()
            received = str(actual.get(key) or "").strip().casefold()
            if requested and requested != received:
                issues[bucket].append(
                    {
                        "lane": lane,
                        "layer_id": layer_id,
                        "asset_id": _asset_id(asset),
                        "expected": requested,
                        "actual": received or None,
                    }
                )
        actual_text = _positive_taxonomy_text(actual)
        for negative in layer.get("hard_negatives", []):
            if str(negative).casefold() in actual_text:
                issues["hard_negative_hits"].append(
                    {
                        "lane": lane,
                        "layer_id": layer_id,
                        "asset_id": _asset_id(asset),
                        "hard_negative": negative,
                    }
                )
    return issues


def _asset_issues(
    plan: Mapping[str, Any], *, verify_hashes: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    unresolved: list[dict[str, Any]] = []
    contamination: list[dict[str, Any]] = []
    provenance_errors: list[dict[str, Any]] = []
    for lane, layer_id, layer in _iter_natural_layers(plan):
        asset = layer.get("asset")
        if not isinstance(asset, Mapping):
            unresolved.append({"lane": lane, "layer_id": layer_id})
            continue
        quality = asset.get("quality")
        if not isinstance(quality, Mapping):
            contamination.append(
                {"lane": lane, "layer_id": layer_id, "reason": "missing_quality_classifier"}
            )
        else:
            for key in ("speech_probability", "music_probability"):
                try:
                    probability = float(quality.get(key))
                except (TypeError, ValueError):
                    probability = 1.0
                if probability > 0.05:
                    contamination.append(
                        {
                            "lane": lane,
                            "layer_id": layer_id,
                            "kind": key.replace("_probability", ""),
                            "probability": probability,
                        }
                    )
        provenance = asset.get("provenance")
        if not isinstance(provenance, Mapping):
            provenance_errors.append(
                {"lane": lane, "layer_id": layer_id, "reason": "missing_provenance"}
            )
            continue
        if verify_hashes and asset.get("path"):
            path = Path(str(asset["path"]))
            if not path.is_file():
                provenance_errors.append(
                    {"lane": lane, "layer_id": layer_id, "reason": "asset_missing", "path": str(path)}
                )
            else:
                digest = sha256_file(path)
                if digest.casefold() != str(provenance.get("sha256") or "").casefold():
                    provenance_errors.append(
                        {"lane": lane, "layer_id": layer_id, "reason": "sha256_mismatch"}
                    )
    return unresolved, contamination, provenance_errors


def _valid_exact_ambience_disposition(
    plan: Mapping[str, Any],
    disposition: Any,
    *,
    lane: str,
    slot_id: str,
    source_id: str,
) -> bool:
    if not isinstance(disposition, Mapping):
        return False
    try:
        expected_key = foreground_sound_disposition_cache_key(disposition)
    except (SoundPlanError, TypeError, ValueError):
        return False
    diagnostics = plan.get("resolution_diagnostics")
    current_catalog_fingerprint = (
        str(diagnostics.get("catalog_fingerprint") or "")
        if isinstance(diagnostics, Mapping)
        else ""
    )
    if (
        disposition.get("outcome") != "covered_by_exact_ambience"
        or disposition.get("lane") != lane
        or disposition.get("wrong_species_used") is not False
        or not current_catalog_fingerprint
        or str(disposition.get("catalog_fingerprint") or "")
        != current_catalog_fingerprint
        or str(disposition.get("visual_source_id") or "") != source_id
        or slot_id not in {str(value) for value in disposition.get("slot_ids", [])}
        or disposition.get("disposition_key") != expected_key
    ):
        return False
    declared_regions = {str(value) for value in disposition.get("ambience_region_ids", [])}
    declared_assets = {str(value) for value in disposition.get("ambience_asset_ids", [])}
    for region in plan.get("ambience_regions", []):
        if slot_id not in {str(value) for value in region.get("slot_ids", [])}:
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
        if str(region.get("region_id") or "") in declared_regions and asset_id in declared_assets:
            return True
    return False


def _notable_source_coverage(plan: Mapping[str, Any]) -> dict[str, Any]:
    expected: dict[tuple[str, str], dict[str, Any]] = {}
    for scene in plan.get("acoustic_scenes", []):
        slot_id = str(scene.get("slot_id") or "")
        for source in scene.get("visible_sources", []):
            if source.get("audible_presence") or source.get("audibility_requested"):
                source_id = str(source.get("source_id") or "")
                expected[(slot_id, source_id)] = {
                    "weight": float(source.get("confidence") or 0.0),
                    "lane": "presence",
                    "disposition": source.get("sound_disposition"),
                    "required": source.get("required") is True,
                }
        for event in scene.get("events", []):
            if event.get("audible_event") or event.get("audibility_requested"):
                source_id = str(event.get("event_id") or "")
                expected[(slot_id, source_id)] = {
                    "weight": float(event.get("confidence") or 0.0),
                    "lane": "events",
                    "disposition": event.get("sound_disposition"),
                    "required": event.get("required") is True,
                }
    if not expected:
        return {
            "weighted_ratio": 1.0,
            "asset_weighted_ratio": 1.0,
            "expected": 0,
            "covered": 0,
            "dispositioned": 0,
            "missing": [],
        }
    covered_keys: set[tuple[str, str]] = set()
    for _lane, _layer_id, layer in _iter_natural_layers(plan):
        source_id = str(layer.get("visual_source_id") or "")
        for slot_id in layer.get("slot_ids", []):
            if (str(slot_id), source_id) in expected and isinstance(layer.get("asset"), Mapping):
                covered_keys.add((str(slot_id), source_id))
    dispositioned_keys = {
        key
        for key, row in expected.items()
        if key not in covered_keys
        and not row.get("required")
        and _valid_exact_ambience_disposition(
            plan,
            row.get("disposition"),
            lane=str(row["lane"]),
            slot_id=key[0],
            source_id=key[1],
        )
    }
    total_weight = sum(float(row["weight"]) for row in expected.values())
    covered_weight = sum(float(expected[key]["weight"]) for key in covered_keys)
    dispositioned_weight = sum(
        float(expected[key]["weight"]) for key in dispositioned_keys
    )
    return {
        "weighted_ratio": (
            (covered_weight + dispositioned_weight) / total_weight
            if total_weight
            else 1.0
        ),
        "asset_weighted_ratio": covered_weight / total_weight if total_weight else 1.0,
        "expected": len(expected),
        "covered": len(covered_keys),
        "dispositioned": len(dispositioned_keys),
        "missing": [
            {
                "slot_id": key[0],
                "source_id": key[1],
                "weight": expected[key]["weight"],
            }
            for key in sorted(expected)
            if key not in covered_keys and key not in dispositioned_keys
        ],
    }


def _pcm_dbfs(value: int, sample_width: int) -> float:
    if value <= 0:
        return -120.0
    return 20.0 * math.log10(value / float(1 << (sample_width * 8 - 1)))


def _read_required_cue_pcm(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file():
        raise SoundQAError("required cue stem does not exist")
    try:
        with wave.open(str(target), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            frames_total = source.getnframes()
            if channels != 2 or sample_width not in {2, 3, 4} or sample_rate != 48_000:
                raise SoundQAError(
                    "required cue stem must be 48 kHz stereo PCM16/24/32"
                )
            payload = source.readframes(frames_total)
            expected_bytes = frames_total * channels * sample_width
            if len(payload) != expected_bytes:
                raise SoundQAError("required cue stem is truncated")
    except (wave.Error, EOFError) as exc:
        raise SoundQAError(f"cannot read required cue WAV: {exc}") from exc
    return {
        "stem_sha256": sha256_file(target),
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width": sample_width,
        "sample_count": frames_total,
        "payload": payload,
    }


def _window_rms_dbfs(
    pcm: Mapping[str, Any],
    *,
    start_frame: int,
    end_frame: int,
    window_frames: int,
) -> list[float]:
    channels = int(pcm["channels"])
    sample_width = int(pcm["sample_width"])
    frame_bytes = channels * sample_width
    payload = pcm["payload"]
    values: list[float] = []
    cursor = start_frame
    while cursor + window_frames <= end_frame:
        chunk = payload[
            cursor * frame_bytes : (cursor + window_frames) * frame_bytes
        ]
        values.append(_pcm_dbfs(audioop.rms(chunk, sample_width), sample_width))
        cursor += window_frames
    return values


def _required_cue_layer_binding(layer: Mapping[str, Any]) -> dict[str, Any]:
    asset = layer.get("asset")
    if not isinstance(asset, Mapping):
        raise SoundQAError("required cue has no resolved asset")
    provenance = asset.get("provenance")
    if not isinstance(provenance, Mapping):
        provenance = {}
    digest = str(
        asset.get("sha256") or provenance.get("sha256") or ""
    ).casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SoundQAError("required cue asset SHA-256 is invalid")
    source_offset = float(layer.get("source_offset", asset.get("source_offset", 0.0)))
    source_duration = float(
        layer.get(
            "source_duration",
            asset.get("duration_seconds", asset.get("duration", 0.0)),
        )
    )
    return {
        "layer_id": str(layer.get("layer_id") or layer.get("cue_id") or ""),
        "lane": str(layer.get("lane") or ""),
        "slot_ids": [str(value) for value in layer.get("slot_ids", [])],
        "visual_source_id": str(layer.get("visual_source_id") or ""),
        "obligation_key": str(
            (layer.get("physical_foley_obligation") or {}).get("obligation_key")
            or ""
        ),
        "start": round(float(layer.get("start") or 0.0), 6),
        "end": round(float(layer.get("end") or 0.0), 6),
        "duration": round(float(layer.get("duration") or 0.0), 6),
        "source_offset": round(source_offset, 9),
        "source_duration": round(source_duration, 9),
        "gain_db": round(float(layer.get("gain_db") or 0.0), 6),
        "fade_in": round(float(layer.get("fade_in", 0.04)), 6),
        "fade_out": round(float(layer.get("fade_out", 0.08)), 6),
        "horizontal_position": round(
            float((layer.get("perspective") or {}).get("horizontal_position") or 0.0),
            6,
        ),
        "asset_id": str(
            asset.get("asset_id")
            or asset.get("canonical_id")
            or provenance.get("asset_id")
            or ""
        ),
        "asset_sha256": digest,
    }


def required_cue_preview_layer(
    layer: Mapping[str, Any],
) -> tuple[dict[str, Any], float]:
    """Shift one cue into a short padded timeline for source-isolated rendering."""

    audibility = layer.get("required_audibility")
    if not isinstance(audibility, Mapping):
        raise SoundQAError("required cue audibility binding is missing")
    sample_rate = 48_000
    pad_frames = int(audibility.get("preview_padding_frames") or 0)
    duration_frames = int(round(float(layer.get("duration") or 0.0) * sample_rate))
    if pad_frames < 12_000 or duration_frames <= 0:
        raise SoundQAError("required cue preview geometry is invalid")
    preview = copy.deepcopy(dict(layer))
    start = pad_frames / sample_rate
    end = (pad_frames + duration_frames) / sample_rate
    preview["start"] = start
    preview["end"] = end
    preview["duration"] = duration_frames / sample_rate
    preview["sync_interval"] = {"start": start, "end": end}
    return preview, (duration_frames + 2 * pad_frames) / sample_rate


def _measure_required_cue_preview(
    path: str | Path,
    layer: Mapping[str, Any],
) -> dict[str, Any]:
    pcm = _read_required_cue_pcm(path)
    audibility = layer.get("required_audibility")
    if not isinstance(audibility, Mapping):
        raise SoundQAError("required cue audibility binding is missing")
    sample_rate = int(pcm["sample_rate"])
    pad_frames = int(audibility["preview_padding_frames"])
    duration_frames = int(round(float(layer["duration"]) * sample_rate))
    expected_frames = duration_frames + 2 * pad_frames
    if int(pcm["sample_count"]) != expected_frames:
        raise SoundQAError("required cue preview sample count is stale")
    cue_start = pad_frames
    cue_end = pad_frames + duration_frames
    fade_in_frames = min(
        int(round(float(layer.get("fade_in", 0.04)) * sample_rate)),
        duration_frames // 2,
    )
    fade_out_frames = min(
        int(round(float(layer.get("fade_out", 0.08)) * sample_rate)),
        duration_frames // 2,
    )
    core_start = cue_start + fade_in_frames
    core_end = cue_end - fade_out_frames
    window_frames = int(audibility["window_frames"])
    windows = _window_rms_dbfs(
        pcm,
        start_frame=core_start,
        end_frame=core_end,
        window_frames=window_frames,
    )
    if not windows:
        raise SoundQAError("required cue has no complete core audibility window")
    float_samples = _pcm_float_samples(pcm)
    a_weighted_windows = [
        _fft_a_weighted_dbfs(
            float_samples[cursor : cursor + REQUIRED_CUE_AB_WINDOW_FRAMES],
            sample_rate,
        )
        for cursor in range(
            cue_start,
            cue_end - REQUIRED_CUE_AB_WINDOW_FRAMES + 1,
            REQUIRED_CUE_AB_WINDOW_FRAMES,
        )
    ]
    if not a_weighted_windows:
        raise SoundQAError("required cue has no complete 400 ms A-weighted window")
    frame_bytes = int(pcm["channels"]) * int(pcm["sample_width"])
    cue_payload = pcm["payload"][cue_start * frame_bytes : cue_end * frame_bytes]
    peak_dbfs = _pcm_dbfs(
        audioop.max(cue_payload, int(pcm["sample_width"])),
        int(pcm["sample_width"]),
    )
    leakage_window_frames = 480
    outside_windows = [
        *_window_rms_dbfs(
            pcm,
            start_frame=0,
            end_frame=cue_start,
            window_frames=leakage_window_frames,
        ),
        None,
        *_window_rms_dbfs(
            pcm,
            start_frame=cue_end,
            end_frame=int(pcm["sample_count"]),
            window_frames=leakage_window_frames,
        ),
    ]
    leakage_threshold = float(audibility["outside_leakage_threshold_dbfs"])
    longest_run = current_run = 0
    for value in outside_windows:
        if value is not None and value > leakage_threshold:
            current_run += leakage_window_frames
            longest_run = max(longest_run, current_run)
        else:
            current_run = 0
    core_threshold = float(audibility["core_window_rms_floor_dbfs"])
    return {
        "stem_sha256": pcm["stem_sha256"],
        "sample_rate": sample_rate,
        "channels": int(pcm["channels"]),
        "sample_width": int(pcm["sample_width"]),
        "sample_count": int(pcm["sample_count"]),
        "cue_start_frame": cue_start,
        "cue_end_frame": cue_end,
        "core_start_frame": core_start,
        "core_end_frame": core_end,
        "window_frames": window_frames,
        "core_window_count": len(windows),
        "max_window_rms_dbfs": max(windows),
        "core_windows_above_floor": sum(value > core_threshold for value in windows),
        "core_window_ratio_above_floor": sum(
            value > core_threshold for value in windows
        )
        / len(windows),
        "core_window_rank_dbfs": sorted(windows, reverse=True)[
            max(
                0,
                math.ceil(
                    float(audibility["minimum_core_window_ratio"]) * len(windows)
                )
                - 1,
            )
        ],
        "a_weighted_window_frames": REQUIRED_CUE_AB_WINDOW_FRAMES,
        "max_a_weighted_window_dbfs": max(a_weighted_windows),
        "peak_dbfs": peak_dbfs,
        "outside_longest_leakage_run_frames": longest_run,
    }


def _required_cue_metrics_pass(
    metrics: Mapping[str, Any], audibility: Mapping[str, Any]
) -> bool:
    return bool(
        float(metrics["peak_dbfs"]) >= float(audibility["peak_floor_dbfs"]) - 1e-6
        and float(metrics["peak_dbfs"])
        <= float(audibility["peak_ceiling_dbfs"]) + 1e-6
        and float(metrics["max_window_rms_dbfs"])
        >= float(audibility["max_window_rms_floor_dbfs"]) - 1e-6
        and float(metrics["core_window_ratio_above_floor"])
        >= float(audibility["minimum_core_window_ratio"]) - 1e-9
        and int(metrics["outside_longest_leakage_run_frames"])
        <= int(audibility["outside_max_run_frames"])
    )


def derive_required_cue_static_gain(
    provisional_wav: str | Path,
    layer: Mapping[str, Any],
    *,
    premaster_presence_gain: float = 1.0,
    premaster_presence_duck_ratio: float = 1.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive the minimum safe static gain from an exact isolated render."""

    from .acoustic_scene import REQUIRED_CUE_AUDIBILITY_CONTRACT

    audibility = layer.get("required_audibility")
    if (
        layer.get("required") is not True
        or not isinstance(audibility, Mapping)
        or audibility.get("contract") != REQUIRED_CUE_AUDIBILITY_CONTRACT
    ):
        raise SoundQAError("required cue static gain contract is missing")
    original_binding = _required_cue_layer_binding(layer)
    if (
        not math.isfinite(float(premaster_presence_gain))
        or not 0.0 < float(premaster_presence_gain) <= 2.0
        or abs(float(premaster_presence_duck_ratio) - 1.0) > 1e-9
    ):
        raise SoundQAError(
            "required cue A/B projection needs a positive linear premaster route"
        )
    metrics = _measure_required_cue_preview(provisional_wav, layer)
    if int(metrics["outside_longest_leakage_run_frames"]) > int(
        audibility["outside_max_run_frames"]
    ):
        raise SoundQAError(
            f"required cue {original_binding['layer_id']} leaks outside its rendered interval"
        )
    peak = float(metrics["peak_dbfs"])
    max_window = float(metrics["max_window_rms_dbfs"])
    ranked_window = float(metrics["core_window_rank_dbfs"])
    premaster_gain_db = 20.0 * math.log10(float(premaster_presence_gain))
    max_a_weighted = float(metrics["max_a_weighted_window_dbfs"])
    provisional_premaster_delta = max_a_weighted + premaster_gain_db
    projection_recovery_required = (
        provisional_premaster_delta < REQUIRED_CUE_AB_DELTA_FLOOR_DBFS - 1e-6
    )
    projection_attenuation_required = (
        provisional_premaster_delta > REQUIRED_CUE_AB_DELTA_CEILING_DBFS + 1e-6
    )
    required_projection_floor = (
        REQUIRED_CUE_AB_TARGET_DBFS
        if projection_recovery_required
        else REQUIRED_CUE_AB_DELTA_FLOOR_DBFS
    )
    # Leave a deterministic fence above lower audibility thresholds.  FFmpeg's
    # volume/PCM round-trip can otherwise land a few ten-thousandths of a dB
    # below an exact floor even when the analytical gain lands on it exactly.
    floor_margin_db = 0.01
    lower_delta = max(
        float(audibility["peak_floor_dbfs"]) - peak + floor_margin_db,
        float(audibility["max_window_rms_floor_dbfs"])
        - max_window
        + floor_margin_db,
        float(audibility["core_window_rms_floor_dbfs"])
        - ranked_window
        + floor_margin_db,
        required_projection_floor - provisional_premaster_delta
        + floor_margin_db,
    )
    upper_delta = float(audibility["peak_ceiling_dbfs"]) - peak
    if lower_delta > upper_delta + 1e-9:
        raise SoundQAError(
            f"required cue {original_binding['layer_id']} cannot satisfy static-gain audibility bounds"
        )
    if projection_attenuation_required:
        projection_ceiling_delta = (
            REQUIRED_CUE_AB_DELTA_CEILING_DBFS - provisional_premaster_delta
        )
        if lower_delta > projection_ceiling_delta + 1e-9:
            raise SoundQAError(
                f"required cue {original_binding['layer_id']} cannot satisfy A-weighted premaster bounds"
            )
        gain_delta = max(
            lower_delta,
            min(
                REQUIRED_CUE_AB_TARGET_DBFS - provisional_premaster_delta,
                upper_delta - 0.01,
            ),
        )
    elif peak > float(audibility["peak_ceiling_dbfs"]):
        gain_delta = upper_delta - 0.01
    elif _required_cue_metrics_pass(metrics, audibility):
        gain_delta = 0.0
    else:
        gain_delta = max(0.0, lower_delta)
    effective_gain = round(float(original_binding["gain_db"]) + gain_delta, 6)
    if not -48.0 <= effective_gain <= 12.0:
        raise SoundQAError(
            f"required cue {original_binding['layer_id']} cannot satisfy static-gain audibility bounds"
        )
    adjusted = copy.deepcopy(dict(layer))
    adjusted["gain_db"] = effective_gain
    projected = copy.deepcopy(metrics)
    for key in (
        "peak_dbfs",
        "max_window_rms_dbfs",
        "core_window_rank_dbfs",
        "max_a_weighted_window_dbfs",
    ):
        projected[key] = float(projected[key]) + gain_delta
    projected["core_windows_above_floor"] = None
    projected["core_window_ratio_above_floor"] = None
    facts: dict[str, Any] = {
        "schema_version": 1,
        "contract": REQUIRED_CUE_RENDER_RECEIPT_CONTRACT,
        "status": "gain_derived",
        "identity": {
            "slot_id": original_binding["slot_ids"][0]
            if len(original_binding["slot_ids"]) == 1
            else "",
            "visual_source_id": original_binding["visual_source_id"],
            "obligation_key": original_binding["obligation_key"],
            "lane": original_binding["lane"],
        },
        "audibility_binding": copy.deepcopy(dict(audibility)),
        "original_layer_binding": original_binding,
        "effective_layer_binding": _required_cue_layer_binding(adjusted),
        "gain": {
            "mode": "minimum_static_gain_v1",
            "floor_margin_db": floor_margin_db,
            "delta_db": round(gain_delta, 6),
            "effective_gain_db": effective_gain,
            "lower_delta_db": round(lower_delta, 6),
            "upper_delta_db": round(upper_delta, 6),
        },
        "premaster_route": {
            "contract": REQUIRED_CUE_LINEAR_PREMASTER_POLICY,
            "presence_gain": float(premaster_presence_gain),
            "presence_gain_db": premaster_gain_db,
            "presence_duck_ratio": float(premaster_presence_duck_ratio),
            "a_weighted_window_frames": REQUIRED_CUE_AB_WINDOW_FRAMES,
            "cue_delta_floor_dbfs": REQUIRED_CUE_AB_DELTA_FLOOR_DBFS,
            "cue_delta_ceiling_dbfs": REQUIRED_CUE_AB_DELTA_CEILING_DBFS,
            "cue_delta_target_dbfs": REQUIRED_CUE_AB_TARGET_DBFS,
            "projection_margin_db": REQUIRED_CUE_AB_PROJECTION_MARGIN_DB,
            "projection_floor_dbfs": (
                REQUIRED_CUE_AB_DELTA_FLOOR_DBFS
                + REQUIRED_CUE_AB_PROJECTION_MARGIN_DB
            ),
            "projection_recovery_required": projection_recovery_required,
            "projection_attenuation_required": projection_attenuation_required,
            "required_projection_floor_dbfs": required_projection_floor,
            "projected_max_cue_delta_a_weighted_dbfs": (
                max_a_weighted + gain_delta + premaster_gain_db
            ),
        },
        "provisional": metrics,
        "projected": projected,
    }
    facts["adjustment_receipt_sha256"] = canonical_hash(facts)
    return adjusted, facts


def finalize_required_cue_static_gain(
    final_isolated_wav: str | Path,
    adjusted_layer: Mapping[str, Any],
    adjustment_facts: Mapping[str, Any],
) -> dict[str, Any]:
    facts = copy.deepcopy(dict(adjustment_facts))
    supplied_hash = str(facts.pop("adjustment_receipt_sha256", "") or "")
    if supplied_hash != canonical_hash(facts):
        raise SoundQAError("required cue gain adjustment receipt is stale")
    if facts.get("effective_layer_binding") != _required_cue_layer_binding(
        adjusted_layer
    ):
        raise SoundQAError("required cue effective layer binding is stale")
    metrics = _measure_required_cue_preview(final_isolated_wav, adjusted_layer)
    if not _required_cue_metrics_pass(metrics, facts["audibility_binding"]):
        raise SoundQAError("required cue final isolated render is not audible")
    route = facts.get("premaster_route")
    if (
        not isinstance(route, Mapping)
        or route.get("contract") != REQUIRED_CUE_LINEAR_PREMASTER_POLICY
        or int(route.get("a_weighted_window_frames") or 0)
        != REQUIRED_CUE_AB_WINDOW_FRAMES
        or abs(float(route.get("presence_duck_ratio") or 0.0) - 1.0) > 1e-9
    ):
        raise SoundQAError("required cue linear premaster route is stale")
    measured_premaster_delta = float(metrics["max_a_weighted_window_dbfs"]) + float(
        route["presence_gain_db"]
    )
    if measured_premaster_delta < float(route["required_projection_floor_dbfs"]) - 1e-6:
        raise SoundQAError("required cue final A-weighted premaster projection is inaudible")
    if (
        route.get("projection_attenuation_required") is True
        and measured_premaster_delta
        > float(route["cue_delta_ceiling_dbfs"]) + 1e-6
    ):
        raise SoundQAError("required cue final A-weighted premaster projection is excessive")
    route["measured_max_cue_delta_a_weighted_dbfs"] = measured_premaster_delta
    facts["premaster_route"] = route
    facts["adjustment_receipt_sha256"] = supplied_hash
    facts["status"] = "isolated_verified"
    facts["final_isolated"] = metrics
    facts["isolated_receipt_sha256"] = canonical_hash(facts)
    return facts


def bind_required_cue_combined_stem(
    isolated_facts: Mapping[str, Any],
    *,
    combined_stem_sha256: str,
    combined_command_sha256: str,
) -> dict[str, Any]:
    facts = copy.deepcopy(dict(isolated_facts))
    supplied_hash = str(facts.pop("isolated_receipt_sha256", "") or "")
    if supplied_hash != canonical_hash(facts):
        raise SoundQAError("required cue isolated render receipt is stale")
    for value, label in (
        (combined_stem_sha256, "combined stem"),
        (combined_command_sha256, "combined command"),
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", str(value).casefold()):
            raise SoundQAError(f"required cue {label} SHA-256 is invalid")
    facts["isolated_receipt_sha256"] = supplied_hash
    facts["status"] = "verified"
    facts["combined_stem_sha256"] = str(combined_stem_sha256).casefold()
    facts["combined_command_sha256"] = str(combined_command_sha256).casefold()
    facts["receipt_sha256"] = canonical_hash(facts)
    return facts


def _pcm_float_samples(pcm: Mapping[str, Any]) -> np.ndarray:
    payload = pcm["payload"]
    sample_width = int(pcm["sample_width"])
    channels = int(pcm["channels"])
    if sample_width == 2:
        samples = np.frombuffer(payload, dtype="<i2").astype(np.float64)
        scale = float(1 << 15)
    elif sample_width == 4:
        samples = np.frombuffer(payload, dtype="<i4").astype(np.float64)
        scale = float(1 << 31)
    else:
        raw = np.frombuffer(payload, dtype=np.uint8).reshape(-1, 3)
        values = (
            raw[:, 0].astype(np.int32)
            | (raw[:, 1].astype(np.int32) << 8)
            | (raw[:, 2].astype(np.int32) << 16)
        )
        values = np.where(values & 0x800000, values | ~0xFFFFFF, values)
        samples = values.astype(np.float64)
        scale = float(1 << 23)
    return (samples / scale).reshape(-1, channels)


def _fft_a_weighted_dbfs(samples: np.ndarray, sample_rate: int) -> float:
    if samples.size == 0:
        return -120.0
    frequencies = np.fft.rfftfreq(samples.shape[0], 1.0 / sample_rate)
    squared = frequencies * frequencies
    numerator = (12_194.217**2) * squared * squared
    denominator = (
        (squared + 20.598997**2)
        * np.sqrt(
            (squared + 107.65265**2) * (squared + 737.86223**2)
        )
        * (squared + 12_194.217**2)
    )
    response = np.zeros_like(frequencies)
    np.divide(numerator, denominator, out=response, where=denominator > 0.0)
    response *= 10.0 ** (1.9997 / 20.0)
    spectrum = np.fft.rfft(samples, axis=0)
    weighted = np.fft.irfft(spectrum * response[:, None], n=samples.shape[0], axis=0)
    rms = float(np.sqrt(np.mean(weighted * weighted)))
    return -120.0 if rms <= 0.0 else 20.0 * math.log10(rms)


def measure_required_cue_ab_preview(
    cue_on_wav: str | Path,
    cue_muted_wav: str | Path,
    *,
    cue_start: float,
    cue_end: float,
    window_frames: int = 19_200,
    cue_delta_floor_dbfs: float = -36.0,
    cue_to_masker_floor_db: float = -18.0,
) -> dict[str, Any]:
    """Measure one exact A-weighted cue-on versus cue-muted preview pair.

    This pure, path-free hook intentionally does not infer a muted mix from a
    combined stem.  The caller must supply two renderer-produced PCM previews
    with identical geometry, so the subtraction remains source-attributable.
    """

    on_pcm = _read_required_cue_pcm(cue_on_wav)
    muted_pcm = _read_required_cue_pcm(cue_muted_wav)
    geometry = {
        key: on_pcm[key]
        for key in ("sample_rate", "channels", "sample_width", "sample_count")
    }
    if geometry != {
        key: muted_pcm[key]
        for key in ("sample_rate", "channels", "sample_width", "sample_count")
    }:
        raise SoundQAError("cue-on and cue-muted previews have different geometry")
    sample_rate = int(geometry["sample_rate"])
    start_frame = int(round(float(cue_start) * sample_rate))
    end_frame = int(round(float(cue_end) * sample_rate))
    if (
        window_frames <= 0
        or start_frame < 0
        or end_frame <= start_frame
        or end_frame > int(geometry["sample_count"])
    ):
        raise SoundQAError("cue A/B preview interval is invalid")
    on = _pcm_float_samples(on_pcm)
    muted = _pcm_float_samples(muted_pcm)
    windows: list[dict[str, Any]] = []
    cursor = start_frame
    while cursor + window_frames <= end_frame:
        masker = muted[cursor : cursor + window_frames]
        delta = on[cursor : cursor + window_frames] - masker
        delta_dbfs = _fft_a_weighted_dbfs(delta, sample_rate)
        masker_dbfs = _fft_a_weighted_dbfs(masker, sample_rate)
        cue_to_masker = delta_dbfs - masker_dbfs
        passed = (
            delta_dbfs >= cue_delta_floor_dbfs - 1e-6
            and cue_to_masker >= cue_to_masker_floor_db - 1e-6
        )
        windows.append(
            {
                "start_frame": cursor,
                "end_frame": cursor + window_frames,
                "cue_delta_a_weighted_dbfs": delta_dbfs,
                "masker_a_weighted_dbfs": masker_dbfs,
                "cue_to_masker_db": cue_to_masker,
                "passed": passed,
            }
        )
        cursor += window_frames
    if not windows:
        raise SoundQAError("cue A/B preview has no complete 400 ms window")
    result: dict[str, Any] = {
        "schema_version": 1,
        "contract": "required-cue-a-weighted-ab-preview-v1",
        "cue_on_sha256": on_pcm["stem_sha256"],
        "cue_muted_sha256": muted_pcm["stem_sha256"],
        **geometry,
        "cue_start_frame": start_frame,
        "cue_end_frame": end_frame,
        "window_frames": int(window_frames),
        "cue_delta_floor_dbfs": float(cue_delta_floor_dbfs),
        "cue_to_masker_floor_db": float(cue_to_masker_floor_db),
        "windows": windows,
        "passed": any(row["passed"] for row in windows),
    }
    result["receipt_sha256"] = canonical_hash(result)
    return result


def bind_required_cue_ab_preview(
    combined_facts: Mapping[str, Any],
    ab_preview_facts: Mapping[str, Any],
) -> dict[str, Any]:
    facts = copy.deepcopy(dict(combined_facts))
    combined_receipt = str(facts.pop("receipt_sha256", "") or "")
    if combined_receipt != canonical_hash(facts):
        raise SoundQAError("required cue combined render receipt is stale")
    ab_facts = copy.deepcopy(dict(ab_preview_facts))
    ab_receipt = str(ab_facts.pop("receipt_sha256", "") or "")
    if (
        ab_receipt != canonical_hash(ab_facts)
        or ab_facts.get("contract") != "required-cue-a-weighted-ab-preview-v1"
        or ab_facts.get("passed") is not True
    ):
        raise SoundQAError("required cue A/B preview failed audibility acceptance")
    ab_facts["receipt_sha256"] = ab_receipt
    facts["combined_receipt_sha256"] = combined_receipt
    facts["status"] = "verified_ab"
    facts["ab_preview"] = ab_facts
    facts["receipt_sha256"] = canonical_hash(facts)
    return facts


def _physical_foley_audibility_report(
    plan: Mapping[str, Any],
    *,
    presence_wav: str | Path | None,
    events_wav: str | Path | None,
    render_facts: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    from .acoustic_scene import (
        PHYSICAL_FOLEY_OBLIGATION_CONTRACT,
        REQUIRED_CUE_AUDIBILITY_CONTRACT,
    )

    expected: dict[tuple[str, str], Mapping[str, Any]] = {}
    for scene in plan.get("acoustic_scenes", []):
        slot_id = str(scene.get("slot_id") or "")
        for source in scene.get("visible_sources", []):
            obligation = source.get("physical_foley_obligation")
            if isinstance(obligation, Mapping):
                identity = (slot_id, str(source.get("source_id") or ""))
                if identity in expected:
                    raise SoundQAError("duplicate required physical foley identity")
                expected[identity] = source

    layers: dict[tuple[str, str], tuple[str, Mapping[str, Any]]] = {}
    for lane_name, lane in (
        ("presence", plan.get("presence_layers", [])),
        ("events", plan.get("event_cues", [])),
    ):
        for layer in lane:
            for slot_id in layer.get("slot_ids", []):
                identity = (str(slot_id), str(layer.get("visual_source_id") or ""))
                if identity in layers:
                    raise SoundQAError("duplicate required cue layer identity")
                layers[identity] = (lane_name, layer)

    facts_by_identity: dict[tuple[str, str], Mapping[str, Any]] = {}
    for index, row in enumerate(render_facts or []):
        if not isinstance(row, Mapping):
            raise SoundQAError(f"required cue render fact {index} must be a mapping")
        identity_row = row.get("identity")
        if not isinstance(identity_row, Mapping):
            raise SoundQAError("required cue render fact identity is missing")
        identity = (
            str(identity_row.get("slot_id") or ""),
            str(identity_row.get("visual_source_id") or ""),
        )
        if identity in facts_by_identity:
            raise SoundQAError("duplicate required cue render fact identity")
        facts_by_identity[identity] = row

    failures: list[dict[str, Any]] = []
    measurements: list[dict[str, Any]] = []
    covered_by_asset = 0
    for identity, source in expected.items():
        located = layers.get(identity)
        if located is None:
            failures.append(
                {"slot_id": identity[0], "source_id": identity[1], "reason": "missing_layer"}
            )
            continue
        lane_name, layer = located
        if isinstance(layer.get("asset"), Mapping):
            covered_by_asset += 1
        else:
            failures.append(
                {"slot_id": identity[0], "source_id": identity[1], "reason": "missing_asset"}
            )

    for identity, source in sorted(expected.items()):
        located = layers.get(identity)
        if located is None:
            continue
        lane_name, layer = located
        obligation = source.get("physical_foley_obligation")
        audibility = source.get("required_audibility")
        if (
            not isinstance(obligation, Mapping)
            or obligation.get("contract") != PHYSICAL_FOLEY_OBLIGATION_CONTRACT
            or not isinstance(audibility, Mapping)
            or audibility.get("contract") != REQUIRED_CUE_AUDIBILITY_CONTRACT
        ):
            failures.append(
                {"slot_id": identity[0], "source_id": identity[1], "reason": "invalid_contract"}
            )
            continue
        stem = presence_wav if lane_name == "presence" else events_wav
        if stem is None:
            failures.append(
                {"slot_id": identity[0], "source_id": identity[1], "reason": "measurement_missing"}
            )
            continue
        fact = facts_by_identity.get(identity)
        if fact is None:
            failures.append(
                {
                    "slot_id": identity[0],
                    "source_id": identity[1],
                    "reason": "isolated_render_fact_missing",
                }
            )
            continue
        try:
            unsigned = copy.deepcopy(dict(fact))
            receipt_sha = str(unsigned.pop("receipt_sha256", "") or "")
            if receipt_sha != canonical_hash(unsigned):
                raise SoundQAError("required cue render receipt is stale")
            if (
                fact.get("schema_version") != 1
                or fact.get("contract") != REQUIRED_CUE_RENDER_RECEIPT_CONTRACT
                or fact.get("status") != "verified_ab"
                or fact.get("identity")
                != {
                    "slot_id": identity[0],
                    "visual_source_id": identity[1],
                    "obligation_key": obligation.get("obligation_key"),
                    "lane": lane_name,
                }
                or fact.get("audibility_binding") != audibility
                or fact.get("original_layer_binding")
                != _required_cue_layer_binding(layer)
            ):
                raise SoundQAError("required cue render binding is stale")
            ab_preview = fact.get("ab_preview")
            if not isinstance(ab_preview, Mapping):
                raise SoundQAError("required cue A/B preview is missing")
            ab_unsigned = copy.deepcopy(dict(ab_preview))
            ab_hash = str(ab_unsigned.pop("receipt_sha256", "") or "")
            if (
                ab_hash != canonical_hash(ab_unsigned)
                or ab_preview.get("contract")
                != "required-cue-a-weighted-ab-preview-v1"
                or ab_preview.get("passed") is not True
            ):
                raise SoundQAError("required cue A/B preview is stale or failed")
            final_metrics = fact.get("final_isolated")
            if not isinstance(final_metrics, Mapping) or not _required_cue_metrics_pass(
                final_metrics, audibility
            ):
                raise SoundQAError("required cue isolated metrics are invalid")
            combined_sha = sha256_file(stem)
            if fact.get("combined_stem_sha256") != combined_sha:
                raise SoundQAError("required cue combined stem hash is stale")
        except (KeyError, SoundQAError, TypeError, ValueError) as exc:
            failures.append(
                {
                    "slot_id": identity[0],
                    "source_id": identity[1],
                    "reason": "isolated_render_fact_invalid",
                    "detail": str(exc),
                }
            )
            continue
        measurements.append(
            {
                "slot_id": identity[0],
                "source_id": identity[1],
                "obligation_key": obligation.get("obligation_key"),
                "lane": lane_name,
                "combined_stem_sha256": combined_sha,
                "effective_gain_db": fact["gain"]["effective_gain_db"],
                "final_isolated": copy.deepcopy(dict(final_metrics)),
                "ab_preview": copy.deepcopy(dict(ab_preview)),
                "passed": True,
            }
        )
    for identity in sorted(set(facts_by_identity) - set(expected)):
        failures.append(
            {
                "slot_id": identity[0],
                "source_id": identity[1],
                "reason": "unexpected_isolated_render_fact",
            }
        )
    return {
        "contract": REQUIRED_CUE_AUDIBILITY_CONTRACT,
        "expected": len(expected),
        "covered_by_asset": covered_by_asset,
        "measured": len(measurements),
        "passed": not failures and len(measurements) == len(expected),
        "failures": failures,
        "measurements": measurements,
    }


def run_sound_qa(
    plan: Mapping[str, Any],
    timeline: Mapping[str, Any],
    *,
    ambience_wav: str | Path | None = None,
    presence_wav: str | Path | None = None,
    events_wav: str | Path | None = None,
    physical_foley_render_facts: Sequence[Mapping[str, Any]] | None = None,
    ambience_energy_windows: Sequence[Mapping[str, Any]] | None = None,
    silence_threshold_dbfs: float = -55.0,
    max_ambience_gap_seconds: float = MAX_AMBIENCE_GAP_SECONDS,
    release_mode: bool = True,
    verify_hashes: bool = True,
    require_causal_compilation: bool = False,
) -> dict[str, Any]:
    """Return a machine release report; any contradiction fails closed."""

    duration = _finite(plan.get("timeline_duration"), "timeline_duration")
    structural_errors: list[str] = []
    try:
        validate_sound_plan(
            plan,
            timeline,
            require_resolved_assets=release_mode,
            require_production_v2=True,
            require_causal_compilation=require_causal_compilation,
        )
    except SoundPlanError as exc:
        structural_errors.append(str(exc))

    intervals = [
        (float(region.get("start", 0.0)), float(region.get("end", 0.0)))
        for region in plan.get("ambience_regions", [])
        if isinstance(region, Mapping)
    ]
    coverage = coverage_report(intervals, duration) if intervals else {
        "duration": duration,
        "covered_seconds": 0.0,
        "coverage_ratio": 0.0,
        "gaps": [{"start": 0.0, "end": duration, "duration": duration}],
        "overlaps": [],
    }

    energy_analysis_missing = False
    if ambience_wav is not None:
        windows, measured_duration = measure_wav_energy(ambience_wav)
        if abs(measured_duration - duration) > 0.051:
            structural_errors.append(
                f"ambience stem duration {measured_duration:.6f}s disagrees with timeline {duration:.6f}s"
            )
        energy_gaps = _energy_gaps_from_windows(
            windows,
            duration=duration,
            silence_threshold_dbfs=silence_threshold_dbfs,
            max_gap_seconds=max_ambience_gap_seconds,
        )
    elif ambience_energy_windows is not None:
        energy_gaps = _energy_gaps_from_windows(
            ambience_energy_windows,
            duration=duration,
            silence_threshold_dbfs=silence_threshold_dbfs,
            max_gap_seconds=max_ambience_gap_seconds,
        )
    else:
        energy_gaps = []
        energy_analysis_missing = release_mode

    taxonomy = _taxonomy_issues(plan)
    unresolved, contamination, provenance_errors = _asset_issues(
        plan, verify_hashes=verify_hashes
    )
    notable = _notable_source_coverage(plan)
    physical_foley = _physical_foley_audibility_report(
        plan,
        presence_wav=presence_wav,
        events_wav=events_wav,
        render_facts=physical_foley_render_facts,
    )
    exact_total = 0
    exact_correct = 0
    wrong_exact_ids = {row["layer_id"] for row in taxonomy["wrong_species"]}
    for _lane, layer_id, layer in _iter_natural_layers(plan):
        if (layer.get("taxonomy") or {}).get("specificity") == "exact_species":
            exact_total += 1
            if layer_id not in wrong_exact_ids and isinstance(layer.get("asset"), Mapping):
                exact_correct += 1
    exact_precision = exact_correct / exact_total if exact_total else 1.0

    blockers = (
        len(structural_errors)
        + len(coverage["gaps"])
        + len(coverage["overlaps"])
        + len(energy_gaps)
        + int(energy_analysis_missing)
        + len(unresolved)
        + len(contamination)
        + len(provenance_errors)
        + sum(len(rows) for rows in taxonomy.values())
        + int(exact_precision < 0.95)
        + int(notable["weighted_ratio"] < 0.90)
        + int(not physical_foley["passed"])
    )
    report = {
        "schema_version": 1,
        "sound_plan_schema_version": plan.get("schema_version"),
        "timeline_fingerprint": plan.get("timeline_fingerprint"),
        "release_mode": bool(release_mode),
        "verdict": "PASS" if blockers == 0 else "FAIL",
        "blocker_count": blockers,
        "structural_errors": structural_errors,
        "lanes": {
            "music": {"segments": len(plan.get("music_segments", []))},
            "ambience": {
                "regions": len(plan.get("ambience_regions", [])),
                "coverage": coverage,
                "energy_analysis_missing": energy_analysis_missing,
                "energy_gaps": energy_gaps,
            },
            "presence": {"layers": len(plan.get("presence_layers", []))},
            "events": {"cues": len(plan.get("event_cues", []))},
        },
        **taxonomy,
        "unresolved_assets": unresolved,
        "contamination": contamination,
        "provenance_errors": provenance_errors,
        "exact_species_precision": exact_precision,
        "notable_source_coverage": notable,
        "physical_foley_obligations": physical_foley,
        "acceptance": {
            "ambience_coverage_ratio": 1.0,
            "max_energy_gap_seconds": max_ambience_gap_seconds,
            "exact_species_precision_min": 0.95,
            "notable_source_weighted_coverage_min": 0.90,
            "wrong_species_or_action": 0,
            "speech_or_music_contamination": 0,
        },
    }
    return report


__all__ = [
    "REQUIRED_CUE_RENDER_RECEIPT_CONTRACT",
    "SoundQAError",
    "bind_required_cue_ab_preview",
    "bind_required_cue_combined_stem",
    "coverage_report",
    "derive_required_cue_static_gain",
    "detect_ambience_energy_gaps",
    "finalize_required_cue_static_gain",
    "measure_required_cue_ab_preview",
    "measure_wav_energy",
    "required_cue_preview_layer",
    "run_sound_qa",
]
