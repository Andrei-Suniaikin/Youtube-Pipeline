"""Bounded cold-start sparse-frame sampling for the Pareto montage path.

The sampler deliberately produces only sparse JPEG frames.  Video is decoded
normally because keyframe-only decoder shortcuts can return corrupt or shifted
frames for open-GOP media.  It does not make proxy videos or contact sheets,
and it never relies on wall-clock timestamps in its portable metadata.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, Sequence

from .common import atomic_write_json


DEFAULT_INTERVAL_SECONDS = 6.0
FRAME_WIDTH = 512
FRAME_HEIGHT = 288
MAX_SAMPLER_PROCESSES = 4
SAMPLER_SCHEMA_VERSION = 3
SAMPLER_IDENTITY = "ffmpeg-normal-decode-showinfo-pts-v3"
_FRAME_NAME_RE = re.compile(r"^frame_(\d{6})\.jpg$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHOWINFO_RE = re.compile(
    r"^\[(?:Parsed_)?showinfo(?:_\d+)?\s+@[^\]]+\]\s+"
    r"n:\s*(?P<index>\d+)\b.*?\bpts_time:\s*(?P<pts>\S+)",
    re.MULTILINE,
)


class FastSamplerError(RuntimeError):
    """A donor could not be sampled into the portable sparse-frame format."""


class FastSamplerDeadlineExceeded(FastSamplerError):
    """The shared monotonic build deadline elapsed during donor sampling."""


@dataclass(frozen=True)
class DonorSampleRequest:
    """One local donor bound to its already-verified content identity."""

    donor_id: str
    source_path: str
    source_sha256: str


@dataclass(frozen=True)
class SampledFrame:
    """Portable metadata for one sparse frame.

    ``relative_path`` is always relative to the sampler output root.  It never
    leaks a host-specific absolute path into a cache or release manifest.
    """

    donor_id: str
    donor_sha256: str
    frame_index: int
    source_time: float
    relative_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DonorSampleResult:
    donor_id: str
    donor_sha256: str
    sample_interval_seconds: float
    frame_width: int
    frame_height: int
    manifest_relative_path: str
    frames: tuple[SampledFrame, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SAMPLER_SCHEMA_VERSION,
            "sampler": SAMPLER_IDENTITY,
            "donor_id": self.donor_id,
            "donor_sha256": self.donor_sha256,
            "sample_interval_seconds": self.sample_interval_seconds,
            "frame_width": self.frame_width,
            "frame_height": self.frame_height,
            "manifest_relative_path": self.manifest_relative_path,
            "frames": [frame.to_dict() for frame in self.frames],
        }


def _validate_request(request: DonorSampleRequest) -> Path:
    donor_id = str(request.donor_id).strip()
    if not donor_id:
        raise ValueError("donor_id must not be empty")
    digest = str(request.source_sha256).strip().casefold()
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
    raw_path = str(request.source_path)
    if "://" in raw_path:
        raise ValueError("fast sampler accepts local donor files only")
    try:
        source = Path(raw_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FastSamplerError(f"donor file is unavailable: {raw_path}") from exc
    if not source.is_file():
        raise FastSamplerError(f"donor path is not a file: {source}")
    return source


def _interval_token(interval_seconds: float) -> str:
    try:
        value = float(interval_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("interval_seconds must be numeric") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError("interval_seconds must be finite and positive")
    return format(value, ".12g")


def _output_component(
    request: DonorSampleRequest, interval_seconds: float
) -> str:
    donor_hash = hashlib.sha256(str(request.donor_id).encode("utf-8")).hexdigest()[:12]
    digest = str(request.source_sha256).strip().casefold()
    cache_policy = (
        f"{SAMPLER_IDENTITY}:{_interval_token(interval_seconds)}:"
        f"{FRAME_WIDTH}x{FRAME_HEIGHT}"
    )
    policy_hash = hashlib.sha256(cache_policy.encode("utf-8")).hexdigest()[:12]
    return f"{donor_hash}-{digest[:16]}-{policy_hash}"


def build_keyframe_command(
    source_path: str | Path,
    output_pattern: str | Path,
    *,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    ffmpeg_bin: str = "ffmpeg",
) -> list[str]:
    """Build the stable normal-decode, sparse-output FFmpeg command contract."""

    interval = _interval_token(interval_seconds)
    # The comma inside gte() is escaped for FFmpeg's filter parser.  argv is
    # passed directly (no shell quotes), so adding literal single quotes here
    # would incorrectly make them part of the expression.
    video_filter = (
        f"select=isnan(prev_selected_t)+gte(t-prev_selected_t\\,{interval}),"
        "showinfo,"
        f"scale={FRAME_WIDTH}:{FRAME_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={FRAME_WIDTH}:{FRAME_HEIGHT}:(ow-iw)/2:(oh-ih)/2,format=yuvj420p"
    )
    return [
        str(ffmpeg_bin),
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostdin",
        "-y",
        "-i",
        str(source_path),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        video_filter,
        "-fps_mode",
        "vfr",
        "-q:v",
        "3",
        "-start_number",
        "0",
        str(output_pattern),
    ]


def _remaining_timeout(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    try:
        remaining = float(deadline) - time.monotonic()
    except (TypeError, ValueError) as exc:
        raise ValueError("deadline must be an absolute monotonic timestamp") from exc
    if not math.isfinite(remaining) or remaining <= 0:
        raise FastSamplerDeadlineExceeded("cold keyframe sampling deadline exceeded")
    return remaining


def _validate_source_times(
    source_times: Sequence[Any], *, donor_id: str
) -> tuple[float, ...]:
    if not source_times:
        raise FastSamplerError(f"showinfo produced no timestamps for {donor_id}")
    normalized: list[float] = []
    previous = -math.inf
    for index, raw in enumerate(source_times):
        if isinstance(raw, bool):
            raise FastSamplerError(
                f"invalid showinfo pts_time for {donor_id} at frame {index}"
            )
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise FastSamplerError(
                f"invalid showinfo pts_time for {donor_id} at frame {index}"
            ) from exc
        if not math.isfinite(value) or value < 0:
            raise FastSamplerError(
                f"invalid showinfo pts_time for {donor_id} at frame {index}"
            )
        if value <= previous:
            raise FastSamplerError(
                f"showinfo pts_time is not strictly increasing for {donor_id} "
                f"at frame {index}"
            )
        normalized_value = round(value, 9)
        if normalized_value <= previous:
            raise FastSamplerError(
                f"showinfo pts_time is not strictly increasing for {donor_id} "
                f"at frame {index}"
            )
        normalized.append(normalized_value)
        previous = normalized_value
    return tuple(normalized)


def parse_showinfo_timestamps(stderr: str, *, donor_id: str) -> tuple[float, ...]:
    """Parse donor-local actual selected PTS values from FFmpeg showinfo."""

    indexed: list[tuple[int, str]] = [
        (int(match.group("index")), match.group("pts"))
        for match in _SHOWINFO_RE.finditer(stderr or "")
    ]
    if not indexed:
        raise FastSamplerError(f"showinfo produced no timestamps for {donor_id}")
    for expected_index, (actual_index, _value) in enumerate(indexed):
        if actual_index != expected_index:
            raise FastSamplerError(
                f"non-contiguous showinfo frame index for {donor_id}: "
                f"expected {expected_index}, got {actual_index}"
            )
    return _validate_source_times(
        [value for _index, value in indexed], donor_id=donor_id
    )


def _relative_frame_rows(
    request: DonorSampleRequest,
    destination: Path,
    output_root: Path,
    source_times: Sequence[Any],
) -> tuple[SampledFrame, ...]:
    paths = sorted(destination.glob("frame_*.jpg"))
    if not paths:
        raise FastSamplerError(f"ffmpeg produced no keyframe samples for {request.donor_id}")
    timestamps = _validate_source_times(
        source_times, donor_id=str(request.donor_id)
    )
    if len(paths) != len(timestamps):
        raise FastSamplerError(
            f"showinfo/JPEG count mismatch for {request.donor_id}: "
            f"{len(timestamps)} timestamps, {len(paths)} JPEGs"
        )
    rows: list[SampledFrame] = []
    for expected_index, path in enumerate(paths):
        match = _FRAME_NAME_RE.fullmatch(path.name)
        if match is None or int(match.group(1)) != expected_index:
            raise FastSamplerError(
                f"non-contiguous sampler output for {request.donor_id}: {path.name}"
            )
        relative = path.relative_to(output_root).as_posix()
        rows.append(
            SampledFrame(
                donor_id=str(request.donor_id),
                donor_sha256=str(request.source_sha256).strip().casefold(),
                frame_index=expected_index,
                source_time=timestamps[expected_index],
                relative_path=relative,
            )
        )
    return tuple(rows)


def _result_from_manifest(
    request: DonorSampleRequest,
    manifest: Path,
    output_root: Path,
    interval_seconds: float,
) -> DonorSampleResult | None:
    if not manifest.is_file():
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    expected = {
        "schema_version": SAMPLER_SCHEMA_VERSION,
        "sampler": SAMPLER_IDENTITY,
        "donor_id": str(request.donor_id),
        "donor_sha256": str(request.source_sha256).strip().casefold(),
        "sample_interval_seconds": float(interval_seconds),
        "frame_width": FRAME_WIDTH,
        "frame_height": FRAME_HEIGHT,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        return None
    stored_frames = payload.get("frames")
    if not isinstance(stored_frames, list):
        return None
    try:
        source_times = [
            row["source_time"] if isinstance(row, dict) else None
            for row in stored_frames
        ]
        frames = _relative_frame_rows(
            request, manifest.parent, output_root, source_times
        )
    except (FastSamplerError, KeyError, TypeError):
        return None
    if stored_frames != [frame.to_dict() for frame in frames]:
        return None
    return DonorSampleResult(
        donor_id=str(request.donor_id),
        donor_sha256=str(request.source_sha256).strip().casefold(),
        sample_interval_seconds=float(interval_seconds),
        frame_width=FRAME_WIDTH,
        frame_height=FRAME_HEIGHT,
        manifest_relative_path=manifest.relative_to(output_root).as_posix(),
        frames=frames,
    )


def sample_donor_keyframes(
    request: DonorSampleRequest,
    output_dir: str | Path,
    *,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    deadline: float | None = None,
    ffmpeg_bin: str = "ffmpeg",
) -> DonorSampleResult:
    """Sample one donor, honoring a shared absolute monotonic deadline."""

    source = _validate_request(request)
    interval = float(_interval_token(interval_seconds))
    output_root = Path(output_dir).expanduser().resolve()
    destination = output_root / "frames" / _output_component(request, interval)
    manifest = destination / "manifest.json"
    _remaining_timeout(deadline)
    cached = _result_from_manifest(
        request, manifest, output_root, interval
    )
    if cached is not None:
        return cached
    if destination.exists():
        raise FastSamplerError(
            f"incomplete or incompatible sampler cache exists: {destination}"
        )
    destination.mkdir(parents=True, exist_ok=False)
    pattern = destination / "frame_%06d.jpg"
    command = build_keyframe_command(
        source,
        pattern,
        interval_seconds=interval,
        ffmpeg_bin=ffmpeg_bin,
    )
    try:
        timeout = _remaining_timeout(deadline)
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or "unknown ffmpeg error").strip()[-800:]
            raise FastSamplerError(
                f"ffmpeg sparse-frame sampling failed for {request.donor_id}: {detail}"
            )
        source_times = parse_showinfo_timestamps(
            result.stderr or "", donor_id=str(request.donor_id)
        )
        frames = _relative_frame_rows(
            request, destination, output_root, source_times
        )
        manifest_relative_path = manifest.relative_to(output_root).as_posix()
        sampled = DonorSampleResult(
            donor_id=str(request.donor_id),
            donor_sha256=str(request.source_sha256).strip().casefold(),
            sample_interval_seconds=interval,
            frame_width=FRAME_WIDTH,
            frame_height=FRAME_HEIGHT,
            manifest_relative_path=manifest_relative_path,
            frames=frames,
        )
        atomic_write_json(manifest, sampled.to_dict())
        return sampled
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(destination, ignore_errors=True)
        raise FastSamplerDeadlineExceeded(
            f"cold keyframe sampling deadline exceeded for {request.donor_id}"
        ) from exc
    except BaseException:
        # The destination is content-addressed sampler state, never user media.
        # A failed run must not masquerade as a reusable cache on resume.
        shutil.rmtree(destination, ignore_errors=True)
        raise


def sample_donors_keyframes(
    requests: Sequence[DonorSampleRequest],
    output_dir: str | Path,
    *,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    max_processes: int = MAX_SAMPLER_PROCESSES,
    deadline: float | None = None,
    ffmpeg_bin: str = "ffmpeg",
) -> tuple[DonorSampleResult, ...]:
    """Sample donors concurrently with no more than four FFmpeg processes.

    Results preserve input order even though individual donors run in parallel.
    Every subprocess receives the remaining time from the same absolute
    monotonic deadline.
    """

    ordered = tuple(requests)
    if not ordered:
        return ()
    try:
        requested_workers = int(max_processes)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_processes must be an integer") from exc
    if requested_workers <= 0:
        raise ValueError("max_processes must be positive")
    workers = min(MAX_SAMPLER_PROCESSES, requested_workers, len(ordered))
    _remaining_timeout(deadline)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                sample_donor_keyframes,
                request,
                output_dir,
                interval_seconds=interval_seconds,
                deadline=deadline,
                ffmpeg_bin=ffmpeg_bin,
            )
            for request in ordered
        ]
        return tuple(future.result() for future in futures)


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "FRAME_HEIGHT",
    "FRAME_WIDTH",
    "MAX_SAMPLER_PROCESSES",
    "SAMPLER_IDENTITY",
    "SAMPLER_SCHEMA_VERSION",
    "DonorSampleRequest",
    "DonorSampleResult",
    "FastSamplerDeadlineExceeded",
    "FastSamplerError",
    "SampledFrame",
    "build_keyframe_command",
    "parse_showinfo_timestamps",
    "sample_donor_keyframes",
    "sample_donors_keyframes",
]
