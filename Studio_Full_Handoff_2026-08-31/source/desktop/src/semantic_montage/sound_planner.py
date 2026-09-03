"""LLM-assisted mood and visual-only acoustic-scene planning.

Production v2 analyses eight exact final-clip frames plus a silent motion
preview, then expands every slot into continuous ambience, sustained presence,
and multiple visible event requests. Narration is context only and cannot prove
a species or action. The legacy sparse-SFX planner is retained solely for v1
manifest replay.
"""

from __future__ import annotations

import base64
import concurrent.futures as cf
import json
import math
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .acoustic_scene import (
    acoustic_evidence_cache_key,
    acoustic_evidence_contract,
    build_acoustic_scene,
    build_natural_lanes,
    compile_causal_acoustic_scenes,
    validate_acoustic_scenes,
)
from .common import canonical_hash, sha256_file
from .llm_client import chat as _chat
from .llm_client import extract_json
from .retrieval import make_contact_sheet
from .sound import SoundPlanError, make_music_segment


MOOD_PROMPT = """You are a documentary music editor. Divide the ordered narration slots into
5-8 contiguous music chapters. Chapters must cover every slot exactly once, normally last
25-90 seconds, and change only at a real narrative or emotional turn. Return JSON array only.
Each object: start_slot_id, end_slot_id (both inclusive), label_en, valence, arousal, tension
(numbers 0..1), bpm_min, bpm_max, instrumentation (array), transition (crossfade|build|release),
and prompt_en. prompt_en must request an instrumental cinematic documentary score with no
vocals and no lyrics. Keep one coherent sonic palette across all chapters.

SLOTS:
{slots}
"""


SFX_PROMPT = """You are a conservative documentary sound editor. Judge ONLY what is visibly
present in each labelled four-frame contact sheet from the exact final clip range. Narration is
context, not visual evidence. Return a JSON array with at most one object per clip and omit clips
that do not need a distinct effect. Never infer an animal from narration. Do not identify species
unless visually unambiguous. Prefer silence over a questionable or generic effect.

For an accepted clip return exactly: slot_id, emit, query_en, visible_evidence, source_kind,
action, confidence (0..1), offset_fraction (0.10..0.85), duration_hint (0.2..6.0), gain_db
(-24..-8). emit may be true only when a visible source/action can honestly produce the requested
sound. Use stock-search English such as "distant African wild dog yelp natural habitat" rather
than cinematic words. Return JSON array only.
"""


ACOUSTIC_SCENE_PROMPT = """You are a forensic documentary location-sound editor. Analyse ONLY
the exact final video evidence supplied for every slot: an eight-frame contact sheet and a
low-resolution motion preview. Narration text is context only and can never prove an animal,
species, action, weather, or sound source.

Return one JSON object per slot, in order, and never omit a slot. Each object must contain:
- slot_id and summary;
- environment: location_type, enclosure, weather, time_of_day, water, wind, perspective,
  confidence (0..1);
- visible_sources: zero or more objects with source_id, source_kind, family (or null), species
  (or null), action (or null), distance (extreme_close|close|medium|distant|very_distant),
  horizontal_position (far_left|left|center|right|far_right), confidence, audible_presence,
  body_scale (small|medium|large|massive|unknown), body_scale_evidence (or null),
  surface_contact (bare_ground|dry_grass|mud|none|rock|sand|unknown|vegetation|water|wet_grass),
  surface_contact_evidence (or null), start_fraction, end_fraction, visual_evidence,
  hard_negatives;
- events: zero or more visibly timed objects with event_id, source_id, source_kind, family,
  species, action, distance, horizontal_position, confidence, audible_event, offset_fraction,
  duration_fraction, body_scale, body_scale_evidence, surface_contact,
  surface_contact_evidence, visual_evidence, hard_negatives;
- hard_negatives: sounds/species/actions contradicted by the images.

Species is permitted only when visually unambiguous. Standing, looking, or merely being visible
does not prove a vocalisation: a vocal action requires a visibly calling/roaring animal with a
specific mouth, beak, jaw, or throat cue. Use one atomic action per source/event; never combine a
vocalisation with footsteps, splashing, rustling, or another sound. Continuous waterfall, rain,
or cascade sources are ambience, not duplicate foreground presence. An event is only a short,
localized visible contact. For walking/patrolling, report dry_grass contact only when repeated
foot/paw contact is actually visible, and report body scale only with explicit visual evidence;
otherwise use unknown. audible_* remains your raw visual judgement and may be false even when an
action is visible; a deterministic compiler applies the stricter causal and asset-compatibility
policy later. Multiple simultaneous atomic sources/events are allowed. Environment must always be
described because ambience covers every frame, but use low confidence rather than inventing detail.
No cinematic sound, music, narration, donor audio, or generic animal substitute. Return JSON array
only.
"""

ACOUSTIC_RAW_CONTRACT = "acoustic_scene_raw_batches"
ACOUSTIC_RAW_CONTRACT_VERSION = 1
ACOUSTIC_BATCH_REQUEST_CONTRACT = "acoustic_scene_batch_request"
ACOUSTIC_BATCH_REQUEST_CONTRACT_VERSION = 1
ACOUSTIC_BATCH_RAW_CONTRACT = "acoustic_scene_raw_batch"
ACOUSTIC_BATCH_RAW_CONTRACT_VERSION = 1
DEFAULT_ACOUSTIC_PLANNER_VERSION = "acoustic-scene-v2.2"


class SoundPlannerError(ValueError):
    """Raised when an LLM result violates the planner contract."""


def _slots(timeline: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = timeline.get("slots")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        raise SoundPlannerError("timeline.slots must be a non-empty list")
    result: list[dict[str, Any]] = []
    previous_end = 0.0
    for index, value in enumerate(raw):
        if not isinstance(value, Mapping):
            raise SoundPlannerError(f"timeline slot {index} must be an object")
        slot_id = str(value.get("slot_id") or value.get("beat_id") or value.get("id") or "")
        if not slot_id:
            raise SoundPlannerError(f"timeline slot {index} has no id")
        try:
            start = float(value["start"])
            end = float(value["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SoundPlannerError(f"slot {slot_id} has invalid timing") from exc
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise SoundPlannerError(f"slot {slot_id} has invalid timing")
        if index and abs(start - previous_end) > 0.051:
            raise SoundPlannerError(f"slot {slot_id} is not contiguous with previous slot")
        result.append({
            "slot_id": slot_id,
            "start": start,
            "end": end,
            "text": str(value.get("text") or value.get("narration_text") or "").strip(),
            "selected_clip": value.get("selected_clip"),
        })
        previous_end = end
    return result


def _unit(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SoundPlannerError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise SoundPlannerError(f"{name} must be in [0, 1]")
    return number


def _instrumental_prompt(row: Mapping[str, Any], instruments: list[str], bpm_min: int, bpm_max: int) -> str:
    supplied = str(row.get("prompt_en") or "").strip()
    base = (
        f"instrumental cinematic documentary score, "
        f"{row.get('label_en') or 'restrained wonder'} mood, "
        f"{bpm_min}-{bpm_max} BPM, {', '.join(instruments)}, "
        "coherent natural-history sonic palette"
    )
    # Keep useful model direction, but never let a generic supplied sentence
    # erase the segment-specific mood, tempo and instrumentation.
    if supplied and supplied.lower() not in base.lower():
        base += f"; additional direction: {supplied}"
    base += ", no vocals, no lyrics"
    return base[:1200]


def _mood_distance(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    return sum(abs(float(left[key]) - float(right[key])) for key in
               ("valence", "arousal", "tension"))


def _merge_music_segments(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_duration = float(left["end"]) - float(left["start"])
    right_duration = float(right["end"]) - float(right["start"])
    total = left_duration + right_duration
    mood_left, mood_right = left["mood"], right["mood"]
    instruments = list(dict.fromkeys(
        list(mood_left["instrumentation"]) + list(mood_right["instrumentation"])
    ))[:8]
    label_left, label_right = str(mood_left["label"]), str(mood_right["label"])
    label = label_left if label_left == label_right else f"{label_left} to {label_right}"
    merged = {
        **left,
        "end_slot_id": right["end_slot_id"],
        "end": right["end"],
        "_end_index": right["_end_index"],
        "mood": {
            "label": label[:120],
            "valence": (float(mood_left["valence"]) * left_duration
                        + float(mood_right["valence"]) * right_duration) / total,
            "arousal": (float(mood_left["arousal"]) * left_duration
                        + float(mood_right["arousal"]) * right_duration) / total,
            "tension": (float(mood_left["tension"]) * left_duration
                        + float(mood_right["tension"]) * right_duration) / total,
            "bpm_min": min(int(mood_left["bpm_min"]), int(mood_right["bpm_min"])),
            "bpm_max": max(int(mood_left["bpm_max"]), int(mood_right["bpm_max"])),
            "instrumentation": instruments,
        },
        "transition": right["transition"],
    }
    return merged


def _split_music_segment(segment: dict[str, Any], slots: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    start_index, end_index = segment["_start_index"], segment["_end_index"]
    if end_index <= start_index:
        raise SoundPlannerError("cannot split a one-slot music chapter")
    midpoint = (float(segment["start"]) + float(segment["end"])) / 2.0
    boundaries = range(start_index + 1, end_index + 1)
    split_index = min(boundaries, key=lambda index: abs(float(slots[index]["start"]) - midpoint))
    left = {
        **segment,
        "end_slot_id": slots[split_index - 1]["slot_id"],
        "end": slots[split_index - 1]["end"],
        "_end_index": split_index - 1,
    }
    right = {
        **segment,
        "start_slot_id": slots[split_index]["slot_id"],
        "start": slots[split_index]["start"],
        "_start_index": split_index,
    }
    return left, right


def _canonicalize_music_segments(
    segments: list[dict[str, Any]],
    slots: list[dict[str, Any]],
    *,
    enforce_chapter_bounds: bool,
) -> list[dict[str, Any]]:
    """Return only the canonical production-v2 music contract."""

    minimum_duration = min(
        float(segment["end"]) - float(segment["start"])
        for segment in segments
    )
    common_crossfade = (
        0.0 if len(segments) == 1 else min(3.0, minimum_duration / 2.0)
    )
    canonical: list[dict[str, Any]] = []
    canonical_timeline = {"slots": slots}
    for segment in segments:
        duration = float(segment["end"]) - float(segment["start"])
        if enforce_chapter_bounds and (duration < 20.0 or duration > 105.0):
            raise SoundPlannerError(
                f"music chapter duration is unsafe: {duration:.3f}s"
            )
        mood = segment["mood"]
        prompt = _instrumental_prompt(
            {"label_en": mood["label"]},
            list(mood["instrumentation"]),
            int(mood["bpm_min"]),
            int(mood["bpm_max"]),
        )
        try:
            canonical.append(
                make_music_segment(
                    canonical_timeline,
                    start_slot_id=str(segment["start_slot_id"]),
                    end_slot_id=str(segment["end_slot_id"]),
                    mood=mood,
                    prompt=prompt,
                    provider=str(segment.get("provider") or "suno_official"),
                    crossfade_seconds=common_crossfade,
                )
            )
        except (SoundPlanError, TypeError, ValueError) as exc:
            raise SoundPlannerError(
                f"cannot build canonical production-v2 music segment: {exc}"
            ) from exc
    return canonical


def _normalize_music_segments(
    segments: list[dict[str, Any]], slots: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Deterministically enforce a small number of listenable chapters."""

    total_duration = float(slots[-1]["end"]) - float(slots[0]["start"])
    if total_duration < 120.0:
        # Tiny unit/smoke timelines are allowed to have fewer zones.
        return _canonicalize_music_segments(
            segments, slots, enforce_chapter_bounds=False
        )

    # Split chapters that would make one musical idea drag for too long.
    while True:
        long_indexes = [index for index, row in enumerate(segments)
                        if float(row["end"]) - float(row["start"]) > 90.0]
        if not long_indexes:
            break
        index = max(long_indexes, key=lambda item: float(segments[item]["end"])
                    - float(segments[item]["start"]))
        left, right = _split_music_segment(segments[index], slots)
        segments[index:index + 1] = [left, right]

    # Merge very short chapters first, then merge the closest moods until the
    # product-level 5-8 chapter budget is met.
    while len(segments) > 8 or any(
        float(row["end"]) - float(row["start"]) < 25.0 for row in segments
    ):
        short = {index for index, row in enumerate(segments)
                 if float(row["end"]) - float(row["start"]) < 25.0}
        candidates: list[tuple[float, int]] = []
        for index in range(len(segments) - 1):
            if short and index not in short and index + 1 not in short and len(segments) <= 8:
                continue
            combined = float(segments[index + 1]["end"]) - float(segments[index]["start"])
            overflow = max(0.0, combined - 90.0)
            score = overflow * 10.0 + _mood_distance(
                segments[index]["mood"], segments[index + 1]["mood"]
            )
            candidates.append((score, index))
        if not candidates:
            raise SoundPlannerError("cannot normalize music chapter durations")
        _score, index = min(candidates)
        segments[index:index + 2] = [_merge_music_segments(
            segments[index], segments[index + 1]
        )]

    while len(segments) < 5:
        index = max(range(len(segments)), key=lambda item: float(segments[item]["end"])
                    - float(segments[item]["start"]))
        left, right = _split_music_segment(segments[index], slots)
        segments[index:index + 1] = [left, right]

    return _canonicalize_music_segments(
        segments, slots, enforce_chapter_bounds=True
    )


def plan_music_segments(
    timeline: Mapping[str, Any],
    *,
    model: str = "gpt-5-nano",
    chat_fn: Callable[..., str] = _chat,
) -> list[dict[str, Any]]:
    """Plan contiguous mood chapters and snap every boundary to a slot."""

    slots = _slots(timeline)
    compact = [{"slot_id": row["slot_id"], "start": row["start"],
                "end": row["end"], "text": row["text"]} for row in slots]
    answer = chat_fn(
        model,
        [{"role": "user", "content": MOOD_PROMPT.format(
            slots=json.dumps(compact, ensure_ascii=False))}],
        reasoning_effort="minimal",
        max_tokens=3500,
    )
    parsed = extract_json(answer)
    if not isinstance(parsed, list) or not parsed:
        raise SoundPlannerError("music planner must return a non-empty JSON array")
    by_id = {row["slot_id"]: index for index, row in enumerate(slots)}
    next_index = 0
    result: list[dict[str, Any]] = []
    for number, raw in enumerate(parsed, 1):
        if not isinstance(raw, Mapping):
            raise SoundPlannerError("music segment must be an object")
        start_id = str(raw.get("start_slot_id") or "")
        end_id = str(raw.get("end_slot_id") or "")
        if start_id not in by_id or end_id not in by_id:
            raise SoundPlannerError("music segment references an unknown slot")
        start_index, end_index = by_id[start_id], by_id[end_id]
        if start_index != next_index or end_index < start_index:
            raise SoundPlannerError("music segments must cover slots contiguously and in order")
        next_index = end_index + 1
        instruments = [str(item).strip() for item in (raw.get("instrumentation") or [])
                       if str(item).strip()][:8]
        if not instruments:
            instruments = ["warm strings", "soft percussion", "organic textures"]
        bpm_min = max(40, min(180, int(raw.get("bpm_min") or 70)))
        bpm_max = max(bpm_min, min(200, int(raw.get("bpm_max") or bpm_min + 15)))
        transition = str(raw.get("transition") or "crossfade").lower()
        if transition not in {"crossfade", "build", "release"}:
            transition = "crossfade"
        segment = {
            "segment_id": f"music_{number:03d}",
            "start_slot_id": start_id,
            "end_slot_id": end_id,
            "start": slots[start_index]["start"],
            "end": slots[end_index]["end"],
            "mood": {
                "label": str(raw.get("label_en") or "restrained wonder").strip()[:120],
                "valence": _unit(raw.get("valence"), "valence"),
                "arousal": _unit(raw.get("arousal"), "arousal"),
                "tension": _unit(raw.get("tension"), "tension"),
                "bpm_min": bpm_min,
                "bpm_max": bpm_max,
                "instrumentation": instruments,
            },
            "transition": transition,
            "crossfade_seconds": 3.0,
            "instrumental": True,
            "prompt": _instrumental_prompt(raw, instruments, bpm_min, bpm_max),
            "provider": "suno_official",
            "status": "needs_asset",
            "_start_index": start_index,
            "_end_index": end_index,
        }
        result.append(segment)
    if next_index != len(slots):
        raise SoundPlannerError("music segments do not cover the final timeline slots")
    return _normalize_music_segments(result, slots)


def make_selected_contact_sheets(
    timeline: Mapping[str, Any],
    output_dir: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    workers: int = 8,
) -> dict[str, str]:
    """Render four frames from each exact final ``source_in..source_out`` range."""

    slots = _slots(timeline)
    root = Path(output_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)

    def render(row: Mapping[str, Any]) -> tuple[str, str]:
        clip = row.get("selected_clip")
        if not isinstance(clip, Mapping):
            raise SoundPlannerError(f"slot {row['slot_id']} has no selected_clip")
        exact = dict(clip)
        if not exact.get("source_path"):
            raise SoundPlannerError(f"slot {row['slot_id']} selected clip has no source_path")
        target = root / f"{row['slot_id']}.jpg"
        return str(row["slot_id"]), make_contact_sheet(exact, target, ffmpeg=ffmpeg)

    result: dict[str, str] = {}
    with cf.ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        for slot_id, path in executor.map(render, slots):
            result[slot_id] = path
    return result


def _run_ffmpeg(command: list[str], target: Path) -> None:
    process = subprocess.run(command, capture_output=True, timeout=180)
    if process.returncode or not target.is_file() or target.stat().st_size < 512:
        target.unlink(missing_ok=True)
        detail = (process.stderr or b"ffmpeg acoustic evidence failed")[-1200:].decode(
            "utf-8", "replace"
        )
        raise SoundPlannerError(detail)


def _render_eight_frame_sheet(
    clip: Mapping[str, Any], target: Path, *, ffmpeg: str
) -> None:
    if target.is_file() and target.stat().st_size > 2048:
        return
    start = float(
        clip.get(
            "render_source_in",
            clip.get("safe_source_in", clip.get("safe_source_start", clip["source_in"])),
        )
    )
    end = float(
        clip.get(
            "render_source_out",
            clip.get("safe_source_out", clip.get("safe_source_end", clip["source_out"])),
        )
    )
    duration = end - start
    if duration <= 0:
        raise SoundPlannerError("selected clip duration must be positive")
    offsets = [duration * value for value in (0.05, 0.18, 0.31, 0.44, 0.56, 0.69, 0.82, 0.95)]
    branches = ["[0:v]setpts=PTS-STARTPTS,split=8" + "".join(f"[v{i}]" for i in range(8))]
    for index, offset in enumerate(offsets):
        branches.append(
            f"[v{index}]trim=start={offset:.6f}:duration=0.160000,"
            "setpts=PTS-STARTPTS,scale=400:225:force_original_aspect_ratio=decrease:flags=lanczos,"
            "crop=w='min(iw,400)':h='min(ih,225)',setsar=1,"
            f"pad=400:225:(ow-iw)/2:(oh-ih)/2[s{index}]"
        )
    branches.append(
        "[s0][s1][s2][s3][s4][s5][s6][s7]xstack=inputs=8:"
        "layout=0_0|w0_0|w0+w1_0|w0+w1+w2_0|0_h0|w0_h0|w0+w1_h0|w0+w1+w2_h0:fill=black[sheet]"
    )
    temporary = target.with_name(f".{target.stem}.{os.getpid()}.tmp.jpg")
    _run_ffmpeg(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.6f}",
            "-t",
            f"{duration:.6f}",
            "-i",
            str(clip["source_path"]),
            "-an",
            "-filter_complex",
            ";".join(branches),
            "-map",
            "[sheet]",
            "-frames:v",
            "1",
            "-q:v",
            "3",
            "-y",
            str(temporary),
        ],
        temporary,
    )
    os.replace(temporary, target)


def _render_motion_preview(
    clip: Mapping[str, Any], target: Path, *, ffmpeg: str
) -> None:
    if target.is_file() and target.stat().st_size > 2048:
        return
    start = float(
        clip.get(
            "render_source_in",
            clip.get("safe_source_in", clip.get("safe_source_start", clip["source_in"])),
        )
    )
    end = float(
        clip.get(
            "render_source_out",
            clip.get("safe_source_out", clip.get("safe_source_end", clip["source_out"])),
        )
    )
    duration = end - start
    if duration <= 0:
        raise SoundPlannerError("selected clip duration must be positive")
    temporary = target.with_name(f".{target.stem}.{os.getpid()}.tmp.gif")
    filters = (
        "fps=6,scale=320:-2:flags=lanczos,split[p0][p1];"
        "[p0]palettegen=max_colors=128:stats_mode=diff[palette];"
        "[p1][palette]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle[gif]"
    )
    _run_ffmpeg(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.6f}",
            "-t",
            f"{duration:.6f}",
            "-i",
            str(clip["source_path"]),
            "-an",
            "-filter_complex",
            filters,
            "-map",
            "[gif]",
            "-loop",
            "0",
            "-y",
            str(temporary),
        ],
        temporary,
    )
    os.replace(temporary, target)


def make_acoustic_evidence(
    timeline: Mapping[str, Any],
    output_dir: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    workers: int = 8,
) -> dict[str, dict[str, Any]]:
    """Materialise eight frames plus a silent motion preview for every exact clip."""

    slots = _slots(timeline)
    root = Path(output_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)

    def render(row: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        clip = row.get("selected_clip")
        if not isinstance(clip, Mapping):
            raise SoundPlannerError(f"slot {row['slot_id']} has no selected_clip")
        exact = dict(clip)
        source_path = exact.get("source_path") or exact.get("path")
        if not source_path:
            raise SoundPlannerError(f"slot {row['slot_id']} selected clip has no source_path")
        exact["source_path"] = str(source_path)
        exact_start = float(
            exact.get(
                "render_source_in",
                exact.get("safe_source_in", exact.get("safe_source_start", exact["source_in"])),
            )
        )
        exact_end = float(
            exact.get(
                "render_source_out",
                exact.get("safe_source_out", exact.get("safe_source_end", exact["source_out"])),
            )
        )
        source_contract = acoustic_evidence_contract(timeline, row)
        evidence_key = acoustic_evidence_cache_key(timeline, row)
        sheet = root / f"{row['slot_id']}.{evidence_key}.eight_frames.jpg"
        motion = root / f"{row['slot_id']}.{evidence_key}.motion.gif"
        _render_eight_frame_sheet(exact, sheet, ffmpeg=ffmpeg)
        _render_motion_preview(exact, motion, ffmpeg=ffmpeg)
        return str(row["slot_id"]), {
            "contact_sheet": str(sheet),
            "contact_sheet_sha256": sha256_file(sheet),
            "motion_preview_path": str(motion),
            "motion_preview_sha256": sha256_file(motion),
            "sampled_frames": 8,
            "motion_preview": True,
            "donor_audio_included": False,
            "source_asset_id": source_contract["source_asset_id"],
            "frame_time_contract": source_contract,
            "source_path": exact["source_path"],
            "source_in": exact_start,
            "source_out": exact_end,
            "evidence_key": evidence_key,
        }

    result: dict[str, dict[str, Any]] = {}
    with cf.ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        for slot_id, evidence in executor.map(render, slots):
            result[slot_id] = evidence
    return result


def _image_part(path: str | Path) -> dict[str, Any]:
    encoded = base64.standard_b64encode(Path(path).read_bytes()).decode("ascii")
    mime = "image/gif" if Path(path).suffix.casefold() == ".gif" else "image/jpeg"
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


_ACOUSTIC_BATCH_REQUEST_KEYS = frozenset(
    {
        "contract",
        "contract_version",
        "planner_model",
        "planner_version",
        "batch_index",
        "batch_count",
        "slot_ids",
        "slots",
        "evidence_hashes",
        "batch_timeline_hash",
        "batch_request_hash",
    }
)
_ACOUSTIC_BATCH_RESULT_KEYS = frozenset(
    {
        "contract",
        "contract_version",
        "batch_index",
        "batch_request_hash",
        "expected_slot_ids",
        "response_text",
        "batch_result_hash",
    }
)


def _portable_acoustic_slots(
    slots: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "slot_id": str(row["slot_id"]),
            "start": float(row["start"]),
            "end": float(row["end"]),
            "text": str(row.get("text") or ""),
        }
        for row in slots
    ]


def _sha256_identity(value: Any, label: str) -> str:
    text = str(value or "").strip().casefold()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise SoundPlannerError(f"{label} must be a SHA-256 hex digest")
    return text


def _hash_json(value: Any, label: str) -> str:
    try:
        return canonical_hash(value)
    except (TypeError, ValueError) as exc:
        raise SoundPlannerError(f"{label} must be JSON-safe") from exc


def _batch_request_material(request: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in request.items() if key != "batch_request_hash"}


def _batch_result_material(result: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if key != "batch_result_hash"}


def build_acoustic_batch_requests(
    timeline: Mapping[str, Any],
    evidence_hashes: Mapping[str, Mapping[str, Any]],
    *,
    model: str = "gpt-5-mini",
    batch_size: int = 6,
    planner_version: str = DEFAULT_ACOUSTIC_PLANNER_VERSION,
) -> list[dict[str, Any]]:
    """Build deterministic, path-free identities for independent paid calls."""

    slots = _slots(timeline)
    model_name = str(model).strip()
    version = str(planner_version).strip()
    if not model_name or not version:
        raise SoundPlannerError("acoustic batch request lacks planner identity")
    try:
        size = max(1, int(batch_size))
    except (TypeError, ValueError) as exc:
        raise SoundPlannerError("acoustic batch_size must be an integer") from exc
    if not isinstance(evidence_hashes, Mapping):
        raise SoundPlannerError("acoustic evidence_hashes must be a mapping")

    supplied: dict[str, Mapping[str, Any]] = {}
    for raw_slot_id, value in evidence_hashes.items():
        slot_id = str(raw_slot_id)
        if slot_id in supplied:
            raise SoundPlannerError(f"duplicate acoustic evidence hash slot: {slot_id}")
        if not isinstance(value, Mapping):
            raise SoundPlannerError(f"acoustic evidence hashes for {slot_id} must be a mapping")
        supplied[slot_id] = value
    expected_ids = [str(row["slot_id"]) for row in slots]
    if set(supplied) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(supplied))
        unknown = sorted(set(supplied) - set(expected_ids))
        raise SoundPlannerError(
            f"acoustic evidence hashes do not match timeline; missing={missing}, unknown={unknown}"
        )

    batch_count = (len(slots) + size - 1) // size
    requests: list[dict[str, Any]] = []
    for batch_index, start in enumerate(range(0, len(slots), size)):
        batch = slots[start : start + size]
        batch_ids = [str(row["slot_id"]) for row in batch]
        compact = _portable_acoustic_slots(batch)
        portable_hashes = {
            slot_id: {
                "contact_sheet_sha256": _sha256_identity(
                    supplied[slot_id].get("contact_sheet_sha256"),
                    f"{slot_id}.contact_sheet_sha256",
                ),
                "motion_preview_sha256": _sha256_identity(
                    supplied[slot_id].get("motion_preview_sha256"),
                    f"{slot_id}.motion_preview_sha256",
                ),
            }
            for slot_id in batch_ids
        }
        request: dict[str, Any] = {
            "contract": ACOUSTIC_BATCH_REQUEST_CONTRACT,
            "contract_version": ACOUSTIC_BATCH_REQUEST_CONTRACT_VERSION,
            "planner_model": model_name,
            "planner_version": version,
            "batch_index": batch_index,
            "batch_count": batch_count,
            "slot_ids": batch_ids,
            "slots": compact,
            "evidence_hashes": portable_hashes,
            "batch_timeline_hash": _hash_json(compact, "acoustic batch timeline"),
        }
        request["batch_request_hash"] = _hash_json(
            request, "acoustic batch request"
        )
        requests.append(request)
    return requests


def _validate_acoustic_batch_request(
    timeline: Mapping[str, Any], batch_request: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], str, str]:
    if not isinstance(batch_request, Mapping):
        raise SoundPlannerError("acoustic batch request must be a mapping")
    if set(batch_request) != _ACOUSTIC_BATCH_REQUEST_KEYS:
        raise SoundPlannerError("acoustic batch request has unknown or missing fields")
    if batch_request.get("contract") != ACOUSTIC_BATCH_REQUEST_CONTRACT:
        raise SoundPlannerError("acoustic batch request has unsupported contract")
    if (
        batch_request.get("contract_version")
        != ACOUSTIC_BATCH_REQUEST_CONTRACT_VERSION
    ):
        raise SoundPlannerError("acoustic batch request has unsupported contract version")
    model = str(batch_request.get("planner_model") or "").strip()
    planner_version = str(batch_request.get("planner_version") or "").strip()
    if not model or not planner_version:
        raise SoundPlannerError("acoustic batch request lacks planner identity")
    batch_index = batch_request.get("batch_index")
    batch_count = batch_request.get("batch_count")
    if (
        isinstance(batch_index, bool)
        or not isinstance(batch_index, int)
        or isinstance(batch_count, bool)
        or not isinstance(batch_count, int)
        or batch_count <= 0
        or not 0 <= batch_index < batch_count
    ):
        raise SoundPlannerError("acoustic batch request has invalid batch position")

    supplied_ids = batch_request.get("slot_ids")
    if not isinstance(supplied_ids, Sequence) or isinstance(supplied_ids, (str, bytes)):
        raise SoundPlannerError("acoustic batch request slot_ids must be a list")
    slot_ids = [str(value) for value in supplied_ids]
    if not slot_ids or len(set(slot_ids)) != len(slot_ids):
        raise SoundPlannerError("acoustic batch request slot_ids are empty or duplicated")
    slots = _slots(timeline)
    all_ids = [str(row["slot_id"]) for row in slots]
    try:
        positions = [all_ids.index(slot_id) for slot_id in slot_ids]
    except ValueError as exc:
        raise SoundPlannerError("acoustic batch request references an unknown slot") from exc
    if positions != list(range(positions[0], positions[0] + len(positions))):
        raise SoundPlannerError("acoustic batch request slots must be contiguous and ordered")
    expected_slots = [slots[position] for position in positions]
    compact = _portable_acoustic_slots(expected_slots)
    if batch_request.get("slots") != compact:
        raise SoundPlannerError("acoustic batch request slot material is stale")
    if batch_request.get("batch_timeline_hash") != _hash_json(
        compact, "acoustic batch timeline"
    ):
        raise SoundPlannerError("acoustic batch request timeline hash mismatch")

    raw_hashes = batch_request.get("evidence_hashes")
    if not isinstance(raw_hashes, Mapping) or set(raw_hashes) != set(slot_ids):
        raise SoundPlannerError("acoustic batch request evidence hashes do not match slots")
    for slot_id in slot_ids:
        row = raw_hashes.get(slot_id)
        if not isinstance(row, Mapping) or set(row) != {
            "contact_sheet_sha256",
            "motion_preview_sha256",
        }:
            raise SoundPlannerError(
                f"acoustic batch request evidence hashes for {slot_id} are invalid"
            )
        _sha256_identity(row.get("contact_sheet_sha256"), f"{slot_id}.contact_sheet_sha256")
        _sha256_identity(row.get("motion_preview_sha256"), f"{slot_id}.motion_preview_sha256")
    supplied_hash = _sha256_identity(
        batch_request.get("batch_request_hash"), "batch_request_hash"
    )
    if supplied_hash != _hash_json(
        _batch_request_material(batch_request), "acoustic batch request"
    ):
        raise SoundPlannerError("acoustic batch request hash mismatch")
    return expected_slots, model, planner_version


def request_acoustic_batch_raw(
    timeline: Mapping[str, Any],
    evidence: Mapping[str, Mapping[str, Any]],
    batch_request: Mapping[str, Any],
    *,
    chat_fn: Callable[..., str] = _chat,
) -> dict[str, Any]:
    """Perform exactly one provider call and return its text without parsing it."""

    batch, model, _planner_version = _validate_acoustic_batch_request(
        timeline, batch_request
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": ACOUSTIC_SCENE_PROMPT}]
    expected_hashes = batch_request["evidence_hashes"]
    for row in batch:
        slot_id = str(row["slot_id"])
        artefact = evidence.get(slot_id)
        if not isinstance(artefact, Mapping):
            raise SoundPlannerError(f"missing acoustic evidence for {slot_id}")
        sheet = Path(str(artefact.get("contact_sheet") or ""))
        motion = Path(str(artefact.get("motion_preview_path") or ""))
        if not sheet.is_file() or not motion.is_file():
            raise SoundPlannerError(f"incomplete acoustic evidence for {slot_id}")
        if sha256_file(sheet) != expected_hashes[slot_id]["contact_sheet_sha256"]:
            raise SoundPlannerError(f"stale acoustic contact sheet for {slot_id}")
        if sha256_file(motion) != expected_hashes[slot_id]["motion_preview_sha256"]:
            raise SoundPlannerError(f"stale acoustic motion preview for {slot_id}")
        content.append(
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "slot_id": slot_id,
                        "narration_context_only": row["text"],
                        "instruction": "First inspect eight frames, then use motion preview only for visible timing/action.",
                    },
                    ensure_ascii=False,
                ),
            }
        )
        content.append(_image_part(sheet))
        content.append(_image_part(motion))
    answer = chat_fn(
        model,
        [{"role": "user", "content": content}],
        reasoning_effort="minimal",
        max_tokens=max(2400, len(batch) * 900),
    )
    result: dict[str, Any] = {
        "contract": ACOUSTIC_BATCH_RAW_CONTRACT,
        "contract_version": ACOUSTIC_BATCH_RAW_CONTRACT_VERSION,
        "batch_index": int(batch_request["batch_index"]),
        "batch_request_hash": str(batch_request["batch_request_hash"]),
        "expected_slot_ids": [str(row["slot_id"]) for row in batch],
        "response_text": str(answer),
    }
    result["batch_result_hash"] = _hash_json(result, "acoustic batch result")
    return result


def assemble_acoustic_raw_envelope(
    timeline: Mapping[str, Any],
    batch_requests: Sequence[Mapping[str, Any]],
    batch_results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind independently persisted raw results into one canonical envelope."""

    if not isinstance(batch_requests, Sequence) or isinstance(
        batch_requests, (str, bytes)
    ) or not batch_requests:
        raise SoundPlannerError("acoustic batch requests must be a non-empty list")
    if not isinstance(batch_results, Sequence) or isinstance(batch_results, (str, bytes)):
        raise SoundPlannerError("acoustic batch results must be a list")

    validated_requests: list[Mapping[str, Any]] = []
    seen_indexes: set[int] = set()
    for request in batch_requests:
        _validate_acoustic_batch_request(timeline, request)
        batch_index = int(request["batch_index"])
        if batch_index in seen_indexes:
            raise SoundPlannerError("duplicate acoustic batch request index")
        seen_indexes.add(batch_index)
        validated_requests.append(request)
    validated_requests.sort(key=lambda row: int(row["batch_index"]))
    if seen_indexes != set(range(len(validated_requests))):
        raise SoundPlannerError("acoustic batch request indexes are incomplete")
    if any(int(row["batch_count"]) != len(validated_requests) for row in validated_requests):
        raise SoundPlannerError("acoustic batch request count mismatch")
    models = {str(row["planner_model"]) for row in validated_requests}
    versions = {str(row["planner_version"]) for row in validated_requests}
    if len(models) != 1 or len(versions) != 1:
        raise SoundPlannerError("acoustic batch requests mix planner identities")
    expected_ids = [str(row["slot_id"]) for row in _slots(timeline)]
    assembled_ids = [
        str(slot_id)
        for request in validated_requests
        for slot_id in request["slot_ids"]
    ]
    if assembled_ids != expected_ids or len(set(assembled_ids)) != len(assembled_ids):
        raise SoundPlannerError(
            "acoustic batch requests have unknown, missing, duplicated, or out-of-order slots"
        )
    request_by_hash = {
        str(row["batch_request_hash"]): row for row in validated_requests
    }
    if len(request_by_hash) != len(validated_requests):
        raise SoundPlannerError("duplicate acoustic batch request hash")

    result_by_request_hash: dict[str, Mapping[str, Any]] = {}
    for result in batch_results:
        if not isinstance(result, Mapping):
            raise SoundPlannerError("acoustic batch result must be a mapping")
        if set(result) != _ACOUSTIC_BATCH_RESULT_KEYS:
            raise SoundPlannerError("acoustic batch result has unknown or missing fields")
        if result.get("contract") != ACOUSTIC_BATCH_RAW_CONTRACT:
            raise SoundPlannerError("acoustic batch result has unsupported contract")
        if result.get("contract_version") != ACOUSTIC_BATCH_RAW_CONTRACT_VERSION:
            raise SoundPlannerError("acoustic batch result has unsupported contract version")
        request_hash = _sha256_identity(
            result.get("batch_request_hash"), "batch result request hash"
        )
        request = request_by_hash.get(request_hash)
        if request is None:
            raise SoundPlannerError("acoustic batch result references an unknown request")
        if request_hash in result_by_request_hash:
            raise SoundPlannerError("duplicate acoustic batch result")
        if result.get("batch_index") != request["batch_index"]:
            raise SoundPlannerError("acoustic batch result index mismatch")
        if result.get("expected_slot_ids") != request["slot_ids"]:
            raise SoundPlannerError("acoustic batch result slot binding mismatch")
        if not isinstance(result.get("response_text"), str):
            raise SoundPlannerError("acoustic batch result response_text must be a string")
        supplied_result_hash = _sha256_identity(
            result.get("batch_result_hash"), "batch_result_hash"
        )
        if supplied_result_hash != _hash_json(
            _batch_result_material(result), "acoustic batch result"
        ):
            raise SoundPlannerError("acoustic batch result hash mismatch")
        result_by_request_hash[request_hash] = result
    missing_hashes = set(request_by_hash) - set(result_by_request_hash)
    if missing_hashes:
        raise SoundPlannerError("acoustic batch results are incomplete")

    ordered_results = [
        result_by_request_hash[str(request["batch_request_hash"])]
        for request in validated_requests
    ]
    envelope: dict[str, Any] = {
        "contract": ACOUSTIC_RAW_CONTRACT,
        "contract_version": ACOUSTIC_RAW_CONTRACT_VERSION,
        "planner_model": next(iter(models)),
        "planner_version": next(iter(versions)),
        "slot_ids": expected_ids,
        "batch_request_hashes": [
            str(request["batch_request_hash"]) for request in validated_requests
        ],
        "batches": [
            {
                "expected_slot_ids": list(result["expected_slot_ids"]),
                "response_text": str(result["response_text"]),
                "batch_request_hash": str(result["batch_request_hash"]),
                "batch_result_hash": str(result["batch_result_hash"]),
            }
            for result in ordered_results
        ],
    }
    envelope["assembly_hash"] = _hash_json(envelope, "acoustic raw envelope")
    return envelope


def request_acoustic_scenes_raw(
    timeline: Mapping[str, Any],
    evidence: Mapping[str, Mapping[str, Any]],
    *,
    model: str = "gpt-5-mini",
    batch_size: int = 6,
    chat_fn: Callable[..., str] = _chat,
    planner_version: str = DEFAULT_ACOUSTIC_PLANNER_VERSION,
) -> dict[str, Any]:
    """Request provider text without applying any local scene validation.

    The JSON-safe envelope is deliberately raw so a paid-operation layer can
    persist it as completed before deterministic parsing and validation run.
    """

    slots = _slots(timeline)
    evidence_hashes: dict[str, dict[str, str]] = {}
    for row in slots:
        slot_id = str(row["slot_id"])
        artefact = evidence.get(slot_id)
        if not isinstance(artefact, Mapping):
            raise SoundPlannerError(f"missing acoustic evidence for {slot_id}")
        sheet = Path(str(artefact.get("contact_sheet") or ""))
        motion = Path(str(artefact.get("motion_preview_path") or ""))
        if not sheet.is_file() or not motion.is_file():
            raise SoundPlannerError(f"incomplete acoustic evidence for {slot_id}")
        evidence_hashes[slot_id] = {
            "contact_sheet_sha256": sha256_file(sheet),
            "motion_preview_sha256": sha256_file(motion),
        }
    requests = build_acoustic_batch_requests(
        timeline,
        evidence_hashes,
        model=model,
        batch_size=batch_size,
        planner_version=planner_version,
    )
    results = [
        request_acoustic_batch_raw(
            timeline,
            evidence,
            request,
            chat_fn=chat_fn,
        )
        for request in requests
    ]
    return assemble_acoustic_raw_envelope(timeline, requests, results)


def normalize_acoustic_scenes_raw(
    timeline: Mapping[str, Any],
    evidence: Mapping[str, Mapping[str, Any]],
    raw_result: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Validate one persisted raw envelope and build canonical scenes.

    Unknown enum values fail closed in :func:`build_acoustic_scene`; only the
    small audited alias set in ``acoustic_scene`` is canonicalised.
    """

    if not isinstance(raw_result, Mapping):
        raise SoundPlannerError("acoustic raw result must be a mapping envelope")
    if raw_result.get("contract") != ACOUSTIC_RAW_CONTRACT:
        raise SoundPlannerError("acoustic raw result has unsupported contract")
    if raw_result.get("contract_version") != ACOUSTIC_RAW_CONTRACT_VERSION:
        raise SoundPlannerError("acoustic raw result has unsupported contract version")
    model = str(raw_result.get("planner_model") or "").strip()
    planner_version = str(raw_result.get("planner_version") or "").strip()
    if not model or not planner_version:
        raise SoundPlannerError("acoustic raw result lacks planner identity")

    slots = _slots(timeline)
    expected_all = [str(row["slot_id"]) for row in slots]
    supplied_all = raw_result.get("slot_ids")
    if not isinstance(supplied_all, Sequence) or isinstance(supplied_all, (str, bytes)):
        raise SoundPlannerError("acoustic raw result slot_ids must be a list")
    if [str(value) for value in supplied_all] != expected_all:
        raise SoundPlannerError("acoustic raw result is stale for this timeline")
    raw_batches = raw_result.get("batches")
    if not isinstance(raw_batches, Sequence) or isinstance(raw_batches, (str, bytes)):
        raise SoundPlannerError("acoustic raw result batches must be a list")

    result: list[dict[str, Any]] = []
    consumed_ids: list[str] = []
    slot_by_id = {str(row["slot_id"]): row for row in slots}
    for batch_index, raw_batch in enumerate(raw_batches):
        if not isinstance(raw_batch, Mapping):
            raise SoundPlannerError(f"acoustic raw batch {batch_index} must be an object")
        supplied_ids = raw_batch.get("expected_slot_ids")
        if not isinstance(supplied_ids, Sequence) or isinstance(supplied_ids, (str, bytes)):
            raise SoundPlannerError(
                f"acoustic raw batch {batch_index} expected_slot_ids must be a list"
            )
        expected_ids = [str(value) for value in supplied_ids]
        if not expected_ids or expected_all[len(consumed_ids):len(consumed_ids) + len(expected_ids)] != expected_ids:
            raise SoundPlannerError(
                f"acoustic raw batch {batch_index} is stale or out of order"
            )
        response_text = raw_batch.get("response_text")
        if not isinstance(response_text, str) or not response_text.strip():
            raise SoundPlannerError(
                f"acoustic raw batch {batch_index} response_text must be non-empty"
            )
        try:
            parsed = extract_json(response_text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SoundPlannerError(
                f"acoustic raw batch {batch_index} contains invalid JSON"
            ) from exc
        if not isinstance(parsed, list):
            raise SoundPlannerError("acoustic planner must return a JSON array")
        rows_by_id: dict[str, Mapping[str, Any]] = {}
        for raw in parsed:
            if not isinstance(raw, Mapping):
                raise SoundPlannerError("acoustic planner row must be an object")
            slot_id = str(raw.get("slot_id") or "")
            if slot_id not in expected_ids or slot_id in rows_by_id:
                raise SoundPlannerError("acoustic planner returned unknown/duplicate slot")
            rows_by_id[slot_id] = raw
        if set(rows_by_id) != set(expected_ids):
            missing = sorted(set(expected_ids) - set(rows_by_id))
            raise SoundPlannerError(f"acoustic planner omitted slots: {missing}")
        for slot_id in expected_ids:
            row = slot_by_id[slot_id]
            raw = rows_by_id[slot_id]
            artefact = evidence.get(slot_id)
            if not isinstance(artefact, Mapping):
                raise SoundPlannerError(f"missing acoustic evidence for {slot_id}")
            result.append(
                build_acoustic_scene(
                    timeline,
                    slot_id=slot_id,
                    environment=raw.get("environment") or {},
                    visible_sources=raw.get("visible_sources") or [],
                    events=raw.get("events") or [],
                    hard_negatives=raw.get("hard_negatives") or [],
                    summary=str(raw.get("summary") or "visual acoustic scene"),
                    planner_model=model,
                    planner_version=planner_version,
                    evidence_artifact={
                        "contact_sheet_sha256": artefact.get("contact_sheet_sha256"),
                        "motion_preview_sha256": artefact.get("motion_preview_sha256"),
                        "evidence_key": artefact.get("evidence_key"),
                        "source_asset_id": artefact.get("source_asset_id"),
                        "frame_time_contract": artefact.get("frame_time_contract"),
                        "source_path": artefact.get("source_path"),
                        "source_in": artefact.get("source_in"),
                        "source_out": artefact.get("source_out"),
                        "sampled_frames": 8,
                        "motion_preview": True,
                        "donor_audio_included": False,
                    },
                )
            )
        consumed_ids.extend(expected_ids)
    if consumed_ids != expected_all:
        missing = expected_all[len(consumed_ids):]
        raise SoundPlannerError(f"acoustic raw result omitted batches for slots: {missing}")
    validate_acoustic_scenes(result, timeline)
    return result


def plan_acoustic_scenes(
    timeline: Mapping[str, Any],
    evidence: Mapping[str, Mapping[str, Any]],
    *,
    model: str = "gpt-5-mini",
    batch_size: int = 6,
    chat_fn: Callable[..., str] = _chat,
    planner_version: str = DEFAULT_ACOUSTIC_PLANNER_VERSION,
) -> list[dict[str, Any]]:
    """Compatibility wrapper: request raw provider text, then normalise it."""

    raw_result = request_acoustic_scenes_raw(
        timeline,
        evidence,
        model=model,
        batch_size=batch_size,
        chat_fn=chat_fn,
        planner_version=planner_version,
    )
    return normalize_acoustic_scenes_raw(timeline, evidence, raw_result)


def normalize_continuous_sound_lanes_raw(
    timeline: Mapping[str, Any],
    evidence: Mapping[str, Mapping[str, Any]],
    raw_result: Mapping[str, Any],
    *,
    provider: str = "local_licensed",
) -> dict[str, Any]:
    """Turn a completed raw paid result into all deterministic natural lanes."""

    raw_scenes = normalize_acoustic_scenes_raw(timeline, evidence, raw_result)
    scenes = compile_causal_acoustic_scenes(timeline, raw_scenes)
    return {
        "acoustic_scenes": scenes,
        **build_natural_lanes(timeline, scenes, provider=provider),
    }


def plan_continuous_sound_lanes(
    timeline: Mapping[str, Any],
    evidence: Mapping[str, Mapping[str, Any]],
    *,
    provider: str = "local_licensed",
    model: str = "gpt-5-mini",
    batch_size: int = 6,
    chat_fn: Callable[..., str] = _chat,
) -> dict[str, Any]:
    raw_result = request_acoustic_scenes_raw(
        timeline,
        evidence,
        model=model,
        batch_size=batch_size,
        chat_fn=chat_fn,
    )
    return normalize_continuous_sound_lanes_raw(
        timeline, evidence, raw_result, provider=provider
    )


def plan_sfx_queries(
    timeline: Mapping[str, Any],
    contact_sheets: Mapping[str, str | Path],
    *,
    model: str = "gpt-5-nano",
    batch_size: int = 10,
    min_confidence: float = 0.82,
    chat_fn: Callable[..., str] = _chat,
) -> list[dict[str, Any]]:
    """Create sparse stock-search cues from exact selected visual ranges."""

    slots = _slots(timeline)
    accepted: list[dict[str, Any]] = []
    for start in range(0, len(slots), max(1, int(batch_size))):
        batch = slots[start:start + max(1, int(batch_size))]
        content: list[dict[str, Any]] = [{"type": "text", "text": SFX_PROMPT}]
        for row in batch:
            sheet = contact_sheets.get(row["slot_id"])
            if not sheet or not Path(sheet).is_file():
                raise SoundPlannerError(f"missing exact contact sheet for {row['slot_id']}")
            content.append({"type": "text", "text": json.dumps({
                "slot_id": row["slot_id"], "narration_context": row["text"]
            }, ensure_ascii=False)})
            content.append(_image_part(sheet))
        answer = chat_fn(
            model,
            [{"role": "user", "content": content}],
            reasoning_effort="minimal",
            max_tokens=max(1200, len(batch) * 220),
        )
        parsed = extract_json(answer)
        if not isinstance(parsed, list):
            raise SoundPlannerError("SFX planner must return a JSON array")
        rows_by_id = {row["slot_id"]: row for row in batch}
        for raw in parsed:
            if not isinstance(raw, Mapping) or raw.get("emit") is not True:
                continue
            slot_id = str(raw.get("slot_id") or "")
            if slot_id not in rows_by_id:
                raise SoundPlannerError("SFX planner referenced a slot outside its batch")
            confidence = _unit(raw.get("confidence"), "SFX confidence")
            query = str(raw.get("query_en") or "").strip()
            evidence = str(raw.get("visible_evidence") or "").strip()
            source_kind = str(raw.get("source_kind") or "").strip()
            action = str(raw.get("action") or "").strip()
            if confidence < min_confidence or not all((query, evidence, source_kind, action)):
                continue
            slot = rows_by_id[slot_id]
            fraction = max(0.10, min(0.85, float(raw.get("offset_fraction") or 0.30)))
            duration = max(0.20, min(6.0, float(raw.get("duration_hint") or 2.0)))
            offset = (slot["end"] - slot["start"]) * fraction
            accepted.append({
                "cue_id": f"sfx_{len(accepted) + 1:03d}",
                "slot_id": slot_id,
                "offset": offset,
                "time": slot["start"] + offset,
                "duration_hint": min(duration, slot["end"] - (slot["start"] + offset)),
                "query": query[:300],
                "visible_evidence": evidence[:500],
                "source_kind": source_kind[:120],
                "action": action[:160],
                "confidence": confidence,
                "gain_db": max(-24.0, min(-8.0, float(raw.get("gain_db") or -16.0))),
                "provider": "shutterstock_sfx",
                "status": "needs_asset",
            })
    return accepted


__all__ = [
    "ACOUSTIC_BATCH_RAW_CONTRACT",
    "ACOUSTIC_BATCH_RAW_CONTRACT_VERSION",
    "ACOUSTIC_BATCH_REQUEST_CONTRACT",
    "ACOUSTIC_BATCH_REQUEST_CONTRACT_VERSION",
    "ACOUSTIC_RAW_CONTRACT",
    "ACOUSTIC_RAW_CONTRACT_VERSION",
    "ACOUSTIC_SCENE_PROMPT",
    "DEFAULT_ACOUSTIC_PLANNER_VERSION",
    "MOOD_PROMPT",
    "SFX_PROMPT",
    "SoundPlannerError",
    "assemble_acoustic_raw_envelope",
    "build_acoustic_batch_requests",
    "make_acoustic_evidence",
    "make_selected_contact_sheets",
    "normalize_continuous_sound_lanes_raw",
    "normalize_acoustic_scenes_raw",
    "plan_acoustic_scenes",
    "plan_continuous_sound_lanes",
    "plan_music_segments",
    "plan_sfx_queries",
    "request_acoustic_batch_raw",
    "request_acoustic_scenes_raw",
]
