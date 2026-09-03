"""FFmpeg command builders for the semantic-montage audio lane.

The module deliberately keeps donor-video audio out of its API.  It accepts a
finished, video-only timeline file and audio sources that are independent of
the donor videos.  Intermediate audio is lossless PCM; AAC is encoded exactly
once, in either :func:`build_final_mix_command` or
:func:`build_layered_final_mix_command`.

All public command builders return JSON-friendly ``(argv, facts)`` tuples so a
caller can store both the executable command and the relevant decisions in a
montage manifest without parsing shell strings.
"""

from __future__ import annotations

import audioop
import copy
import hashlib
import json
import math
import re
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .common import sha256_file


class AudioPlanError(ValueError):
    """Raised when an audio render request violates the mix contract."""


AMBIENCE_ENERGY_FLOOR_POLICY_VERSION = "ambience-energy-floor-static-gain-v1"


@dataclass(frozen=True)
class AudioMixConfig:
    """Conservative spoken-documentary mix defaults.

    ``background_gain`` is intentionally low before side-chain compression.
    The final loudness target is EBU R128 ``-16 LUFS`` and ``-1 dBTP``.
    """

    sample_rate: int = 48_000
    channels: int = 2
    background_gain: float = 0.20
    sfx_gain: float = 0.55
    ambience_gain: float = 0.65
    presence_gain: float = 0.75
    event_gain: float = 0.90
    crossfade_seconds: float = 1.50
    duck_threshold: float = 0.020
    duck_ratio: float = 12.0
    sfx_duck_ratio: float = 4.0
    ambience_duck_ratio: float = 5.0
    presence_duck_ratio: float = 3.0
    event_duck_ratio: float = 2.0
    duck_attack_ms: float = 20.0
    duck_release_ms: float = 400.0
    narration_lufs: float = -18.0
    target_lufs: float = -16.0
    target_lra: float = 7.0
    true_peak_db: float = -1.0
    aac_bitrate: str = "192k"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "AudioMixConfig":
        if not value:
            return cls()
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise AudioPlanError(f"Unknown audio config fields: {', '.join(unknown)}")
        return cls(**dict(value))

    def validate(self) -> None:
        if self.sample_rate <= 0:
            raise AudioPlanError("sample_rate must be positive")
        if self.channels != 2:
            raise AudioPlanError("MVP mix contract requires stereo output")
        if not 0.0 < self.background_gain <= 1.0:
            raise AudioPlanError("background_gain must be in (0, 1]")
        if not 0.0 <= self.sfx_gain <= 2.0:
            raise AudioPlanError("sfx_gain must be in [0, 2]")
        for name, value in (
            ("ambience_gain", self.ambience_gain),
            ("presence_gain", self.presence_gain),
            ("event_gain", self.event_gain),
        ):
            if not 0.0 <= value <= 2.0:
                raise AudioPlanError(f"{name} must be in [0, 2]")
        if not 0.0 <= self.crossfade_seconds <= 10.0:
            raise AudioPlanError("crossfade_seconds must be in [0, 10]")
        if not 0.000976563 <= self.duck_threshold <= 1.0:
            raise AudioPlanError("duck_threshold is outside FFmpeg limits")
        if not 1.0 <= self.duck_ratio <= 20.0:
            raise AudioPlanError("duck_ratio is outside FFmpeg limits")
        if not 1.0 <= self.sfx_duck_ratio <= 20.0:
            raise AudioPlanError("sfx_duck_ratio is outside FFmpeg limits")
        for name, value in (
            ("ambience_duck_ratio", self.ambience_duck_ratio),
            ("presence_duck_ratio", self.presence_duck_ratio),
            ("event_duck_ratio", self.event_duck_ratio),
        ):
            if not 1.0 <= value <= 20.0:
                raise AudioPlanError(f"{name} is outside FFmpeg limits")
        if not 0.01 <= self.duck_attack_ms <= 2_000.0:
            raise AudioPlanError("duck_attack_ms is outside FFmpeg limits")
        if not 0.01 <= self.duck_release_ms <= 9_000.0:
            raise AudioPlanError("duck_release_ms is outside FFmpeg limits")
        if not -70.0 <= self.target_lufs <= -5.0:
            raise AudioPlanError("target_lufs is outside loudnorm limits")
        if not -9.0 <= self.true_peak_db <= 0.0:
            raise AudioPlanError("true_peak_db is outside loudnorm limits")


def _positive_duration(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise AudioPlanError(f"{name} must be numeric") from exc
    if not math.isfinite(result) or result <= 0:
        raise AudioPlanError(f"{name} must be a finite positive duration")
    return result


def _overwrite_flag(overwrite: bool) -> str:
    return "-y" if overwrite else "-n"


def _fmt(value: float) -> str:
    return f"{value:.6f}"


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise AudioPlanError(f"{name} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise AudioPlanError(f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise AudioPlanError(f"{name} must be finite")
    return result


def _nonnegative_number(value: Any, name: str) -> float:
    result = _finite_number(value, name)
    if result < 0:
        raise AudioPlanError(f"{name} must be non-negative")
    return result


def _gain(value: Any, name: str) -> float:
    result = _finite_number(value, name)
    if not 0.0 <= result <= 4.0:
        raise AudioPlanError(f"{name} must be in [0, 4]")
    return result


def _sample_count(
    value: Any,
    name: str,
    sample_rate: int,
    *,
    allow_zero: bool = False,
) -> tuple[float, int]:
    """Resolve seconds to an exact integer PCM-frame boundary.

    Production-v2 never rounds an editorial timestamp by milliseconds.  A
    timestamp that cannot be represented at the configured sample rate is an
    invalid plan and is rejected before FFmpeg runs.
    """

    seconds = _finite_number(value, name)
    if seconds < 0 or (seconds == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise AudioPlanError(f"{name} must be {qualifier}")
    frames = int(round(seconds * sample_rate))
    if frames < 0 or (frames == 0 and not allow_zero):
        raise AudioPlanError(f"{name} is shorter than one PCM frame")
    resolved = frames / sample_rate
    if abs(seconds - resolved) > (0.5 / sample_rate) + 1e-12:
        raise AudioPlanError(
            f"{name}={seconds:.9f}s is not representable at {sample_rate} Hz"
        )
    return resolved, frames


def _fmt_samples(frames: int, sample_rate: int) -> str:
    return f"{frames / sample_rate:.9f}"


def _layer_path(raw: Mapping[str, Any], name: str) -> str:
    value = raw.get("path")
    if value is None and isinstance(raw.get("asset"), Mapping):
        value = raw["asset"].get("path")
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise AudioPlanError(f"{name}.path is required")
    return str(Path(value).expanduser())


def _layer_source_duration(raw: Mapping[str, Any], name: str) -> float:
    value = raw.get("source_duration")
    if value is None and isinstance(raw.get("asset"), Mapping):
        value = raw["asset"].get("duration")
    return _positive_duration(value, f"{name}.source_duration")


def _layer_gain(raw: Mapping[str, Any], name: str) -> float:
    if raw.get("gain") is not None and raw.get("gain_db") is not None:
        raise AudioPlanError(f"{name} cannot contain both gain and gain_db")
    if raw.get("gain_db") is not None:
        gain_db = _finite_number(raw.get("gain_db"), f"{name}.gain_db")
        if not -96.0 <= gain_db <= 12.0:
            raise AudioPlanError(f"{name}.gain_db must be in [-96, 12]")
        return 10.0 ** (gain_db / 20.0)
    return _gain(raw.get("gain", 1.0), f"{name}.gain")


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _dbfs(amplitude: float) -> float:
    return 20.0 * math.log10(amplitude) if amplitude > 0.0 else float("-inf")


def _region_gain_facts(raw: Mapping[str, Any], name: str) -> tuple[float, float]:
    gain = _layer_gain(raw, name)
    if gain <= 0.0:
        raise AudioPlanError(f"{name} has zero gain in a mandatory ambience lane")
    return gain, 20.0 * math.log10(gain)


def _energy_floor_region_projection(
    regions: Sequence[Mapping[str, Any]], sample_rate: int
) -> list[dict[str, Any]]:
    projection: list[dict[str, Any]] = []
    for index, raw in enumerate(regions):
        prefix = f"regions[{index}]"
        if not isinstance(raw, Mapping):
            raise AudioPlanError(f"{prefix} must be a mapping")
        if raw.get("start_frame") is not None and raw.get("end_frame") is not None:
            start_frame = int(raw["start_frame"])
            end_frame = int(raw["end_frame"])
        else:
            _start, start_frame = _sample_count(
                raw.get("start"), f"{prefix}.start", sample_rate, allow_zero=True
            )
            _end, end_frame = _sample_count(
                raw.get("end"), f"{prefix}.end", sample_rate
            )
        gain, gain_db = _region_gain_facts(raw, prefix)
        projection.append(
            {
                "region_id": str(raw.get("region_id") or f"ambience-{index}"),
                "start_frame": start_frame,
                "end_frame": end_frame,
                "effective_gain": round(gain, 12),
                "effective_gain_db": round(gain_db, 6),
            }
        )
    return projection


def _pcm_window_dbfs(
    source: wave.Wave_read,
    *,
    start_frame: int,
    end_frame: int,
    window_frames: int,
    mono: bool,
) -> tuple[list[float], float]:
    source.setpos(start_frame)
    remaining = end_frame - start_frame
    sample_width = source.getsampwidth()
    channels = source.getnchannels()
    full_scale = float(1 << (sample_width * 8 - 1))
    db_windows: list[float] = []
    peak = 0.0
    while remaining > 0:
        count = min(window_frames, remaining)
        payload = source.readframes(count)
        expected_bytes = count * channels * sample_width
        if len(payload) != expected_bytes:
            raise AudioPlanError("provisional ambience PCM is truncated")
        peak = max(peak, audioop.max(payload, sample_width) / full_scale)
        measured = (
            audioop.tomono(payload, sample_width, 0.5, 0.5)
            if mono
            else payload
        )
        db_windows.append(_dbfs(audioop.rms(measured, sample_width) / full_scale))
        remaining -= count
    return db_windows, peak


def _overlapping_block_windows(
    windows_dbfs: Sequence[float],
    *,
    start_frame: int,
    end_frame: int,
    window_frames: int,
    max_silent_windows: int,
) -> Sequence[float]:
    """Select globally aligned QA blocks that intersect one plan region."""

    block_size = max_silent_windows + 1
    block_count = len(windows_dbfs) - block_size + 1
    if block_count <= 0:
        return ()
    first_block = max(0, start_frame // window_frames - block_size + 1)
    last_block = min(block_count, (end_frame - 1) // window_frames + 1)
    if last_block <= first_block:
        return ()
    return windows_dbfs[first_block:last_block + block_size - 1]


def _required_static_boost_db(
    windows_dbfs: Sequence[float],
    *,
    threshold_dbfs: float,
    max_silent_windows: int,
) -> tuple[bool, float]:
    block_size = max_silent_windows + 1
    if len(windows_dbfs) < block_size:
        return False, 0.0
    needs = [
        threshold_dbfs - max(windows_dbfs[index:index + block_size])
        for index in range(len(windows_dbfs) - block_size + 1)
    ]
    failing = any(value >= 0.0 for value in needs)
    return failing, max(0.0, max(needs)) if failing else 0.0


def derive_ambience_energy_floor_adjustments(
    pcm_wav: str | Path,
    regions: Sequence[Mapping[str, Any]],
    *,
    sample_rate: int = 48_000,
    outer_threshold_dbfs: float = -65.0,
    outer_window_frames: int = 480,
    outer_max_silent_windows: int = 10,
    semantic_threshold_dbfs: float = -55.0,
    semantic_window_frames: int = 2_400,
    semantic_max_silent_windows: int = 2,
    safety_margin_db: float = 2.0,
    max_effective_gain_db: float = 12.0,
    per_region_peak_ceiling_dbfs: float = -9.0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Derive deterministic per-region gain from an exact provisional stem.

    The provisional stem already contains the real decoder output, loop joins,
    source offsets, plan gain and region crossfades.  This pass only measures
    fixed PCM windows; it never performs dynamic normalization or compression.
    """

    source_path = Path(pcm_wav).expanduser().resolve()
    if not source_path.is_file():
        raise AudioPlanError(f"provisional ambience PCM does not exist: {source_path}")
    if isinstance(regions, (str, bytes)) or not isinstance(regions, Sequence):
        raise AudioPlanError("regions must be a sequence of mappings")
    if not regions:
        raise AudioPlanError("ambience energy-floor analysis requires regions")
    integer_fields = {
        "sample_rate": sample_rate,
        "outer_window_frames": outer_window_frames,
        "outer_max_silent_windows": outer_max_silent_windows,
        "semantic_window_frames": semantic_window_frames,
        "semantic_max_silent_windows": semantic_max_silent_windows,
    }
    if any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        for value in integer_fields.values()
    ):
        raise AudioPlanError(
            "ambience energy-floor integer policy values must be positive integers"
        )
    for name, value in (
        ("outer_threshold_dbfs", outer_threshold_dbfs),
        ("semantic_threshold_dbfs", semantic_threshold_dbfs),
        ("safety_margin_db", safety_margin_db),
        ("max_effective_gain_db", max_effective_gain_db),
        ("per_region_peak_ceiling_dbfs", per_region_peak_ceiling_dbfs),
    ):
        if not math.isfinite(float(value)):
            raise AudioPlanError(f"{name} must be finite")
    if safety_margin_db <= 0.0:
        raise AudioPlanError("safety_margin_db must be positive")

    projection = _energy_floor_region_projection(regions, sample_rate)
    adjusted = [copy.deepcopy(dict(row)) for row in regions]
    receipts: list[dict[str, Any]] = []
    cursor = 0
    with wave.open(str(source_path), "rb") as source:
        if (
            source.getcomptype() != "NONE"
            or source.getframerate() != sample_rate
            or source.getnchannels() != 2
            or source.getsampwidth() != 3
        ):
            raise AudioPlanError(
                "provisional ambience must be stereo 48 kHz PCM24 WAV"
            )
        total_frames = source.getnframes()
        outer_global_windows, _outer_global_peak = _pcm_window_dbfs(
            source,
            start_frame=0,
            end_frame=total_frames,
            window_frames=outer_window_frames,
            # Match audio_qa.detect_energy_gaps exactly: its 10 ms
            # continuity gate measures the stereo downmix.
            mono=True,
        )
        semantic_global_windows, _semantic_global_peak = _pcm_window_dbfs(
            source,
            start_frame=0,
            end_frame=total_frames,
            window_frames=semantic_window_frames,
            # sound_qa.measure_wav_energy measures interleaved stereo PCM.
            mono=False,
        )
        for index, (raw, binding) in enumerate(zip(regions, projection)):
            region_id = binding["region_id"]
            start_frame = binding["start_frame"]
            end_frame = binding["end_frame"]
            if start_frame != cursor or end_frame <= start_frame or end_frame > total_frames:
                raise AudioPlanError(
                    "ambience analysis regions must exactly cover provisional PCM"
                )
            _peak_windows, provisional_peak = _pcm_window_dbfs(
                source,
                start_frame=start_frame,
                end_frame=end_frame,
                window_frames=semantic_window_frames,
                mono=False,
            )
            outer_windows = _overlapping_block_windows(
                outer_global_windows,
                start_frame=start_frame,
                end_frame=end_frame,
                window_frames=outer_window_frames,
                max_silent_windows=outer_max_silent_windows,
            )
            semantic_windows = _overlapping_block_windows(
                semantic_global_windows,
                start_frame=start_frame,
                end_frame=end_frame,
                window_frames=semantic_window_frames,
                max_silent_windows=semantic_max_silent_windows,
            )
            outer_failed, need_outer = _required_static_boost_db(
                outer_windows,
                threshold_dbfs=outer_threshold_dbfs,
                max_silent_windows=outer_max_silent_windows,
            )
            semantic_failed, need_semantic = _required_static_boost_db(
                semantic_windows,
                threshold_dbfs=semantic_threshold_dbfs,
                max_silent_windows=semantic_max_silent_windows,
            )
            required = max(need_outer, need_semantic)
            if (outer_failed or semantic_failed) and not math.isfinite(required):
                raise AudioPlanError(
                    f"ambience region {region_id} cannot satisfy energy floor within "
                    "static-gain/peak bounds"
                )
            boost_db = round(required + safety_margin_db, 6) if (
                outer_failed or semantic_failed
            ) else 0.0
            effective_gain_db = binding["effective_gain_db"] + boost_db
            predicted_peak_dbfs = _dbfs(provisional_peak) + boost_db
            if (
                provisional_peak <= 0.0
                or effective_gain_db > max_effective_gain_db + 1e-9
                or predicted_peak_dbfs > per_region_peak_ceiling_dbfs + 1e-9
            ):
                raise AudioPlanError(
                    f"ambience region {region_id} cannot satisfy energy floor within "
                    "static-gain/peak bounds"
                )
            multiplier = 10.0 ** (boost_db / 20.0)
            if raw.get("gain_db") is not None:
                adjusted[index].pop("gain", None)
                adjusted[index]["gain_db"] = round(effective_gain_db, 6)
            else:
                adjusted[index].pop("gain_db", None)
                adjusted[index]["gain"] = binding["effective_gain"] * multiplier
            receipts.append(
                {
                    "region_id": region_id,
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                    "raw_plan_gain": binding["effective_gain"],
                    "raw_plan_gain_db": binding["effective_gain_db"],
                    "outer_failed": outer_failed,
                    "semantic_failed": semantic_failed,
                    "need_outer_db": round(need_outer, 6),
                    "need_semantic_db": round(need_semantic, 6),
                    "boost_db": boost_db,
                    "effective_gain": round(
                        binding["effective_gain"] * multiplier, 12
                    ),
                    "effective_gain_db": round(effective_gain_db, 6),
                    "provisional_peak": round(provisional_peak, 12),
                    "provisional_peak_dbfs": round(_dbfs(provisional_peak), 6),
                    "predicted_peak_dbfs": round(predicted_peak_dbfs, 6),
                }
            )
            cursor = end_frame
        if cursor != total_frames:
            raise AudioPlanError(
                "ambience analysis regions must exactly cover provisional PCM"
            )

    adjusted_projection = _energy_floor_region_projection(adjusted, sample_rate)
    policy = {
        "version": AMBIENCE_ENERGY_FLOOR_POLICY_VERSION,
        "outer_threshold_dbfs": float(outer_threshold_dbfs),
        "outer_window_frames": int(outer_window_frames),
        "outer_max_silent_windows": int(outer_max_silent_windows),
        "semantic_threshold_dbfs": float(semantic_threshold_dbfs),
        "semantic_window_frames": int(semantic_window_frames),
        "semantic_max_silent_windows": int(semantic_max_silent_windows),
        "safety_margin_db": float(safety_margin_db),
        "max_effective_gain_db": float(max_effective_gain_db),
        "per_region_peak_ceiling_dbfs": float(per_region_peak_ceiling_dbfs),
        "method": "static_per_region_gain_from_exact_provisional_pcm",
        "window_grid_origin_frame": 0,
        "region_assignment": "every_failing_global_block_intersecting_region",
        "dynamic_normalization": False,
    }
    facts: dict[str, Any] = {
        "contract": AMBIENCE_ENERGY_FLOOR_POLICY_VERSION,
        "status": "adjusted",
        "provisional_stem_sha256": sha256_file(source_path),
        "provisional_frames": total_frames,
        "sample_rate": sample_rate,
        "channels": 2,
        "sample_width_bytes": 3,
        "policy": policy,
        "regions": receipts,
        "region_count": len(receipts),
        "adjusted_region_count": sum(row["boost_db"] > 0.0 for row in receipts),
        "adjusted_regions_sha256": _canonical_sha256(adjusted_projection),
    }
    facts["receipt_sha256"] = _canonical_sha256(facts)
    return adjusted, facts


def finalize_ambience_energy_floor_facts(
    pcm_wav: str | Path,
    adjustment_facts: Mapping[str, Any],
    boundaries: Sequence[Mapping[str, Any] | float],
) -> dict[str, Any]:
    """Bind strict rebuilt-stem QA to a static-gain adjustment receipt."""

    if not isinstance(adjustment_facts, Mapping):
        raise AudioPlanError("ambience adjustment facts must be a mapping")
    material = copy.deepcopy(dict(adjustment_facts))
    supplied_receipt = material.pop("receipt_sha256", None)
    if (
        material.get("contract") != AMBIENCE_ENERGY_FLOOR_POLICY_VERSION
        or supplied_receipt != _canonical_sha256(material)
    ):
        raise AudioPlanError("ambience energy-floor adjustment receipt is invalid")
    policy = material.get("policy")
    if not isinstance(policy, Mapping):
        raise AudioPlanError("ambience energy-floor policy is missing")
    target_frames = int(material.get("provisional_frames") or 0)
    sample_rate = int(material.get("sample_rate") or 0)
    if target_frames <= 0 or sample_rate <= 0:
        raise AudioPlanError("ambience energy-floor frame binding is invalid")

    from .audio_qa import detect_boundary_clicks, detect_energy_gaps, validate_pcm_stem
    from .sound_qa import detect_ambience_energy_gaps

    path = Path(pcm_wav).expanduser().resolve()
    stem = validate_pcm_stem(
        path,
        expected_duration=target_frames / sample_rate,
        required_sample_rate=sample_rate,
        required_channels=2,
    )
    outer = detect_energy_gaps(
        path,
        threshold_dbfs=float(policy["outer_threshold_dbfs"]),
        max_gap_seconds=(
            int(policy["outer_max_silent_windows"])
            * int(policy["outer_window_frames"])
            / sample_rate
        ),
        window_seconds=int(policy["outer_window_frames"]) / sample_rate,
    )
    semantic_gaps = detect_ambience_energy_gaps(
        path,
        silence_threshold_dbfs=float(policy["semantic_threshold_dbfs"]),
        max_gap_seconds=(
            int(policy["semantic_max_silent_windows"])
            * int(policy["semantic_window_frames"])
            / sample_rate
        ),
        window_seconds=int(policy["semantic_window_frames"]) / sample_rate,
    )
    boundary_seconds = [
        float(value.get("at")) if isinstance(value, Mapping) else float(value)
        for value in boundaries
    ]
    clicks = detect_boundary_clicks(path, boundary_seconds)
    if (
        not stem["passed"]
        or stem["sample_width"] != 3
        or stem["frames"] != target_frames
        or not outer["passed"]
        or semantic_gaps
        or not clicks["passed"]
        or stem["peak"] >= 1.0
    ):
        raise AudioPlanError("rebuilt ambience failed strict energy-floor PCM QA")
    result = copy.deepcopy(dict(adjustment_facts))
    result["status"] = "verified_rebuilt"
    result["rebuilt_stem"] = {
        "sha256": sha256_file(path),
        "frames": stem["frames"],
        "sample_rate": stem["sample_rate"],
        "channels": stem["channels"],
        "sample_width_bytes": stem["sample_width"],
        "peak": stem["peak"],
        "outer_energy_gaps": outer["gaps"],
        "semantic_energy_gaps": semantic_gaps,
        "boundary_click_flags": clicks["flags"],
    }
    result.pop("receipt_sha256", None)
    result["receipt_sha256"] = _canonical_sha256(result)
    return result


def _pan_value(raw: Mapping[str, Any], name: str) -> float:
    value = raw.get("horizontal_position")
    if value is None and isinstance(raw.get("perspective"), Mapping):
        value = raw["perspective"].get("horizontal_position", 0.0)
    pan = _finite_number(0.0 if value is None else value, f"{name}.horizontal_position")
    if not -1.0 <= pan <= 1.0:
        raise AudioPlanError(f"{name}.horizontal_position must be in [-1, 1]")
    return pan


def _effective_crossfade(source_duration: float, requested: float) -> float:
    """Keep a useful equal-power overlap without consuming a short source."""

    if requested <= 0:
        return 0.0
    # A loop needs substantial non-overlapped material.  At most one quarter of
    # a source is cross-faded; this also keeps the copy-count bounded.
    return min(requested, source_duration * 0.25)


def _loop_copy_count(
    source_duration: float,
    target_duration: float,
    crossfade_seconds: float,
) -> int:
    if target_duration <= source_duration:
        return 1
    step = source_duration - crossfade_seconds
    if step <= 0:
        raise AudioPlanError("background source is too short for requested crossfade")
    copies = 1 + math.ceil((target_duration - source_duration) / step)
    if copies > 512:
        raise AudioPlanError(
            "background would require more than 512 loop copies; select a longer bed"
        )
    return copies


def build_looped_background_command(
    background_path: str | Path,
    source_duration: float,
    target_duration: float,
    output_wav: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    config: AudioMixConfig | Mapping[str, Any] | None = None,
    overwrite: bool = True,
) -> tuple[list[str], dict[str, Any]]:
    """Build a PCM background stem with equal-power loop crossfades.

    FFmpeg's ``-stream_loop`` makes a hard join.  Instead, this command opens
    the selected bed as many times as needed and chains ``acrossfade`` filters.
    The result is trimmed to narration duration and stored as 48 kHz PCM WAV.
    No lossy audio encode happens here.
    """

    cfg = config if isinstance(config, AudioMixConfig) else AudioMixConfig.from_mapping(config)
    cfg.validate()
    source_duration = _positive_duration(source_duration, "source_duration")
    target_duration = _positive_duration(target_duration, "target_duration")
    path = str(Path(background_path).expanduser())
    output = str(Path(output_wav).expanduser())

    crossfade = _effective_crossfade(source_duration, cfg.crossfade_seconds)
    copies = _loop_copy_count(source_duration, target_duration, crossfade)

    argv: list[str] = [str(ffmpeg), "-hide_banner", "-loglevel", "error", _overwrite_flag(overwrite)]
    for _ in range(copies):
        argv.extend(["-i", path])

    common = (
        f"aformat=sample_fmts=fltp:sample_rates={cfg.sample_rate}:"
        "channel_layouts=stereo,"
        f"aresample={cfg.sample_rate},"
        f"atrim=0:{_fmt(source_duration)},asetpts=PTS-STARTPTS"
    )
    filters: list[str] = []
    for index in range(copies):
        filters.append(f"[{index}:a]{common}[bed{index}]")

    current = "bed0"
    if copies > 1:
        if crossfade <= 0:
            # A zero-crossfade request is an explicit hard concat.  The default
            # always uses acrossfade.
            labels = "".join(f"[bed{i}]" for i in range(copies))
            filters.append(f"{labels}concat=n={copies}:v=0:a=1[looped]")
            current = "looped"
        else:
            for index in range(1, copies):
                out_label = f"loop{index}"
                filters.append(
                    f"[{current}][bed{index}]acrossfade=d={_fmt(crossfade)}:"
                    f"c1=qsin:c2=qsin[{out_label}]"
                )
                current = out_label

    fade_in = min(0.25, target_duration / 8.0)
    fade_out = min(0.50, target_duration / 6.0)
    fade_out_start = max(0.0, target_duration - fade_out)
    filters.append(
        f"[{current}]atrim=0:{_fmt(target_duration)},asetpts=PTS-STARTPTS,"
        f"afade=t=in:st=0:d={_fmt(fade_in)},"
        f"afade=t=out:st={_fmt(fade_out_start)}:d={_fmt(fade_out)}[background]"
    )

    argv.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[background]",
            "-vn",
            "-sn",
            "-dn",
            "-c:a",
            "pcm_s24le",
            "-ar",
            str(cfg.sample_rate),
            "-ac",
            str(cfg.channels),
            output,
        ]
    )

    facts: dict[str, Any] = {
        "kind": "looped_background_pcm",
        "source_path": path,
        "source_duration": source_duration,
        "target_duration": target_duration,
        "loop_copies": copies,
        "crossfade_seconds": crossfade if copies > 1 else 0.0,
        "crossfade_curve": "qsin" if copies > 1 and crossfade > 0 else None,
        "output_path": output,
        "codec": "pcm_s24le",
        "sample_rate": cfg.sample_rate,
        "channels": cfg.channels,
        "config": asdict(cfg),
    }
    return argv, facts


def build_background_stem_command(*args: Any, **kwargs: Any) -> tuple[list[str], dict[str, Any]]:
    """Compatibility alias with the terminology used by the product spec."""

    return build_looped_background_command(*args, **kwargs)


def build_segmented_music_stem_command(
    segments: Sequence[Mapping[str, Any]],
    target_duration: float,
    output_wav: str | Path,
    *,
    crossfade_seconds: float = 3.0,
    ffmpeg: str = "ffmpeg",
    config: AudioMixConfig | Mapping[str, Any] | None = None,
    overwrite: bool = True,
) -> tuple[list[str], dict[str, Any]]:
    """Build one exact-duration PCM music stem from contiguous mood segments.

    Each segment must contain path, start and end. Optional source_offset and
    gain fields select a point in the local asset and apply a per-chapter level.
    Inputs are stream-looped defensively, then adjacent chapters are joined with
    an equal-power crossfade centred on their declared boundary. The declared
    intervals themselves remain unchanged, so the resulting stem is exactly
    target_duration rather than becoming shorter after the crossfades.
    """

    cfg = config if isinstance(config, AudioMixConfig) else AudioMixConfig.from_mapping(config)
    cfg.validate()
    target_duration, target_frames = _sample_count(
        target_duration, "target_duration", cfg.sample_rate
    )
    crossfade = _finite_number(crossfade_seconds, "crossfade_seconds")

    if isinstance(segments, (str, bytes)) or not isinstance(segments, Sequence):
        raise AudioPlanError("segments must be a sequence of mappings")
    if not segments:
        raise AudioPlanError("at least one music segment is required")
    if len(segments) > 1 and not 2.0 <= crossfade <= 5.0:
        raise AudioPlanError(
            "music crossfade_seconds must be in [2, 5] for segmented music"
        )
    if len(segments) == 1:
        crossfade = 0.0
        crossfade_frames = 0
    else:
        crossfade, crossfade_frames = _sample_count(
            crossfade, "crossfade_seconds", cfg.sample_rate
        )

    normalized: list[dict[str, Any]] = []
    previous_end_frame = 0
    for index, raw in enumerate(segments):
        if not isinstance(raw, Mapping):
            raise AudioPlanError(f"segments[{index}] must be a mapping")
        path_value = raw.get("path")
        if not isinstance(path_value, (str, Path)) or not str(path_value).strip():
            raise AudioPlanError(f"segments[{index}].path is required")
        start, start_frame = _sample_count(
            raw.get("start"),
            f"segments[{index}].start",
            cfg.sample_rate,
            allow_zero=True,
        )
        end, end_frame = _sample_count(
            raw.get("end"), f"segments[{index}].end", cfg.sample_rate
        )
        if end_frame <= start_frame:
            raise AudioPlanError(f"segments[{index}].end must be after start")
        if start_frame != previous_end_frame:
            raise AudioPlanError(
                "music segments must be contiguous at the PCM boundary: "
                f"expected frame {previous_end_frame}, got {start_frame}"
            )
        source_offset, source_offset_frame = _sample_count(
            raw.get("source_offset", 0.0),
            f"segments[{index}].source_offset",
            cfg.sample_rate,
            allow_zero=True,
        )
        gain = _gain(raw.get("gain", 1.0), f"segments[{index}].gain")
        normalized.append(
            {
                "path": str(Path(path_value).expanduser()),
                "start": start,
                "end": end,
                "start_frame": start_frame,
                "end_frame": end_frame,
                "duration_frames": end_frame - start_frame,
                "duration": (end_frame - start_frame) / cfg.sample_rate,
                "source_offset": source_offset,
                "source_offset_frame": source_offset_frame,
                "gain": gain,
            }
        )
        previous_end_frame = end_frame

    if previous_end_frame != target_frames:
        raise AudioPlanError(
            "music segments must cover the full target sample range: "
            f"last frame {previous_end_frame}, target {target_frames}"
        )
    if len(normalized) > 1:
        for index, segment in enumerate(normalized):
            transition_in_frames = (
                crossfade_frames - crossfade_frames // 2 if index else 0
            )
            transition_out_frames = (
                crossfade_frames // 2 if index < len(normalized) - 1 else 0
            )
            # Acrossfade needs at least its full duration on each side. Adding
            # half an overlap to the nominal interval is not sufficient when a
            # mood chapter itself is extremely short, so fail before FFmpeg.
            rendered_frames = (
                segment["duration_frames"]
                + transition_in_frames
                + transition_out_frames
            )
            if rendered_frames < crossfade_frames:
                raise AudioPlanError(
                    f"segments[{index}] is too short for a {crossfade:.3f}s crossfade"
                )

    output = str(Path(output_wav).expanduser())
    argv: list[str] = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        _overwrite_flag(overwrite),
    ]
    for segment in normalized:
        # Music generated for a chapter is normally longer than that chapter.
        # Infinite demuxer looping is a safe fallback when a local asset is
        # shorter; final atrim below still gives the command a finite duration.
        argv.extend(["-stream_loop", "-1", "-i", segment["path"]])

    audio_format = (
        f"aformat=sample_fmts=fltp:sample_rates={cfg.sample_rate}:"
        "channel_layouts=stereo,"
        f"aresample={cfg.sample_rate}"
    )
    filters: list[str] = []
    rendered_segments: list[dict[str, Any]] = []
    for index, segment in enumerate(normalized):
        transition_in_frames = (
            crossfade_frames - crossfade_frames // 2 if index else 0
        )
        transition_out_frames = (
            crossfade_frames // 2 if index < len(normalized) - 1 else 0
        )
        rendered_frames = (
            segment["duration_frames"]
            + transition_in_frames
            + transition_out_frames
        )
        source_end_frame = segment["source_offset_frame"] + rendered_frames
        filters.append(
            f"[{index}:a]{audio_format},"
            f"atrim=start_sample={segment['source_offset_frame']}:"
            f"end_sample={source_end_frame},"
            "asetpts=PTS-STARTPTS,"
            f"apad=whole_len={rendered_frames},"
            f"atrim=end_sample={rendered_frames},asetpts=PTS-STARTPTS,"
            f"volume={segment['gain']:.6f}[music{index}]"
        )
        rendered_segments.append(
            {
                **segment,
                "transition_in_seconds": crossfade if index else 0.0,
                "transition_out_seconds": (
                    crossfade if index < len(normalized) - 1 else 0.0
                ),
                "transition_in_frames": transition_in_frames,
                "transition_out_frames": transition_out_frames,
                "rendered_source_frames": rendered_frames,
                "rendered_source_duration": rendered_frames / cfg.sample_rate,
            }
        )

    current = "music0"
    for index in range(1, len(normalized)):
        out_label = f"music_join{index}"
        filters.append(
            f"[{current}][music{index}]acrossfade=ns={crossfade_frames}:"
            f"c1=qsin:c2=qsin[{out_label}]"
        )
        current = out_label
    # Lossy source decoders may legitimately return float samples just above
    # 0 dBFS, and equal-power crossfades can add another peak.  Keep the
    # intermediate PCM stem strictly below full scale without weakening the
    # exact-sample QA contract.  ``latency=true`` compensates the limiter's
    # lookahead; the following sample trim/pad remains the final length fence.
    limiter_ceiling = min(0.999, 10.0 ** (cfg.true_peak_db / 20.0))
    filters.append(
        f"[{current}]alimiter=limit={limiter_ceiling:.6f}:"
        "attack=5:release=50:level=false:latency=true,"
        f"atrim=end_sample={target_frames},"
        f"apad=whole_len={target_frames},"
        f"atrim=end_sample={target_frames},asetpts=PTS-STARTPTS[music]"
    )

    argv.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[music]",
            "-vn",
            "-sn",
            "-dn",
            "-c:a",
            "pcm_s24le",
            "-ar",
            str(cfg.sample_rate),
            "-ac",
            str(cfg.channels),
            output,
        ]
    )
    facts: dict[str, Any] = {
        "kind": "segmented_music_pcm",
        "segments": rendered_segments,
        "segment_count": len(rendered_segments),
        "target_duration": target_duration,
        "target_frames": target_frames,
        "crossfade_seconds": crossfade,
        "crossfade_frames": crossfade_frames,
        "crossfade_curve": "qsin" if len(rendered_segments) > 1 else None,
        "transition_count": max(0, len(rendered_segments) - 1),
        "clip_safe_limiter": True,
        "limiter_ceiling": limiter_ceiling,
        "input_loop_fallback": True,
        "output_path": output,
        "codec": "pcm_s24le",
        "sample_rate": cfg.sample_rate,
        "channels": cfg.channels,
        "config": asdict(cfg),
    }
    return argv, facts


def build_music_stem_command(
    *args: Any, **kwargs: Any
) -> tuple[list[str], dict[str, Any]]:
    """Short product-facing alias for segmented mood music."""

    return build_segmented_music_stem_command(*args, **kwargs)


def build_sfx_stem_command(
    cues: Sequence[Mapping[str, Any]],
    target_duration: float,
    output_wav: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    config: AudioMixConfig | Mapping[str, Any] | None = None,
    overwrite: bool = True,
) -> tuple[list[str], dict[str, Any]]:
    """Build a timestamped PCM SFX stem, or exact silence for no cues.

    A cue requires path, at and duration. Optional source_offset, gain,
    fade_in and fade_out fields are applied before millisecond-accurate delay.
    Cues that extend past the programme end are clipped, never shifted.
    """

    cfg = config if isinstance(config, AudioMixConfig) else AudioMixConfig.from_mapping(config)
    cfg.validate()
    target_duration = _positive_duration(target_duration, "target_duration")
    if isinstance(cues, (str, bytes)) or not isinstance(cues, Sequence):
        raise AudioPlanError("cues must be a sequence of mappings")

    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(cues):
        if not isinstance(raw, Mapping):
            raise AudioPlanError(f"cues[{index}] must be a mapping")
        path_value = raw.get("path")
        if not isinstance(path_value, (str, Path)) or not str(path_value).strip():
            raise AudioPlanError(f"cues[{index}].path is required")
        at = _nonnegative_number(raw.get("at"), f"cues[{index}].at")
        if at >= target_duration:
            raise AudioPlanError(f"cues[{index}].at must be before target_duration")
        requested_duration = _positive_duration(
            raw.get("duration"), f"cues[{index}].duration"
        )
        duration = min(requested_duration, target_duration - at)
        source_offset = _nonnegative_number(
            raw.get("source_offset", 0.0), f"cues[{index}].source_offset"
        )
        gain = _gain(raw.get("gain", 1.0), f"cues[{index}].gain")
        fade_in = _nonnegative_number(
            raw.get("fade_in", 0.015), f"cues[{index}].fade_in"
        )
        fade_out = _nonnegative_number(
            raw.get("fade_out", 0.080), f"cues[{index}].fade_out"
        )
        fade_in = min(fade_in, duration / 2.0)
        fade_out = min(fade_out, duration / 2.0)
        delay_ms = int(round(at * 1000.0))
        normalized.append(
            {
                "path": str(Path(path_value).expanduser()),
                "at": at,
                "actual_at": delay_ms / 1000.0,
                "requested_duration": requested_duration,
                "duration": duration,
                "clipped_at_programme_end": duration + 1e-9 < requested_duration,
                "source_offset": source_offset,
                "gain": gain,
                "fade_in": fade_in,
                "fade_out": fade_out,
                "delay_ms": delay_ms,
            }
        )

    output = str(Path(output_wav).expanduser())
    argv: list[str] = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        _overwrite_flag(overwrite),
    ]
    for cue in normalized:
        argv.extend(["-i", cue["path"]])

    filters: list[str] = [
        (
            f"anullsrc=r={cfg.sample_rate}:cl=stereo:d={_fmt(target_duration)},"
            f"atrim=0:{_fmt(target_duration)},asetpts=PTS-STARTPTS[sfx_silence]"
        )
    ]
    cue_labels: list[str] = []
    audio_format = (
        f"aformat=sample_fmts=fltp:sample_rates={cfg.sample_rate}:"
        "channel_layouts=stereo,"
        f"aresample={cfg.sample_rate}"
    )
    for index, cue in enumerate(normalized):
        source_end = cue["source_offset"] + cue["duration"]
        chain = (
            f"[{index}:a]{audio_format},"
            f"atrim=start={_fmt(cue['source_offset'])}:end={_fmt(source_end)},"
            "asetpts=PTS-STARTPTS,"
            f"apad=pad_dur={_fmt(cue['duration'])},"
            f"atrim=0:{_fmt(cue['duration'])}"
        )
        if cue["fade_in"] > 0:
            chain += f",afade=t=in:st=0:d={_fmt(cue['fade_in'])}"
        if cue["fade_out"] > 0:
            fade_out_start = cue["duration"] - cue["fade_out"]
            chain += (
                f",afade=t=out:st={_fmt(fade_out_start)}:"
                f"d={_fmt(cue['fade_out'])}"
            )
        chain += (
            f",volume={cue['gain']:.6f},"
            f"adelay={cue['delay_ms']}:all=1[sfx_cue{index}]"
        )
        filters.append(chain)
        cue_labels.append(f"[sfx_cue{index}]")

    if cue_labels:
        inputs = "[sfx_silence]" + "".join(cue_labels)
        filters.append(
            f"{inputs}amix=inputs={len(cue_labels) + 1}:duration=longest:"
            "dropout_transition=0:normalize=0,"
            f"atrim=0:{_fmt(target_duration)},asetpts=PTS-STARTPTS[sfx]"
        )
    else:
        filters.append("[sfx_silence]anull[sfx]")

    argv.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[sfx]",
            "-vn",
            "-sn",
            "-dn",
            "-c:a",
            "pcm_s24le",
            "-ar",
            str(cfg.sample_rate),
            "-ac",
            str(cfg.channels),
            "-t",
            _fmt(target_duration),
            output,
        ]
    )
    facts: dict[str, Any] = {
        "kind": "timestamped_sfx_pcm",
        "cues": normalized,
        "cue_count": len(normalized),
        "silence_only": not normalized,
        "timestamp_resolution_ms": 1,
        "target_duration": target_duration,
        "output_path": output,
        "codec": "pcm_s24le",
        "sample_rate": cfg.sample_rate,
        "channels": cfg.channels,
        "config": asdict(cfg),
    }
    return argv, facts


def _append_seamless_loop(
    argv: list[str],
    filters: list[str],
    *,
    path: str,
    source_duration: float,
    source_offset: float,
    rendered_frames: int,
    label: str,
    first_input_index: int,
    config: AudioMixConfig,
) -> tuple[int, dict[str, Any]]:
    """Append a finite equal-power loop from one deterministic decoder.

    Combining independent delayed decoders with ``amix`` is racy: FFmpeg can
    exit zero after dropping every copy except the first. One finite demuxer
    loop is split into disjoint source cycles, then those cycles are joined
    sequentially with equal-power ``acrossfade`` filters.
    """

    _resolved_source, source_frames = _sample_count(
        source_duration, f"{label}.source_duration", config.sample_rate
    )
    _resolved_offset, offset_frames = _sample_count(
        source_offset,
        f"{label}.source_offset",
        config.sample_rate,
        allow_zero=True,
    )
    if offset_frames >= source_frames:
        raise AudioPlanError(f"{label}.source_offset is outside the source")
    available_frames = source_frames - offset_frames
    requested_loop_frames = int(round(config.crossfade_seconds * config.sample_rate))
    loop_crossfade_frames = min(requested_loop_frames, available_frames // 4)
    if rendered_frames <= available_frames:
        copies = 1
        loop_crossfade_frames = 0
    else:
        if loop_crossfade_frames <= 0:
            raise AudioPlanError(f"{label} source is too short for a seamless loop")
        step_frames = available_frames - loop_crossfade_frames
        copies = 1 + math.ceil((rendered_frames - available_frames) / step_frames)
        if copies > 512:
            raise AudioPlanError(
                f"{label} would require more than 512 loop copies; use a longer asset"
            )

    audio_format = (
        f"aformat=sample_fmts=fltp:sample_rates={config.sample_rate}:"
        "channel_layouts=stereo,"
        f"aresample={config.sample_rate}"
    )
    argv.extend(["-stream_loop", str(copies - 1), "-i", path])
    source_labels = [f"{label}_copy{index}" for index in range(copies)]
    if copies == 1:
        filters.append(
            f"[{first_input_index}:a]{audio_format},"
            f"atrim=start_sample={offset_frames}:end_sample={source_frames},"
            f"asetpts=PTS-STARTPTS[{source_labels[0]}]"
        )
    else:
        split_labels = [f"{label}_cycle{index}" for index in range(copies)]
        filters.append(
            f"[{first_input_index}:a]{audio_format},asplit=outputs={copies}"
            + "".join(f"[{cycle_label}]" for cycle_label in split_labels)
        )
        for copy_index, (cycle_label, copy_label) in enumerate(
            zip(split_labels, source_labels)
        ):
            cycle_start = copy_index * source_frames + offset_frames
            cycle_end = (copy_index + 1) * source_frames
            filters.append(
                f"[{cycle_label}]atrim=start_sample={cycle_start}:"
                f"end_sample={cycle_end},asetpts=PTS-STARTPTS[{copy_label}]"
            )

    current = source_labels[0]
    if copies > 1:
        for copy_index, source_label in enumerate(source_labels[1:], start=1):
            joined = f"{label}_join{copy_index}"
            filters.append(
                f"[{current}][{source_label}]"
                f"acrossfade=ns={loop_crossfade_frames}:c1=qsin:c2=qsin"
                f"[{joined}]"
            )
            current = joined
    filters.append(
        f"[{current}]atrim=end_sample={rendered_frames},"
        f"asetpts=PTS-STARTPTS[{label}]"
    )
    return first_input_index + 1, {
        "path": path,
        "source_duration": source_frames / config.sample_rate,
        "source_offset": offset_frames / config.sample_rate,
        "available_source_frames": available_frames,
        "rendered_frames": rendered_frames,
        "loop_copies": copies,
        "decoder_input_count": 1,
        "demuxer_loop_count": copies - 1,
        "loop_crossfade_frames": loop_crossfade_frames,
        "loop_crossfade_seconds": loop_crossfade_frames / config.sample_rate,
        "loop_join": "sequential_acrossfade_equal_power_qsin" if copies > 1 else None,
    }


def build_ambience_stem_command(
    regions: Sequence[Mapping[str, Any]],
    target_duration: float,
    output_wav: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    config: AudioMixConfig | Mapping[str, Any] | None = None,
    overwrite: bool = True,
    energy_floor_facts: Mapping[str, Any] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Build the mandatory 100%-covered production ambience lane.

    Region boundaries are exact PCM-frame positions.  The incoming region's
    ``crossfade_seconds`` controls its boundary transition, matching the v2
    sound-plan contract.  Short ambience assets are repeated with equal-power
    crossfades rather than hard ``-stream_loop`` joins.
    """

    cfg = config if isinstance(config, AudioMixConfig) else AudioMixConfig.from_mapping(config)
    cfg.validate()
    target_duration, target_frames = _sample_count(
        target_duration, "target_duration", cfg.sample_rate
    )
    if isinstance(regions, (str, bytes)) or not isinstance(regions, Sequence):
        raise AudioPlanError("regions must be a sequence of mappings")
    if not regions:
        raise AudioPlanError("production ambience requires at least one region")

    normalized: list[dict[str, Any]] = []
    previous_end_frame = 0
    for index, raw in enumerate(regions):
        if not isinstance(raw, Mapping):
            raise AudioPlanError(f"regions[{index}] must be a mapping")
        prefix = f"regions[{index}]"
        start, start_frame = _sample_count(
            raw.get("start"), f"{prefix}.start", cfg.sample_rate, allow_zero=True
        )
        end, end_frame = _sample_count(raw.get("end"), f"{prefix}.end", cfg.sample_rate)
        if end_frame <= start_frame:
            raise AudioPlanError(f"{prefix}.end must be after start")
        if start_frame != previous_end_frame:
            raise AudioPlanError(
                "ambience regions must cover the timeline exactly: "
                f"expected frame {previous_end_frame}, got {start_frame}"
            )
        if end_frame > target_frames:
            raise AudioPlanError(f"{prefix} extends past target_duration")
        if raw.get("duration") is not None:
            _declared, declared_frames = _sample_count(
                raw.get("duration"), f"{prefix}.duration", cfg.sample_rate
            )
            if declared_frames != end_frame - start_frame:
                raise AudioPlanError(f"{prefix}.duration disagrees with start/end")
        crossfade, crossfade_frames = _sample_count(
            raw.get("crossfade_seconds", 0.4),
            f"{prefix}.crossfade_seconds",
            cfg.sample_rate,
        )
        if not 0.08 <= crossfade <= 0.8:
            raise AudioPlanError(f"{prefix}.crossfade_seconds must be in [0.08, 0.8]")
        source_offset = _nonnegative_number(
            raw.get("source_offset", 0.0), f"{prefix}.source_offset"
        )
        plan_gain, plan_gain_db = _region_gain_facts(raw, prefix)
        normalized.append(
            {
                "region_id": str(raw.get("region_id") or f"ambience-{index}"),
                "path": _layer_path(raw, prefix),
                "source_duration": _layer_source_duration(raw, prefix),
                "source_offset": source_offset,
                "gain": plan_gain,
                "plan_gain_db": plan_gain_db,
                "start": start,
                "end": end,
                "start_frame": start_frame,
                "end_frame": end_frame,
                "duration_frames": end_frame - start_frame,
                "crossfade_frames": crossfade_frames,
                "crossfade_seconds": crossfade_frames / cfg.sample_rate,
            }
        )
        previous_end_frame = end_frame
    if previous_end_frame != target_frames:
        raise AudioPlanError(
            "ambience regions must cover [0, target_duration) exactly"
        )

    validated_energy_floor: dict[str, Any] | None = None
    if energy_floor_facts is not None:
        if not isinstance(energy_floor_facts, Mapping):
            raise AudioPlanError("energy_floor_facts must be a mapping")
        validated_energy_floor = copy.deepcopy(dict(energy_floor_facts))
        receipt_material = copy.deepcopy(validated_energy_floor)
        supplied_receipt = receipt_material.pop("receipt_sha256", None)
        if (
            receipt_material.get("contract")
            != AMBIENCE_ENERGY_FLOOR_POLICY_VERSION
            or receipt_material.get("status") != "adjusted"
            or supplied_receipt != _canonical_sha256(receipt_material)
            or int(receipt_material.get("provisional_frames") or 0) != target_frames
            or int(receipt_material.get("sample_rate") or 0) != cfg.sample_rate
            or int(receipt_material.get("region_count") or 0) != len(normalized)
            or receipt_material.get("adjusted_regions_sha256")
            != _canonical_sha256(
                _energy_floor_region_projection(normalized, cfg.sample_rate)
            )
        ):
            raise AudioPlanError("ambience energy-floor adjustment receipt is invalid")

    argv: list[str] = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        # FFmpeg's parallel filter scheduler can acknowledge success while
        # dropping delayed inputs from a later amix branch. A continuous
        # ambience stem must be deterministic; one filter thread still lets
        # the independent stem processes run in parallel at the stage level.
        "-filter_complex_threads",
        "1",
        _overwrite_flag(overwrite),
    ]
    filters: list[str] = []
    next_input = 0
    region_facts: list[dict[str, Any]] = []
    for index, region in enumerate(normalized):
        incoming_frames = region["crossfade_frames"] if index else 0
        outgoing_frames = (
            normalized[index + 1]["crossfade_frames"]
            if index + 1 < len(normalized)
            else 0
        )
        # At every boundary the two adjacent extensions sum to the exact
        # acrossfade length, so the joined stem remains exactly target_frames.
        incoming_extension = incoming_frames - (incoming_frames // 2)
        outgoing_extension = outgoing_frames // 2
        rendered_frames = (
            region["duration_frames"] + incoming_extension + outgoing_extension
        )
        if rendered_frames < max(incoming_frames, outgoing_frames):
            raise AudioPlanError(
                f"regions[{index}] is too short for the requested boundary crossfade"
            )
        label = f"ambience_region{index}"
        next_input, loop_facts = _append_seamless_loop(
            argv,
            filters,
            path=region["path"],
            source_duration=region["source_duration"],
            source_offset=region["source_offset"],
            rendered_frames=rendered_frames,
            label=label,
            first_input_index=next_input,
            config=cfg,
        )
        filters.append(f"[{label}]volume={region['gain']:.9f}[{label}_gain]")
        region_facts.append(
            {
                **{key: value for key, value in region.items() if not key.endswith("_frame") and key != "crossfade_frames"},
                "start_frame": region["start_frame"],
                "end_frame": region["end_frame"],
                "duration_frames": region["duration_frames"],
                "incoming_crossfade_frames": incoming_frames,
                "outgoing_crossfade_frames": outgoing_frames,
                "rendered_frames": rendered_frames,
                "loop": loop_facts,
            }
        )

    boundaries: list[dict[str, Any]] = []
    current_region = "ambience_region0_gain"
    for index in range(1, len(normalized)):
        crossfade_frames = normalized[index]["crossfade_frames"]
        joined = f"ambience_join{index}"
        filters.append(
            f"[{current_region}][ambience_region{index}_gain]"
            f"acrossfade=ns={crossfade_frames}:c1=qsin:c2=qsin[{joined}]"
        )
        current_region = joined
        boundaries.append(
            {
                "at_frame": normalized[index]["start_frame"],
                "at": normalized[index]["start"],
                "crossfade_frames": crossfade_frames,
                "crossfade_seconds": crossfade_frames / cfg.sample_rate,
                "curve": "qsin",
            }
        )
    filters.append(
        f"[{current_region}]"
        f"atrim=end_sample={target_frames},"
        "asetpts=PTS-STARTPTS[ambience]"
    )
    output = str(Path(output_wav).expanduser())
    argv.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[ambience]",
            "-vn",
            "-sn",
            "-dn",
            "-c:a",
            "pcm_s24le",
            "-ar",
            str(cfg.sample_rate),
            "-ac",
            str(cfg.channels),
            output,
        ]
    )
    facts = {
        "kind": (
            "continuous_ambience_pcm_v3_energy_floor"
            if validated_energy_floor is not None
            else "continuous_ambience_pcm_v2_provisional"
        ),
        "regions": region_facts,
        "region_count": len(region_facts),
        "boundaries": boundaries,
        "coverage_start_frame": 0,
        "coverage_end_frame": target_frames,
        "coverage_ratio": 1.0,
        "target_duration": target_duration,
        "target_frames": target_frames,
        "output_path": output,
        "codec": "pcm_s24le",
        "sample_rate": cfg.sample_rate,
        "channels": cfg.channels,
        "hard_loop_join_count": 0,
        "filter_complex_threads": 1,
        "energy_floor": (
            validated_energy_floor
            if validated_energy_floor is not None
            else {
                "contract": AMBIENCE_ENERGY_FLOOR_POLICY_VERSION,
                "status": "provisional_unadjusted",
                "dynamic_normalization": False,
            }
        ),
        "config": asdict(cfg),
    }
    return argv, facts


def _build_timed_natural_stem_command(
    items: Sequence[Mapping[str, Any]],
    target_duration: float,
    output_wav: str | Path,
    *,
    lane: str,
    loop_sustained: bool,
    ffmpeg: str,
    config: AudioMixConfig,
    overwrite: bool,
) -> tuple[list[str], dict[str, Any]]:
    target_duration, target_frames = _sample_count(
        target_duration, "target_duration", config.sample_rate
    )
    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
        raise AudioPlanError(f"{lane} items must be a sequence of mappings")
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(items):
        if not isinstance(raw, Mapping):
            raise AudioPlanError(f"{lane}[{index}] must be a mapping")
        prefix = f"{lane}[{index}]"
        start, start_frame = _sample_count(
            raw.get("start"), f"{prefix}.start", config.sample_rate, allow_zero=True
        )
        end, end_frame = _sample_count(raw.get("end"), f"{prefix}.end", config.sample_rate)
        if end_frame <= start_frame or end_frame > target_frames:
            raise AudioPlanError(f"{prefix} interval is outside target_duration")
        duration_frames = end_frame - start_frame
        if raw.get("duration") is not None:
            _declared, declared_frames = _sample_count(
                raw.get("duration"), f"{prefix}.duration", config.sample_rate
            )
            if declared_frames != duration_frames:
                raise AudioPlanError(f"{prefix}.duration disagrees with start/end")
        source_duration = _layer_source_duration(raw, prefix)
        source_offset = _nonnegative_number(
            raw.get("source_offset", 0.0), f"{prefix}.source_offset"
        )
        _source_resolved, source_frames = _sample_count(
            source_duration, f"{prefix}.source_duration", config.sample_rate
        )
        _offset_resolved, offset_frames = _sample_count(
            source_offset,
            f"{prefix}.source_offset",
            config.sample_rate,
            allow_zero=True,
        )
        if offset_frames >= source_frames:
            raise AudioPlanError(f"{prefix}.source_offset is outside the source")
        if not loop_sustained and source_frames - offset_frames < duration_frames:
            raise AudioPlanError(
                f"{prefix} source is shorter than its event; event padding is forbidden"
            )
        default_fade_in = 0.040 if loop_sustained else 0.005
        default_fade_out = 0.080
        _fade_in, fade_in_frames = _sample_count(
            raw.get("fade_in", default_fade_in),
            f"{prefix}.fade_in",
            config.sample_rate,
            allow_zero=True,
        )
        _fade_out, fade_out_frames = _sample_count(
            raw.get("fade_out", default_fade_out),
            f"{prefix}.fade_out",
            config.sample_rate,
            allow_zero=True,
        )
        fade_in_frames = min(fade_in_frames, duration_frames // 2)
        fade_out_frames = min(fade_out_frames, duration_frames // 2)
        normalized.append(
            {
                "entry_id": str(
                    raw.get("layer_id")
                    or raw.get("cue_id")
                    or f"{lane}-{index}"
                ),
                "path": _layer_path(raw, prefix),
                "source_duration": source_duration,
                "source_offset": source_offset,
                "gain": _layer_gain(raw, prefix),
                "horizontal_position": _pan_value(raw, prefix),
                "start": start,
                "end": end,
                "start_frame": start_frame,
                "end_frame": end_frame,
                "duration_frames": duration_frames,
                "fade_in_frames": fade_in_frames,
                "fade_out_frames": fade_out_frames,
            }
        )
    normalized.sort(key=lambda item: (item["start_frame"], item["entry_id"]))

    argv: list[str] = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
    ]
    if loop_sustained:
        # Sustained presence uses the same delayed seamless-loop + amix graph
        # as ambience and is subject to the same scheduler race.
        argv.extend(["-filter_complex_threads", "1"])
    argv.append(_overwrite_flag(overwrite))
    filters: list[str] = [
        (
            f"anullsrc=r={config.sample_rate}:cl=stereo,"
            f"atrim=end_sample={target_frames},asetpts=PTS-STARTPTS[{lane}_silence]"
        )
    ]
    next_input = 0
    labels: list[str] = []
    rendered: list[dict[str, Any]] = []
    audio_format = (
        f"aformat=sample_fmts=fltp:sample_rates={config.sample_rate}:"
        "channel_layouts=stereo,"
        f"aresample={config.sample_rate}"
    )
    for index, item in enumerate(normalized):
        source_label = f"{lane}_source{index}"
        loop_facts: dict[str, Any] | None = None
        if loop_sustained:
            next_input, loop_facts = _append_seamless_loop(
                argv,
                filters,
                path=item["path"],
                source_duration=item["source_duration"],
                source_offset=item["source_offset"],
                rendered_frames=item["duration_frames"],
                label=source_label,
                first_input_index=next_input,
                config=config,
            )
        else:
            _source, source_frames = _sample_count(
                item["source_duration"],
                f"{lane}[{index}].source_duration",
                config.sample_rate,
            )
            _offset, offset_frames = _sample_count(
                item["source_offset"],
                f"{lane}[{index}].source_offset",
                config.sample_rate,
                allow_zero=True,
            )
            argv.extend(["-i", item["path"]])
            filters.append(
                f"[{next_input}:a]{audio_format},"
                f"atrim=start_sample={offset_frames}:"
                f"end_sample={offset_frames + item['duration_frames']},"
                f"asetpts=PTS-STARTPTS[{source_label}]"
            )
            next_input += 1

        chain = f"[{source_label}]"
        operations: list[str] = []
        if item["fade_in_frames"]:
            operations.append(
                f"afade=t=in:ss=0:ns={item['fade_in_frames']}"
            )
        if item["fade_out_frames"]:
            operations.append(
                "afade=t=out:"
                f"ss={item['duration_frames'] - item['fade_out_frames']}:"
                f"ns={item['fade_out_frames']}"
            )
        pan = item["horizontal_position"]
        left = math.sqrt(max(0.0, 1.0 - max(0.0, pan)))
        right = math.sqrt(max(0.0, 1.0 + min(0.0, pan)))
        operations.extend(
            [
                f"pan=stereo|c0={left:.9f}*c0|c1={right:.9f}*c1",
                f"volume={item['gain']:.9f}",
                f"adelay={item['start_frame']}S:all=1",
            ]
        )
        output_label = f"{lane}_item{index}"
        filters.append(chain + ",".join(operations) + f"[{output_label}]")
        labels.append(output_label)
        rendered.append({**item, "loop": loop_facts})

    if labels:
        inputs = f"[{lane}_silence]" + "".join(f"[{label}]" for label in labels)
        filters.append(
            f"{inputs}amix=inputs={len(labels) + 1}:duration=longest:"
            "dropout_transition=0:normalize=0,"
            f"atrim=end_sample={target_frames},asetpts=PTS-STARTPTS[{lane}]"
        )
    else:
        filters.append(f"[{lane}_silence]anull[{lane}]")
    output = str(Path(output_wav).expanduser())
    argv.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            f"[{lane}]",
            "-vn",
            "-sn",
            "-dn",
            "-c:a",
            "pcm_s24le",
            "-ar",
            str(config.sample_rate),
            "-ac",
            str(config.channels),
            output,
        ]
    )
    facts = {
        "kind": f"{lane}_pcm_v2",
        "lane": lane,
        "items": rendered,
        "item_count": len(rendered),
        "silence_only": not rendered,
        "target_duration": target_duration,
        "target_frames": target_frames,
        "timestamp_resolution": "one_pcm_frame",
        "output_path": output,
        "codec": "pcm_s24le",
        "sample_rate": config.sample_rate,
        "channels": config.channels,
        "hard_loop_join_count": 0,
        "filter_complex_threads": 1 if loop_sustained else None,
        "config": asdict(config),
    }
    return argv, facts


def build_presence_stem_command(
    layers: Sequence[Mapping[str, Any]],
    target_duration: float,
    output_wav: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    config: AudioMixConfig | Mapping[str, Any] | None = None,
    overwrite: bool = True,
) -> tuple[list[str], dict[str, Any]]:
    """Build overlapping, perspective-aware sustained presence layers."""

    cfg = config if isinstance(config, AudioMixConfig) else AudioMixConfig.from_mapping(config)
    cfg.validate()
    return _build_timed_natural_stem_command(
        layers,
        target_duration,
        output_wav,
        lane="presence",
        loop_sustained=True,
        ffmpeg=ffmpeg,
        config=cfg,
        overwrite=overwrite,
    )


def build_event_stem_command(
    cues: Sequence[Mapping[str, Any]],
    target_duration: float,
    output_wav: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    config: AudioMixConfig | Mapping[str, Any] | None = None,
    overwrite: bool = True,
) -> tuple[list[str], dict[str, Any]]:
    """Build sample-synchronised event cues without silence-padding assets."""

    cfg = config if isinstance(config, AudioMixConfig) else AudioMixConfig.from_mapping(config)
    cfg.validate()
    return _build_timed_natural_stem_command(
        cues,
        target_duration,
        output_wav,
        lane="events",
        loop_sustained=False,
        ffmpeg=ffmpeg,
        config=cfg,
        overwrite=overwrite,
    )


def build_final_mix_command(
    video_path: str | Path,
    narration_path: str | Path,
    background_wav: str | Path,
    output_mp4: str | Path,
    duration: float,
    *,
    ffmpeg: str = "ffmpeg",
    config: AudioMixConfig | Mapping[str, Any] | None = None,
    overwrite: bool = True,
) -> tuple[list[str], dict[str, Any]]:
    """Build the final video-copy + narration/background mix command.

    Input ``0`` must be the video-only concat produced by ``render.py``.  The
    only video mapping is ``0:v:0``; there is deliberately no ``0:a`` mapping.
    Narration is the side-chain control signal, so the bed is attenuated under
    speech.  Final AAC is encoded once after loudness normalization and limiter.
    """

    cfg = config if isinstance(config, AudioMixConfig) else AudioMixConfig.from_mapping(config)
    cfg.validate()
    duration = _positive_duration(duration, "duration")

    video = str(Path(video_path).expanduser())
    narration = str(Path(narration_path).expanduser())
    background = str(Path(background_wav).expanduser())
    output = str(Path(output_mp4).expanduser())

    audio_format = (
        f"aformat=sample_fmts=fltp:sample_rates={cfg.sample_rate}:"
        "channel_layouts=stereo,"
        f"aresample={cfg.sample_rate}"
    )
    trim = f"atrim=0:{_fmt(duration)},asetpts=PTS-STARTPTS"
    limiter_ceiling = 10.0 ** (cfg.true_peak_db / 20.0)
    filter_graph = ";".join(
        [
            (
                f"[1:a]{audio_format},{trim},"
                f"loudnorm=I={cfg.narration_lufs:.3f}:TP=-1.500:LRA={cfg.target_lra:.3f}:"
                "linear=true,asplit=2[narration][speech_sidechain]"
            ),
            (
                f"[2:a]{audio_format},{trim},"
                f"volume={cfg.background_gain:.6f}[background_bed]"
            ),
            (
                "[background_bed][speech_sidechain]sidechaincompress="
                f"threshold={cfg.duck_threshold:.6f}:ratio={cfg.duck_ratio:.3f}:"
                f"attack={cfg.duck_attack_ms:.3f}:release={cfg.duck_release_ms:.3f}:"
                "makeup=1:knee=4:link=maximum:detection=rms[ducked_background]"
            ),
            (
                "[narration][ducked_background]"
                "amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
                f"loudnorm=I={cfg.target_lufs:.3f}:TP={cfg.true_peak_db:.3f}:"
                f"LRA={cfg.target_lra:.3f}:linear=true,"
                f"alimiter=limit={limiter_ceiling:.6f}:"
                "attack=5:release=50:level=false:latency=true,"
                f"aresample={cfg.sample_rate}[master]"
            ),
        ]
    )

    argv = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        _overwrite_flag(overwrite),
        "-i",
        video,
        "-i",
        narration,
        "-i",
        background,
        "-filter_complex",
        filter_graph,
        "-map",
        "0:v:0",
        "-map",
        "[master]",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        cfg.aac_bitrate,
        "-ar",
        str(cfg.sample_rate),
        "-ac",
        str(cfg.channels),
        "-t",
        _fmt(duration),
        "-shortest",
        "-max_muxing_queue_size",
        "1024",
        "-movflags",
        "+faststart",
        output,
    ]

    facts: dict[str, Any] = {
        "kind": "final_spoken_documentary_mix",
        "video_path": video,
        "narration_path": narration,
        "background_path": background,
        "output_path": output,
        "duration": duration,
        "video_codec_action": "copy",
        "audio_codec": "aac",
        "aac_encode_count": 1,
        "video_mapping": "0:v:0",
        "audio_mapping": "[master]",
        "donor_audio_mapping": False,
        "ducking": {
            "filter": "sidechaincompress",
            "threshold": cfg.duck_threshold,
            "ratio": cfg.duck_ratio,
            "attack_ms": cfg.duck_attack_ms,
            "release_ms": cfg.duck_release_ms,
        },
        "loudness": {
            "integrated_lufs": cfg.target_lufs,
            "lra": cfg.target_lra,
            "true_peak_db": cfg.true_peak_db,
            "limiter": True,
        },
        "config": asdict(cfg),
    }
    return argv, facts


def build_layered_final_mix_command(
    video_path: str | Path,
    narration_path: str | Path,
    music_wav: str | Path,
    sfx_wav: str | Path,
    output_mp4: str | Path,
    duration: float,
    *,
    ffmpeg: str = "ffmpeg",
    config: AudioMixConfig | Mapping[str, Any] | None = None,
    overwrite: bool = True,
) -> tuple[list[str], dict[str, Any]]:
    """Mix video-only + narration + mood music + SFX into one final MP4.

    Input zero is structurally restricted to its video stream. Narration drives
    separate side-chain compressors for music and SFX, keeping speech dominant
    without muting brief scene sounds. The master is loudness-normalized and
    limited before its only lossy encode.
    """

    cfg = config if isinstance(config, AudioMixConfig) else AudioMixConfig.from_mapping(config)
    cfg.validate()
    duration = _positive_duration(duration, "duration")

    video = str(Path(video_path).expanduser())
    narration = str(Path(narration_path).expanduser())
    music = str(Path(music_wav).expanduser())
    sfx = str(Path(sfx_wav).expanduser())
    output = str(Path(output_mp4).expanduser())

    audio_format = (
        f"aformat=sample_fmts=fltp:sample_rates={cfg.sample_rate}:"
        "channel_layouts=stereo,"
        f"aresample={cfg.sample_rate}"
    )
    trim = f"atrim=0:{_fmt(duration)},asetpts=PTS-STARTPTS"
    limiter_ceiling = 10.0 ** (cfg.true_peak_db / 20.0)
    filter_graph = ";".join(
        [
            (
                f"[1:a]{audio_format},{trim},"
                f"loudnorm=I={cfg.narration_lufs:.3f}:TP=-1.500:"
                f"LRA={cfg.target_lra:.3f}:linear=true,"
                "asplit=3[narration][speech_for_music][speech_for_sfx]"
            ),
            (
                f"[2:a]{audio_format},{trim},"
                f"volume={cfg.background_gain:.6f}[music_bed]"
            ),
            (
                "[music_bed][speech_for_music]sidechaincompress="
                f"threshold={cfg.duck_threshold:.6f}:ratio={cfg.duck_ratio:.3f}:"
                f"attack={cfg.duck_attack_ms:.3f}:release={cfg.duck_release_ms:.3f}:"
                "makeup=1:knee=4:link=maximum:detection=rms[ducked_music]"
            ),
            (
                f"[3:a]{audio_format},{trim},"
                f"volume={cfg.sfx_gain:.6f}[sfx_bed]"
            ),
            (
                "[sfx_bed][speech_for_sfx]sidechaincompress="
                f"threshold={cfg.duck_threshold:.6f}:ratio={cfg.sfx_duck_ratio:.3f}:"
                f"attack={cfg.duck_attack_ms:.3f}:release={cfg.duck_release_ms:.3f}:"
                "makeup=1:knee=4:link=maximum:detection=rms[ducked_sfx]"
            ),
            (
                "[narration][ducked_music][ducked_sfx]"
                "amix=inputs=3:duration=first:dropout_transition=0:normalize=0,"
                f"loudnorm=I={cfg.target_lufs:.3f}:TP={cfg.true_peak_db:.3f}:"
                f"LRA={cfg.target_lra:.3f}:linear=true,"
                f"alimiter=limit={limiter_ceiling:.6f}:"
                "attack=5:release=50:level=false:latency=true,"
                f"aresample={cfg.sample_rate}[master]"
            ),
        ]
    )

    argv = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        _overwrite_flag(overwrite),
        "-i",
        video,
        "-i",
        narration,
        "-i",
        music,
        "-i",
        sfx,
        "-filter_complex",
        filter_graph,
        "-map",
        "0:v:0",
        "-map",
        "[master]",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        cfg.aac_bitrate,
        "-ar",
        str(cfg.sample_rate),
        "-ac",
        str(cfg.channels),
        "-t",
        _fmt(duration),
        "-max_muxing_queue_size",
        "1024",
        "-movflags",
        "+faststart",
        output,
    ]
    facts: dict[str, Any] = {
        "kind": "final_layered_spoken_documentary_mix",
        "video_path": video,
        "narration_path": narration,
        "music_path": music,
        "sfx_path": sfx,
        "output_path": output,
        "duration": duration,
        "video_codec_action": "copy",
        "stream_end_policy": "explicit_duration_without_shortest",
        "audio_codec": "aac",
        "aac_encode_count": 1,
        "video_mapping": "0:v:0",
        "audio_mapping": "[master]",
        "donor_audio_mapping": False,
        "narration_dominant": True,
        "ducking": {
            "music": {
                "filter": "sidechaincompress",
                "threshold": cfg.duck_threshold,
                "ratio": cfg.duck_ratio,
                "attack_ms": cfg.duck_attack_ms,
                "release_ms": cfg.duck_release_ms,
                "pre_duck_gain": cfg.background_gain,
            },
            "sfx": {
                "filter": "sidechaincompress",
                "threshold": cfg.duck_threshold,
                "ratio": cfg.sfx_duck_ratio,
                "attack_ms": cfg.duck_attack_ms,
                "release_ms": cfg.duck_release_ms,
                "pre_duck_gain": cfg.sfx_gain,
            },
        },
        "loudness": {
            "integrated_lufs": cfg.target_lufs,
            "lra": cfg.target_lra,
            "true_peak_db": cfg.true_peak_db,
            "limiter": True,
        },
        "config": asdict(cfg),
    }
    return argv, facts


def _documentary_premaster_filters(
    *,
    first_input_index: int,
    target_frames: int,
    config: AudioMixConfig,
) -> list[str]:
    """Return the five-lane ducking graph ending at ``[premaster]``."""

    audio_format = (
        f"aformat=sample_fmts=fltp:sample_rates={config.sample_rate}:"
        "channel_layouts=stereo,"
        f"aresample={config.sample_rate}"
    )
    # Container duration can include codec delay that the decoder removes
    # (MP3 commonly exposes this through Skip Samples).  Fence every lane in
    # the sample domain before ducking so a short narration control stream
    # cannot truncate otherwise-complete music and natural-sound tails.
    sample_fence = (
        f"atrim=end_sample={target_frames},"
        f"apad=whole_len={target_frames},"
        f"atrim=end_sample={target_frames},asetpts=PTS-STARTPTS"
    )
    narration = first_input_index
    music = first_input_index + 1
    ambience = first_input_index + 2
    presence = first_input_index + 3
    events = first_input_index + 4
    compressor = (
        f"threshold={config.duck_threshold:.6f}:"
        "ratio={ratio:.3f}:"
        f"attack={config.duck_attack_ms:.3f}:"
        f"release={config.duck_release_ms:.3f}:"
        "makeup=1:knee=4:link=maximum:detection=rms"
    )
    return [
        (
            f"[{narration}:a]{audio_format},{sample_fence},"
            f"loudnorm=I={config.narration_lufs:.3f}:TP=-1.500:"
            f"LRA={config.target_lra:.3f}:linear=true,"
            f"aresample={config.sample_rate},"
            "asplit=5[narration][speech_music][speech_ambience]"
            "[speech_presence][speech_events]"
        ),
        (
            f"[{music}:a]{audio_format},{sample_fence},"
            f"volume={config.background_gain:.9f}[music_bed]"
        ),
        (
            "[music_bed][speech_music]sidechaincompress="
            + compressor.format(ratio=config.duck_ratio)
            + "[ducked_music]"
        ),
        (
            f"[{ambience}:a]{audio_format},{sample_fence},"
            f"volume={config.ambience_gain:.9f}[ambience_bed]"
        ),
        (
            "[ambience_bed][speech_ambience]sidechaincompress="
            + compressor.format(ratio=config.ambience_duck_ratio)
            + "[ducked_ambience]"
        ),
        (
            f"[{presence}:a]{audio_format},{sample_fence},"
            f"volume={config.presence_gain:.9f}[presence_bed]"
        ),
        (
            "[presence_bed][speech_presence]sidechaincompress="
            + compressor.format(ratio=config.presence_duck_ratio)
            + "[ducked_presence]"
        ),
        (
            f"[{events}:a]{audio_format},{sample_fence},"
            f"volume={config.event_gain:.9f}[events_bed]"
        ),
        (
            "[events_bed][speech_events]sidechaincompress="
            + compressor.format(ratio=config.event_duck_ratio)
            + "[ducked_events]"
        ),
        (
            "[narration][ducked_music][ducked_ambience]"
            "[ducked_presence][ducked_events]"
            "amix=inputs=5:duration=first:dropout_transition=0:normalize=0,"
            f"{sample_fence}[premaster]"
        ),
    ]


def build_documentary_premaster_command(
    narration_path: str | Path,
    music_wav: str | Path,
    ambience_wav: str | Path,
    presence_wav: str | Path,
    events_wav: str | Path,
    output_wav: str | Path,
    duration: float,
    *,
    ffmpeg: str = "ffmpeg",
    config: AudioMixConfig | Mapping[str, Any] | None = None,
    overwrite: bool = True,
) -> tuple[list[str], dict[str, Any]]:
    """Mix all production lanes to exact PCM for two-pass mastering."""

    cfg = config if isinstance(config, AudioMixConfig) else AudioMixConfig.from_mapping(config)
    cfg.validate()
    duration, target_frames = _sample_count(duration, "duration", cfg.sample_rate)
    inputs = [
        str(Path(value).expanduser())
        for value in (narration_path, music_wav, ambience_wav, presence_wav, events_wav)
    ]
    output = str(Path(output_wav).expanduser())
    argv: list[str] = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        _overwrite_flag(overwrite),
    ]
    for value in inputs:
        argv.extend(["-i", value])
    filters = _documentary_premaster_filters(
        first_input_index=0, target_frames=target_frames, config=cfg
    )
    argv.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[premaster]",
            "-vn",
            "-sn",
            "-dn",
            "-c:a",
            "pcm_s24le",
            "-ar",
            str(cfg.sample_rate),
            "-ac",
            str(cfg.channels),
            output,
        ]
    )
    return argv, {
        "kind": "documentary_premaster_pcm_v2",
        "narration_path": inputs[0],
        "music_path": inputs[1],
        "ambience_path": inputs[2],
        "presence_path": inputs[3],
        "events_path": inputs[4],
        "output_path": output,
        "duration": duration,
        "target_frames": target_frames,
        "codec": "pcm_s24le",
        "sample_rate": cfg.sample_rate,
        "channels": cfg.channels,
        "lane_count": 5,
        "ducking_ratios": {
            "music": cfg.duck_ratio,
            "ambience": cfg.ambience_duck_ratio,
            "presence": cfg.presence_duck_ratio,
            "events": cfg.event_duck_ratio,
        },
        "config": asdict(cfg),
    }


def build_loudnorm_analysis_command(
    premaster_wav: str | Path,
    duration: float,
    *,
    ffmpeg: str = "ffmpeg",
    config: AudioMixConfig | Mapping[str, Any] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Build pass one of deterministic EBU R128 mastering.

    FFmpeg writes the measurement JSON to stderr.  Feed that text to
    :func:`parse_loudnorm_measurements` and then build the final master.
    """

    cfg = config if isinstance(config, AudioMixConfig) else AudioMixConfig.from_mapping(config)
    cfg.validate()
    duration, target_frames = _sample_count(duration, "duration", cfg.sample_rate)
    source = str(Path(premaster_wav).expanduser())
    analysis_filter = (
        f"aformat=sample_fmts=fltp:sample_rates={cfg.sample_rate}:"
        "channel_layouts=stereo,"
        f"aresample={cfg.sample_rate},"
        f"atrim=end_sample={target_frames},asetpts=PTS-STARTPTS,"
        f"loudnorm=I={cfg.target_lufs:.3f}:TP={cfg.true_peak_db:.3f}:"
        f"LRA={cfg.target_lra:.3f}:print_format=json"
    )
    argv = [
        str(ffmpeg),
        "-hide_banner",
        "-nostats",
        "-i",
        source,
        "-vn",
        "-sn",
        "-dn",
        "-af",
        analysis_filter,
        "-f",
        "null",
        "-",
    ]
    return argv, {
        "kind": "loudnorm_analysis_pass_1",
        "input_path": source,
        "duration": duration,
        "target_frames": target_frames,
        "target_lufs": cfg.target_lufs,
        "target_lra": cfg.target_lra,
        "true_peak_db": cfg.true_peak_db,
    }


_LOUDNORM_KEYS = {
    "input_i": "measured_I",
    "input_lra": "measured_LRA",
    "input_tp": "measured_TP",
    "input_thresh": "measured_thresh",
    "target_offset": "offset",
}


def parse_loudnorm_measurements(stderr_text: str) -> dict[str, float]:
    """Extract and validate FFmpeg's flat loudnorm JSON object."""

    if not isinstance(stderr_text, str) or not stderr_text.strip():
        raise AudioPlanError("loudnorm analysis produced no stderr JSON")
    candidates = re.findall(r"\{[^{}]*\}", stderr_text, flags=re.DOTALL)
    for candidate in reversed(candidates):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, Mapping) or not set(_LOUDNORM_KEYS).issubset(value):
            continue
        result: dict[str, float] = {}
        for input_key, output_key in _LOUDNORM_KEYS.items():
            result[output_key] = _finite_number(
                value[input_key], f"loudnorm.{input_key}"
            )
        return result
    raise AudioPlanError("loudnorm analysis JSON is missing required measurements")


def _validated_loudnorm_measurements(
    measurements: Mapping[str, Any],
) -> dict[str, float]:
    if not isinstance(measurements, Mapping):
        raise AudioPlanError("loudnorm measurements must be a mapping")
    required = set(_LOUDNORM_KEYS.values())
    missing = sorted(required - set(measurements))
    if missing:
        raise AudioPlanError(
            "loudnorm measurements are incomplete: " + ", ".join(missing)
        )
    return {
        key: _finite_number(measurements[key], f"loudnorm.{key}")
        for key in sorted(required)
    }


def _master_loudnorm_filter(
    config: AudioMixConfig,
    measurements: Mapping[str, Any] | None,
) -> str:
    base = (
        f"loudnorm=I={config.target_lufs:.3f}:TP={config.true_peak_db:.3f}:"
        f"LRA={config.target_lra:.3f}"
    )
    if measurements is None:
        return base + ":linear=true"
    measured = _validated_loudnorm_measurements(measurements)
    return (
        base
        + f":measured_I={measured['measured_I']:.6f}"
        + f":measured_LRA={measured['measured_LRA']:.6f}"
        + f":measured_TP={measured['measured_TP']:.6f}"
        + f":measured_thresh={measured['measured_thresh']:.6f}"
        + f":offset={measured['offset']:.6f}:linear=true"
    )


def build_documentary_master_command(
    video_path: str | Path,
    premaster_wav: str | Path,
    output_mp4: str | Path,
    duration: float,
    measurements: Mapping[str, Any],
    *,
    ffmpeg: str = "ffmpeg",
    config: AudioMixConfig | Mapping[str, Any] | None = None,
    overwrite: bool = True,
) -> tuple[list[str], dict[str, Any]]:
    """Apply measured pass-two loudness and perform the only AAC encode."""

    cfg = config if isinstance(config, AudioMixConfig) else AudioMixConfig.from_mapping(config)
    cfg.validate()
    duration, target_frames = _sample_count(duration, "duration", cfg.sample_rate)
    measured = _validated_loudnorm_measurements(measurements)
    video = str(Path(video_path).expanduser())
    premaster = str(Path(premaster_wav).expanduser())
    output = str(Path(output_mp4).expanduser())
    limiter_ceiling = 10.0 ** (cfg.true_peak_db / 20.0)
    graph = (
        f"[1:a]aformat=sample_fmts=fltp:sample_rates={cfg.sample_rate}:"
        "channel_layouts=stereo,"
        f"aresample={cfg.sample_rate},"
        f"atrim=end_sample={target_frames},asetpts=PTS-STARTPTS,"
        + _master_loudnorm_filter(cfg, measured)
        + f",alimiter=limit={limiter_ceiling:.6f}:"
        "attack=5:release=50:level=false:latency=true,"
        # loudnorm performs true-peak analysis at 192 kHz.  Without an
        # explicit conversion back, end_sample below counts 192 kHz frames and
        # silently truncates a 48 kHz programme to one quarter of its length.
        f"aresample={cfg.sample_rate},"
        f"atrim=end_sample={target_frames},asetpts=PTS-STARTPTS[master]"
    )
    argv = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        _overwrite_flag(overwrite),
        "-i",
        video,
        "-i",
        premaster,
        "-filter_complex",
        graph,
        "-map",
        "0:v:0",
        "-map",
        "[master]",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        cfg.aac_bitrate,
        "-ar",
        str(cfg.sample_rate),
        "-ac",
        str(cfg.channels),
        "-t",
        _fmt_samples(target_frames, cfg.sample_rate),
        "-max_muxing_queue_size",
        "1024",
        "-movflags",
        "+faststart",
        output,
    ]
    return argv, {
        "kind": "documentary_master_v2_two_pass",
        "video_path": video,
        "premaster_path": premaster,
        "output_path": output,
        "duration": duration,
        "target_frames": target_frames,
        "video_codec_action": "copy",
        "video_mapping": "0:v:0",
        "audio_mapping": "[master]",
        "donor_audio_mapping": False,
        "video_padding": False,
        "audio_codec": "aac",
        "aac_encode_count": 1,
        "loudness": {
            "mode": "two_pass_measured",
            "measurements": measured,
            "integrated_lufs": cfg.target_lufs,
            "lra": cfg.target_lra,
            "true_peak_db": cfg.true_peak_db,
            "limiter": True,
        },
        "config": asdict(cfg),
    }


def build_documentary_mix_command(
    video_path: str | Path,
    narration_path: str | Path,
    music_wav: str | Path,
    ambience_wav: str | Path,
    presence_wav: str | Path,
    events_wav: str | Path,
    output_mp4: str | Path,
    duration: float,
    *,
    ffmpeg: str = "ffmpeg",
    config: AudioMixConfig | Mapping[str, Any] | None = None,
    overwrite: bool = True,
) -> tuple[list[str], dict[str, Any]]:
    """One-command documentary mix; two-pass helpers are release-preferred.

    This convenience path still keeps every natural lane separate, uses four
    ducking policies, maps no audio from input zero, and encodes AAC once.
    """

    cfg = config if isinstance(config, AudioMixConfig) else AudioMixConfig.from_mapping(config)
    cfg.validate()
    duration, target_frames = _sample_count(duration, "duration", cfg.sample_rate)
    inputs = [
        str(Path(value).expanduser())
        for value in (
            video_path,
            narration_path,
            music_wav,
            ambience_wav,
            presence_wav,
            events_wav,
        )
    ]
    output = str(Path(output_mp4).expanduser())
    filters = _documentary_premaster_filters(
        first_input_index=1, target_frames=target_frames, config=cfg
    )
    limiter_ceiling = 10.0 ** (cfg.true_peak_db / 20.0)
    filters.append(
        "[premaster]"
        + _master_loudnorm_filter(cfg, None)
        + f",alimiter=limit={limiter_ceiling:.6f}:"
        "attack=5:release=50:level=false:latency=true,"
        f"atrim=end_sample={target_frames},asetpts=PTS-STARTPTS[master]"
    )
    argv: list[str] = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        _overwrite_flag(overwrite),
    ]
    for value in inputs:
        argv.extend(["-i", value])
    argv.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "0:v:0",
            "-map",
            "[master]",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            cfg.aac_bitrate,
            "-ar",
            str(cfg.sample_rate),
            "-ac",
            str(cfg.channels),
            "-t",
            _fmt_samples(target_frames, cfg.sample_rate),
            "-max_muxing_queue_size",
            "1024",
            "-movflags",
            "+faststart",
            output,
        ]
    )
    return argv, {
        "kind": "documentary_mix_v2_one_pass",
        "video_path": inputs[0],
        "narration_path": inputs[1],
        "music_path": inputs[2],
        "ambience_path": inputs[3],
        "presence_path": inputs[4],
        "events_path": inputs[5],
        "output_path": output,
        "duration": duration,
        "target_frames": target_frames,
        "lane_count": 5,
        "video_mapping": "0:v:0",
        "audio_mapping": "[master]",
        "donor_audio_mapping": False,
        "video_padding": False,
        "audio_codec": "aac",
        "aac_encode_count": 1,
        "mastering": "one_pass; use premaster/analysis/master for release",
        "config": asdict(cfg),
    }


def command_maps_input_audio(argv: Sequence[str], input_index: int) -> bool:
    """Return whether explicit ``-map`` directives reference input audio.

    This is intentionally structural: checking a UI flag is insufficient.  It
    handles maps such as ``0:a``, ``0:a:0`` and optional ``0:a?``.
    """

    prefix = f"{int(input_index)}:a"
    for index, token in enumerate(argv[:-1]):
        if token != "-map":
            continue
        mapping = str(argv[index + 1]).lstrip("-")
        if mapping.startswith(prefix):
            return True
    return False


__all__ = [
    "AMBIENCE_ENERGY_FLOOR_POLICY_VERSION",
    "AudioMixConfig",
    "AudioPlanError",
    "build_ambience_stem_command",
    "build_background_stem_command",
    "build_documentary_master_command",
    "build_documentary_mix_command",
    "build_documentary_premaster_command",
    "build_event_stem_command",
    "build_final_mix_command",
    "build_layered_final_mix_command",
    "build_loudnorm_analysis_command",
    "build_looped_background_command",
    "build_music_stem_command",
    "build_presence_stem_command",
    "build_segmented_music_stem_command",
    "build_sfx_stem_command",
    "command_maps_input_audio",
    "derive_ambience_energy_floor_adjustments",
    "finalize_ambience_energy_floor_facts",
    "parse_loudnorm_measurements",
]
