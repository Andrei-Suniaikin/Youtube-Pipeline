"""Fast, fail-closed FFmpeg rendering for the Pareto montage lane.

The fast lane deliberately has a much smaller contract than ``render.py``:

* RU and EN use the same ordered visual selections, but may have different
  target durations;
* one mixed-input filter graph renders each language's video timeline;
* donor audio is never mapped;
* at most two NVENC encodes run in this Python process, while large bilingual
  EDLs run sequentially to keep aggregate FFmpeg RSS bounded;
* narration is muxed in a separate, video-stream-copy pass; and
* every published media file is first completed as ``*.partial`` and then
  atomically replaced.

This module contains no strict-candidate receipts.  Its returned dictionaries
are operational results only and are intentionally JSON-friendly.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence


FAST_FPS = "24000/1001"
FAST_FPS_VALUE = 24000.0 / 1001.0
MAX_NVENC_JOBS = 2
# Measured on the 31 GiB Minsk renderer: two 64-shot encodes used about
# 2.9 GiB RSS each, while two 244-shot encodes grew to about 12.6 GiB each and
# exhausted host headroom.  Keep the proven canary parallel and serialize
# larger graphs.  This is an explicit operational guard, not a quality knob.
MAX_PARALLEL_LANGUAGE_SHOTS = 96
MAX_RENDER_INPUTS = 512
MAX_COMMAND_CHARACTERS = 750_000
_CROP_MARGIN_KEYS = ("left", "right", "top", "bottom")


class FastRenderError(RuntimeError):
    """The fast render contract is invalid or FFmpeg failed."""


class DeadlineExceeded(FastRenderError):
    """The shared end-to-end deadline has expired."""


class DeadlineLike(Protocol):
    """Small duck-typed contract accepted from an outer pipeline deadline."""

    def remaining(self) -> float: ...


@dataclass(frozen=True)
class Deadline:
    """Monotonic deadline shared by all subprocesses in one fast build."""

    expires_at: float

    @classmethod
    def after(cls, seconds: float) -> "Deadline":
        value = _positive_finite(seconds, "deadline seconds")
        return cls(time.monotonic() + value)

    def remaining(self) -> float:
        return max(0.0, float(self.expires_at) - time.monotonic())

    def remaining_seconds(self) -> float:
        return self.remaining()


@dataclass(frozen=True)
class FastRenderConfig:
    ffmpeg: str = "ffmpeg"
    width: int = 1920
    height: int = 1080
    fps: str = FAST_FPS
    preset: str = "p5"
    tune: str = "hq"
    rate_control: str = "vbr"
    cq: int = 19
    edge_crop_percent: float = 0.05
    audio_bitrate: str = "192k"
    render_timeout_seconds: float = 1_800.0
    mux_timeout_seconds: float = 600.0
    keep_intermediates: bool = False

    def validate(self) -> None:
        if (int(self.width), int(self.height)) != (1920, 1080):
            raise FastRenderError("fast output is fixed at 1920x1080")
        if str(self.fps) != FAST_FPS:
            raise FastRenderError(f"fast output fps is fixed at {FAST_FPS}")
        if str(self.preset) != "p5" or str(self.tune) != "hq":
            raise FastRenderError("fast NVENC contract requires preset p5 and tune hq")
        if str(self.rate_control) != "vbr" or int(self.cq) != 19:
            raise FastRenderError("fast NVENC contract requires rc=vbr and cq=19")
        try:
            crop = float(self.edge_crop_percent)
        except (TypeError, ValueError) as exc:
            raise FastRenderError("edge_crop_percent must be in [0, 0.08]") from exc
        if (
            isinstance(self.edge_crop_percent, bool)
            or not math.isfinite(crop)
            or not 0 <= crop <= 0.08
        ):
            raise FastRenderError("edge_crop_percent must be in [0, 0.08]")
        _positive_finite(self.render_timeout_seconds, "render timeout")
        _positive_finite(self.mux_timeout_seconds, "mux timeout")


@dataclass(frozen=True)
class FastEDLItem:
    slot_id: str
    canonical_id: str
    source_path: str
    source_start: float
    source_end: float
    target_duration: float
    crop_margins: Mapping[str, float] = field(
        default_factory=lambda: {key: 0.0 for key in _CROP_MARGIN_KEYS}
    )


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


_LANGUAGE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_NVENC_SLOTS = threading.BoundedSemaphore(MAX_NVENC_JOBS)


def _positive_finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise FastRenderError(f"{name} must be a positive finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise FastRenderError(f"{name} must be a positive finite number") from exc
    if not math.isfinite(result) or result <= 0:
        raise FastRenderError(f"{name} must be a positive finite number")
    return result


def _nonnegative_finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise FastRenderError(f"{name} must be a non-negative finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise FastRenderError(f"{name} must be a non-negative finite number") from exc
    if not math.isfinite(result) or result < 0:
        raise FastRenderError(f"{name} must be a non-negative finite number")
    return result


def _deadline_remaining(deadline: Deadline | DeadlineLike | float | None) -> float | None:
    """Return remaining seconds for local or compatible outer deadlines.

    Numeric values are interpreted as absolute ``time.monotonic()`` deadlines,
    never as a fresh per-command timeout.  Objects may expose either
    ``remaining_seconds()`` or ``remaining()``.
    """

    if deadline is None:
        return None
    if isinstance(deadline, bool):
        raise FastRenderError("deadline must not be boolean")
    if isinstance(deadline, (int, float)):
        remaining = float(deadline) - time.monotonic()
    else:
        candidate = getattr(deadline, "remaining_seconds", None)
        if candidate is None:
            candidate = getattr(deadline, "remaining", None)
        if candidate is None:
            raise FastRenderError(
                "deadline must expose remaining_seconds/remaining as a value or method"
            )
        remaining = float(candidate() if callable(candidate) else candidate)
    if not math.isfinite(remaining):
        raise FastRenderError("deadline remaining time must be finite")
    return max(0.0, remaining)


def deadline_timeout(
    deadline: Deadline | DeadlineLike | float | None,
    *,
    cap_seconds: float,
) -> float:
    """Bound a subprocess timeout by the one shared pipeline deadline."""

    cap = _positive_finite(cap_seconds, "timeout cap")
    remaining = _deadline_remaining(deadline)
    timeout = cap if remaining is None else min(cap, remaining)
    if timeout <= 0:
        raise DeadlineExceeded("fast build deadline expired")
    # Avoid passing a zero-ish timeout to subprocess after scheduling jitter.
    return max(0.001, timeout)


def _edl_rows(edl: Sequence[Mapping[str, Any]] | Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if isinstance(edl, Mapping):
        raw: Any = None
        for key in ("items", "shots", "edl"):
            if key in edl:
                raw = edl[key]
                break
        if raw is None:
            raise FastRenderError("EDL mapping requires items, shots, or edl")
    else:
        raw = edl
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise FastRenderError("EDL must be a sequence of mappings")
    rows = list(raw)
    if not rows:
        raise FastRenderError("EDL must contain at least one item")
    if not all(isinstance(row, Mapping) for row in rows):
        raise FastRenderError("every EDL item must be a mapping")
    return rows


def _normalize_crop_margins(value: Any, *, item_index: int) -> dict[str, float]:
    if value is None:
        return {key: 0.0 for key in _CROP_MARGIN_KEYS}
    if not isinstance(value, Mapping):
        raise FastRenderError(f"EDL item {item_index} crop_margins must be a mapping")
    missing = [key for key in _CROP_MARGIN_KEYS if key not in value]
    unknown = sorted(str(key) for key in value if key not in _CROP_MARGIN_KEYS)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unknown:
            details.append(f"unknown {', '.join(unknown)}")
        raise FastRenderError(
            f"EDL item {item_index} crop_margins requires left/right/top/bottom "
            f"({'; '.join(details)})"
        )
    margins: dict[str, float] = {}
    for key in _CROP_MARGIN_KEYS:
        raw = value[key]
        if isinstance(raw, bool):
            raise FastRenderError(
                f"EDL item {item_index} crop_margins.{key} must be in [0, 0.25]"
            )
        try:
            margin = float(raw)
        except (TypeError, ValueError) as exc:
            raise FastRenderError(
                f"EDL item {item_index} crop_margins.{key} must be in [0, 0.25]"
            ) from exc
        if not math.isfinite(margin) or not 0 <= margin <= 0.25:
            raise FastRenderError(
                f"EDL item {item_index} crop_margins.{key} must be in [0, 0.25]"
            )
        margins[key] = margin
    retained_width = 1.0 - margins["left"] - margins["right"]
    retained_height = 1.0 - margins["top"] - margins["bottom"]
    retained_area = retained_width * retained_height
    if retained_area + 1e-12 < 0.65:
        raise FastRenderError(
            f"EDL item {item_index} crop_margins retain only "
            f"{retained_area:.3f} of the uniform-cropped frame; minimum is 0.65"
        )
    return margins


def normalize_edl(
    edl: Sequence[Mapping[str, Any]] | Mapping[str, Any],
) -> list[FastEDLItem]:
    """Validate and normalize the compact fast-lane EDL contract."""

    normalized: list[FastEDLItem] = []
    seen_canonical: set[str] = set()
    frame = 1.0 / FAST_FPS_VALUE
    for index, row in enumerate(_edl_rows(edl)):
        slot_id = str(row.get("slot_id") or "").strip()
        canonical_id = str(row.get("canonical_id") or "").strip()
        source_path = str(row.get("source_path") or "").strip()
        if not slot_id or not canonical_id or not source_path:
            raise FastRenderError(
                f"EDL item {index} requires slot_id, canonical_id, and source_path"
            )
        if "\n" in source_path or "\r" in source_path:
            raise FastRenderError(f"EDL item {index} source_path contains a newline")
        if canonical_id in seen_canonical:
            raise FastRenderError(f"duplicate canonical_id in EDL: {canonical_id}")
        seen_canonical.add(canonical_id)
        source_start = _nonnegative_finite(
            row.get("source_start"), f"EDL item {index} source_start"
        )
        source_end = _positive_finite(
            row.get("source_end"), f"EDL item {index} source_end"
        )
        target_duration = _positive_finite(
            row.get("target_duration"), f"EDL item {index} target_duration"
        )
        crop_margins = _normalize_crop_margins(
            row.get("crop_margins"), item_index=index
        )
        if source_end <= source_start:
            raise FastRenderError(f"EDL item {index} source range is empty")
        if source_end - source_start + frame < target_duration:
            raise FastRenderError(
                f"EDL item {index} source range is shorter than target_duration"
            )
        normalized.append(
            FastEDLItem(
                slot_id=slot_id,
                canonical_id=canonical_id,
                source_path=str(Path(source_path).expanduser().resolve()),
                source_start=source_start,
                source_end=source_end,
                target_duration=target_duration,
                crop_margins=crop_margins,
            )
        )
    return normalized


def validate_language_pair(
    ru_edl: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    en_edl: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    *,
    source_start_tolerance: float = 1e-6,
) -> tuple[list[FastEDLItem], list[FastEDLItem]]:
    """Prove RU and EN retain one ordered visual identity.

    Language-specific ``slot_id``, ``target_duration`` and ``source_end`` may
    differ.  The ordered canonical IDs, donor paths, and source starts may not.
    """

    ru = normalize_edl(ru_edl)
    en = normalize_edl(en_edl)
    if len(ru) != len(en):
        raise FastRenderError("RU and EN EDLs have different shot counts")
    tolerance = _nonnegative_finite(
        source_start_tolerance, "source_start_tolerance"
    )
    for index, (ru_item, en_item) in enumerate(zip(ru, en, strict=True)):
        if ru_item.canonical_id != en_item.canonical_id:
            raise FastRenderError(
                f"RU/EN canonical sequence differs at item {index}: "
                f"{ru_item.canonical_id!r} != {en_item.canonical_id!r}"
            )
        if ru_item.source_path != en_item.source_path:
            raise FastRenderError(
                f"RU/EN donor source differs for {ru_item.canonical_id}"
            )
        if abs(ru_item.source_start - en_item.source_start) > tolerance:
            raise FastRenderError(
                f"RU/EN source_start differs for {ru_item.canonical_id}"
            )
        if dict(ru_item.crop_margins) != dict(en_item.crop_margins):
            raise FastRenderError(
                f"RU/EN crop_margins differ for {ru_item.canonical_id}"
            )
    return ru, en


def edl_duration(edl: Sequence[FastEDLItem]) -> float:
    if not edl:
        raise FastRenderError("EDL must not be empty")
    return math.fsum(item.target_duration for item in edl)


def _ffconcat_quote(path: str | Path) -> str:
    absolute = Path(path).expanduser().resolve().as_posix()
    return "'" + absolute.replace("'", "'\\''") + "'"


def build_ffconcat(edl: Sequence[FastEDLItem]) -> str:
    """Build one concat-demuxer EDL with in/out points and target durations."""

    if not edl:
        raise FastRenderError("EDL must not be empty")
    lines = ["ffconcat version 1.0"]
    for item in edl:
        # Extra selection headroom is not allowed to shift following shots.
        # Exact selected decode may provide a source_end a frame beyond the
        # language duration, so cap the demuxer outpoint here.
        effective_end = min(
            item.source_end, item.source_start + item.target_duration
        )
        lines.extend(
            (
                f"file {_ffconcat_quote(item.source_path)}",
                f"inpoint {item.source_start:.9f}",
                f"outpoint {effective_end:.9f}",
                f"duration {item.target_duration:.9f}",
            )
        )
    return "\n".join(lines) + "\n"


def _fsync_parent(path: Path) -> None:
    try:
        fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    partial.unlink(missing_ok=True)
    with partial.open("w", encoding="utf-8") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    os.replace(partial, path)
    _fsync_parent(path)


def write_ffconcat(
    edl: Sequence[FastEDLItem], destination: str | Path
) -> Path:
    destination_path = Path(destination).expanduser().resolve()
    _atomic_write_text(destination_path, build_ffconcat(edl))
    return destination_path


def build_video_render_command(
    edl: Sequence[FastEDLItem],
    output_path: str | Path,
    *,
    duration: float | None = None,
    config: FastRenderConfig | None = None,
) -> list[str]:
    """Build one mixed-input filter graph and a video-only NVENC output.

    A concat-demuxer cannot safely join the heterogeneous donor set: codec
    parameters and time bases may change between files without producing a
    process error.  Every selected shot is therefore an independent seeked
    input, normalized before the concat filter.  The fixed input-count and
    argv-size guards keep this bounded while allowing the ~244-shot target.
    """

    cfg = config or FastRenderConfig()
    cfg.validate()
    rows = list(edl)
    if not rows or not all(isinstance(row, FastEDLItem) for row in rows):
        raise FastRenderError("video render requires normalized FastEDLItem rows")
    if len(rows) > MAX_RENDER_INPUTS:
        raise FastRenderError(
            f"video render has {len(rows)} inputs; maximum is {MAX_RENDER_INPUTS}"
        )
    planned_duration = edl_duration(rows)
    exact_duration = (
        planned_duration
        if duration is None
        else _positive_finite(duration, "video duration")
    )
    if abs(exact_duration - planned_duration) > 1e-6:
        raise FastRenderError("video duration disagrees with EDL target durations")
    crop = float(cfg.edge_crop_percent)
    crop_filter = (
        f"crop=trunc(iw*(1-2*{crop:.6f})/2)*2:"
        f"trunc(ih*(1-2*{crop:.6f})/2)*2,"
        if crop > 0
        else ""
    )
    argv = [
        str(cfg.ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
    ]
    for row in rows:
        # Decode the exact checker's complete clean source window, not merely
        # the language target.  Mixed donor seek/time-base rounding can yield
        # one fewer post-fps frame when input ``-t`` equals target_duration.
        # The downstream frame trim consumes only the telescoping target span;
        # unused clean headroom is discarded, never cloned or held.
        source_duration = row.source_end - row.source_start
        argv.extend(
            [
                "-ss",
                f"{row.source_start:.9f}",
                "-t",
                f"{source_duration:.9f}",
                "-threads",
                "1",
                "-i",
                row.source_path,
            ]
        )

    filters: list[str] = []
    elapsed = 0.0
    for index, row in enumerate(rows):
        next_elapsed = elapsed + row.target_duration
        start_frame = int(round(elapsed * FAST_FPS_VALUE))
        end_frame = int(round(next_elapsed * FAST_FPS_VALUE))
        frame_count = end_frame - start_frame
        if frame_count <= 0:
            raise FastRenderError(
                f"EDL item {row.canonical_id} is shorter than one output frame"
            )
        margins = _normalize_crop_margins(row.crop_margins, item_index=index)
        retained_width = 1.0 - margins["left"] - margins["right"]
        retained_height = 1.0 - margins["top"] - margins["bottom"]
        overlay_crop_filter = ""
        if any(margins[key] > 0 for key in _CROP_MARGIN_KEYS):
            overlay_crop_filter = (
                f"crop=trunc(iw*{retained_width:.9f}/2)*2:"
                f"trunc(ih*{retained_height:.9f}/2)*2:"
                f"trunc(iw*{margins['left']:.9f}/2)*2:"
                f"trunc(ih*{margins['top']:.9f}/2)*2,"
            )
        filters.append(
            "".join(
                (
                    f"[{index}:v:0]",
                    crop_filter,
                    overlay_crop_filter,
                    "scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,",
                    "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,",
                    "setsar=1,",
                    f"fps={FAST_FPS},",
                    f"trim=end_frame={frame_count},",
                    "format=yuv420p,",
                    "setpts=PTS-STARTPTS",
                    f"[v{index}]",
                )
            )
        )
        elapsed = next_elapsed
    concat_inputs = "".join(f"[v{index}]" for index in range(len(rows)))
    filters.append(f"{concat_inputs}concat=n={len(rows)}:v=1:a=0[outv]")
    filter_complex = ";".join(filters)
    argv.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            "[outv]",
            "-an",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-fps_mode",
            "cfr",
            "-r",
            FAST_FPS,
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p5",
            "-tune",
            "hq",
            "-rc",
            "vbr",
            "-cq",
            "19",
            "-b:v",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-t",
            f"{exact_duration:.9f}",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(Path(output_path).expanduser().resolve()),
        ]
    )
    command_characters = sum(len(str(token)) + 1 for token in argv)
    if command_characters > MAX_COMMAND_CHARACTERS:
        raise FastRenderError(
            f"video render command is {command_characters} characters; "
            f"maximum is {MAX_COMMAND_CHARACTERS}"
        )
    return argv


def build_narration_mux_command(
    video_path: str | Path,
    narration_path: str | Path,
    output_path: str | Path,
    *,
    duration: float,
    ambience_path: str | Path | None = None,
    ambience_gain_db: float = -30.0,
    config: FastRenderConfig | None = None,
) -> list[str]:
    """Build an exact-duration AAC narration mux with video stream-copy.

    Optional ambience is looped at input, normalized to stereo/48 kHz, mixed
    quietly under narration, and bounded by the narration timeline.  Loudness
    normalization is deliberately last in the audio graph.
    """

    cfg = config or FastRenderConfig()
    cfg.validate()
    exact_duration = _positive_finite(duration, "mux duration")
    narration_filter = (
        "[1:a:0]"
        "aresample=48000,"
        "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
        "apad,"
        f"atrim=duration={exact_duration:.9f},"
        "asetpts=N/SR/TB"
    )
    inputs = [
        str(cfg.ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(Path(video_path).expanduser().resolve()),
        "-i",
        str(Path(narration_path).expanduser().resolve()),
    ]
    if ambience_path is None:
        audio_filter = (
            narration_filter
            + ",loudnorm=I=-16:TP=-1:LRA=11[aout]"
        )
    else:
        try:
            gain = float(ambience_gain_db)
        except (TypeError, ValueError) as exc:
            raise FastRenderError("ambience_gain_db must be finite and in [-60, 0]") from exc
        if isinstance(ambience_gain_db, bool) or not math.isfinite(gain) or not -60 <= gain <= 0:
            raise FastRenderError("ambience_gain_db must be finite and in [-60, 0]")
        inputs.extend(
            [
                "-stream_loop",
                "-1",
                "-i",
                str(Path(ambience_path).expanduser().resolve()),
            ]
        )
        audio_filter = (
            narration_filter
            + "[narr];"
            + "[2:a:0]aresample=48000,"
            + "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            + f"volume={gain:.3f}dB,"
            + f"atrim=duration={exact_duration:.9f},"
            + "asetpts=N/SR/TB[ambience];"
            + "[narr][ambience]"
            + "amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            + "loudnorm=I=-16:TP=-1:LRA=11[aout]"
        )
    return inputs + [
        "-filter_complex",
        audio_filter,
        "-map",
        "0:v:0",
        "-map",
        "[aout]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        str(cfg.audio_bitrate),
        "-t",
        f"{exact_duration:.9f}",
        "-shortest",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(Path(output_path).expanduser().resolve()),
    ]


def donor_audio_is_excluded(argv: Sequence[str]) -> bool:
    """Return whether a donor-render command explicitly emits video only."""

    command = [str(token) for token in argv]
    maps = [
        command[index + 1]
        for index, token in enumerate(command[:-1])
        if token == "-map"
    ]
    filter_text = " ".join(
        command[index + 1]
        for index, token in enumerate(command[:-1])
        if token == "-filter_complex"
    )
    return (
        "-an" in command
        and maps == ["[outv]"]
        and not any(":a" in mapping or mapping == "[aout]" for mapping in maps)
        and not re.search(r"\[\d+:a(?::\d+)?\]", filter_text)
    )


def run_command(
    argv: Sequence[str],
    *,
    deadline: Deadline | DeadlineLike | float | None,
    timeout_cap: float,
) -> subprocess.CompletedProcess[str]:
    """Execute without a shell and consume only the shared remaining budget."""

    timeout = deadline_timeout(deadline, cap_seconds=timeout_cap)
    try:
        process = subprocess.run(
            [str(token) for token in argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise DeadlineExceeded(
            f"command exceeded remaining fast-build budget ({timeout:.3f}s)"
        ) from exc
    if process.returncode != 0:
        detail = "\n".join((process.stderr or "").strip().splitlines()[-20:])
        raise FastRenderError(
            f"command failed with exit code {process.returncode}: {detail}"
        )
    return process


def _partial_path(final_path: Path) -> Path:
    return final_path.with_name(final_path.name + ".partial")


def _publish_partial(partial: Path, final: Path) -> None:
    if not partial.is_file() or partial.stat().st_size <= 0:
        raise FastRenderError(f"FFmpeg did not create a non-empty output: {partial}")
    with partial.open("rb") as source:
        os.fsync(source.fileno())
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(partial, final)
    _fsync_parent(final)


def _run_atomic_media_command(
    argv: Sequence[str],
    *,
    final_path: Path,
    deadline: Deadline | DeadlineLike | float | None,
    timeout_cap: float,
) -> subprocess.CompletedProcess[str]:
    partial = _partial_path(final_path)
    if Path(str(argv[-1])).resolve() != partial.resolve():
        raise FastRenderError("atomic command must target final_path + '.partial'")
    final_path.parent.mkdir(parents=True, exist_ok=True)
    partial.unlink(missing_ok=True)
    process = run_command(argv, deadline=deadline, timeout_cap=timeout_cap)
    _publish_partial(partial, final_path)
    return process


def _acquire_nvenc(
    deadline: Deadline | DeadlineLike | float | None,
    *,
    timeout_cap: float,
) -> None:
    timeout = deadline_timeout(deadline, cap_seconds=timeout_cap)
    if not _NVENC_SLOTS.acquire(timeout=timeout):
        raise DeadlineExceeded("no NVENC slot became available before the deadline")


def render_language(
    language: str,
    edl: Sequence[Mapping[str, Any]] | Mapping[str, Any] | Sequence[FastEDLItem],
    narration_path: str | Path,
    final_path: str | Path,
    *,
    working_dir: str | Path | None = None,
    ambience_path: str | Path | None = None,
    ambience_gain_db: float = -30.0,
    deadline: Deadline | DeadlineLike | float | None = None,
    config: FastRenderConfig | None = None,
) -> dict[str, Any]:
    """Render one language EDL and atomically publish its narrated master."""

    cfg = config or FastRenderConfig()
    cfg.validate()
    language_value = str(language).strip().lower()
    if not language_value or not _LANGUAGE_RE.fullmatch(language_value):
        raise FastRenderError("language must contain only letters, digits, '_' or '-'")
    rows = (
        list(edl)
        if isinstance(edl, Sequence)
        and not isinstance(edl, (str, bytes))
        and all(isinstance(item, FastEDLItem) for item in edl)
        else normalize_edl(edl)  # type: ignore[arg-type]
    )
    if not rows:
        raise FastRenderError("EDL must not be empty")
    output = Path(final_path).expanduser().resolve()
    narration = Path(narration_path).expanduser().resolve()
    work = (
        Path(working_dir).expanduser().resolve()
        if working_dir is not None
        else output.parent / ".fast-render"
    )
    work.mkdir(parents=True, exist_ok=True)
    stem = f"{output.stem}.{language_value}"
    concat_path = work / f"{stem}.ffconcat"
    video_path = work / f"{stem}.video.mp4"
    duration = edl_duration(rows)
    started = time.monotonic()
    timings: dict[str, float] = {}

    stage_started = time.monotonic()
    write_ffconcat(rows, concat_path)
    timings["write_edl_seconds"] = time.monotonic() - stage_started

    render_partial = _partial_path(video_path)
    render_argv = build_video_render_command(
        rows,
        render_partial,
        duration=duration,
        config=cfg,
    )
    if not donor_audio_is_excluded(render_argv):
        raise FastRenderError("donor render command could map donor audio")
    stage_started = time.monotonic()
    _acquire_nvenc(deadline, timeout_cap=cfg.render_timeout_seconds)
    try:
        _run_atomic_media_command(
            render_argv,
            final_path=video_path,
            deadline=deadline,
            timeout_cap=cfg.render_timeout_seconds,
        )
    finally:
        _NVENC_SLOTS.release()
    timings["video_render_seconds"] = time.monotonic() - stage_started

    mux_partial = _partial_path(output)
    mux_argv = build_narration_mux_command(
        video_path,
        narration,
        mux_partial,
        duration=duration,
        ambience_path=ambience_path,
        ambience_gain_db=ambience_gain_db,
        config=cfg,
    )
    stage_started = time.monotonic()
    _run_atomic_media_command(
        mux_argv,
        final_path=output,
        deadline=deadline,
        timeout_cap=cfg.mux_timeout_seconds,
    )
    timings["narration_mux_seconds"] = time.monotonic() - stage_started
    timings["total_seconds"] = time.monotonic() - started

    if not cfg.keep_intermediates:
        video_path.unlink(missing_ok=True)
        concat_path.unlink(missing_ok=True)

    return {
        "language": language_value,
        "output_path": str(output),
        "shot_count": len(rows),
        "duration": duration,
        "timings": timings,
        "commands": {
            "video_render": render_argv,
            "narration_mux": mux_argv,
        },
    }


def render_language_pair(
    ru_edl: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    en_edl: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    ru_narration: str | Path,
    en_narration: str | Path,
    ru_final: str | Path,
    en_final: str | Path,
    *,
    working_dir: str | Path | None = None,
    ambience_path: str | Path | None = None,
    ambience_gain_db: float = -30.0,
    max_workers: int = MAX_NVENC_JOBS,
    deadline: Deadline | DeadlineLike | float | None = None,
    config: FastRenderConfig | None = None,
) -> dict[str, Any]:
    """Render one bilingual pair under a deterministic memory policy.

    Small EDLs retain the two-language NVENC fast path.  Above the measured
    safe shot threshold, RU is completed before EN starts so the two large
    filter graphs cannot coexist in memory.  Both modes use the same commands,
    quality settings, outputs, and shared deadline.
    """

    started = time.monotonic()
    if isinstance(max_workers, bool) or int(max_workers) <= 0:
        raise FastRenderError("max_workers must be positive")
    if Path(ru_final).expanduser().resolve() == Path(en_final).expanduser().resolve():
        raise FastRenderError("RU and EN final paths must differ")
    ru_rows, en_rows = validate_language_pair(ru_edl, en_edl)
    shared_deadline = deadline if deadline is not None else Deadline.after(3_600.0)
    requested_workers = int(max_workers)
    worker_cap = min(requested_workers, MAX_NVENC_JOBS)
    parallel_allowed = (
        len(ru_rows) <= MAX_PARALLEL_LANGUAGE_SHOTS and worker_cap >= 2
    )
    execution_mode = "parallel" if parallel_allowed else "sequential"
    effective_workers = 2 if parallel_allowed else 1
    jobs = {
        "ru": (ru_rows, ru_narration, ru_final),
        "en": (en_rows, en_narration, en_final),
    }
    results: dict[str, dict[str, Any]] = {}
    if execution_mode == "sequential":
        # Do not pre-submit EN: if RU fails or consumes the shared deadline,
        # starting a second large FFmpeg graph would only waste memory/work.
        for language in ("ru", "en"):
            rows, narration, final = jobs[language]
            results[language] = render_language(
                language,
                rows,
                narration,
                final,
                working_dir=working_dir,
                ambience_path=ambience_path,
                ambience_gain_db=ambience_gain_db,
                deadline=shared_deadline,
                config=config,
            )
    else:
        with ThreadPoolExecutor(max_workers=effective_workers) as executor:
            futures = {
                executor.submit(
                    render_language,
                    language,
                    rows,
                    narration,
                    final,
                    working_dir=working_dir,
                    ambience_path=ambience_path,
                    ambience_gain_db=ambience_gain_db,
                    deadline=shared_deadline,
                    config=config,
                ): language
                for language, (rows, narration, final) in jobs.items()
            }
            try:
                for future in as_completed(futures):
                    language = futures[future]
                    results[language] = future.result()
            except Exception:
                for future in futures:
                    future.cancel()
                raise
    return {
        "status": "completed",
        "shot_sequence": [item.canonical_id for item in ru_rows],
        "execution_mode": execution_mode,
        "execution": {
            "mode": execution_mode,
            "shot_count": len(ru_rows),
            "parallel_shot_limit": MAX_PARALLEL_LANGUAGE_SHOTS,
            "requested_max_workers": requested_workers,
            "effective_workers": effective_workers,
        },
        "timings": {"total_seconds": time.monotonic() - started},
        "languages": {language: results[language] for language in ("ru", "en")},
    }


__all__ = [
    "Deadline",
    "DeadlineExceeded",
    "FAST_FPS",
    "FAST_FPS_VALUE",
    "FastEDLItem",
    "FastRenderConfig",
    "FastRenderError",
    "MAX_COMMAND_CHARACTERS",
    "MAX_NVENC_JOBS",
    "MAX_PARALLEL_LANGUAGE_SHOTS",
    "MAX_RENDER_INPUTS",
    "build_ffconcat",
    "build_narration_mux_command",
    "build_video_render_command",
    "deadline_timeout",
    "donor_audio_is_excluded",
    "edl_duration",
    "normalize_edl",
    "render_language",
    "render_language_pair",
    "run_command",
    "validate_language_pair",
    "write_ffconcat",
]
