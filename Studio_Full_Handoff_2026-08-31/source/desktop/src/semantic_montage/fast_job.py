"""Isolated cold-start runner for the Okavango Pareto montage lane.

The runner intentionally does not import or mutate the strict production DAG,
its SQLite state, or paid-operation receipts.  It consumes the six already
prepared chapter projects, builds one bilingual visual identity, and publishes
two technical-QA-verified masters under a single monotonic deadline.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Mapping, Sequence

import numpy as np

from .common import atomic_write_json, canonical_hash, sha256_file
from .fast_pipeline import (
    Deadline,
    DeadlineExceeded,
    MAX_FALLBACKS,
    MAX_TOP_K,
    plan_bilingual_chapter,
    rank_diverse_top_k,
    select_lazy_candidates,
)
from .fast_qa import FastQAConfig, run_fast_qa_pair
from .fast_render import (
    FastRenderConfig,
    donor_audio_is_excluded,
    render_language_pair,
    run_command,
    validate_language_pair,
)
from .fast_sampler import DonorSampleRequest, sample_donors_keyframes
from .overlay_detector import analyze_temporal_overlays
from .project import ProjectError, load_project


FAST_JOB_SCHEMA_VERSION = 1
AMBIENCE_SHA256 = "49d4ebce0f02230efc09fa1d5b534f9e9510d033bd41fb3bae97145121d770df"
RU_FINAL_NAME = "OKAVANGO_RU_FULL.mp4"
EN_FINAL_NAME = "OKAVANGO_EN_FULL.mp4"
EXPECTED_CONFIGS = tuple(
    (language, chapter, f"project.{language}.ch{chapter}.json")
    for language in ("ru", "en")
    for chapter in (1, 2, 3)
)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_BLACK_RE = re.compile(
    r"black_start:(?P<start>-?\d+(?:\.\d+)?)\s+"
    r"black_end:(?P<end>-?\d+(?:\.\d+)?)\s+"
    r"black_duration:(?P<duration>\d+(?:\.\d+)?)"
)
_FREEZE_DURATION_RE = re.compile(r"freeze_duration:\s*(?P<duration>\d+(?:\.\d+)?)")
_FREEZE_START_RE = re.compile(r"freeze_start:\s*(?P<start>-?\d+(?:\.\d+)?)")
_CROP_EDGES = ("left", "right", "top", "bottom")
_ZERO_CROP_MARGINS = {edge: 0.0 for edge in _CROP_EDGES}


class FastJobError(RuntimeError):
    """The isolated fast-build contract could not be satisfied."""


@dataclass(frozen=True)
class FastJobConfig:
    input_root: Path
    donor_root: Path
    job_root: Path
    deadline_seconds: float = 3_600.0
    sample_interval: float = 6.0
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    chapters: tuple[int, ...] = (1, 2, 3)
    resume: bool = False
    dry_run: bool = False

    def validate(self) -> None:
        for name in ("deadline_seconds", "sample_interval"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not math.isfinite(float(value))
                or float(value) <= 0
            ):
                raise FastJobError(f"{name} must be a positive finite number")
        if not str(self.ffmpeg).strip() or not str(self.ffprobe).strip():
            raise FastJobError("ffmpeg and ffprobe commands must not be empty")
        if (
            not self.chapters
            or tuple(sorted(set(self.chapters))) != tuple(self.chapters)
            or any(isinstance(value, bool) or value not in (1, 2, 3) for value in self.chapters)
        ):
            raise FastJobError("chapters must be a sorted unique subset of 1,2,3")


class StageEventLog:
    """Durable, append-only operational events for one recognized job root."""

    def __init__(self, path: Path, *, clock: Callable[[], float]) -> None:
        self.path = path
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **data: Any) -> None:
        row = {
            "schema_version": FAST_JOB_SCHEMA_VERSION,
            "event": str(event),
            "monotonic_seconds": round(float(self.clock()), 6),
            "wall_time": datetime.now(UTC).isoformat(),
            **data,
        }
        encoded = (
            json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        ).encode("utf-8")
        descriptor = os.open(
            self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600
        )
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _resolved_file(root: Path, relative: str, label: str) -> Path:
    portable = PurePosixPath(str(relative).replace("\\", "/"))
    if portable.is_absolute() or ".." in portable.parts or not portable.parts:
        raise FastJobError(f"{label} is not a portable relative path")
    resolved = (root / Path(portable)).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise FastJobError(f"{label} escapes input root") from exc
    if not resolved.is_file():
        raise FastJobError(f"missing {label}: {resolved}")
    return resolved


def _optional_resolved_file(root: Path, relative: str, label: str) -> Path | None:
    portable = PurePosixPath(str(relative).replace("\\", "/"))
    if portable.is_absolute() or ".." in portable.parts or not portable.parts:
        raise FastJobError(f"{label} is not a portable relative path")
    resolved = (root / Path(portable)).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise FastJobError(f"{label} escapes input root") from exc
    return resolved if resolved.is_file() else None


def _guarded_sha256(
    path: Path,
    *,
    absolute_deadline: float,
    clock: Callable[[], float],
) -> str:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            if clock() >= absolute_deadline:
                raise FastJobError(f"hard deadline expired while hashing {path}")
            digest.update(chunk)
    after = path.stat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise FastJobError(f"input changed while hashing: {path}")
    return digest.hexdigest()


def _probe_video_duration(
    path: Path,
    *,
    ffprobe: str,
    timeout: float,
) -> float:
    try:
        process = subprocess.run(
            [
                str(ffprobe),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=max(0.001, timeout),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise FastJobError(f"ffprobe deadline expired for {path}") from exc
    if process.returncode != 0:
        raise FastJobError(
            f"ffprobe failed for {path}: {(process.stderr or 'unknown error')[-800:]}"
        )
    try:
        duration = float((process.stdout or "").strip())
    except ValueError as exc:
        raise FastJobError(f"ffprobe returned invalid duration for {path}") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise FastJobError(f"ffprobe returned non-positive duration for {path}")
    return duration


def load_fast_input_contract(
    input_root: str | Path,
    donor_root: str | Path,
    *,
    deadline: Deadline,
    absolute_deadline: float,
    ffprobe: str = "ffprobe",
    clock: Callable[[], float] = time.monotonic,
    hash_file: Callable[[Path], str] | None = None,
    probe_duration: Callable[[Path], float] | None = None,
    chapters: Sequence[int] = (1, 2, 3),
) -> dict[str, Any]:
    """Verify and map six configs to content-addressed local media."""

    source_root = Path(input_root).expanduser().resolve()
    videos_root = Path(donor_root).expanduser().resolve()
    if not source_root.is_dir() or not videos_root.is_dir():
        raise FastJobError("input_root and donor_root must be existing directories")

    selected_chapters = tuple(int(value) for value in chapters)
    if (
        not selected_chapters
        or tuple(sorted(set(selected_chapters))) != selected_chapters
        or any(value not in (1, 2, 3) for value in selected_chapters)
    ):
        raise FastJobError("chapters must be a sorted unique subset of 1,2,3")
    chapter_map: dict[str, dict[str, dict[str, Any]]] = {
        str(i): {} for i in selected_chapters
    }
    required_hashes: dict[Path, str] = {}
    donors_by_sha: dict[str, dict[str, Any]] = {}
    ambience_path: Path | None = None
    config_rows: list[dict[str, Any]] = []

    def require_hash(path: Path, expected: str, label: str) -> None:
        normalized = str(expected).strip().lower()
        if not _HEX64.fullmatch(normalized):
            raise FastJobError(f"invalid SHA-256 for {label}")
        previous = required_hashes.setdefault(path, normalized)
        if previous != normalized:
            raise FastJobError(f"conflicting hashes configured for {path}")

    for language, chapter, name in EXPECTED_CONFIGS:
        if chapter not in selected_chapters:
            continue
        deadline.check()
        config_path = source_root / name
        if not config_path.is_file():
            raise FastJobError(f"missing project config: {config_path}")
        try:
            loaded = load_project(config_path)
            payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, ProjectError) as exc:
            raise FastJobError(f"invalid project config {config_path}: {exc}") from exc
        production = (payload.get("settings") or {}).get("production") or {}
        if not isinstance(production, Mapping):
            raise FastJobError(f"settings.production is invalid in {config_path}")
        asset_rows = {
            str(row.get("asset_id") or ""): dict(row)
            for row in payload.get("assets") or ()
            if isinstance(row, Mapping)
        }
        chapter_inputs: dict[str, Any] = {}
        for kind in ("script", "narration", "words"):
            asset_id = str(production.get(f"{kind}_asset_id") or kind)
            asset = asset_rows.get(asset_id)
            if asset is None or asset.get("mount"):
                raise FastJobError(f"missing local {kind} asset in {config_path}")
            path = _resolved_file(
                source_root,
                str(asset.get("relative_path") or ""),
                f"{language}.ch{chapter}.{kind}",
            )
            require_hash(path, str(asset.get("sha256") or ""), f"{kind} asset")
            chapter_inputs[kind] = str(path)
            chapter_inputs[f"{kind}_sha256"] = str(asset["sha256"]).lower()

        configured_ids = [str(value) for value in production.get("donor_asset_ids") or ()]
        if not configured_ids:
            raise FastJobError(f"no donor_asset_ids in {config_path}")
        chapter_donors: list[str] = []
        for asset_id in configured_ids:
            asset = asset_rows.get(asset_id)
            if asset is None or asset.get("mount") != "donors":
                raise FastJobError(f"invalid donor {asset_id!r} in {config_path}")
            digest = str(asset.get("sha256") or "").strip().lower()
            if not _HEX64.fullmatch(digest):
                raise FastJobError(f"invalid donor hash for {asset_id}")
            donor_path = (videos_root / f"{digest}.mp4").resolve()
            try:
                donor_path.relative_to(videos_root)
            except ValueError as exc:  # pragma: no cover - digest already constrains path
                raise FastJobError("donor path escaped donor_root") from exc
            if not donor_path.is_file():
                raise FastJobError(f"missing content-addressed donor: {donor_path}")
            require_hash(donor_path, digest, f"donor {asset_id}")
            existing = donors_by_sha.setdefault(
                digest,
                {
                    "source_id": asset_id,
                    "sha256": digest,
                    "path": str(donor_path),
                    "chapters": set(),
                },
            )
            if existing["source_id"] != asset_id:
                raise FastJobError(f"donor {digest} has conflicting asset IDs")
            existing["chapters"].add(chapter)
            chapter_donors.append(digest)

        for asset in asset_rows.values():
            if str(asset.get("sha256") or "").lower() != AMBIENCE_SHA256:
                continue
            if asset.get("mount"):
                raise FastJobError("River ambience must be a local catalog asset")
            candidate = _optional_resolved_file(
                source_root,
                str(asset.get("relative_path") or ""),
                "River ambience",
            )
            if candidate is None:
                continue
            require_hash(candidate, AMBIENCE_SHA256, "River ambience")
            if ambience_path is not None and candidate != ambience_path:
                raise FastJobError("River ambience resolves to multiple files")
            ambience_path = candidate

        config_sha = sha256_file(config_path)
        config_rows.append(
            {
                "language": language,
                "chapter": chapter,
                "path": str(config_path),
                "sha256": config_sha,
                "project_id": loaded.project_id,
            }
        )
        chapter_map[str(chapter)][language] = {
            **chapter_inputs,
            "config_path": str(config_path),
            "config_sha256": config_sha,
            "project_id": loaded.project_id,
            "donor_sha256s": chapter_donors,
        }

    for chapter in selected_chapters:
        ru = chapter_map[str(chapter)].get("ru")
        en = chapter_map[str(chapter)].get("en")
        if ru is None or en is None:
            raise FastJobError(f"chapter {chapter} lacks RU or EN input")
        if ru["donor_sha256s"] != en["donor_sha256s"]:
            raise FastJobError(f"chapter {chapter} RU/EN donor groups differ")

    def calculate(item: tuple[Path, str]) -> tuple[Path, str, str]:
        path, expected = item
        actual = (
            hash_file(path)
            if hash_file is not None
            else _guarded_sha256(
                path,
                absolute_deadline=absolute_deadline,
                clock=clock,
            )
        )
        return path, expected, str(actual).lower()

    with ThreadPoolExecutor(max_workers=min(4, len(required_hashes))) as executor:
        futures = [executor.submit(calculate, item) for item in required_hashes.items()]
        for future in as_completed(futures):
            path, expected, actual = future.result()
            if actual != expected:
                raise FastJobError(
                    f"SHA-256 mismatch for {path}: expected {expected}, got {actual}"
                )
    deadline.check()

    probe_timeout = max(0.001, deadline.remaining)

    def probe(row: dict[str, Any]) -> tuple[str, float]:
        path = Path(str(row["path"]))
        duration = (
            probe_duration(path)
            if probe_duration is not None
            else _probe_video_duration(
                path,
                ffprobe=ffprobe,
                timeout=probe_timeout,
            )
        )
        return str(row["sha256"]), float(duration)

    with ThreadPoolExecutor(max_workers=min(4, len(donors_by_sha))) as executor:
        futures = [executor.submit(probe, row) for row in donors_by_sha.values()]
        for future in as_completed(futures):
            digest, duration = future.result()
            if not math.isfinite(duration) or duration <= 0:
                raise FastJobError(f"invalid donor duration for {digest}")
            donors_by_sha[digest]["duration"] = duration
    deadline.check()

    donors = []
    for digest in sorted(donors_by_sha):
        row = dict(donors_by_sha[digest])
        row["chapters"] = sorted(row["chapters"])
        donors.append(row)
    contract = {
        "schema_version": FAST_JOB_SCHEMA_VERSION,
        "kind": "okavango_fast_input_contract",
        "input_root": str(source_root),
        "donor_root": str(videos_root),
        "selected_chapters": list(selected_chapters),
        "configs": sorted(config_rows, key=lambda row: (row["chapter"], row["language"])),
        "chapters": chapter_map,
        "donors": donors,
        "ambience_path": str(ambience_path) if ambience_path else None,
        "ambience_sha256": AMBIENCE_SHA256 if ambience_path else None,
    }
    contract["contract_sha256"] = canonical_hash(contract)
    return contract


def _prepare_job_root(config: FastJobConfig) -> tuple[Path, dict[str, Any]]:
    root = config.job_root.expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise FastJobError(f"job_root is not a directory: {root}")
    if not root.exists():
        root.mkdir(parents=True, exist_ok=False)
    identity_path = root / "fast_job.identity.json"
    entries = list(root.iterdir())
    expected = {
        "schema_version": FAST_JOB_SCHEMA_VERSION,
        "kind": "semantic_montage_okavango_fast_job",
        "input_root": str(config.input_root.expanduser().resolve()),
        "donor_root": str(config.donor_root.expanduser().resolve()),
        "chapters": list(config.chapters),
        "sample_interval": float(config.sample_interval),
    }
    if entries:
        if not config.resume:
            raise FastJobError(
                f"job_root is non-empty; pass --resume only for a recognized job: {root}"
            )
        if not identity_path.is_file():
            raise FastJobError("refusing unrecognized non-empty job_root")
        try:
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FastJobError("job identity is unreadable") from exc
        if any(identity.get(key) != value for key, value in expected.items()):
            raise FastJobError("resume roots do not match the existing job identity")
    else:
        identity = dict(expected)
        identity["status"] = "initialized"
        atomic_write_json(identity_path, identity)
    return root, identity


@contextmanager
def _measured_stage(
    deadline: Deadline,
    events: StageEventLog,
    name: str,
) -> Iterator[None]:
    events.emit("stage_start", stage=name, remaining_seconds=deadline.remaining)
    try:
        with deadline.stage(name):
            yield
    except BaseException as exc:
        events.emit(
            "stage_failed",
            stage=name,
            error_type=type(exc).__name__,
            error=str(exc),
            timings=deadline.timings(),
        )
        raise
    else:
        events.emit("stage_complete", stage=name, timings=deadline.timings())


def _atomic_save_vectors(path: Path, vectors: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as stream:
            np.save(stream, np.asarray(vectors, dtype=np.float32), allow_pickle=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _build_candidate_catalog(
    sampled: Sequence[Any],
    input_contract: Mapping[str, Any],
    sample_root: Path,
) -> list[dict[str, Any]]:
    donors = {str(row["source_id"]): dict(row) for row in input_contract["donors"]}
    output: list[dict[str, Any]] = []
    for result in sampled:
        donor = donors.get(str(result.donor_id))
        if donor is None:
            raise FastJobError(f"sampler returned unknown donor {result.donor_id}")
        donor_duration = float(donor["duration"])
        interval = float(result.sample_interval_seconds)
        for frame in result.frames:
            source_time = min(donor_duration, max(0.0, float(frame.source_time)))
            source_in = max(0.0, source_time - interval / 2.0)
            source_out = min(donor_duration, source_time + interval / 2.0)
            if source_out <= source_in:
                source_in = max(0.0, donor_duration - min(interval, donor_duration))
                source_out = donor_duration
            frame_path = (sample_root / frame.relative_path).resolve()
            if not frame_path.is_file():
                raise FastJobError(f"sample frame is missing: {frame_path}")
            output.append(
                {
                    "candidate_id": f"{donor['sha256']}:{int(frame.frame_index):06d}",
                    "source_id": donor["source_id"],
                    "source_asset_id": donor["source_id"],
                    "source_sha256": donor["sha256"],
                    "source_path": donor["path"],
                    "donor_duration": donor_duration,
                    "source_time": source_time,
                    "source_in": source_in,
                    "source_out": source_out,
                    "frame_index": int(frame.frame_index),
                    "frame_path": str(frame_path),
                }
            )
    if not output:
        raise FastJobError("sparse sampler produced an empty candidate catalog")
    return output


def _exact_segment(source_time: float, required: float, duration: float) -> tuple[float, float]:
    if required <= 0 or duration + 1e-6 < required:
        raise FastJobError("donor is shorter than the required visual segment")
    length = min(duration, required + 0.10)
    start = max(0.0, min(source_time - length / 2.0, duration - length))
    end = min(duration, start + length)
    if end - start + 1e-6 < required:
        start = max(0.0, duration - required)
        end = duration
    return start, end


def _safe_crop_margins(value: Any) -> dict[str, float] | None:
    """Normalize one detector crop or return ``None`` when it is unsafe.

    The detector runs after the fixed five-percent edge crop.  These margins
    are therefore fractions of that already-cropped frame and are intentionally
    kept separate for the renderer's second, asymmetric crop.
    """

    if not isinstance(value, Mapping):
        return None
    margins: dict[str, float] = {}
    for edge in _CROP_EDGES:
        raw = value.get(edge)
        if isinstance(raw, bool):
            return None
        try:
            margin = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(margin) or not 0.0 <= margin <= 0.25:
            return None
        margins[edge] = margin
    retained_area = (1.0 - margins["left"] - margins["right"]) * (
        1.0 - margins["top"] - margins["bottom"]
    )
    if retained_area + 1e-9 < 0.65:
        return None
    return margins


def _source_diverse_attempt_order(
    ranked: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    attempt_count: int = 1 + MAX_FALLBACKS,
) -> dict[str, list[dict[str, Any]]]:
    """Prefer distinct donors in the bounded exact-attempt prefix.

    Ranking still supplies at most five vector candidates.  This only reorders
    that fixed shortlist: first-seen distinct sources occupy the primary plus
    two fallback positions.  When a chapter has fewer distinct donors, the
    remaining positions use farthest-point sampling in source time so an exact
    attempt is not wasted on an adjacent frame from the same bad scene.
    """

    if attempt_count < 1:
        raise FastJobError("exact attempt_count must be positive")

    def source_id(row: Mapping[str, Any]) -> str:
        return str(
            row.get("source_id")
            or row.get("source_asset_id")
            or row.get("donor_id")
            or ""
        )

    def source_time(row: Mapping[str, Any]) -> float | None:
        raw = row.get("source_time")
        if raw is None and "source_in" in row and "source_out" in row:
            try:
                raw = (float(row["source_in"]) + float(row["source_out"])) / 2.0
            except (TypeError, ValueError):
                return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    def stable_score(row: Mapping[str, Any]) -> float:
        raw = row.get("selection_score", row.get("cosine_score", float("-inf")))
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return float("-inf")
        return value if math.isfinite(value) else float("-inf")

    output: dict[str, list[dict[str, Any]]] = {}
    for canonical_id, raw_rows in ranked.items():
        rows = [dict(row) for row in raw_rows]
        prefix: list[dict[str, Any]] = []
        selected_indexes: set[int] = set()
        seen_sources: set[str] = set()
        for index, row in enumerate(rows):
            identity = source_id(row)
            if not identity or identity in seen_sources:
                continue
            prefix.append(row)
            selected_indexes.add(index)
            seen_sources.add(identity)
            if len(prefix) >= attempt_count:
                break
        while len(prefix) < min(attempt_count, len(rows)):
            def fill_key(index: int) -> tuple[float, float, int]:
                row = rows[index]
                identity = source_id(row)
                at = source_time(row)
                same_source_times = [
                    selected_time
                    for selected in prefix
                    if source_id(selected) == identity
                    for selected_time in (source_time(selected),)
                    if selected_time is not None
                ]
                separation = (
                    min(abs(at - selected_time) for selected_time in same_source_times)
                    if at is not None and same_source_times
                    else 0.0
                )
                return separation, stable_score(row), -index

            remaining = [index for index in range(len(rows)) if index not in selected_indexes]
            selected_index = max(remaining, key=fill_key)
            prefix.append(rows[selected_index])
            selected_indexes.add(selected_index)
        output[str(canonical_id)] = prefix + [
            row for index, row in enumerate(rows) if index not in selected_indexes
        ]
    return output


class ExactMediaChecker:
    """Decode only shortlisted ranges; semantic rejects do not hide tool errors."""

    def __init__(
        self,
        work_root: Path,
        *,
        ffmpeg: str,
        deadline: Deadline,
    ) -> None:
        self.work_root = work_root
        self.ffmpeg = ffmpeg
        self.deadline = deadline
        self.work_root.mkdir(parents=True, exist_ok=True)

    def __call__(
        self, slot: Mapping[str, Any], candidate: Mapping[str, Any]
    ) -> dict[str, Any]:
        self.deadline.check()
        required = float(slot.get("required_visual_duration") or 0.0)
        donor_duration = float(candidate.get("donor_duration") or 0.0)
        start, end = _exact_segment(
            float(candidate.get("source_time") or 0.0), required, donor_duration
        )
        duration = end - start
        token = hashlib.sha256(
            f"{slot['canonical_id']}|{candidate['candidate_id']}|{start:.6f}|{end:.6f}".encode()
        ).hexdigest()[:16]
        with tempfile.TemporaryDirectory(prefix=f"{token}-", dir=self.work_root) as raw:
            temporary = Path(raw)
            pattern = temporary / "frame_%03d.jpg"
            sample_count = 12
            sample_fps = sample_count / duration
            filters = (
                "crop=trunc(iw*0.90/2)*2:trunc(ih*0.90/2)*2:"
                "trunc(iw*0.05/2)*2:trunc(ih*0.05/2)*2,"
                "blackdetect=d=0.300:pix_th=0.10:pic_th=0.98,"
                "freezedetect=n=-50dB:d=1.000,"
                f"fps={sample_fps:.9f},"
                "scale=640:-2:flags=bilinear,format=yuvj420p"
            )
            command = [
                str(self.ffmpeg),
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "info",
                "-y",
                "-ss",
                f"{start:.9f}",
                "-t",
                f"{duration:.9f}",
                "-i",
                str(Path(str(candidate["source_path"])).resolve()),
                "-map",
                "0:v:0",
                "-an",
                "-sn",
                "-dn",
                "-vf",
                filters,
                "-q:v",
                "3",
                "-fps_mode",
                "vfr",
                str(pattern),
            ]
            process = run_command(command, deadline=self.deadline, timeout_cap=120.0)
            diagnostics = process.stderr or ""
            black = [
                {
                    "start": float(match.group("start")),
                    "end": float(match.group("end")),
                    "duration": float(match.group("duration")),
                }
                for match in _BLACK_RE.finditer(diagnostics)
                if float(match.group("duration")) + 1e-9 >= 0.30
            ]
            freeze_durations = [
                float(match.group("duration"))
                for match in _FREEZE_DURATION_RE.finditer(diagnostics)
            ]
            freeze_starts = [
                float(match.group("start")) for match in _FREEZE_START_RE.finditer(diagnostics)
            ]
            trailing_freeze = (
                bool(freeze_starts)
                and len(freeze_starts) > len(freeze_durations)
                and duration - freeze_starts[-1] >= 1.0
            )
            if black:
                return {
                    "accepted": False,
                    "reason": "black_interval_at_least_0_3s",
                    "black_intervals": black,
                }
            if any(value + 1e-9 >= 1.0 for value in freeze_durations) or trailing_freeze:
                return {
                    "accepted": False,
                    "reason": "freeze_interval_at_least_1s",
                    "freeze_durations": freeze_durations,
                    "trailing_freeze": trailing_freeze,
                }
            try:
                import cv2
            except ImportError as exc:  # pragma: no cover - deployment preflight
                raise FastJobError("opencv-python is required for exact overlay checks") from exc
            frame_paths = sorted(temporary.glob("frame_*.jpg"))
            frames = [cv2.imread(str(path), cv2.IMREAD_COLOR) for path in frame_paths]
            if len(frames) < 2 or any(frame is None for frame in frames):
                raise FastJobError(
                    f"exact decode produced unreadable temporal frames for {slot['canonical_id']}"
                )
            overlay = analyze_temporal_overlays(frames, fps=sample_fps)
            action = str(overlay.get("action") or "")
            if action == "none":
                crop_margins = dict(_ZERO_CROP_MARGINS)
            elif action == "crop":
                normalized = _safe_crop_margins(overlay.get("crop_margins"))
                if normalized is None:
                    return {
                        "accepted": False,
                        "reason": "unsafe_overlay_crop_margins",
                        "overlay": overlay,
                    }
                crop_margins = normalized
            else:
                return {
                    "accepted": False,
                    "reason": f"overlay_action_after_5pct_crop:{action}",
                    "overlay": overlay,
                }
            retained_area = (1.0 - crop_margins["left"] - crop_margins["right"]) * (
                1.0 - crop_margins["top"] - crop_margins["bottom"]
            )
            return {
                "accepted": True,
                "reason": (
                    "exact_segment_safe_crop" if action == "crop" else "exact_segment_clean"
                ),
                "candidate": {"crop_margins": dict(crop_margins)},
                "source_id": str(candidate["source_id"]),
                "source_asset_id": str(candidate["source_id"]),
                "source_path": str(Path(str(candidate["source_path"])).resolve()),
                "source_in": start,
                "source_out": end,
                "black_intervals": [],
                "freeze_durations": freeze_durations,
                "overlay": {
                    "action": action,
                    "crop_margins": dict(crop_margins),
                    "retained_area": retained_area,
                    "observation_count": overlay.get("observation_count"),
                    "frame_count": overlay.get("frame_count"),
                },
                "uniform_edge_crop_percent": 0.05,
            }


def build_language_edls(
    plans: Sequence[Mapping[str, Any]],
    selection_by_canonical_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Bind the one exact selection sequence to both narration timelines."""

    output: dict[str, list[dict[str, Any]]] = {"ru": [], "en": []}
    for plan in plans:
        for language in ("ru", "en"):
            for slot in plan["language_slots"][language]:
                canonical_id = str(slot["canonical_id"])
                selected = selection_by_canonical_id.get(canonical_id)
                if selected is None:
                    raise FastJobError(f"missing exact selection for {canonical_id}")
                candidate = selected["candidate"]
                crop_margins = _safe_crop_margins(
                    candidate.get("crop_margins", _ZERO_CROP_MARGINS)
                )
                if crop_margins is None:
                    raise FastJobError(f"unsafe crop margins for {canonical_id}")
                source_start = float(candidate["source_in"])
                source_end = float(candidate["source_out"])
                duration = float(slot["duration"])
                if source_end - source_start + 1e-6 < duration:
                    raise FastJobError(f"exact range is too short for {canonical_id}")
                output[language].append(
                    {
                        "slot_id": f"{canonical_id}-{language}",
                        "canonical_id": canonical_id,
                        "source_path": str(candidate["source_path"]),
                        "source_start": source_start,
                        "source_end": source_end,
                        "target_duration": duration,
                        "crop_margins": dict(crop_margins),
                    }
                )
    validate_language_pair(output["ru"], output["en"])
    return output


def output_names(chapters: Sequence[int]) -> dict[str, str]:
    selected = tuple(int(value) for value in chapters)
    if selected == (1, 2, 3):
        return {"ru": RU_FINAL_NAME, "en": EN_FINAL_NAME}
    suffix = "_".join(f"CH{chapter}" for chapter in selected)
    return {
        "ru": f"OKAVANGO_RU_{suffix}_FAST.mp4",
        "en": f"OKAVANGO_EN_{suffix}_FAST.mp4",
    }


def concatenate_narrations(
    sources: Sequence[str | Path],
    output_path: str | Path,
    *,
    expected_duration: float,
    ffmpeg: str,
    deadline: Deadline,
) -> dict[str, Any]:
    """Decode selected MP3 chapters once into one exact-duration PCM timeline."""

    paths = [Path(path).expanduser().resolve() for path in sources]
    if not paths or any(not path.is_file() for path in paths):
        raise FastJobError("narration concatenation requires local chapter files")
    duration = float(expected_duration)
    if not math.isfinite(duration) or duration <= 0:
        raise FastJobError("expected narration duration must be positive")
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".partial")
    partial.unlink(missing_ok=True)
    inputs: list[str] = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y"]
    for path in paths:
        inputs.extend(["-i", str(path)])
    filters = []
    labels = []
    for index in range(len(paths)):
        label = f"a{index}"
        labels.append(f"[{label}]")
        filters.append(
            f"[{index}:a:0]aresample=48000,"
            "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"asetpts=N/SR/TB[{label}]"
        )
    filters.append(
        "".join(labels)
        + f"concat=n={len(paths)}:v=0:a=1,apad,"
        + f"atrim=duration={duration:.9f},asetpts=N/SR/TB[aout]"
    )
    command = inputs + [
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[aout]",
        "-vn",
        "-sn",
        "-dn",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(partial),
    ]
    try:
        run_command(command, deadline=deadline, timeout_cap=300.0)
        if not partial.is_file() or partial.stat().st_size == 0:
            raise FastJobError("FFmpeg produced no concatenated narration")
        os.replace(partial, output)
    finally:
        partial.unlink(missing_ok=True)
    return {
        "path": str(output),
        "duration": duration,
        "source_paths": [str(path) for path in paths],
        "command": command,
        "sha256": sha256_file(output),
        "bytes": output.stat().st_size,
    }


def scan_gpu_render_conflicts(
    *, nvidia_smi: str = "nvidia-smi"
) -> list[dict[str, Any]]:
    """Return only render-worker/FFmpeg processes currently registered on CUDA."""

    process = subprocess.run(
        [
            str(nvidia_smi),
            "--query-compute-apps=pid,process_name,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if process.returncode != 0:
        raise FastJobError(
            f"cannot inspect active CUDA jobs: {(process.stderr or 'nvidia-smi failed')[-500:]}"
        )
    conflicts: list[dict[str, Any]] = []
    for line in (process.stdout or "").splitlines():
        parts = [part.strip() for part in line.split(",", 2)]
        if len(parts) < 2 or not parts[0].isdigit() or int(parts[0]) == os.getpid():
            continue
        pid = int(parts[0])
        process_name = parts[1]
        command = ""
        proc_cmdline = Path(f"/proc/{pid}/cmdline")
        try:
            command = proc_cmdline.read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace"
            ).strip()
        except OSError:
            ps = subprocess.run(
                ["ps", "-p", str(pid), "-o", "args="],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            command = (ps.stdout or "").strip()
        normalized = f"{process_name} {command}".lower()
        if "/opt/video_agent/render_worker.py" not in normalized and "ffmpeg" not in normalized:
            continue
        conflicts.append(
            {
                "pid": pid,
                "process_name": process_name,
                "command": command,
                "used_gpu_memory_mib": parts[2] if len(parts) > 2 else None,
            }
        )
    return conflicts


def wait_for_gpu_render_idle(
    deadline: Deadline,
    events: StageEventLog,
    *,
    scanner: Callable[[], Sequence[Mapping[str, Any]]] = scan_gpu_render_conflicts,
    max_wait_seconds: float = 120.0,
    poll_seconds: float = 5.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    started = clock()
    next_heartbeat = started
    while True:
        deadline.check()
        conflicts = [dict(row) for row in scanner()]
        if not conflicts:
            return
        now = clock()
        if now >= next_heartbeat:
            events.emit(
                "gpu_busy_wait",
                conflict_count=len(conflicts),
                conflicts=conflicts,
                waited_seconds=round(now - started, 3),
                remaining_seconds=deadline.remaining,
            )
            next_heartbeat = now + 15.0
        allowed = min(float(max_wait_seconds) - (now - started), deadline.remaining)
        if allowed <= 0:
            if deadline.remaining <= 0:
                raise DeadlineExceeded(
                    "hard deadline exhausted while waiting for the GPU render lane",
                    elapsed_seconds=deadline.elapsed,
                    budget_seconds=deadline.total_seconds,
                    stage=deadline.active_stage,
                )
            raise FastJobError(
                "GPU is still used by render_worker.py/FFmpeg after the bounded wait"
            )
        sleep(min(float(poll_seconds), allowed))


def _write_timing_receipt(
    path: Path,
    *,
    status: str,
    deadline: Deadline,
    started_at: str,
    error: BaseException | None = None,
) -> dict[str, Any]:
    receipt = {
        "schema_version": FAST_JOB_SCHEMA_VERSION,
        "kind": "okavango_fast_timing_receipt",
        "status": status,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "timings": deadline.timings(),
        "error": (
            {"type": type(error).__name__, "message": str(error)} if error else None
        ),
    }
    atomic_write_json(path, receipt)
    return receipt


def _fsync_file_and_parent(path: Path) -> None:
    with path.open("rb") as source:
        os.fsync(source.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as source:
        os.fsync(source.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_qa_master_pair(
    staging_paths: Mapping[str, Path],
    final_root: Path,
    *,
    names: Mapping[str, str],
    expected_sha256s: Mapping[str, str],
    copy_file: Callable[[Path, Path], Any] = shutil.copy2,
    replace: Callable[[Path, Path], Any] = os.replace,
    fsync_file: Callable[[Path], None] = _fsync_file,
    fsync_directory: Callable[[Path], None] = _fsync_directory,
) -> dict[str, dict[str, Any]]:
    """Publish the verified RU/EN pair with one directory rename.

    Source masters are copied, rather than moved, into a unique sibling
    directory.  Thus a failed second copy, fsync, or final rename never exposes
    a half-pair and leaves both QA-staged originals available for recovery.
    ``final`` is a create-once release directory: an existing path is never
    overwritten, including an empty directory or a broken symlink.
    """

    languages = ("ru", "en")
    if set(staging_paths) != set(languages):
        raise FastJobError("pair publish requires exactly RU and EN staging masters")
    if set(names) != set(languages) or set(expected_sha256s) != set(languages):
        raise FastJobError("pair publish requires exactly RU and EN names/hashes")
    # Keep the final path itself unresolved so ``lexists`` can reject a broken
    # symlink instead of silently following it to a non-existent target.
    final_root = final_root.expanduser().absolute()
    if os.path.lexists(final_root):
        raise FastJobError(f"refusing to overwrite existing final directory: {final_root}")

    verified: dict[str, dict[str, Any]] = {}
    for language in languages:
        staging = Path(staging_paths[language]).resolve()
        name = str(names[language])
        if Path(name).name != name or not name:
            raise FastJobError(f"unsafe {language} final filename: {name!r}")
        if not staging.is_file():
            raise FastJobError(f"QA staging master is missing: {staging}")
        actual = sha256_file(staging)
        if actual != str(expected_sha256s[language]):
            raise FastJobError(f"QA staging master changed before publish: {staging}")
        verified[language] = {
            "staging_path": staging,
            "name": name,
            "sha256": actual,
            "bytes": staging.stat().st_size,
        }

    final_root.parent.mkdir(parents=True, exist_ok=True)
    partial_root = Path(
        tempfile.mkdtemp(prefix=f".{final_root.name}.partial.", dir=final_root.parent)
    )
    for language in languages:
        row = verified[language]
        destination = partial_root / str(row["name"])
        copy_file(Path(row["staging_path"]), destination)
        if not destination.is_file():
            raise FastJobError(f"pair staging copy is missing: {destination}")
        if sha256_file(destination) != row["sha256"]:
            raise FastJobError(f"pair staging copy hash mismatch: {destination}")
        fsync_file(destination)
    fsync_directory(partial_root)
    if os.path.lexists(final_root):
        raise FastJobError(f"final directory appeared during pair publish: {final_root}")
    replace(partial_root, final_root)
    fsync_directory(final_root.parent)

    return {
        language: {
            "path": str((final_root / str(row["name"])).resolve()),
            "sha256": str(row["sha256"]),
            "bytes": int(row["bytes"]),
            "backup_path": None,
            "qa_staging_path": str(row["staging_path"]),
        }
        for language, row in verified.items()
    }


def _publish_qa_master(
    staging_path: Path,
    final_path: Path,
    *,
    expected_sha256: str,
    backup_root: Path,
) -> dict[str, Any]:
    """Publish only a QA-bound staging master, backing up any old final."""

    if not staging_path.is_file():
        raise FastJobError(f"QA staging master is missing: {staging_path}")
    actual = sha256_file(staging_path)
    if actual != str(expected_sha256):
        raise FastJobError(f"QA staging master changed before publish: {staging_path}")
    final_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Path | None = None
    if final_path.exists():
        if not final_path.is_file():
            raise FastJobError(f"refusing to replace non-file final: {final_path}")
        old_sha = sha256_file(final_path)
        backup_root.mkdir(parents=True, exist_ok=True)
        backup_path = backup_root / f"{final_path.name}.pre-fast.{old_sha[:16]}.bak"
        if backup_path.exists():
            if not backup_path.is_file() or sha256_file(backup_path) != old_sha:
                raise FastJobError(f"existing final backup is inconsistent: {backup_path}")
        else:
            temporary = backup_path.with_name(f".{backup_path.name}.{os.getpid()}.tmp")
            try:
                shutil.copy2(final_path, temporary)
                if sha256_file(temporary) != old_sha:
                    raise FastJobError("final backup hash verification failed")
                os.replace(temporary, backup_path)
                _fsync_file_and_parent(backup_path)
            finally:
                temporary.unlink(missing_ok=True)
    _fsync_file_and_parent(staging_path)
    os.replace(staging_path, final_path)
    _fsync_file_and_parent(final_path)
    if sha256_file(final_path) != actual:
        raise FastJobError(f"published final hash mismatch: {final_path}")
    return {
        "path": str(final_path.resolve()),
        "sha256": actual,
        "bytes": final_path.stat().st_size,
        "backup_path": str(backup_path) if backup_path else None,
    }


def _completed_resume(
    root: Path, contract_sha256: str
) -> dict[str, Any] | None:
    path = root / "receipts" / "fast_release_receipt.json"
    if not path.is_file():
        return None
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        receipt.get("status") != "completed"
        or receipt.get("input_contract_sha256") != contract_sha256
        or receipt.get("qa_passed") is not True
        or receipt.get("donor_audio_excluded") is not True
        or receipt.get("same_ordered_canonical_sequence") is not True
    ):
        return None
    masters = receipt.get("masters") or {}
    if not isinstance(masters, Mapping) or set(masters) != {"ru", "en"}:
        return None
    for result in masters.values():
        media = Path(str(result.get("path") or "")).expanduser().resolve()
        try:
            media.relative_to((root / "final").resolve())
        except ValueError:
            return None
        if not media.is_file() or sha256_file(media) != result.get("sha256"):
            return None
    return receipt


def run_fast_job(
    config: FastJobConfig,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    hash_file: Callable[[Path], str] | None = None,
    probe_duration: Callable[[Path], float] | None = None,
    sampler: Callable[..., Sequence[Any]] = sample_donors_keyframes,
    image_embedder: Callable[..., Mapping[str, np.ndarray]] | None = None,
    text_embedder: Callable[..., np.ndarray] | None = None,
    chapter_planner: Callable[..., Mapping[str, Any]] = plan_bilingual_chapter,
    exact_checker_factory: Callable[[Path, str, Deadline], Callable[..., Any]] | None = None,
    narration_concatenator: Callable[..., Mapping[str, Any]] = concatenate_narrations,
    gpu_scanner: Callable[[], Sequence[Mapping[str, Any]]] = scan_gpu_render_conflicts,
    renderer: Callable[..., Mapping[str, Any]] = render_language_pair,
    qa_runner: Callable[..., Mapping[str, Any]] = run_fast_qa_pair,
) -> dict[str, Any]:
    """Run the complete isolated fast lane or its hash-verifying dry run."""

    config.validate()
    started_at = datetime.now(UTC).isoformat()
    started_monotonic = float(clock())
    absolute_deadline = started_monotonic + float(config.deadline_seconds)
    deadline = Deadline(float(config.deadline_seconds), clock=clock)
    root, identity = _prepare_job_root(config)
    events = StageEventLog(root / "events.jsonl", clock=clock)
    timing_path = root / "receipts" / "timings.json"
    events.emit("job_start", resume=config.resume, dry_run=config.dry_run)
    terminal_status = "failed"
    caught: BaseException | None = None
    try:
        with _measured_stage(deadline, events, "preflight"):
            for executable in (config.ffmpeg, config.ffprobe):
                if os.path.sep not in str(executable) and shutil.which(str(executable)) is None:
                    raise FastJobError(f"required executable is unavailable: {executable}")
            contract = load_fast_input_contract(
                config.input_root,
                config.donor_root,
                deadline=deadline,
                absolute_deadline=absolute_deadline,
                ffprobe=config.ffprobe,
                clock=clock,
                hash_file=hash_file,
                probe_duration=probe_duration,
                chapters=config.chapters,
            )
            atomic_write_json(root / "manifests" / "input_contract.json", contract)
            existing_contract = identity.get("input_contract_sha256")
            if existing_contract and existing_contract != contract["contract_sha256"]:
                raise FastJobError("resume input contract differs from the existing job")
            identity = {
                **identity,
                "status": "running",
                "input_contract_sha256": contract["contract_sha256"],
            }
            atomic_write_json(root / "fast_job.identity.json", identity)

        if config.resume:
            completed = _completed_resume(root, str(contract["contract_sha256"]))
            if completed is not None:
                terminal_status = "completed_resume"
                events.emit("job_complete_resume", receipt=str(root / "receipts" / "fast_release_receipt.json"))
                return completed

        with _measured_stage(deadline, events, "bilingual_plan"):
            plans = []
            for chapter in config.chapters:
                row = contract["chapters"][str(chapter)]
                plan = dict(
                    chapter_planner(
                        row["ru"]["words"],
                        row["en"]["words"],
                        chapter=chapter,
                        deadline=deadline,
                    )
                )
                plans.append(plan)
                atomic_write_json(root / "plans" / f"chapter_{chapter}.json", plan)
            canonical_slots = [slot for plan in plans for slot in plan["canonical_slots"]]
            canonical_ids = [str(row["canonical_id"]) for row in canonical_slots]
            if len(canonical_ids) != len(set(canonical_ids)) or not canonical_ids:
                raise FastJobError("canonical plan IDs are empty or duplicate")

        if config.dry_run:
            terminal_status = "dry_run_completed"
            receipt = {
                "schema_version": FAST_JOB_SCHEMA_VERSION,
                "kind": "okavango_fast_dry_run_receipt",
                "status": terminal_status,
                "input_contract_sha256": contract["contract_sha256"],
                "chapters": list(config.chapters),
                "canonical_slot_count": len(canonical_slots),
                "timings": deadline.timings(),
            }
            atomic_write_json(root / "receipts" / "dry_run.json", receipt)
            identity["status"] = terminal_status
            atomic_write_json(root / "fast_job.identity.json", identity)
            events.emit("job_dry_run_complete", canonical_slot_count=len(canonical_slots))
            return receipt

        with _measured_stage(deadline, events, "sparse_sampling"):
            requests = [
                DonorSampleRequest(
                    donor_id=str(row["source_id"]),
                    source_path=str(row["path"]),
                    source_sha256=str(row["sha256"]),
                )
                for row in contract["donors"]
            ]
            sample_root = root / "cache" / "sparse_samples"
            sampled = sampler(
                requests,
                sample_root,
                interval_seconds=float(config.sample_interval),
                max_processes=4,
                deadline=absolute_deadline,
                ffmpeg_bin=config.ffmpeg,
            )
            candidates = _build_candidate_catalog(sampled, contract, sample_root)

        with _measured_stage(deadline, events, "clip_embedding"):
            if image_embedder is None or text_embedder is None:
                from .clip_model import embed_images, embed_texts

                image_embedder = image_embedder or embed_images
                text_embedder = text_embedder or embed_texts
            frame_paths = [str(row["frame_path"]) for row in candidates]
            image_by_path = image_embedder(frame_paths, batch_size=128)
            missing = [path for path in frame_paths if path not in image_by_path]
            if missing:
                raise FastJobError(f"CLIP omitted {len(missing)} sampled frame(s)")
            candidate_vectors = np.stack(
                [np.asarray(image_by_path[path], dtype=np.float32) for path in frame_paths]
            )
            query_texts = [str(row["query_en"]) for row in canonical_slots]
            query_vectors = np.asarray(text_embedder(query_texts, batch_size=128), dtype=np.float32)
            if query_vectors.shape[0] != len(canonical_slots):
                raise FastJobError("CLIP text embedding count differs from canonical slots")
            atomic_write_json(root / "manifests" / "candidate_catalog.json", candidates)
            _atomic_save_vectors(root / "manifests" / "candidate_vectors.npy", candidate_vectors)
            _atomic_save_vectors(root / "manifests" / "query_vectors.npy", query_vectors)

        with _measured_stage(deadline, events, "vector_and_exact_selection"):
            query_offset = 0
            selection_rows: list[dict[str, Any]] = []
            selection_by_id: dict[str, dict[str, Any]] = {}
            ranked_all: dict[str, list[dict[str, Any]]] = {}
            checker = (
                exact_checker_factory(root / "work" / "exact", config.ffmpeg, deadline)
                if exact_checker_factory is not None
                else ExactMediaChecker(
                    root / "work" / "exact",
                    ffmpeg=config.ffmpeg,
                    deadline=deadline,
                )
            )
            for chapter, plan in zip(config.chapters, plans, strict=True):
                slots = list(plan["canonical_slots"])
                count = len(slots)
                allowed_sha = set(contract["chapters"][str(chapter)]["ru"]["donor_sha256s"])
                indexes = [
                    index
                    for index, candidate in enumerate(candidates)
                    if candidate["source_sha256"] in allowed_sha
                ]
                if not indexes:
                    raise FastJobError(f"chapter {chapter} has no embedded candidates")
                chapter_candidates = [candidates[index] for index in indexes]
                chapter_vectors = candidate_vectors[indexes]
                ranked = rank_diverse_top_k(
                    query_vectors[query_offset : query_offset + count],
                    chapter_vectors,
                    chapter_candidates,
                    canonical_ids=[str(row["canonical_id"]) for row in slots],
                    top_k=MAX_TOP_K,
                    deadline=deadline,
                )
                ranked = _source_diverse_attempt_order(ranked)
                exact = select_lazy_candidates(
                    slots,
                    ranked,
                    checker,
                    max_fallbacks=MAX_FALLBACKS,
                    deadline=deadline,
                )
                ranked_all.update(ranked)
                selection_rows.extend(exact["selections"])
                selection_by_id.update(exact["selection_by_canonical_id"])
                query_offset += count
            if query_offset != len(canonical_slots):
                raise FastJobError("query vector chapter slicing is inconsistent")
            selection_manifest = {
                "schema_version": FAST_JOB_SCHEMA_VERSION,
                "kind": "okavango_fast_selection",
                "top_k": MAX_TOP_K,
                "max_fallbacks": MAX_FALLBACKS,
                "canonical_ids": canonical_ids,
                "ranked_candidates": ranked_all,
                "selections": selection_rows,
                "selection_by_canonical_id": selection_by_id,
            }
            atomic_write_json(root / "manifests" / "selection.json", selection_manifest)
            edls = build_language_edls(plans, selection_by_id)
            atomic_write_json(root / "manifests" / "edl.ru.json", edls["ru"])
            atomic_write_json(root / "manifests" / "edl.en.json", edls["en"])

        expected_durations = {
            language: math.fsum(float(row["target_duration"]) for row in edls[language])
            for language in ("ru", "en")
        }
        with _measured_stage(deadline, events, "narration_concat"):
            narration_results = {}
            for language in ("ru", "en"):
                sources = [
                    contract["chapters"][str(chapter)][language]["narration"]
                    for chapter in config.chapters
                ]
                narration_results[language] = dict(
                    narration_concatenator(
                        sources,
                        root / "work" / f"narration.{language}.wav",
                        expected_duration=expected_durations[language],
                        ffmpeg=config.ffmpeg,
                        deadline=deadline,
                    )
                )
            atomic_write_json(root / "manifests" / "narrations.json", narration_results)

        with _measured_stage(deadline, events, "gpu_idle_gate"):
            wait_for_gpu_render_idle(
                deadline,
                events,
                scanner=gpu_scanner,
                clock=clock,
                sleep=sleep,
            )

        # Render and QA stay outside ``final``.  A master becomes visible as a
        # final only in the release stage after full technical QA succeeds.
        staging_root = root / "qa-staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        names = output_names(config.chapters)
        ru_staging = staging_root / names["ru"]
        en_staging = staging_root / names["en"]
        with _measured_stage(deadline, events, "parallel_nvenc_render"):
            render_result = dict(
                renderer(
                    edls["ru"],
                    edls["en"],
                    narration_results["ru"]["path"],
                    narration_results["en"]["path"],
                    ru_staging,
                    en_staging,
                    working_dir=root / "work" / "render",
                    ambience_path=contract.get("ambience_path"),
                    ambience_gain_db=-30.0,
                    max_workers=2,
                    deadline=deadline,
                    config=FastRenderConfig(ffmpeg=config.ffmpeg),
                )
            )
            languages = render_result.get("languages") or {}
            no_donor_audio = all(
                donor_audio_is_excluded(
                    ((languages.get(language) or {}).get("commands") or {}).get(
                        "video_render", ()
                    )
                )
                for language in ("ru", "en")
            )
            if not no_donor_audio:
                raise FastJobError("render result does not prove donor audio exclusion")
            render_result["donor_audio_excluded"] = True
            atomic_write_json(root / "manifests" / "render.json", render_result)

        with _measured_stage(deadline, events, "parallel_full_qa"):
            qa = dict(
                qa_runner(
                    {"ru": ru_staging, "en": en_staging},
                    expected_durations,
                    manifest_paths={
                        "ru": root / "qa" / "ru.fast-qa.json",
                        "en": root / "qa" / "en.fast-qa.json",
                    },
                    deadline=deadline,
                    config=FastQAConfig(ffmpeg=config.ffmpeg, ffprobe=config.ffprobe),
                    raise_on_failure=True,
                )
            )
            if not qa.get("passed"):
                raise FastJobError("fast QA pair did not pass")

        with _measured_stage(deadline, events, "release_receipt"):
            final_root = root / "final"
            ru_final = final_root / names["ru"]
            en_final = final_root / names["en"]
            published_pair = _publish_qa_master_pair(
                {"ru": ru_staging, "en": en_staging},
                final_root,
                names=names,
                expected_sha256s={
                    language: str(qa["languages"][language]["sha256"])
                    for language in ("ru", "en")
                },
            )
            masters = {}
            for language, path in (
                ("ru", ru_final),
                ("en", en_final),
            ):
                qa_row = qa["languages"][language]
                published = published_pair[language]
                qa_row["path"] = str(path.resolve())
                qa_manifest_path = Path(str(qa_row["manifest_path"]))
                if qa_manifest_path.is_file():
                    atomic_write_json(qa_manifest_path, qa_row)
                masters[language] = {
                    "path": published["path"],
                    "duration": qa_row["probe"]["actual_duration"],
                    "bytes": published["bytes"],
                    "sha256": published["sha256"],
                    "qa_manifest_path": qa_row["manifest_path"],
                    "previous_final_backup": published["backup_path"],
                }
            receipt = {
                "schema_version": FAST_JOB_SCHEMA_VERSION,
                "kind": "okavango_fast_release_receipt",
                "status": "completed",
                "chapters": list(config.chapters),
                "input_contract_sha256": contract["contract_sha256"],
                "selection_sha256": canonical_hash(selection_manifest),
                "edl_sha256": {
                    language: canonical_hash(edls[language]) for language in ("ru", "en")
                },
                "same_ordered_canonical_sequence": True,
                "donor_audio_excluded": True,
                "qa_passed": True,
                "masters": masters,
            }
        # The stage has now closed, so the release receipt cannot accidentally
        # persist a misleading ``release_receipt: running`` timing row.
        deadline.check()
        receipt["timings"] = deadline.timings()
        receipt["timing_receipt_path"] = str(timing_path.resolve())
        atomic_write_json(root / "receipts" / "fast_release_receipt.json", receipt)
        identity["status"] = "completed"
        atomic_write_json(root / "fast_job.identity.json", identity)
        terminal_status = "completed"
        events.emit("job_complete", masters=receipt["masters"], timings=deadline.timings())
        return receipt
    except BaseException as exc:
        caught = exc
        events.emit(
            "job_failed", error_type=type(exc).__name__, error=str(exc), timings=deadline.timings()
        )
        identity["status"] = "failed"
        identity["last_error"] = {"type": type(exc).__name__, "message": str(exc)}
        atomic_write_json(root / "fast_job.identity.json", identity)
        raise
    finally:
        _write_timing_receipt(
            timing_path,
            status=terminal_status,
            deadline=deadline,
            started_at=started_at,
            error=caught,
        )


__all__ = [
    "AMBIENCE_SHA256",
    "EN_FINAL_NAME",
    "ExactMediaChecker",
    "FAST_JOB_SCHEMA_VERSION",
    "FastJobConfig",
    "FastJobError",
    "RU_FINAL_NAME",
    "StageEventLog",
    "build_language_edls",
    "concatenate_narrations",
    "load_fast_input_contract",
    "output_names",
    "run_fast_job",
    "scan_gpu_render_conflicts",
    "wait_for_gpu_render_idle",
]
