"""Lane-specific audio QA; music cannot hide a missing natural soundscape."""
from __future__ import annotations

import math
import os
import wave
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np

from .common import sha256_file


class AudioQAError(ValueError):
    pass


def _decode_pcm(path: str | os.PathLike[str]) -> tuple[np.ndarray, int, int]:
    source = Path(path).expanduser().resolve()
    with wave.open(str(source), "rb") as stream:
        channels = stream.getnchannels()
        sample_rate = stream.getframerate()
        sample_width = stream.getsampwidth()
        frames = stream.getnframes()
        compression = stream.getcomptype()
        raw = stream.readframes(frames)
    if compression != "NONE" or channels < 1 or sample_rate <= 0:
        raise AudioQAError(f"unsupported WAV format: {source}")
    if sample_width == 2:
        values = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 4:
        values = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif sample_width == 3:
        triples = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        integers = (
            triples[:, 0].astype(np.int32)
            | (triples[:, 1].astype(np.int32) << 8)
            | (triples[:, 2].astype(np.int32) << 16)
        )
        integers = (integers ^ 0x800000) - 0x800000
        values = integers.astype(np.float32) / 8388608.0
    else:
        raise AudioQAError(f"unsupported PCM sample width: {sample_width}")
    if values.size % channels:
        raise AudioQAError("truncated PCM frame")
    return values.reshape(-1, channels), sample_rate, sample_width


def detect_energy_gaps(
    path: str | os.PathLike[str],
    *,
    threshold_dbfs: float = -65.0,
    max_gap_seconds: float = 0.100,
    window_seconds: float = 0.010,
) -> dict[str, Any]:
    samples, sample_rate, _ = _decode_pcm(path)
    mono = np.mean(samples, axis=1)
    window = max(1, int(round(window_seconds * sample_rate)))
    rms = []
    for start in range(0, len(mono), window):
        chunk = mono[start:start + window]
        rms.append(float(np.sqrt(np.mean(np.square(chunk), dtype=np.float64))))
    threshold = 10.0 ** (threshold_dbfs / 20.0)
    silent = np.asarray(rms) <= threshold
    gaps: list[dict[str, float]] = []
    start_index: int | None = None
    for index in range(len(silent) + 1):
        is_silent = bool(silent[index]) if index < len(silent) else False
        if is_silent and start_index is None:
            start_index = index
        elif not is_silent and start_index is not None:
            start_seconds = start_index * window / sample_rate
            end_seconds = min(index * window / sample_rate, len(mono) / sample_rate)
            if end_seconds - start_seconds > max_gap_seconds + 1e-9:
                gaps.append(
                    {
                        "start": start_seconds,
                        "end": end_seconds,
                        "duration": end_seconds - start_seconds,
                    }
                )
            start_index = None
    return {
        "passed": not gaps,
        "path_sha256": sha256_file(path),
        "sample_rate": sample_rate,
        "duration": len(mono) / sample_rate,
        "threshold_dbfs": threshold_dbfs,
        "max_gap_seconds": max_gap_seconds,
        "gaps": gaps,
    }


def detect_boundary_clicks(
    path: str | os.PathLike[str],
    boundaries: Iterable[float],
    *,
    absolute_threshold: float = 0.30,
    local_ratio_threshold: float = 10.0,
) -> dict[str, Any]:
    samples, sample_rate, _ = _decode_pcm(path)
    mono = np.mean(samples, axis=1)
    flags = []
    radius = max(8, int(round(0.025 * sample_rate)))
    for boundary in boundaries:
        frame = int(round(float(boundary) * sample_rate))
        if not 1 <= frame < len(mono):
            continue
        jump = abs(float(mono[frame] - mono[frame - 1]))
        left = mono[max(1, frame - radius):frame]
        right = mono[frame:min(len(mono), frame + radius)]
        neighborhood = np.concatenate((np.diff(left), np.diff(right)))
        local = float(np.median(np.abs(neighborhood))) if neighborhood.size else 0.0
        ratio = jump / max(local, 1e-6)
        if jump >= absolute_threshold and ratio >= local_ratio_threshold:
            flags.append(
                {"at": frame / sample_rate, "jump": jump, "local_ratio": ratio}
            )
    return {"passed": not flags, "flags": flags}


def validate_pcm_stem(
    path: str | os.PathLike[str],
    *,
    expected_duration: float,
    required_sample_rate: int = 48000,
    required_channels: int = 2,
) -> dict[str, Any]:
    samples, sample_rate, sample_width = _decode_pcm(path)
    duration = len(samples) / sample_rate
    errors = []
    try:
        expected_seconds = float(expected_duration)
    except (TypeError, ValueError) as exc:
        raise AudioQAError("expected_duration must be numeric") from exc
    if not math.isfinite(expected_seconds) or expected_seconds <= 0:
        raise AudioQAError("expected_duration must be finite and positive")
    expected_frames = int(round(expected_seconds * required_sample_rate))
    if sample_rate != required_sample_rate:
        errors.append(f"sample_rate={sample_rate}")
    if samples.shape[1] != required_channels:
        errors.append(f"channels={samples.shape[1]}")
    if len(samples) != expected_frames:
        errors.append(f"frames={len(samples)} expected_frames={expected_frames}")
    if not np.all(np.isfinite(samples)):
        errors.append("non-finite samples")
    if float(np.max(np.abs(samples), initial=0.0)) >= 1.0:
        errors.append("clipping")
    return {
        "passed": not errors,
        "errors": errors,
        "sha256": sha256_file(path),
        "sample_rate": sample_rate,
        "channels": samples.shape[1],
        "sample_width": sample_width,
        "frames": len(samples),
        "expected_frames": expected_frames,
        "duration": duration,
        "peak": float(np.max(np.abs(samples), initial=0.0)),
    }


def run_diegetic_audio_qa(
    *,
    ambience_path: str | os.PathLike[str],
    presence_path: str | os.PathLike[str],
    events_path: str | os.PathLike[str],
    expected_duration: float,
    boundaries: Iterable[float],
    semantic_report: Mapping[str, Any],
    contamination_report: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate natural stems independently of voice and music."""

    stems = {
        name: validate_pcm_stem(path, expected_duration=expected_duration)
        for name, path in {
            "ambience": ambience_path,
            "presence": presence_path,
            "events": events_path,
        }.items()
    }
    energy = detect_energy_gaps(ambience_path)
    clicks = detect_boundary_clicks(ambience_path, boundaries)
    speech_probability = float(contamination_report.get("speech_probability") or 0.0)
    music_probability = float(contamination_report.get("music_probability") or 0.0)
    contamination_pass = speech_probability <= 0.05 and music_probability <= 0.05
    semantic_pass = (
        bool(semantic_report.get("passed"))
        and int(semantic_report.get("acoustic_contradictions") or 0) == 0
        and float(semantic_report.get("notable_source_coverage") or 0.0) >= 0.90
    )
    errors = []
    if not all(row["passed"] for row in stems.values()):
        errors.append("invalid natural PCM stem")
    if not energy["passed"]:
        errors.append("ambience has an energy gap longer than 100ms")
    if not clicks["passed"]:
        errors.append("ambience has an audible boundary click")
    if not contamination_pass:
        errors.append("speech or music contamination in diegetic stems")
    if not semantic_pass:
        errors.append("diegetic semantic QA failed")
    return {
        "passed": not errors,
        "errors": errors,
        "stems": stems,
        "ambience_energy": energy,
        "boundary_clicks": clicks,
        "contamination": {
            **dict(contamination_report),
            "passed": contamination_pass,
        },
        "semantic": {**dict(semantic_report), "passed": semantic_pass},
    }


__all__ = [
    "AudioQAError",
    "detect_boundary_clicks",
    "detect_energy_gaps",
    "run_diegetic_audio_qa",
    "validate_pcm_stem",
]
