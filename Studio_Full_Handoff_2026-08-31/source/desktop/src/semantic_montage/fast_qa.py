"""Bounded technical QA for fast semantic-montage masters.

The checks are intentionally technical and deterministic: stream contract,
duration, a complete decode, whole-frame black intervals, and release
identity.  Semantic quality remains the responsibility of selection and the
accelerated human review; this module does not recreate strict receipts.
"""

from __future__ import annotations

import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .common import atomic_write_json, sha256_file
from .fast_render import (
    FAST_FPS,
    Deadline,
    DeadlineExceeded,
    DeadlineLike,
    FastRenderError,
    deadline_timeout,
    run_command,
)


class FastQAError(RuntimeError):
    """A final master failed the bounded fast QA contract."""


@dataclass(frozen=True)
class FastQAConfig:
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    duration_tolerance_seconds: float = 0.09
    black_min_duration_seconds: float = 0.30
    black_pixel_threshold: float = 0.10
    black_picture_ratio: float = 0.98
    integrated_lufs_min: float = -17.5
    integrated_lufs_max: float = -14.5
    true_peak_dbtp_max: float = -0.5
    av_duration_tolerance_seconds: float = 0.15
    probe_timeout_seconds: float = 60.0
    decode_timeout_seconds: float = 1_200.0
    analysis_timeout_seconds: float = 1_200.0

    def validate(self) -> None:
        for name in (
            "duration_tolerance_seconds",
            "black_min_duration_seconds",
            "av_duration_tolerance_seconds",
            "probe_timeout_seconds",
            "decode_timeout_seconds",
            "analysis_timeout_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0:
                raise FastQAError(f"{name} must be a positive finite number")
        for name in ("black_pixel_threshold", "black_picture_ratio"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not math.isfinite(float(value))
                or not 0 < float(value) <= 1
            ):
                raise FastQAError(f"{name} must be in (0, 1]")
        try:
            loudness_min = float(self.integrated_lufs_min)
            loudness_max = float(self.integrated_lufs_max)
            peak_max = float(self.true_peak_dbtp_max)
        except (TypeError, ValueError) as exc:
            raise FastQAError("loudness limits must be finite numbers") from exc
        if not all(math.isfinite(value) for value in (loudness_min, loudness_max, peak_max)):
            raise FastQAError("loudness limits must be finite numbers")
        if loudness_min > loudness_max:
            raise FastQAError("integrated_lufs_min must not exceed integrated_lufs_max")


_BLACK_RE = re.compile(
    r"black_start:(?P<start>-?\d+(?:\.\d+)?)\s+"
    r"black_end:(?P<end>-?\d+(?:\.\d+)?)\s+"
    r"black_duration:(?P<duration>\d+(?:\.\d+)?)"
)


def build_ffprobe_command(
    path: str | Path, *, ffprobe: str = "ffprobe"
) -> list[str]:
    return [
        str(ffprobe),
        "-v",
        "error",
        "-show_entries",
        (
            "format=duration,size,bit_rate:"
            "stream=index,codec_type,codec_name,width,height,pix_fmt,"
            "r_frame_rate,avg_frame_rate,sample_rate,channels,channel_layout,duration"
        ),
        "-of",
        "json",
        str(Path(path).expanduser().resolve()),
    ]


def build_full_decode_command(
    path: str | Path, *, ffmpeg: str = "ffmpeg"
) -> list[str]:
    """Decode both required streams completely and fail on corrupt packets."""

    return [
        str(ffmpeg),
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "error",
        "-xerror",
        "-i",
        str(Path(path).expanduser().resolve()),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-f",
        "null",
        "-",
    ]


def build_blackdetect_command(
    path: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    min_duration_seconds: float = 0.30,
    pixel_threshold: float = 0.10,
    picture_ratio: float = 0.98,
) -> list[str]:
    return [
        str(ffmpeg),
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "info",
        "-i",
        str(Path(path).expanduser().resolve()),
        "-map",
        "0:v:0",
        "-vf",
        (
            f"blackdetect=d={float(min_duration_seconds):.6f}:"
            f"pix_th={float(pixel_threshold):.6f}:"
            f"pic_th={float(picture_ratio):.6f}"
        ),
        "-an",
        "-sn",
        "-dn",
        "-f",
        "null",
        "-",
    ]


def build_loudness_command(
    path: str | Path, *, ffmpeg: str = "ffmpeg"
) -> list[str]:
    """Build a complete audio decode with machine-readable loudnorm metrics."""

    return [
        str(ffmpeg),
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "info",
        "-xerror",
        "-i",
        str(Path(path).expanduser().resolve()),
        "-map",
        "0:a:0",
        "-vn",
        "-af",
        "loudnorm=I=-16:TP=-1:LRA=11:print_format=json",
        "-f",
        "null",
        "-",
    ]


def build_combined_analysis_command(
    path: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    min_duration_seconds: float = 0.30,
    pixel_threshold: float = 0.10,
    picture_ratio: float = 0.98,
) -> list[str]:
    """Decode A/V once while running black and loudness measurements."""

    filters = (
        "[0:v:0]"
        f"blackdetect=d={float(min_duration_seconds):.6f}:"
        f"pix_th={float(pixel_threshold):.6f}:"
        f"pic_th={float(picture_ratio):.6f}[vqa];"
        "[0:a:0]loudnorm=I=-16:TP=-1:LRA=11:print_format=json[aqa]"
    )
    return [
        str(ffmpeg),
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "info",
        "-xerror",
        "-i",
        str(Path(path).expanduser().resolve()),
        "-filter_complex",
        filters,
        "-map",
        "[vqa]",
        "-map",
        "[aqa]",
        "-f",
        "null",
        "-",
    ]


def parse_blackdetect(stderr: str) -> list[dict[str, float]]:
    return [
        {
            "start": float(match.group("start")),
            "end": float(match.group("end")),
            "duration": float(match.group("duration")),
        }
        for match in _BLACK_RE.finditer(stderr or "")
    ]


def parse_loudness(stderr: str) -> dict[str, float]:
    """Extract the final loudnorm JSON block from FFmpeg diagnostics."""

    measurement: Mapping[str, Any] | None = None
    for match in re.finditer(r"\{[^{}]*\}", stderr or "", re.S):
        try:
            candidate = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, Mapping) and {
            "input_i",
            "input_tp",
        }.issubset(candidate):
            measurement = candidate
    if measurement is None:
        raise FastQAError("loudnorm measurement is missing")
    try:
        integrated = float(measurement["input_i"])
        true_peak = float(measurement["input_tp"])
    except (TypeError, ValueError) as exc:
        raise FastQAError("loudnorm measurement is invalid") from exc
    if not math.isfinite(integrated) or not math.isfinite(true_peak):
        raise FastQAError("loudnorm measurement is not finite")
    return {"integrated_lufs": integrated, "true_peak_dbtp": true_peak}


def validate_loudness(
    measurement: Mapping[str, Any],
    *,
    integrated_lufs_min: float = -17.5,
    integrated_lufs_max: float = -14.5,
    true_peak_dbtp_max: float = -0.5,
) -> dict[str, Any]:
    integrated = float(measurement["integrated_lufs"])
    peak = float(measurement["true_peak_dbtp"])
    errors: list[str] = []
    if not float(integrated_lufs_min) <= integrated <= float(integrated_lufs_max):
        errors.append(
            f"integrated loudness {integrated:.2f} LUFS outside "
            f"[{float(integrated_lufs_min):.2f}, {float(integrated_lufs_max):.2f}]"
        )
    if peak > float(true_peak_dbtp_max):
        errors.append(
            f"true peak {peak:.2f} dBTP exceeds {float(true_peak_dbtp_max):.2f} dBTP"
        )
    return {**dict(measurement), "passed": not errors, "errors": errors}


def _probe_duration(payload: Mapping[str, Any]) -> float:
    try:
        duration = float((payload.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError) as exc:
        raise FastQAError("ffprobe returned an invalid duration") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise FastQAError("ffprobe returned a non-positive duration")
    return duration


def validate_probe(
    payload: Mapping[str, Any],
    *,
    expected_duration: float,
    duration_tolerance_seconds: float = 0.09,
    av_duration_tolerance_seconds: float = 0.15,
) -> dict[str, Any]:
    """Validate the exact fast-master stream and timing contract."""

    expected = float(expected_duration)
    tolerance = float(duration_tolerance_seconds)
    av_tolerance = float(av_duration_tolerance_seconds)
    if not math.isfinite(expected) or expected <= 0:
        raise FastQAError("expected_duration must be positive and finite")
    if not math.isfinite(tolerance) or tolerance <= 0:
        raise FastQAError("duration tolerance must be positive and finite")
    if not math.isfinite(av_tolerance) or av_tolerance <= 0:
        raise FastQAError("A/V duration tolerance must be positive and finite")
    streams = list(payload.get("streams") or [])
    videos = [row for row in streams if row.get("codec_type") == "video"]
    audios = [row for row in streams if row.get("codec_type") == "audio"]
    errors: list[str] = []
    if len(videos) != 1:
        errors.append(f"expected exactly one video stream, got {len(videos)}")
    if len(audios) != 1:
        errors.append(f"expected exactly one audio stream, got {len(audios)}")
    if len(videos) == 1:
        video = videos[0]
        if video.get("codec_name") != "h264":
            errors.append(f"video codec is {video.get('codec_name')!r}, expected 'h264'")
        if (int(video.get("width") or 0), int(video.get("height") or 0)) != (1920, 1080):
            errors.append(
                f"video geometry is {video.get('width')}x{video.get('height')}, "
                "expected 1920x1080"
            )
        if video.get("pix_fmt") != "yuv420p":
            errors.append(f"video pix_fmt is {video.get('pix_fmt')!r}, expected 'yuv420p'")
        rate = str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "")
        if rate != FAST_FPS:
            errors.append(f"video frame rate is {rate!r}, expected {FAST_FPS!r}")
    if len(audios) == 1:
        audio = audios[0]
        if audio.get("codec_name") != "aac":
            errors.append(f"audio codec is {audio.get('codec_name')!r}, expected 'aac'")
        if str(audio.get("sample_rate") or "") != "48000":
            errors.append(
                f"audio sample rate is {audio.get('sample_rate')!r}, expected '48000'"
            )
        if int(audio.get("channels") or 0) != 2:
            errors.append(f"audio channels is {audio.get('channels')!r}, expected 2")
    video_duration: float | None = None
    audio_duration: float | None = None
    if len(videos) == 1 and len(audios) == 1:
        try:
            video_duration = float(videos[0].get("duration"))
            audio_duration = float(audios[0].get("duration"))
        except (TypeError, ValueError):
            errors.append("ffprobe stream durations are missing or invalid")
        else:
            if (
                not math.isfinite(video_duration)
                or not math.isfinite(audio_duration)
                or video_duration <= 0
                or audio_duration <= 0
            ):
                errors.append("ffprobe stream durations are missing or invalid")
            elif abs(video_duration - audio_duration) > av_tolerance:
                errors.append(
                    f"A/V stream duration difference "
                    f"{abs(video_duration - audio_duration):.3f}s exceeds "
                    f"{av_tolerance:.3f}s"
                )
    try:
        actual_duration = _probe_duration(payload)
    except FastQAError as exc:
        actual_duration = 0.0
        errors.append(str(exc))
    drift = actual_duration - expected
    if actual_duration > 0 and abs(drift) > tolerance:
        errors.append(
            f"duration drift {drift:+.3f}s exceeds tolerance {tolerance:.3f}s"
        )
    return {
        "passed": not errors,
        "errors": errors,
        "expected_duration": expected,
        "actual_duration": actual_duration,
        "duration_drift": drift,
        "duration_tolerance": tolerance,
        "video_duration": video_duration,
        "audio_duration": audio_duration,
        "av_duration_difference": (
            abs(video_duration - audio_duration)
            if video_duration is not None and audio_duration is not None
            else None
        ),
        "av_duration_tolerance": av_tolerance,
        "video_stream_count": len(videos),
        "audio_stream_count": len(audios),
        "streams": streams,
    }


def _default_manifest_path(media_path: Path) -> Path:
    return media_path.with_name(media_path.name + ".fast-qa.json")


def _check_deadline(deadline: Deadline | DeadlineLike | float | None) -> None:
    # Calling the shared helper guarantees identical duck-typing and expiry
    # semantics without assigning a new per-stage budget.
    deadline_timeout(deadline, cap_seconds=86_400.0)


def run_fast_qa(
    path: str | Path,
    *,
    expected_duration: float,
    language: str | None = None,
    manifest_path: str | Path | None = None,
    deadline: Deadline | DeadlineLike | float | None = None,
    config: FastQAConfig | None = None,
    raise_on_failure: bool = False,
) -> dict[str, Any]:
    """Run the complete bounded QA and atomically persist one manifest."""

    cfg = config or FastQAConfig()
    cfg.validate()
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FastQAError(f"master does not exist: {source}")
    target_manifest = (
        Path(manifest_path).expanduser().resolve()
        if manifest_path is not None
        else _default_manifest_path(source)
    )
    started = time.monotonic()
    timings: dict[str, float] = {}
    errors: list[str] = []

    stage_started = time.monotonic()
    probe_command = build_ffprobe_command(source, ffprobe=cfg.ffprobe)
    probe_process = run_command(
        probe_command,
        deadline=deadline,
        timeout_cap=cfg.probe_timeout_seconds,
    )
    try:
        probe_payload = json.loads(probe_process.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise FastQAError("ffprobe returned invalid JSON") from exc
    probe_result = validate_probe(
        probe_payload,
        expected_duration=expected_duration,
        duration_tolerance_seconds=cfg.duration_tolerance_seconds,
        av_duration_tolerance_seconds=cfg.av_duration_tolerance_seconds,
    )
    errors.extend(probe_result["errors"])
    timings["probe_seconds"] = time.monotonic() - stage_started

    stage_started = time.monotonic()
    analysis_command = build_combined_analysis_command(
        source,
        ffmpeg=cfg.ffmpeg,
        min_duration_seconds=cfg.black_min_duration_seconds,
        pixel_threshold=cfg.black_pixel_threshold,
        picture_ratio=cfg.black_picture_ratio,
    )
    analysis_error: str | None = None
    black_intervals: list[dict[str, float]] = []
    loudness: dict[str, Any] = {
        "passed": False,
        "errors": ["loudness measurement was not run"],
        "integrated_lufs": None,
        "true_peak_dbtp": None,
    }
    try:
        analysis_process = run_command(
            analysis_command,
            deadline=deadline,
            timeout_cap=min(
                cfg.decode_timeout_seconds, cfg.analysis_timeout_seconds
            ),
        )
    except DeadlineExceeded:
        raise
    except FastRenderError as exc:
        analysis_error = str(exc)
        errors.append(f"full A/V decode/analysis failed: {analysis_error}")
        loudness["errors"] = [f"analysis failed: {analysis_error}"]
    else:
        black_intervals = parse_blackdetect(analysis_process.stderr)
        if black_intervals:
            errors.append(
                f"blackdetect found {len(black_intervals)} interval(s) "
                f">= {cfg.black_min_duration_seconds:.3f}s"
            )
        try:
            loudness = validate_loudness(
                parse_loudness(analysis_process.stderr),
                integrated_lufs_min=cfg.integrated_lufs_min,
                integrated_lufs_max=cfg.integrated_lufs_max,
                true_peak_dbtp_max=cfg.true_peak_dbtp_max,
            )
        except FastQAError as exc:
            loudness = {
                "passed": False,
                "errors": [str(exc)],
                "integrated_lufs": None,
                "true_peak_dbtp": None,
            }
        errors.extend(loudness["errors"])
    timings["decode_black_loudness_seconds"] = time.monotonic() - stage_started

    stage_started = time.monotonic()
    _check_deadline(deadline)
    byte_count = source.stat().st_size
    sha256 = sha256_file(source)
    _check_deadline(deadline)
    timings["hash_seconds"] = time.monotonic() - stage_started
    timings["total_seconds"] = time.monotonic() - started

    report = {
        "schema_version": 1,
        "kind": "semantic_montage_fast_qa",
        "generated_at": datetime.now(UTC).isoformat(),
        "language": str(language).lower() if language is not None else None,
        "path": str(source),
        "passed": not errors,
        "errors": errors,
        "bytes": byte_count,
        "sha256": sha256,
        "timings": timings,
        "probe": probe_result,
        "full_decode": {
            "passed": analysis_error is None,
            "error": analysis_error,
            "command": analysis_command,
        },
        "blackdetect": {
            "passed": analysis_error is None and not black_intervals,
            "error": analysis_error,
            "intervals": black_intervals,
            "minimum_duration": cfg.black_min_duration_seconds,
            "command": analysis_command,
        },
        "loudness": {**loudness, "command": analysis_command},
        "commands": {"ffprobe": probe_command},
    }
    atomic_write_json(target_manifest, report)
    report["manifest_path"] = str(target_manifest)
    if raise_on_failure and not report["passed"]:
        raise FastQAError("; ".join(errors))
    return report


def run_fast_qa_pair(
    masters: Mapping[str, str | Path],
    expected_durations: Mapping[str, float],
    *,
    manifest_paths: Mapping[str, str | Path] | None = None,
    deadline: Deadline | DeadlineLike | float | None = None,
    config: FastQAConfig | None = None,
    raise_on_failure: bool = False,
) -> dict[str, Any]:
    """QA RU and EN with one shared deadline and at most two workers."""

    languages = tuple(masters)
    if not languages or set(languages) != set(expected_durations):
        raise FastQAError("masters and expected_durations must have identical keys")
    if len(languages) > 2:
        raise FastQAError("fast QA pair supports at most two language masters")
    shared_deadline = deadline or Deadline.after(1_800.0)
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(2, len(languages))) as executor:
        futures = {
            executor.submit(
                run_fast_qa,
                masters[language],
                expected_duration=expected_durations[language],
                language=language,
                manifest_path=(manifest_paths or {}).get(language),
                deadline=shared_deadline,
                config=config,
                raise_on_failure=False,
            ): language
            for language in languages
        }
        for future in as_completed(futures):
            language = futures[future]
            results[language] = future.result()
    passed = all(result["passed"] for result in results.values())
    if raise_on_failure and not passed:
        failed = [language for language, result in results.items() if not result["passed"]]
        raise FastQAError(f"fast QA failed for: {', '.join(sorted(failed))}")
    return {"passed": passed, "languages": results}


__all__ = [
    "FastQAConfig",
    "FastQAError",
    "build_blackdetect_command",
    "build_combined_analysis_command",
    "build_ffprobe_command",
    "build_full_decode_command",
    "build_loudness_command",
    "parse_blackdetect",
    "parse_loudness",
    "run_fast_qa",
    "run_fast_qa_pair",
    "validate_loudness",
    "validate_probe",
]
