"""Hash-bound, fail-closed release gate for production montage artifacts."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .audio import AMBIENCE_ENERGY_FLOOR_POLICY_VERSION
from .common import atomic_write_json, canonical_hash, sha256_file


_PRODUCTION_AUDIO_MODE = (
    "production_v2_five_lane_ambience_energy_floor_two_pass"
)
_CANONICAL_AMBIENCE_ENERGY_FLOOR_POLICY = {
    "version": AMBIENCE_ENERGY_FLOOR_POLICY_VERSION,
    "outer_threshold_dbfs": -65.0,
    "outer_window_frames": 480,
    "outer_max_silent_windows": 10,
    "semantic_threshold_dbfs": -55.0,
    "semantic_window_frames": 2_400,
    "semantic_max_silent_windows": 2,
    "safety_margin_db": 2.0,
    "max_effective_gain_db": 12.0,
    "per_region_peak_ceiling_dbfs": -9.0,
    "method": "static_per_region_gain_from_exact_provisional_pcm",
    "window_grid_origin_frame": 0,
    "region_assignment": "every_failing_global_block_intersecting_region",
    "dynamic_normalization": False,
}
_CANONICAL_AMBIENCE_ENERGY_FLOOR_POLICY_HASH = canonical_hash(
    _CANONICAL_AMBIENCE_ENERGY_FLOOR_POLICY
)


class ReleaseBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseInputs:
    master: Path
    timeline: Path
    sound_plan: Path
    render_manifest: Path
    visual_qa: Path
    boundary_qa: Path
    overlay_qa: Path
    audio_qa: Path

    def paths(self) -> dict[str, Path]:
        return {
            "master": self.master,
            "timeline": self.timeline,
            "sound_plan": self.sound_plan,
            "render_manifest": self.render_manifest,
            "visual_qa": self.visual_qa,
            "boundary_qa": self.boundary_qa,
            "overlay_qa": self.overlay_qa,
            "audio_qa": self.audio_qa,
        }


def _json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ReleaseBlocked(f"expected JSON object: {path.name}")
    return payload


def _number(value: Any, default: float = float("nan")) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fingerprint_inputs(inputs: ReleaseInputs) -> dict[str, str]:
    hashes = {}
    for name, path in inputs.paths().items():
        path = Path(path).expanduser().resolve()
        if not path.is_file():
            raise ReleaseBlocked(f"release input missing: {name}")
        hashes[name] = sha256_file(path)
    return hashes


def _require_report_binding(
    report_name: str,
    report: Mapping[str, Any],
    hashes: Mapping[str, str],
) -> list[str]:
    errors = []
    if not bool(report.get("passed")):
        errors.append(f"{report_name} did not pass")
    bindings = report.get("input_hashes")
    if not isinstance(bindings, Mapping) or not bindings:
        errors.append(f"{report_name} has no immutable input_hashes")
        return errors
    for name, digest in bindings.items():
        if name not in hashes or hashes[name] != digest:
            errors.append(f"{report_name} stale binding: {name}")
    return errors


def _continuous_ambience_errors(
    timeline: Mapping[str, Any],
    sound_plan: Mapping[str, Any],
    audio_qa: Mapping[str, Any],
) -> list[str]:
    """Derive ambience acceptance from intervals and rendered-stem QA.

    A producer-supplied scalar such as ``sound_plan.ambience_coverage`` is not
    evidence and is deliberately ignored. Coverage is recomputed on exact
    48 kHz frame boundaries and cross-checked against the hash-bound audio QA.
    """

    errors: list[str] = []
    try:
        duration = float(timeline.get("narration_duration"))
    except (TypeError, ValueError):
        errors.append("timeline has no numeric narration duration for ambience QA")
        return errors
    if duration <= 0.0:
        errors.append("timeline duration is not positive for ambience QA")
        return errors
    sample_rate = 48_000
    target_frames = int(round(duration * sample_rate))
    if abs(duration - target_frames / sample_rate) > 0.5 / sample_rate + 1e-12:
        errors.append("timeline duration is not representable at 48 kHz")

    regions = sound_plan.get("ambience_regions")
    if not isinstance(regions, list) or not regions:
        errors.append("sound plan has no ambience regions")
    else:
        cursor = 0
        for index, row in enumerate(regions):
            if not isinstance(row, Mapping):
                errors.append(f"ambience region {index} is malformed")
                continue
            try:
                start = float(row.get("start"))
                end = float(row.get("end"))
            except (TypeError, ValueError):
                errors.append(f"ambience region {index} has invalid bounds")
                continue
            start_frame = int(round(start * sample_rate))
            end_frame = int(round(end * sample_rate))
            if (
                abs(start - start_frame / sample_rate) > 0.5 / sample_rate + 1e-12
                or abs(end - end_frame / sample_rate) > 0.5 / sample_rate + 1e-12
            ):
                errors.append(f"ambience region {index} is not 48 kHz frame-exact")
            if start_frame != cursor or end_frame <= start_frame:
                errors.append(f"ambience gap/overlap at region {index}")
            cursor = max(cursor, end_frame)
        if cursor != target_frames:
            errors.append("ambience regions do not cover the full timeline")

    stems = audio_qa.get("stems")
    ambience_stem = stems.get("ambience") if isinstance(stems, Mapping) else None
    if not isinstance(ambience_stem, Mapping) or not bool(ambience_stem.get("passed")):
        errors.append("rendered ambience stem QA did not pass")
    else:
        try:
            measured_sample_rate = int(ambience_stem.get("sample_rate") or 0)
            measured_frames = int(ambience_stem.get("frames") or 0)
        except (TypeError, ValueError):
            measured_sample_rate = measured_frames = 0
        if measured_sample_rate != sample_rate:
            errors.append("rendered ambience stem is not 48 kHz")
        if measured_frames != target_frames:
            errors.append("rendered ambience stem does not cover every audio frame")
    energy = audio_qa.get("ambience_energy")
    if not isinstance(energy, Mapping) or not bool(energy.get("passed")):
        errors.append("rendered ambience contains an unaccepted energy gap")

    semantic = audio_qa.get("semantic")
    structural = semantic.get("sound_qa") if isinstance(semantic, Mapping) else {}
    if not isinstance(structural, Mapping):
        structural = {}
    lanes = structural.get("lanes")
    ambience_lane = lanes.get("ambience") if isinstance(lanes, Mapping) else {}
    coverage = (
        ambience_lane.get("coverage") if isinstance(ambience_lane, Mapping) else {}
    )
    if not isinstance(coverage, Mapping):
        coverage = {}
    if structural.get("verdict") != "PASS":
        errors.append("hash-bound sound QA did not pass")
    try:
        coverage_ratio = float(coverage.get("coverage_ratio") or 0.0)
    except (TypeError, ValueError):
        coverage_ratio = 0.0
    if (
        coverage_ratio < 1.0
        or coverage.get("gaps")
        or coverage.get("overlaps")
    ):
        errors.append("hash-bound sound QA did not prove continuous ambience")
    return errors


def build_release_report(inputs: ReleaseInputs) -> dict[str, Any]:
    hashes_before = fingerprint_inputs(inputs)
    timeline = _json(inputs.timeline)
    sound_plan = _json(inputs.sound_plan)
    manifest = _json(inputs.render_manifest)
    reports = {
        "visual_qa": _json(inputs.visual_qa),
        "boundary_qa": _json(inputs.boundary_qa),
        "overlay_qa": _json(inputs.overlay_qa),
        "audio_qa": _json(inputs.audio_qa),
    }
    errors: list[str] = []
    if timeline.get("schema_version") != 2 or timeline.get("contract") != "production_frame_exact":
        errors.append("timeline is not production frame-exact schema v2")
    slots = timeline.get("slots") or []
    if not slots:
        errors.append("timeline has no slots")
    for slot in slots:
        slot_id = slot.get("slot_id")
        if slot.get("status") != "resolved" or slot.get("verdict") not in {"exact", "relevant"}:
            errors.append(f"slot {slot_id} is not semantically resolved")
        clip = slot.get("selected_clip") or {}
        if clip.get("fill_strategy") != "none" or _number(clip.get("retime_factor"), 0.0) != 1.0:
            errors.append(f"slot {slot_id} uses hidden fill/retime")
        if (clip.get("overlay_analysis") or {}).get("status") not in {"clean", "resolved"}:
            errors.append(f"slot {slot_id} has unresolved overlay")
        if not slot.get("selection_provenance"):
            errors.append(f"slot {slot_id} lacks selection provenance")
    if sound_plan.get("schema_version") != 2 or sound_plan.get("status") != "ready":
        errors.append("sound plan is not ready schema v2")
    errors.extend(_continuous_ambience_errors(timeline, sound_plan, reports["audio_qa"]))
    output = manifest.get("output") or {}
    if output.get("sha256") != hashes_before["master"]:
        errors.append("manifest master hash mismatch")
    proof = manifest.get("donor_audio_proof") or {}
    if not bool(proof.get("passed")):
        errors.append("donor audio exclusion proof failed")
    if proof.get("final_video_input_audio_map") is not False:
        errors.append("final master has no structural proof that donor audio was unmapped")
    if _number(proof.get("aac_encode_count"), 0.0) != 1.0:
        errors.append("final master does not prove exactly one AAC encode")
    manifest_audio = manifest.get("audio")
    if not isinstance(manifest_audio, Mapping):
        errors.append("render manifest has no production audio facts")
        manifest_audio = {}
    if manifest_audio.get("mode") != _PRODUCTION_AUDIO_MODE:
        errors.append(
            "render did not use the verified ambience energy-floor production mix"
        )
    if _number(manifest_audio.get("aac_encode_count"), 0.0) != 1.0:
        errors.append("render audio facts do not prove one AAC encode")
    rendered_stems = manifest_audio.get("stems")
    if not isinstance(rendered_stems, Mapping):
        rendered_stems = {}
    for lane in ("music", "ambience", "presence", "events"):
        facts = rendered_stems.get(lane)
        if not isinstance(facts, Mapping) or not str(facts.get("sha256") or ""):
            errors.append(f"render manifest lacks immutable {lane} stem facts")
    energy_floor = manifest_audio.get("ambience_energy_floor")
    if not isinstance(energy_floor, Mapping):
        errors.append("render manifest lacks verified ambience energy-floor facts")
    else:
        energy_material = dict(energy_floor)
        supplied_receipt = energy_material.pop("receipt_sha256", None)
        policy = energy_floor.get("policy")
        rebuilt = energy_floor.get("rebuilt_stem")
        rendered_ambience = rendered_stems.get("ambience")
        rendered_ambience_hash = (
            rendered_ambience.get("sha256")
            if isinstance(rendered_ambience, Mapping)
            else None
        )
        if (
            energy_floor.get("contract")
            != AMBIENCE_ENERGY_FLOOR_POLICY_VERSION
            or energy_floor.get("status") != "verified_rebuilt"
            or supplied_receipt != canonical_hash(energy_material)
            or not isinstance(policy, Mapping)
            or canonical_hash(dict(policy))
            != _CANONICAL_AMBIENCE_ENERGY_FLOOR_POLICY_HASH
            or not isinstance(rebuilt, Mapping)
            or rebuilt.get("sha256") != rendered_ambience_hash
            or rebuilt.get("outer_energy_gaps")
            or rebuilt.get("semantic_energy_gaps")
            or rebuilt.get("boundary_click_flags")
        ):
            errors.append("render ambience energy-floor proof is invalid")
    audio_stem_hashes = reports["audio_qa"].get("stem_hashes")
    if not isinstance(audio_stem_hashes, Mapping):
        audio_stem_hashes = {}
    for lane in ("ambience", "presence", "events"):
        rendered = rendered_stems.get(lane)
        rendered_hash = rendered.get("sha256") if isinstance(rendered, Mapping) else None
        if not rendered_hash or audio_stem_hashes.get(lane) != rendered_hash:
            errors.append(f"audio QA is not bound to the rendered {lane} stem")
    manifest_qa = manifest.get("qa")
    if not isinstance(manifest_qa, Mapping) or not bool(manifest_qa.get("passed")):
        errors.append("final master runtime QA did not pass")
    elif str(manifest_qa.get("audio_codec") or "").casefold() != "aac":
        errors.append("final master runtime QA did not verify AAC")
    clip_facts = manifest.get("clips") or []
    if len(clip_facts) != len(slots):
        errors.append("render clip facts do not cover every slot")
    for row in clip_facts:
        if row.get("fill_strategy") != "none" or _number(row.get("retime_factor"), 0.0) != 1.0:
            errors.append(f"rendered clip {row.get('slot_id')} uses fill/retime")
    for name, report in reports.items():
        errors.extend(_require_report_binding(name, report, hashes_before))
    # Detect a writer mutating any release input while the gate is reading it.
    hashes_after = fingerprint_inputs(inputs)
    if hashes_after != hashes_before:
        errors.append("release input changed during QA")
    return {
        "schema_version": 2,
        "passed": not errors,
        "errors": errors,
        "created_at": time.time(),
        "input_hashes": hashes_after,
        "release_fingerprint": canonical_hash(hashes_after),
        "slot_count": len(slots),
        "checks": {
            "production_timeline": not any("timeline" in row or "slot " in row for row in errors),
            "continuous_ambience": not any("ambience" in row for row in errors),
            "no_donor_audio": not any("donor audio" in row for row in errors),
            "all_qa_hash_bound": not any("binding" in row for row in errors),
        },
    }


def write_release_report(inputs: ReleaseInputs, output: str | os.PathLike[str]) -> dict[str, Any]:
    report = build_release_report(inputs)
    atomic_write_json(output, report)
    if not report["passed"]:
        raise ReleaseBlocked("; ".join(report["errors"][:8]))
    return report


def verify_release_report(report_path: str | os.PathLike[str], inputs: ReleaseInputs) -> bool:
    report = _json(Path(report_path).expanduser().resolve())
    return bool(report.get("passed")) and report.get("input_hashes") == fingerprint_inputs(inputs)


__all__ = [
    "ReleaseBlocked",
    "ReleaseInputs",
    "build_release_report",
    "fingerprint_inputs",
    "verify_release_report",
    "write_release_report",
]
