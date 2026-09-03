"""Deterministic 4--8 second narration beat planning.

The planner works globally rather than greedily: a short final tail therefore
causes earlier cuts to move instead of becoming an invalid extra scene.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


BEATS_SCHEMA_VERSION = 1


class BeatPlanningError(ValueError):
    """Raised when valid 4--8 second coverage cannot be built from word timings."""


@dataclass(frozen=True)
class _Boundary:
    word_index: int
    time: float
    reason: str
    penalty: float


_SENTENCE_END = re.compile(r"[.!?…][\"'»)\]]*$")
_COLON_END = re.compile(r"[:;][\"'»)\]]*$")
_COMMA_END = re.compile(r",[\"'»)\]]*$")
_NUMBER = re.compile(r"^[+-]?(?:\d+[\d\s.,]*|[IVXLCDM]+)$", re.IGNORECASE)
_UNITS = {
    "%",
    "ампер",
    "ватт",
    "г",
    "гектар",
    "градус",
    "день",
    "дней",
    "кг",
    "килограмм",
    "километр",
    "км",
    "л",
    "лет",
    "м",
    "метр",
    "минут",
    "мл",
    "мм",
    "секунд",
    "см",
    "т",
    "тонн",
    "час",
    "часа",
    "часов",
}
_PROTECTED_PAIRS = {
    ("для", "того"),
    ("если", "бы"),
    ("как", "будто"),
    ("потому", "что"),
    ("так", "как"),
    ("того", "чтобы"),
}


def _word_text(word: Mapping[str, Any]) -> str:
    return str(word.get("text") or word.get("spoken_text") or "").strip()


def _punctuation_after(word: Mapping[str, Any]) -> str:
    return str(word.get("punctuation_after") or "")


def _boundary_quality(
    previous: Mapping[str, Any], following: Mapping[str, Any], pause: float
) -> tuple[str, float]:
    tail = (_word_text(previous) + _punctuation_after(previous)).rstrip()
    if _SENTENCE_END.search(tail):
        reason, penalty = "sentence", 0.0
    elif pause >= 0.45:
        reason, penalty = "pause", 0.35
    elif _COLON_END.search(tail):
        reason, penalty = "colon_or_semicolon", 0.75
    elif _COMMA_END.search(tail):
        reason, penalty = "comma", 1.15
    else:
        reason, penalty = "word", 2.0

    left = _word_text(previous)
    right = _word_text(following)
    left_folded = left.casefold().strip("«»\"'.,:;!?")
    right_folded = right.casefold().strip("«»\"'.,:;!?")
    if (_NUMBER.fullmatch(left) and right_folded in _UNITS) or (
        left_folded in _UNITS and _NUMBER.fullmatch(right)
    ):
        penalty += 12.0
        reason += "+protected_number_unit"
    elif (left_folded, right_folded) in _PROTECTED_PAIRS:
        penalty += 10.0
        reason += "+protected_phrase"
    elif (
        left[:1].isupper()
        and right[:1].isupper()
        and not _SENTENCE_END.search(tail)
        and pause < 0.35
    ):
        # Cheap but useful protection for common two-token proper names.
        penalty += 7.0
        reason += "+protected_name"
    return reason, penalty


def _normalise_words(
    words: Sequence[Mapping[str, Any]], audio_duration: float, frame_tolerance: float
) -> list[dict[str, Any]]:
    if not words:
        raise BeatPlanningError("word timings are empty")
    normalised: list[dict[str, Any]] = []
    previous_start = -1.0
    for index, raw in enumerate(words):
        try:
            start = float(raw["start"])
            end = float(raw["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BeatPlanningError(f"word {index} has invalid start/end") from exc
        if not math.isfinite(start) or not math.isfinite(end):
            raise BeatPlanningError(f"word {index} timing is not finite")
        if start < -frame_tolerance or end < start:
            raise BeatPlanningError(f"word {index} has an invalid interval")
        if start < previous_start - frame_tolerance:
            raise BeatPlanningError("word timings are not chronological")
        if end > audio_duration + frame_tolerance:
            raise BeatPlanningError(f"word {index} ends after master audio")
        item = dict(raw)
        item["start"] = max(0.0, min(start, audio_duration))
        item["end"] = max(item["start"], min(end, audio_duration))
        item.setdefault("index", index)
        normalised.append(item)
        previous_start = start
    return normalised


def _candidate_boundaries(words: Sequence[Mapping[str, Any]], duration: float) -> list[_Boundary]:
    candidates = [_Boundary(0, 0.0, "audio_start", 0.0)]
    for index in range(1, len(words)):
        previous = words[index - 1]
        following = words[index]
        previous_end = float(previous["end"])
        following_start = float(following["start"])
        pause = max(0.0, following_start - previous_end)
        reason, penalty = _boundary_quality(previous, following, pause)

        if pause >= 0.08:
            # More than one legal point in a real pause gives the global solver
            # enough room to avoid a 1--3 second final tail.
            times = (previous_end, (previous_end + following_start) / 2, following_start)
        else:
            times = (following_start,)
        for boundary_time in times:
            boundary_time = max(0.0, min(boundary_time, duration))
            candidates.append(_Boundary(index, boundary_time, reason, penalty))

    candidates.append(_Boundary(len(words), duration, "audio_end", 0.0))
    # Stable ordering and de-duplication are important for reproducible manifests.
    ordered = sorted(candidates, key=lambda item: (item.time, item.word_index, item.penalty))
    unique: list[_Boundary] = []
    seen: set[tuple[int, int]] = set()
    for item in ordered:
        key = (item.word_index, round(item.time * 1_000_000))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _segment_text(
    words: Sequence[Mapping[str, Any]], start_index: int, end_index: int, script: str | None
) -> str:
    selected = words[start_index:end_index]
    if not selected:
        return ""
    if script is not None:
        first_start = selected[0].get("char_start")
        if isinstance(first_start, int):
            next_start: int | None = None
            if end_index < len(words):
                candidate = words[end_index].get("char_start")
                if isinstance(candidate, int):
                    next_start = candidate
            if next_start is None:
                next_start = len(script)
            exact = script[first_start:next_start].strip()
            if exact:
                return exact

    pieces = [(_word_text(word) + _punctuation_after(word)).strip() for word in selected]
    return " ".join(piece for piece in pieces if piece).strip()


def assert_beat_coverage(
    beats: Sequence[Mapping[str, Any]],
    audio_duration: float,
    *,
    fps: float = 30.0,
    min_seconds: float = 4.0,
    max_seconds: float = 8.0,
) -> None:
    """Validate full, contiguous master-timeline coverage within one frame."""

    if fps <= 0:
        raise ValueError("fps must be positive")
    tolerance = 1.0 / fps + 1e-7
    if not beats:
        raise BeatPlanningError("beat plan is empty")
    if abs(float(beats[0]["start"])) > tolerance:
        raise BeatPlanningError("beat plan does not start at zero")
    if abs(float(beats[-1]["end"]) - audio_duration) > tolerance:
        raise BeatPlanningError("beat plan does not end at master audio duration")
    for index, beat in enumerate(beats):
        start = float(beat["start"])
        end = float(beat["end"])
        beat_duration = end - start
        if audio_duration >= min_seconds and (
            beat_duration < min_seconds - tolerance
            or beat_duration > max_seconds + tolerance
        ):
            raise BeatPlanningError(
                f"beat {index} duration {beat_duration:.3f}s is outside "
                f"{min_seconds:.3f}--{max_seconds:.3f}s"
            )
        if index:
            gap = start - float(beats[index - 1]["end"])
            if abs(gap) > tolerance:
                raise BeatPlanningError(
                    f"beat boundary {index} has a {gap:+.6f}s gap/overlap"
                )


def plan_beats(
    words_or_payload: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    audio_duration: float | None = None,
    *,
    script: str | None = None,
    fps: float = 30.0,
    min_seconds: float = 4.0,
    target_seconds: float = 6.0,
    max_seconds: float = 8.0,
) -> list[dict[str, Any]]:
    """Globally partition Edge word timings into contiguous semantic beats."""

    if isinstance(words_or_payload, Mapping):
        payload = words_or_payload
        words_value = payload.get("words")
        if not isinstance(words_value, list):
            raise BeatPlanningError("words payload must contain a list")
        words: Sequence[Mapping[str, Any]] = words_value
        if audio_duration is None:
            audio_duration = float(payload.get("audio_duration", 0.0))
        if script is None and isinstance(payload.get("text"), str):
            script = payload["text"]
    else:
        words = words_or_payload

    if audio_duration is None:
        raise BeatPlanningError("audio_duration is required")
    audio_duration = float(audio_duration)
    if not math.isfinite(audio_duration) or audio_duration <= 0:
        raise BeatPlanningError("audio_duration must be positive")
    if fps <= 0:
        raise ValueError("fps must be positive")
    if not (0 < min_seconds <= target_seconds <= max_seconds):
        raise ValueError("expected 0 < min_seconds <= target_seconds <= max_seconds")
    frame_tolerance = 1.0 / fps
    normalised = _normalise_words(words, audio_duration, frame_tolerance)

    if audio_duration < min_seconds - frame_tolerance:
        text = _segment_text(normalised, 0, len(normalised), script)
        beats = [
            {
                "beat_id": 1,
                "slot_id": "b001",
                "start": 0.0,
                "end": round(audio_duration, 6),
                "duration": round(audio_duration, 6),
                "text": text,
                "word_start_index": 0,
                "word_end_index": len(normalised),
                "boundary_reason": "audio_end",
                "short_master": True,
            }
        ]
        assert_beat_coverage(
            beats,
            audio_duration,
            fps=fps,
            min_seconds=min_seconds,
            max_seconds=max_seconds,
        )
        return beats

    boundaries = _candidate_boundaries(normalised, audio_duration)
    # State value: (cost, number_of_segments, path_of_boundary_indices).
    states: dict[int, tuple[float, int, tuple[int, ...]]] = {0: (0.0, 0, (0,))}
    for end_position in range(1, len(boundaries)):
        end_boundary = boundaries[end_position]
        best: tuple[float, int, tuple[int, ...]] | None = None
        for start_position, state in list(states.items()):
            if start_position >= end_position:
                continue
            start_boundary = boundaries[start_position]
            if end_boundary.word_index <= start_boundary.word_index:
                continue
            duration = end_boundary.time - start_boundary.time
            if duration < min_seconds - frame_tolerance:
                continue
            if duration > max_seconds + frame_tolerance:
                continue

            duration_penalty = ((duration - target_seconds) / target_seconds) ** 2 * 0.55
            cut_penalty = 0.0 if end_boundary.word_index == len(normalised) else end_boundary.penalty
            proposed = (
                state[0] + duration_penalty + cut_penalty,
                state[1] + 1,
                state[2] + (end_position,),
            )
            if best is None or proposed < best:
                best = proposed
        if best is not None:
            states[end_position] = best

    final_position = len(boundaries) - 1
    if final_position not in states:
        raise BeatPlanningError(
            "cannot build strict 4--8 second beats at word/pause boundaries; "
            "inspect the narration timing gaps"
        )

    path = states[final_position][2]
    beats: list[dict[str, Any]] = []
    for beat_index, (start_pos, end_pos) in enumerate(zip(path, path[1:]), start=1):
        start_boundary = boundaries[start_pos]
        end_boundary = boundaries[end_pos]
        start = start_boundary.time
        end = end_boundary.time
        text = _segment_text(
            normalised, start_boundary.word_index, end_boundary.word_index, script
        )
        if not text:
            raise BeatPlanningError(f"beat {beat_index} has no narration text")
        beats.append(
            {
                "beat_id": beat_index,
                "slot_id": f"b{beat_index:03d}",
                "start": round(start, 6),
                "end": round(end, 6),
                "duration": round(end - start, 6),
                "text": text,
                "word_start_index": start_boundary.word_index,
                "word_end_index": end_boundary.word_index,
                "boundary_reason": end_boundary.reason,
            }
        )

    assert_beat_coverage(
        beats,
        audio_duration,
        fps=fps,
        min_seconds=min_seconds,
        max_seconds=max_seconds,
    )
    return beats


def repartition_beats_for_visual_limit(
    beats: Sequence[Mapping[str, Any]],
    words_or_payload: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    target_slot_id: str,
    max_target_seconds: float,
    *,
    script: str | None = None,
    fps: float = 25.0,
    min_seconds: float = 4.0,
    target_seconds: float = 6.0,
    max_seconds: float = 8.0,
    window_radius: int = 2,
) -> list[dict[str, Any]]:
    """Return a locally repartitioned copy for a duration-limited visual clip.

    Only word and pause boundaries are considered.  The outer edges and beat
    count of the local window stay fixed, so narration coverage and every slot
    identity outside the window are unchanged.  This is intentionally a pure
    helper: callers persist the returned effective plan and its provenance;
    the original ``beats.json`` is never modified.
    """

    if not beats:
        raise BeatPlanningError("beat plan is empty")
    if fps <= 0:
        raise ValueError("fps must be positive")
    if window_radius < 0:
        raise ValueError("window_radius must be non-negative")
    if not (0 < min_seconds <= target_seconds <= max_seconds):
        raise ValueError("expected 0 < min_seconds <= target_seconds <= max_seconds")
    max_target_seconds = float(max_target_seconds)
    if not math.isfinite(max_target_seconds) or not (
        min_seconds <= max_target_seconds <= max_seconds
    ):
        raise BeatPlanningError(
            "max_target_seconds must permit a strict 4--8 second beat"
        )

    output = [deepcopy(dict(beat)) for beat in beats]
    matching = [
        index
        for index, beat in enumerate(output)
        if str(beat.get("slot_id") or "") == str(target_slot_id)
    ]
    if len(matching) != 1:
        raise BeatPlanningError(
            f"target slot {target_slot_id!r} is missing or duplicate"
        )
    target_index = matching[0]
    audio_duration = float(output[-1]["end"])
    frame_tolerance = 1.0 / fps
    assert_beat_coverage(
        output,
        audio_duration,
        fps=fps,
        min_seconds=min_seconds,
        max_seconds=max_seconds,
    )

    if isinstance(words_or_payload, Mapping):
        words_value = words_or_payload.get("words")
        if not isinstance(words_value, list):
            raise BeatPlanningError("words payload must contain a list")
        words: Sequence[Mapping[str, Any]] = words_value
        if script is None and isinstance(words_or_payload.get("text"), str):
            script = str(words_or_payload["text"])
        payload_duration = words_or_payload.get("audio_duration")
        if payload_duration is not None and abs(float(payload_duration) - audio_duration) > frame_tolerance:
            raise BeatPlanningError("word timings and beat plan have different audio durations")
    else:
        words = words_or_payload
    normalised = _normalise_words(words, audio_duration, frame_tolerance)

    current_duration = float(output[target_index]["end"]) - float(
        output[target_index]["start"]
    )
    if current_duration <= max_target_seconds + 1e-9:
        return output

    window_start_index = max(0, target_index - window_radius)
    window_end_index = min(len(output) - 1, target_index + window_radius)
    window = output[window_start_index : window_end_index + 1]
    segment_count = len(window)
    target_segment = target_index - window_start_index

    def beat_word_index(beat: Mapping[str, Any], key: str, boundary_time: float) -> int:
        value = beat.get(key)
        if type(value) is int and 0 <= int(value) <= len(normalised):
            return int(value)
        # A beat boundary may lie anywhere inside a pause.  The following word
        # index still describes it without cutting through spoken audio.
        return sum(1 for word in normalised if float(word["start"]) < boundary_time - 1e-9)

    window_start = float(window[0]["start"])
    window_end = float(window[-1]["end"])
    start_word_index = beat_word_index(
        window[0], "word_start_index", window_start
    )
    end_word_index = beat_word_index(
        window[-1], "word_end_index", window_end
    )
    if not (0 <= start_word_index < end_word_index <= len(normalised)):
        raise BeatPlanningError("local repair window has invalid word coverage")

    start_boundary = _Boundary(
        start_word_index, window_start, "repair_window_start", 0.0
    )
    end_boundary = _Boundary(
        end_word_index, window_end, "repair_window_end", 0.0
    )
    interior = [
        boundary
        for boundary in _candidate_boundaries(normalised, audio_duration)
        if start_word_index < boundary.word_index < end_word_index
        and window_start < boundary.time < window_end
    ]
    boundaries = [start_boundary, *interior, end_boundary]
    boundaries.sort(key=lambda item: (item.time, item.word_index, item.penalty, item.reason))

    # State: (last boundary position, segments built) -> (cost, path).
    states: dict[tuple[int, int], tuple[float, tuple[int, ...]]] = {
        (0, 0): (0.0, (0,))
    }
    original_ends = [float(beat["end"]) for beat in window]
    for built in range(segment_count):
        next_states: dict[tuple[int, int], tuple[float, tuple[int, ...]]] = {}
        for (start_position, prior_count), (cost, path) in sorted(states.items()):
            if prior_count != built:
                continue
            start = boundaries[start_position]
            for end_position in range(start_position + 1, len(boundaries)):
                end = boundaries[end_position]
                if end.word_index <= start.word_index:
                    continue
                duration = end.time - start.time
                if duration < min_seconds - frame_tolerance:
                    continue
                if duration > max_seconds + frame_tolerance:
                    break
                if built == target_segment and duration > max_target_seconds + 1e-9:
                    continue
                remaining_segments = segment_count - built - 1
                remaining_duration = window_end - end.time
                if remaining_duration < remaining_segments * (min_seconds - frame_tolerance):
                    continue
                if remaining_duration > remaining_segments * (max_seconds + frame_tolerance):
                    continue
                # The final segment must terminate at the fixed window edge;
                # earlier segments may not consume it.
                if remaining_segments == 0 and end_position != len(boundaries) - 1:
                    continue
                if remaining_segments > 0 and end_position == len(boundaries) - 1:
                    continue

                duration_penalty = (
                    (duration - target_seconds) / target_seconds
                ) ** 2 * 0.55
                cut_penalty = 0.0 if remaining_segments == 0 else end.penalty
                shift_penalty = (
                    0.5
                    * ((end.time - original_ends[built]) / target_seconds) ** 2
                    if remaining_segments
                    else 0.0
                )
                proposed = (
                    cost + duration_penalty + cut_penalty + shift_penalty,
                    path + (end_position,),
                )
                key = (end_position, built + 1)
                previous = next_states.get(key)
                if previous is None or proposed < previous:
                    next_states[key] = proposed
        states = next_states
        if not states:
            break

    final_key = (len(boundaries) - 1, segment_count)
    if final_key not in states:
        raise BeatPlanningError(
            f"cannot repartition slots {window[0].get('slot_id')}--"
            f"{window[-1].get('slot_id')} for {target_slot_id} <= "
            f"{max_target_seconds:.6f}s without a mid-word cut"
        )

    path = states[final_key][1]
    repaired_window: list[dict[str, Any]] = []
    for relative_index, (start_position, end_position) in enumerate(
        zip(path, path[1:])
    ):
        start = boundaries[start_position]
        end = boundaries[end_position]
        beat = deepcopy(window[relative_index])
        text = _segment_text(
            normalised, start.word_index, end.word_index, script
        )
        if not text:
            raise BeatPlanningError(
                f"repartitioned slot {beat.get('slot_id')} has no narration text"
            )
        beat.update(
            {
                "start": round(start.time, 6),
                "end": round(end.time, 6),
                "duration": round(end.time - start.time, 6),
                "text": text,
                "word_start_index": start.word_index,
                "word_end_index": end.word_index,
                "boundary_reason": end.reason,
            }
        )
        repaired_window.append(beat)

    output[window_start_index : window_end_index + 1] = repaired_window
    assert_beat_coverage(
        output,
        audio_duration,
        fps=fps,
        min_seconds=min_seconds,
        max_seconds=max_seconds,
    )
    repaired_target = output[target_index]
    if float(repaired_target["duration"]) > max_target_seconds + 1e-6:
        raise BeatPlanningError("repartitioned target still exceeds its visual limit")
    return output


def build_beats_document(
    words_or_payload: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    audio_duration: float | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Return the versioned JSON-ready beats artifact."""

    beats = plan_beats(words_or_payload, audio_duration, **kwargs)
    if audio_duration is None and isinstance(words_or_payload, Mapping):
        audio_duration = float(words_or_payload["audio_duration"])
    fps = float(kwargs.get("fps", 30.0))
    return {
        "schema_version": BEATS_SCHEMA_VERSION,
        "audio_duration": round(float(audio_duration), 7),
        "fps": fps,
        "beats": beats,
    }


def write_beats_json(
    path: str | os.PathLike[str],
    words_or_payload: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    audio_duration: float | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Plan and atomically persist ``beats.json``."""

    payload = build_beats_document(words_or_payload, audio_duration, **kwargs)
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}.", suffix=".tmp.json", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return payload


__all__ = [
    "BEATS_SCHEMA_VERSION",
    "BeatPlanningError",
    "assert_beat_coverage",
    "build_beats_document",
    "plan_beats",
    "repartition_beats_for_visual_limit",
    "write_beats_json",
]
