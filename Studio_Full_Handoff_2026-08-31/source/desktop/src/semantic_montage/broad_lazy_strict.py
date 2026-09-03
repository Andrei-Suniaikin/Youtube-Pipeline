"""Production adapter between portable broad retrieval and strict execution.

This module owns no orchestration stage.  It supplies two dependency-injected
helpers used by :mod:`semantic_montage.broad_lazy_runtime`:

* warm, path-free donor overlay profiles built from exact-PTS separated
  contexts; and
* a callback matching ``run_lazy_strict_upgrade(candidate, *, window,
  render_frames)`` while preserving every broad identity field.

Absolute media paths exist only during a cold decode/callback invocation and
are never written to the profile cache.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence

from .broad_lazy import (
    BroadLazyError,
    build_donor_overlay_profile,
    resolve_strict_detector_policy,
    validate_broad_catalog,
    validate_donor_overlay_profile,
)
from .common import atomic_write_json, canonical_hash, sha256_file
from .overlay_detector import (
    PROFILE_DETECTOR_VERSION,
    OverlayDetectorError,
    build_separated_context_overlay_profile,
)
from .strict_candidate import (
    DEFAULT_MAX_DECODE_BYTES,
    DEFAULT_MAX_DECODE_FRAMES,
    STRICT_ANALYZER_VERSION,
    StrictCandidateError,
    analyze_strict_candidate,
    decode_exact_analysis_window,
    expand_strict_candidate_variants,
    probe_exact_frame_window,
)


RUNTIME_OVERLAY_CACHE_CONTRACT = "runtime_overlay_profile_cache_v1"
RUNTIME_STRICT_CALLBACK_CONTRACT = "broad_lazy_exact_strict_callback_v4"
_ABSOLUTE_PATH = re.compile(r"^(?:/|~/|file://|[A-Za-z]:[\\/])")
_PATH_FIELDS = {
    "path",
    "source_path",
    "absolute_path",
    "original_location",
    "original_locations",
}


class BroadLazyStrictError(ValueError):
    """Runtime overlay/strict integration cannot prove its lineage."""


def _finite(value: Any, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise BroadLazyStrictError(f"{name} must be numeric") from exc
    if not math.isfinite(parsed):
        raise BroadLazyStrictError(f"{name} must be finite")
    return parsed


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise BroadLazyStrictError(f"{name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BroadLazyStrictError(f"{name} must be a positive integer") from exc
    if parsed <= 0 or parsed != value:
        raise BroadLazyStrictError(f"{name} must be a positive integer")
    return parsed


def _clone(value: Any, name: str) -> Any:
    try:
        return json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (TypeError, ValueError) as exc:
        raise BroadLazyStrictError(f"{name} must be finite JSON data") from exc


def _assert_path_free(value: Any, name: str = "artifact") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in _PATH_FIELDS:
                raise BroadLazyStrictError(f"{name} contains path field {key!r}")
            _assert_path_free(item, f"{name}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _assert_path_free(item, f"{name}[{index}]")
    elif isinstance(value, str) and _ABSOLUTE_PATH.search(value.strip()):
        raise BroadLazyStrictError(f"{name} contains an absolute host path")


def _sealed(base: Mapping[str, Any]) -> dict[str, Any]:
    document = _clone(dict(base), "cache document")
    _assert_path_free(document, "cache document")
    return {**document, "content_hash": canonical_hash(document)}


def _validate_sealed(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise BroadLazyStrictError("overlay cache must contain an object")
    row = _clone(dict(value), "overlay cache")
    supplied = str(row.pop("content_hash", ""))
    if supplied != canonical_hash(row):
        raise BroadLazyStrictError("overlay cache content hash mismatch")
    if row.get("schema_version") != 1 or row.get("contract") != RUNTIME_OVERLAY_CACHE_CONTRACT:
        raise BroadLazyStrictError("overlay cache contract mismatch")
    document = {**row, "content_hash": supplied}
    _assert_path_free(document, "overlay cache")
    return document


def _context_plan(
    duration: float,
    *,
    contexts_per_donor: int,
    context_seconds: float,
) -> list[dict[str, Any]]:
    """Return non-overlapping, evenly separated source windows."""

    count = _positive_int(contexts_per_donor, "contexts_per_donor")
    seconds = _finite(context_seconds, "context_seconds")
    if duration <= 0 or seconds <= 0:
        raise BroadLazyStrictError("donor duration/context_seconds must be positive")
    # Short sources still receive independent evidence: shrink the analysis
    # window so adjacent windows retain a gap equal to one context duration.
    effective = min(seconds, duration / max(1, 2 * count - 1))
    if effective < 0.12:
        raise BroadLazyStrictError("donor is too short for separated overlay contexts")
    if count == 1:
        starts = [(duration - effective) / 2.0]
    else:
        step = (duration - effective) / (count - 1)
        starts = [index * step for index in range(count)]
    plan = []
    for index, start in enumerate(starts):
        end = min(duration, start + effective)
        plan.append(
            {
                "context_id": f"source-context-{index:03d}",
                "start": round(start, 9),
                "end": round(end, 9),
                "duration": round(end - start, 9),
            }
        )
    if any(
        left["end"] > right["start"] + 1e-9
        for left, right in zip(plan, plan[1:])
    ):
        raise BroadLazyStrictError("overlay context plan is not separated")
    return plan


def _expanded_detector_policy(
    source_asset_id: str,
    source_sha256: str,
    detector_policy: Mapping[str, Any] | None,
) -> dict[str, Any]:
    try:
        empty = build_donor_overlay_profile(
            source_asset_id,
            source_sha256,
            [],
            detector_policy=detector_policy,
        )
    except BroadLazyError as exc:
        raise BroadLazyStrictError(str(exc)) from exc
    return dict(empty["detector_policy"])


def _profile_request(
    catalog_hash: str,
    donor: Mapping[str, Any],
    context_plan: Sequence[Mapping[str, Any]],
    detector_policy: Mapping[str, Any],
    *,
    analysis_width: int,
    analysis_height: int,
    max_decode_bytes: int,
    max_decode_frames: int,
) -> dict[str, Any]:
    request = {
        "schema_version": 1,
        "contract": "runtime_overlay_profile_request_v1",
        "catalog_hash": str(catalog_hash),
        "source_asset_id": str(donor["source_asset_id"]),
        "source_sha256": str(donor["source_sha256"]),
        "context_plan": _clone(list(context_plan), "context plan"),
        "detector_policy": _clone(dict(detector_policy), "detector policy"),
        "decoder_contract": {
            "strict_analyzer_version": STRICT_ANALYZER_VERSION,
            "profile_detector_version": PROFILE_DETECTOR_VERSION,
            "addressing": "integer_pts_half_open",
            "analysis_width_bound": analysis_width,
            "analysis_height_bound": analysis_height,
            "max_decode_bytes": max_decode_bytes,
            "max_decode_frames": max_decode_frames,
            "fps_inference_used": False,
        },
    }
    _assert_path_free(request, "overlay request")
    return request


def _load_profile_cache(
    path: Path,
    *,
    request: Mapping[str, Any],
    request_key: str,
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BroadLazyStrictError(f"cannot load overlay profile cache: {exc}") from exc
    document = _validate_sealed(raw)
    if document.get("request_key") != request_key or document.get("request") != request:
        raise BroadLazyStrictError("overlay profile cache request identity mismatch")
    try:
        profile = validate_donor_overlay_profile(document.get("profile"))
    except BroadLazyError as exc:
        raise BroadLazyStrictError(str(exc)) from exc
    if (
        profile["source_asset_id"] != request["source_asset_id"]
        or profile["source_sha256"] != request["source_sha256"]
        or profile["detector_policy"] != request["detector_policy"]
        or document.get("profile_hash") != profile["content_hash"]
    ):
        raise BroadLazyStrictError("overlay profile cache lineage mismatch")
    return profile


def build_runtime_overlay_profiles(
    catalog: Mapping[str, Any],
    asset_resolver: Mapping[str, str | os.PathLike[str]],
    cache_dir: str | os.PathLike[str],
    *,
    contexts_per_donor: int = 5,
    context_seconds: float = 1.0,
    detector_policy: Mapping[str, Any] | None = None,
    analysis_width: int = 480,
    analysis_height: int = 270,
    max_decode_bytes: int = 16 * 1024 * 1024,
    max_decode_frames: int = 180,
    max_workers: int = 4,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> dict[str, dict[str, Any]]:
    """Build or load donor-wide overlay profiles with no persisted paths."""

    try:
        validated = validate_broad_catalog(catalog)
    except BroadLazyError as exc:
        raise BroadLazyStrictError(str(exc)) from exc
    if not isinstance(asset_resolver, Mapping):
        raise BroadLazyStrictError("asset_resolver must be a mapping")
    contexts_per_donor = _positive_int(contexts_per_donor, "contexts_per_donor")
    context_seconds = _finite(context_seconds, "context_seconds")
    analysis_width = _positive_int(analysis_width, "analysis_width")
    analysis_height = _positive_int(analysis_height, "analysis_height")
    max_decode_bytes = _positive_int(max_decode_bytes, "max_decode_bytes")
    max_decode_frames = _positive_int(max_decode_frames, "max_decode_frames")
    max_workers = _positive_int(max_workers, "max_workers")
    root = Path(cache_dir).expanduser().resolve() / "runtime_overlay_profiles"

    def build_one(donor: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        asset_id = str(donor["source_asset_id"])
        if asset_id not in asset_resolver:
            raise BroadLazyStrictError(f"asset resolver is missing {asset_id}")
        try:
            source = Path(asset_resolver[asset_id]).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise BroadLazyStrictError(f"resolved donor is unavailable: {asset_id}") from exc
        if not source.is_file():
            raise BroadLazyStrictError(f"resolved donor is not a file: {asset_id}")
        actual_sha = sha256_file(source)
        if actual_sha != donor["source_sha256"]:
            raise BroadLazyStrictError(f"resolved donor SHA-256 mismatch: {asset_id}")
        plan = _context_plan(
            float(donor["duration"]),
            contexts_per_donor=contexts_per_donor,
            context_seconds=context_seconds,
        )
        expanded_policy = _expanded_detector_policy(
            asset_id,
            str(donor["source_sha256"]),
            detector_policy,
        )
        request = _profile_request(
            str(validated["content_hash"]),
            donor,
            plan,
            expanded_policy,
            analysis_width=analysis_width,
            analysis_height=analysis_height,
            max_decode_bytes=max_decode_bytes,
            max_decode_frames=max_decode_frames,
        )
        request_key = "runtime-overlay-v1-" + canonical_hash(request)
        cache_path = root / f"overlay-profile.{request_key}.json"
        cached = _load_profile_cache(
            cache_path,
            request=request,
            request_key=request_key,
        )
        if cached is not None:
            return asset_id, cached

        contexts: list[dict[str, Any]] = []
        for row in plan:
            try:
                probe = probe_exact_frame_window(
                    source,
                    start=float(row["start"]),
                    end=float(row["end"]),
                    ffprobe=ffprobe,
                    max_window_seconds=max(2.0, context_seconds + 0.25),
                    max_frames=max_decode_frames,
                )
                decoded = decode_exact_analysis_window(
                    source,
                    probe,
                    width=analysis_width,
                    height=analysis_height,
                    ffmpeg=ffmpeg,
                    max_decode_bytes=max_decode_bytes,
                )
            except StrictCandidateError as exc:
                raise BroadLazyStrictError(
                    f"overlay context decode failed for {asset_id}/{row['context_id']}: {exc}"
                ) from exc
            contexts.append(
                {
                    "context_id": str(row["context_id"]),
                    "frames": decoded["frames"],
                }
            )
        try:
            profile = build_separated_context_overlay_profile(
                contexts,
                source_asset_id=asset_id,
                source_sha256=str(donor["source_sha256"]),
                detector_policy=expanded_policy,
            )
        except OverlayDetectorError as exc:
            raise BroadLazyStrictError(str(exc)) from exc
        document = _sealed(
            {
                "schema_version": 1,
                "contract": RUNTIME_OVERLAY_CACHE_CONTRACT,
                "request_key": request_key,
                "request": request,
                "profile_hash": profile["content_hash"],
                "profile": profile,
            }
        )
        atomic_write_json(cache_path, document)
        return asset_id, profile

    donors = sorted(validated["donors"], key=lambda row: str(row["source_asset_id"]))
    profiles: dict[str, dict[str, Any]] = {}
    with cf.ThreadPoolExecutor(max_workers=min(max_workers, len(donors))) as executor:
        futures = [executor.submit(build_one, donor) for donor in donors]
        for future in cf.as_completed(futures):
            asset_id, profile = future.result()
            profiles[asset_id] = profile
    return {asset_id: profiles[asset_id] for asset_id in sorted(profiles)}


def _window_alignment(
    candidate: Mapping[str, Any],
    window: Mapping[str, Any],
) -> tuple[float, float, float]:
    identity_fields = (
        "clip_id",
        "candidate_key",
        "source_asset_id",
        "source_sha256",
    )
    for field in identity_fields:
        if str(candidate.get(field) or "") != str(window.get(field) or ""):
            raise BroadLazyStrictError(f"strict window changed immutable {field}")
    source_in = _finite(candidate.get("source_in"), "candidate.source_in")
    source_out = _finite(candidate.get("source_out"), "candidate.source_out")
    candidate_window_in = _finite(
        window.get("candidate_source_in"), "window.candidate_source_in"
    )
    candidate_window_out = _finite(
        window.get("candidate_source_out"), "window.candidate_source_out"
    )
    requested_start = _finite(
        window.get("requested_window_start"), "window.requested_window_start"
    )
    requested_end = _finite(
        window.get("requested_window_end"), "window.requested_window_end"
    )
    if (
        abs(source_in - candidate_window_in) > 1e-6
        or abs(source_out - candidate_window_out) > 1e-6
        or not 0 <= requested_start <= source_in < source_out <= requested_end
    ):
        raise BroadLazyStrictError("strict window is not aligned to its broad candidate")
    left = source_in - requested_start
    right = requested_end - source_out
    search_seconds = max(left, right)
    if search_seconds > 30.0 + 1e-9:
        raise BroadLazyStrictError("strict window search bound exceeds 30 seconds")
    return requested_start, requested_end, search_seconds


def make_lazy_strict_callback(
    output_fps: float,
    overlay_profiles: Mapping[str, Mapping[str, Any]],
    cache_dir: str | os.PathLike[str],
    detector_policy: Mapping[str, Any] | None = None,
    *,
    model_contract_hash: str | None = None,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    max_decode_bytes: int = DEFAULT_MAX_DECODE_BYTES,
    max_decode_frames: int = DEFAULT_MAX_DECODE_FRAMES,
) -> Callable[..., dict[str, Any]]:
    """Return a strict callback accepted by ``run_lazy_strict_upgrade``."""

    output_fps = _finite(output_fps, "output_fps")
    if output_fps <= 0:
        raise BroadLazyStrictError("output_fps must be positive")
    if not isinstance(overlay_profiles, Mapping):
        raise BroadLazyStrictError("overlay_profiles must be a mapping")
    if detector_policy is not None and not isinstance(detector_policy, Mapping):
        raise BroadLazyStrictError("detector_policy must be a mapping")
    profiles: dict[str, dict[str, Any]] = {}
    for asset_id, raw in overlay_profiles.items():
        try:
            profile = validate_donor_overlay_profile(raw)
        except BroadLazyError as exc:
            raise BroadLazyStrictError(str(exc)) from exc
        if str(profile["source_asset_id"]) != str(asset_id):
            raise BroadLazyStrictError("overlay profile mapping key/asset id mismatch")
        try:
            resolve_strict_detector_policy(profile, detector_policy)
        except BroadLazyError as exc:
            raise BroadLazyStrictError(str(exc)) from exc
        profiles[str(asset_id)] = profile
    strict_cache = Path(cache_dir).expanduser().resolve() / "strict_candidate_exact"

    def strict_upgrade(
        candidate: Mapping[str, Any],
        *,
        window: Mapping[str, Any],
        render_frames: int,
    ) -> dict[str, Any]:
        if not isinstance(candidate, Mapping) or not isinstance(window, Mapping):
            raise BroadLazyStrictError("candidate/window must be mappings")
        render_frames_value = _positive_int(render_frames, "render_frames")
        requested_start, requested_end, search_seconds = _window_alignment(candidate, window)
        asset_id = str(candidate.get("source_asset_id") or "")
        profile = profiles.get(asset_id)
        if profile is None:
            raise BroadLazyStrictError(f"overlay profile is missing for {asset_id}")
        if profile["source_sha256"] != str(candidate.get("source_sha256") or ""):
            raise BroadLazyStrictError("overlay profile SHA-256/candidate mismatch")
        if str(window.get("overlay_profile_hash") or "") != profile["content_hash"]:
            raise BroadLazyStrictError("strict window overlay profile hash mismatch")
        identities = {
            field: candidate.get(field)
            for field in ("clip_id", "candidate_key", "source_asset_id", "source_sha256")
        }
        try:
            result = analyze_strict_candidate(
                candidate,
                render_frames=render_frames_value,
                output_fps=output_fps,
                search_seconds=search_seconds,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                donor_overlay_profile=profile,
                detector_policy=detector_policy,
                model_contract_hash=model_contract_hash,
                cache_directory=strict_cache,
                max_decode_bytes=max_decode_bytes,
                max_decode_frames=max_decode_frames,
            )
        except StrictCandidateError as exc:
            raise BroadLazyStrictError(str(exc)) from exc
        for field, expected in identities.items():
            if result.get(field) != expected:
                raise BroadLazyStrictError(f"strict callback changed immutable {field}")
        try:
            variants = expand_strict_candidate_variants(
                result,
                output_fps=output_fps,
                render_frames=render_frames_value,
            )
        except StrictCandidateError as exc:
            raise BroadLazyStrictError(str(exc)) from exc
        for variant in variants:
            for field, expected in identities.items():
                if variant.get(field) != expected:
                    raise BroadLazyStrictError(
                        f"strict variant changed immutable {field}"
                    )
            if not (
                requested_start - 1e-6
                <= float(variant["source_in"])
                < float(variant["source_out"])
                <= requested_end + 1e-6
            ):
                raise BroadLazyStrictError(
                    "strict variant escaped the requested source window"
                )
            analysis_window = variant.get("frame_contract", {}).get(
                "analysis_window", {}
            )
            if (
                abs(
                    float(analysis_window.get("requested_start", math.nan))
                    - requested_start
                )
                > 1e-6
                or abs(
                    float(analysis_window.get("requested_end", math.nan))
                    - requested_end
                )
                > 1e-6
            ):
                raise BroadLazyStrictError(
                    "strict analyzer did not execute the requested window"
                )
            if variant.get("fill_strategy") != "none" or abs(
                float(variant.get("retime_factor", 0)) - 1.0
            ) > 1e-9:
                raise BroadLazyStrictError("strict callback enabled fill or retiming")
            if any(
                bool(variant.get(field))
                for field in (
                    "allow_hold",
                    "allow_tpad",
                    "manual_selection",
                    "manual_crop",
                    "held_tail_seconds",
                )
            ):
                raise BroadLazyStrictError(
                    "strict callback enabled manual/fill behavior"
                )
            overlay = variant.get("overlay_analysis") or {}
            if overlay.get("status") != "resolved" or overlay.get("action") not in {
                "none",
                "crop",
            }:
                raise BroadLazyStrictError(
                    "strict callback returned unresolved overlay evidence"
                )
            if overlay.get("inpaint_markers"):
                raise BroadLazyStrictError(
                    "strict callback returned inpaint markers"
                )
            if model_contract_hash is not None and variant.get(
                "strict_analysis", {}
            ).get("strict_window_cache_key") != window.get("cache_key"):
                raise BroadLazyStrictError(
                    "strict callback cache key disagrees with broad plan"
                )
        # Keep the legacy primary mapping at the top level, enriched only with
        # the same exact-span metadata/key carried by every alternative.  The
        # reserved array includes that primary as row zero for direct callers;
        # broad_lazy_runtime deterministically de-duplicates it by range key.
        return {**variants[0], "strict_variants": variants}

    strict_upgrade.__name__ = "broad_lazy_exact_strict_upgrade"
    strict_upgrade.callback_contract = RUNTIME_STRICT_CALLBACK_CONTRACT  # type: ignore[attr-defined]
    return strict_upgrade


__all__ = [
    "BroadLazyStrictError",
    "RUNTIME_OVERLAY_CACHE_CONTRACT",
    "RUNTIME_STRICT_CALLBACK_CONTRACT",
    "build_runtime_overlay_profiles",
    "make_lazy_strict_callback",
]
