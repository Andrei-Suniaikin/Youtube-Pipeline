"""Deterministic planning and bounded selection primitives for the fast lane.

The module intentionally contains no media or network I/O.  It turns the
existing word-timing documents into one bilingual visual identity, ranks an
already embedded donor catalog in one vectorized operation, and exact-checks
only a primary plus at most two fallbacks.

All returned artifacts are JSON-friendly.  The one exception is
:class:`Deadline`, which is a small runtime helper and exposes a JSON-friendly
snapshot via :meth:`Deadline.timings`.
"""

from __future__ import annotations

import json
import math
import re
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

import numpy as np

from .beats import plan_beats


FAST_PIPELINE_SCHEMA_VERSION = 1
MAX_TOP_K = 5
MAX_FALLBACKS = 2
MAX_ANTI_REPEAT_WINDOW = 3

# English chapter 2 has one real source paragraph (elephants swimming) that is
# absent from the Russian script.  Keeping this as an explicit source-layout
# fact is safer than guessing from translated text or silently shifting all
# following paragraphs by one.
DEFAULT_EN_ONLY_PARAGRAPH_ANCHORS: Mapping[int, tuple[int, ...]] = {2: (17,)}


class FastPipelineError(ValueError):
    """Base error for an invalid or unsatisfied fast-lane contract."""


class SelectionExhausted(FastPipelineError):
    """The fixed primary + fallback shortlist contains no clean candidate."""


class DeadlineExceeded(TimeoutError):
    """The total or current-stage monotonic budget was exceeded."""

    def __init__(
        self,
        message: str,
        *,
        elapsed_seconds: float,
        budget_seconds: float,
        stage: str | None = None,
    ) -> None:
        super().__init__(message)
        self.elapsed_seconds = elapsed_seconds
        self.budget_seconds = budget_seconds
        self.stage = stage


def _finite_positive(value: Any, label: str) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise FastPipelineError(f"{label} must be a positive finite number") from exc
    if not math.isfinite(normalized) or normalized <= 0:
        raise FastPipelineError(f"{label} must be a positive finite number")
    return normalized


def _bounded_integer(value: Any, label: str, maximum: int) -> int:
    if isinstance(value, bool):
        raise FastPipelineError(f"{label} must be an integer in 1..{maximum}")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise FastPipelineError(f"{label} must be an integer in 1..{maximum}") from exc
    if normalized != value or not 1 <= normalized <= maximum:
        raise FastPipelineError(f"{label} must be an integer in 1..{maximum}")
    return normalized


class Deadline:
    """One hard monotonic deadline with optional per-stage budgets.

    ``clock`` is injectable for deterministic tests.  No wall-clock timestamp is
    used in enforcement or persisted output, so clock adjustments cannot extend
    a run.  Stage budgets are upper bounds, not reservations: unused time is not
    added to another stage and the total deadline always remains authoritative.
    """

    def __init__(
        self,
        total_seconds: float,
        *,
        stage_budgets: Mapping[str, float] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.total_seconds = _finite_positive(total_seconds, "total_seconds")
        if not callable(clock):
            raise FastPipelineError("clock must be callable")
        self._clock = clock
        self._started = float(clock())
        if not math.isfinite(self._started):
            raise FastPipelineError("monotonic clock returned a non-finite value")
        self._last_now = self._started
        self._stage_budgets: dict[str, float] = {}
        for raw_name, raw_budget in (stage_budgets or {}).items():
            name = str(raw_name).strip()
            if not name:
                raise FastPipelineError("stage budget name must not be empty")
            self._stage_budgets[name] = _finite_positive(
                raw_budget, f"stage budget {name!r}"
            )
        self._active_stage: dict[str, Any] | None = None
        self._stages: list[dict[str, Any]] = []
        self._checkpoints: list[dict[str, Any]] = []

    def _now(self) -> float:
        current = float(self._clock())
        if not math.isfinite(current):
            raise FastPipelineError("monotonic clock returned a non-finite value")
        if current < self._last_now:
            raise FastPipelineError("monotonic clock moved backwards")
        self._last_now = current
        return current

    def _elapsed_at(self, current: float) -> float:
        return max(0.0, current - self._started)

    @property
    def elapsed(self) -> float:
        return self._elapsed_at(self._now())

    @property
    def remaining(self) -> float:
        return max(0.0, self.total_seconds - self.elapsed)

    @property
    def expired(self) -> bool:
        return self.elapsed > self.total_seconds

    @property
    def active_stage(self) -> str | None:
        if self._active_stage is None:
            return None
        return str(self._active_stage["name"])

    def _raise_total(self, elapsed: float, *, stage: str | None = None) -> None:
        raise DeadlineExceeded(
            f"hard deadline exceeded: {elapsed:.3f}s > {self.total_seconds:.3f}s",
            elapsed_seconds=elapsed,
            budget_seconds=self.total_seconds,
            stage=self.active_stage if stage is None else stage,
        )

    def check(self, *, stage: str | None = None) -> float:
        """Raise immediately when total/current-stage budget is exhausted."""

        current = self._now()
        elapsed = self._elapsed_at(current)
        if elapsed > self.total_seconds:
            self._raise_total(elapsed)

        active = self._active_stage
        if active is not None:
            active_name = str(active["name"])
            if stage is not None and str(stage) != active_name:
                raise FastPipelineError(
                    f"deadline stage mismatch: active {active_name!r}, got {stage!r}"
                )
            budget = active.get("budget_seconds")
            stage_elapsed = max(0.0, current - float(active["started_at"]))
            if budget is not None and stage_elapsed > float(budget):
                raise DeadlineExceeded(
                    f"stage {active_name!r} budget exceeded: "
                    f"{stage_elapsed:.3f}s > {float(budget):.3f}s",
                    elapsed_seconds=stage_elapsed,
                    budget_seconds=float(budget),
                    stage=active_name,
                )
        elif stage is not None:
            raise FastPipelineError(f"stage {stage!r} is not active")
        return max(0.0, self.total_seconds - elapsed)

    def checkpoint(
        self,
        label: str,
        *,
        stage: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record a bounded progress marker and return its detached copy."""

        normalized_label = str(label).strip()
        if not normalized_label:
            raise FastPipelineError("checkpoint label must not be empty")
        current = self._now()
        elapsed = self._elapsed_at(current)
        active_name = self.active_stage
        if stage is not None and str(stage) != active_name:
            raise FastPipelineError(
                f"deadline stage mismatch: active {active_name!r}, got {stage!r}"
            )
        row = {
            "label": normalized_label,
            "stage": active_name,
            "elapsed_seconds": round(elapsed, 6),
            "remaining_seconds": round(
                max(0.0, self.total_seconds - elapsed), 6
            ),
            "data": deepcopy(dict(data or {})),
        }
        self._checkpoints.append(row)
        # Preserve the checkpoint that proves where the budget was crossed.
        if elapsed > self.total_seconds:
            self._raise_total(elapsed)
        if self._active_stage is not None:
            budget = self._active_stage.get("budget_seconds")
            stage_elapsed = current - float(self._active_stage["started_at"])
            if budget is not None and stage_elapsed > float(budget):
                raise DeadlineExceeded(
                    f"stage {active_name!r} budget exceeded: "
                    f"{stage_elapsed:.3f}s > {float(budget):.3f}s",
                    elapsed_seconds=stage_elapsed,
                    budget_seconds=float(budget),
                    stage=active_name,
                )
        return deepcopy(row)

    @contextmanager
    def stage(
        self, name: str, *, budget_seconds: float | None = None
    ) -> Iterator["Deadline"]:
        """Measure and enforce one non-nested stage."""

        normalized_name = str(name).strip()
        if not normalized_name:
            raise FastPipelineError("stage name must not be empty")
        if self._active_stage is not None:
            raise FastPipelineError("deadline stages cannot be nested")
        if any(row["name"] == normalized_name for row in self._stages):
            raise FastPipelineError(f"deadline stage {normalized_name!r} already finished")
        configured = self._stage_budgets.get(normalized_name)
        if budget_seconds is not None:
            explicit = _finite_positive(
                budget_seconds, f"stage budget {normalized_name!r}"
            )
            if configured is not None and not math.isclose(explicit, configured):
                raise FastPipelineError(
                    f"stage {normalized_name!r} budget conflicts with configured budget"
                )
            configured = explicit
        started = self._now()
        elapsed = self._elapsed_at(started)
        if elapsed > self.total_seconds:
            self._raise_total(elapsed)
        active: dict[str, Any] = {
            "name": normalized_name,
            "started_at": started,
            "budget_seconds": configured,
        }
        self._active_stage = active
        error: BaseException | None = None
        try:
            yield self
        except BaseException as exc:
            error = exc
            raise
        finally:
            ended = self._now()
            duration = max(0.0, ended - started)
            total_elapsed = self._elapsed_at(ended)
            total_exceeded = total_elapsed > self.total_seconds
            stage_exceeded = configured is not None and duration > configured
            self._stages.append(
                {
                    "name": normalized_name,
                    "elapsed_seconds": round(duration, 6),
                    "budget_seconds": configured,
                    "status": (
                        "deadline_exceeded"
                        if isinstance(error, DeadlineExceeded)
                        or total_exceeded
                        or stage_exceeded
                        else "error"
                        if error is not None
                        else "completed"
                    ),
                }
            )
            self._active_stage = None
            if error is None:
                if total_exceeded:
                    self._raise_total(total_elapsed, stage=normalized_name)
                if stage_exceeded:
                    raise DeadlineExceeded(
                        f"stage {normalized_name!r} budget exceeded: "
                        f"{duration:.3f}s > {float(configured):.3f}s",
                        elapsed_seconds=duration,
                        budget_seconds=float(configured),
                        stage=normalized_name,
                    )

    def timings(self) -> dict[str, Any]:
        """Return current JSON-ready stage and checkpoint evidence."""

        current = self._now()
        elapsed = self._elapsed_at(current)
        stages = deepcopy(self._stages)
        if self._active_stage is not None:
            active = self._active_stage
            stages.append(
                {
                    "name": str(active["name"]),
                    "elapsed_seconds": round(
                        max(0.0, current - float(active["started_at"])), 6
                    ),
                    "budget_seconds": active.get("budget_seconds"),
                    "status": "running",
                }
            )
        return {
            "schema_version": FAST_PIPELINE_SCHEMA_VERSION,
            "clock": "monotonic",
            "total_budget_seconds": self.total_seconds,
            "elapsed_seconds": round(elapsed, 6),
            "remaining_seconds": round(
                max(0.0, self.total_seconds - elapsed), 6
            ),
            "deadline_exceeded": elapsed > self.total_seconds,
            "stages": stages,
            "checkpoints": deepcopy(self._checkpoints),
        }


def load_words_payload(
    value: str | Path | Mapping[str, Any],
) -> dict[str, Any]:
    """Load and minimally validate one existing ``words.json`` document."""

    if isinstance(value, Mapping):
        payload = deepcopy(dict(value))
    else:
        path = Path(value).expanduser().resolve()
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FastPipelineError(f"cannot load words payload {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise FastPipelineError(f"words payload root must be an object: {path}")
    if not isinstance(payload.get("words"), list) or not payload["words"]:
        raise FastPipelineError("words payload must contain a non-empty words list")
    if not isinstance(payload.get("text"), str) or not payload["text"].strip():
        raise FastPipelineError("words payload must contain the exact non-empty script text")
    _finite_positive(payload.get("audio_duration"), "audio_duration")
    return payload


def load_and_plan_beats(
    value: str | Path | Mapping[str, Any],
    *,
    beat_options: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load a timing artifact and delegate partitioning to ``beats.plan_beats``."""

    payload = load_words_payload(value)
    options = dict(beat_options or {})
    try:
        planned = plan_beats(payload, **options)
    except (TypeError, ValueError) as exc:
        raise FastPipelineError(f"cannot plan narration beats: {exc}") from exc
    return payload, [dict(row) for row in planned]


def _paragraph_spans(text: str) -> list[dict[str, Any]]:
    paragraphs: list[dict[str, Any]] = []
    for match in re.finditer(r"[^\n]+", text):
        raw = match.group(0).rstrip("\r")
        stripped = raw.strip()
        if not stripped:
            continue
        left = len(raw) - len(raw.lstrip())
        right = len(raw.rstrip())
        paragraphs.append(
            {
                "paragraph_id": len(paragraphs) + 1,
                "char_start": match.start() + left,
                "char_end": match.start() + right,
                "text": stripped,
            }
        )
    if not paragraphs:
        raise FastPipelineError("script contains no non-empty paragraphs")
    return paragraphs


def _beat_char_span(
    beat: Mapping[str, Any], words: Sequence[Mapping[str, Any]]
) -> tuple[int, int] | None:
    try:
        start_index = int(beat["word_start_index"])
        end_index = int(beat["word_end_index"])
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 <= start_index < end_index <= len(words):
        return None
    starts = [
        int(word["char_start"])
        for word in words[start_index:end_index]
        if isinstance(word.get("char_start"), int)
    ]
    ends = [
        int(word["char_end"])
        for word in words[start_index:end_index]
        if isinstance(word.get("char_end"), int)
    ]
    if not starts or not ends or max(ends) <= min(starts):
        return None
    return min(starts), max(ends)


def _assign_beats_to_paragraphs(
    beats: Sequence[Mapping[str, Any]],
    words: Sequence[Mapping[str, Any]],
    paragraphs: Sequence[Mapping[str, Any]],
    *,
    audio_duration: float,
) -> tuple[dict[int, list[dict[str, Any]]], dict[str, str]]:
    grouped = {int(row["paragraph_id"]): [] for row in paragraphs}
    methods: dict[str, str] = {}
    for raw_beat in beats:
        beat = dict(raw_beat)
        slot_id = str(beat.get("slot_id") or "")
        char_span = _beat_char_span(beat, words)
        selected: Mapping[str, Any] | None = None
        method = "char_overlap"
        if char_span is not None:
            beat_start, beat_end = char_span
            scored: list[tuple[int, int, Mapping[str, Any]]] = []
            for paragraph in paragraphs:
                overlap = max(
                    0,
                    min(beat_end, int(paragraph["char_end"]))
                    - max(beat_start, int(paragraph["char_start"])),
                )
                scored.append(
                    (-overlap, int(paragraph["paragraph_id"]), paragraph)
                )
            if scored and -min(scored)[0] > 0:
                selected = min(scored)[2]

        if selected is None:
            # Word spans can be missing in imported ASR.  This bounded fallback
            # preserves order without any text model and is explicitly reported.
            method = "time_fraction_fallback"
            midpoint = (float(beat["start"]) + float(beat["end"])) / 2.0
            fraction = min(1.0, max(0.0, midpoint / audio_duration))
            index = min(len(paragraphs) - 1, int(fraction * len(paragraphs)))
            selected = paragraphs[index]
        paragraph_id = int(selected["paragraph_id"])
        grouped[paragraph_id].append(beat)
        methods[slot_id] = method
    return grouped, methods


def _paragraph_alignment(
    ru_paragraphs: Sequence[Mapping[str, Any]],
    en_paragraphs: Sequence[Mapping[str, Any]],
    *,
    en_only_paragraphs: Sequence[int],
) -> list[tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]]:
    anchors = tuple(int(value) for value in en_only_paragraphs)
    if len(set(anchors)) != len(anchors) or any(
        value < 1 or value > len(en_paragraphs) for value in anchors
    ):
        raise FastPipelineError("EN-only paragraph anchors are invalid or duplicate")
    if len(en_paragraphs) - len(anchors) != len(ru_paragraphs):
        raise FastPipelineError(
            "paragraph count mismatch after applying EN-only anchors: "
            f"ru={len(ru_paragraphs)}, en={len(en_paragraphs)}, anchors={list(anchors)}"
        )
    anchor_set = set(anchors)
    ru_index = 0
    result: list[tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]] = []
    for en_paragraph in en_paragraphs:
        if int(en_paragraph["paragraph_id"]) in anchor_set:
            result.append((None, en_paragraph))
            continue
        result.append((ru_paragraphs[ru_index], en_paragraph))
        ru_index += 1
    if ru_index != len(ru_paragraphs):
        raise FastPipelineError("not every RU paragraph was aligned")
    return result


def _beat_paragraph_ids(
    grouped: Mapping[int, Sequence[Mapping[str, Any]]],
) -> dict[str, int]:
    output: dict[str, int] = {}
    for paragraph_id, rows in grouped.items():
        for row in rows:
            slot_id = str(row.get("slot_id") or "")
            if not slot_id or slot_id in output:
                raise FastPipelineError("beat/paragraph assignment is missing or duplicate")
            output[slot_id] = int(paragraph_id)
    return output


def _paragraph_time_spans(
    paragraphs: Sequence[Mapping[str, Any]],
    words: Sequence[Mapping[str, Any]],
    *,
    audio_duration: float,
) -> dict[int, tuple[float, float]]:
    """Map exact script paragraphs to contiguous narration time intervals."""

    starts = [0.0]
    for paragraph in paragraphs[1:]:
        char_start = int(paragraph["char_start"])
        candidates = [
            float(word["start"])
            for word in words
            if isinstance(word.get("char_start"), int)
            and int(word["char_start"]) >= char_start
        ]
        if candidates:
            boundary = min(candidates)
        else:
            # Imported timings can rarely miss a whole short line.  A bounded
            # char-fraction fallback keeps chronology without a text model.
            fraction = char_start / max(1, int(paragraphs[-1]["char_end"]))
            boundary = audio_duration * fraction
        starts.append(boundary)
    starts.append(audio_duration)
    output: dict[int, tuple[float, float]] = {}
    previous = 0.0
    for index, paragraph in enumerate(paragraphs):
        start = max(previous, min(audio_duration, float(starts[index])))
        end = max(start, min(audio_duration, float(starts[index + 1])))
        if end <= start:
            raise FastPipelineError(
                f"paragraph {paragraph['paragraph_id']} has no positive audio interval"
            )
        output[int(paragraph["paragraph_id"])] = (start, end)
        previous = end
    if abs(previous - audio_duration) > 1e-6:
        raise FastPipelineError("paragraph timing does not cover narration")
    return output


def _alignment_groups(
    alignment: Sequence[
        tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]
    ],
) -> list[dict[str, Any]]:
    """Attach an EN-only paragraph to the following paired paragraph."""

    groups: list[dict[str, Any]] = []
    pending_english: list[Mapping[str, Any]] = []
    for ru_paragraph, en_paragraph in alignment:
        if ru_paragraph is None:
            if en_paragraph is None:
                raise FastPipelineError("empty paragraph alignment row")
            pending_english.append(en_paragraph)
            continue
        english = [*pending_english]
        pending_english = []
        if en_paragraph is not None:
            english.append(en_paragraph)
        if not english:
            raise FastPipelineError("RU paragraph has no aligned English paragraph")
        groups.append(
            {
                "ru": [ru_paragraph],
                "en": english,
                "en_only_ids": [
                    int(row["paragraph_id"]) for row in english[:-1]
                ],
            }
        )
    if pending_english:
        if not groups:
            raise FastPipelineError("EN-only paragraphs have no RU neighbour")
        groups[-1]["en"].extend(pending_english)
        groups[-1]["en_only_ids"].extend(
            int(row["paragraph_id"]) for row in pending_english
        )
    return groups


def _group_interval(
    paragraphs: Sequence[Mapping[str, Any]],
    time_spans: Mapping[int, tuple[float, float]],
) -> tuple[float, float]:
    if not paragraphs:
        raise FastPipelineError("aligned paragraph group is empty")
    first_id = int(paragraphs[0]["paragraph_id"])
    last_id = int(paragraphs[-1]["paragraph_id"])
    start = float(time_spans[first_id][0])
    end = float(time_spans[last_id][1])
    if end <= start:
        raise FastPipelineError("aligned paragraph group has no duration")
    return start, end


def _beats_centered_in_interval(
    beats: Sequence[Mapping[str, Any]], start: float, end: float, *, final: bool
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for beat in beats:
        midpoint = (float(beat["start"]) + float(beat["end"])) / 2.0
        if start <= midpoint < end or (final and math.isclose(midpoint, end)):
            rows.append(dict(beat))
    return rows


def _beats_overlapping_interval(
    beats: Sequence[Mapping[str, Any]], start: float, end: float
) -> list[dict[str, Any]]:
    return [
        dict(beat)
        for beat in beats
        if min(end, float(beat["end"])) - max(start, float(beat["start"])) > 1e-9
    ]


def _paragraph_for_time(
    midpoint: float,
    paragraphs: Sequence[Mapping[str, Any]],
    spans: Mapping[int, tuple[float, float]],
) -> int:
    for index, paragraph in enumerate(paragraphs):
        paragraph_id = int(paragraph["paragraph_id"])
        start, end = spans[paragraph_id]
        if start <= midpoint < end or (
            index == len(paragraphs) - 1 and math.isclose(midpoint, end)
        ):
            return paragraph_id
    raise FastPipelineError("slot midpoint is outside paragraph timeline")


def _partition_interval(start: float, end: float, count: int) -> list[tuple[float, float]]:
    if count < 1 or end <= start:
        raise FastPipelineError("cannot partition an empty interval")
    boundaries = [start + (end - start) * index / count for index in range(count + 1)]
    output = list(zip(boundaries, boundaries[1:]))
    if any(slot_end <= slot_start for slot_start, slot_end in output):
        raise FastPipelineError("aligned language slot has no positive duration")
    return output


def _slot_text(overlaps: Sequence[Mapping[str, Any]]) -> str:
    values: list[str] = []
    for beat in overlaps:
        text = str(beat.get("text") or "").strip()
        if text and (not values or values[-1] != text):
            values.append(text)
    return " ".join(values)


def _dominant_beat_text(
    overlaps: Sequence[Mapping[str, Any]], start: float, end: float
) -> str:
    if not overlaps:
        return ""
    selected = max(
        enumerate(overlaps),
        key=lambda item: (
            min(end, float(item[1]["end"]))
            - max(start, float(item[1]["start"])),
            -item[0],
        ),
    )[1]
    return str(selected.get("text") or "").strip()


def plan_bilingual_chapter(
    ru_words: str | Path | Mapping[str, Any],
    en_words: str | Path | Mapping[str, Any],
    *,
    chapter: int,
    en_only_paragraphs: Sequence[int] | None = None,
    beat_options: Mapping[str, Any] | None = None,
    deadline: Deadline | None = None,
) -> dict[str, Any]:
    """Build one shared shot identity with separate RU/EN beat timings.

    Paragraphs are aligned by explicit source layout.  Within each aligned
    group, both continuous audio intervals are partitioned to the same visual
    count.  A source beat may therefore span two canonical slots, but no audio
    time or source-beat coverage is lost and both languages retain the exact
    same ordered canonical sequence.
    """

    if isinstance(chapter, bool) or int(chapter) != chapter or int(chapter) < 1:
        raise FastPipelineError("chapter must be a positive integer")
    chapter = int(chapter)
    if deadline is not None:
        deadline.checkpoint("bilingual_plan_start", data={"chapter": chapter})

    ru_payload, ru_beats = load_and_plan_beats(ru_words, beat_options=beat_options)
    en_payload, en_beats = load_and_plan_beats(en_words, beat_options=beat_options)
    ru_paragraphs = _paragraph_spans(str(ru_payload["text"]))
    en_paragraphs = _paragraph_spans(str(en_payload["text"]))
    anchors = (
        tuple(en_only_paragraphs)
        if en_only_paragraphs is not None
        else DEFAULT_EN_ONLY_PARAGRAPH_ANCHORS.get(chapter, ())
    )
    visual_max_seconds = _finite_positive(
        (beat_options or {}).get("max_seconds", 8.0), "visual max_seconds"
    )
    alignment = _paragraph_alignment(
        ru_paragraphs,
        en_paragraphs,
        en_only_paragraphs=anchors,
    )
    ru_grouped, ru_methods = _assign_beats_to_paragraphs(
        ru_beats,
        ru_payload["words"],
        ru_paragraphs,
        audio_duration=float(ru_payload["audio_duration"]),
    )
    en_grouped, en_methods = _assign_beats_to_paragraphs(
        en_beats,
        en_payload["words"],
        en_paragraphs,
        audio_duration=float(en_payload["audio_duration"]),
    )

    # ``grouped`` is retained as evidence that every beat was assigned from its
    # word char span.  Canonical cuts are then made from contiguous paragraph
    # time intervals, allowing a shorter language to split one source beat.
    _beat_paragraph_ids(ru_grouped)
    _beat_paragraph_ids(en_grouped)
    ru_times = _paragraph_time_spans(
        ru_paragraphs,
        ru_payload["words"],
        audio_duration=float(ru_payload["audio_duration"]),
    )
    en_times = _paragraph_time_spans(
        en_paragraphs,
        en_payload["words"],
        audio_duration=float(en_payload["audio_duration"]),
    )
    groups = _alignment_groups(alignment)

    canonical_slots: list[dict[str, Any]] = []
    language_slots: dict[str, list[dict[str, Any]]] = {"ru": [], "en": []}
    canonical_ordinal = 0
    for group_index, group in enumerate(groups):
        ru_interval = _group_interval(group["ru"], ru_times)
        en_interval = _group_interval(group["en"], en_times)
        ru_centered = _beats_centered_in_interval(
            ru_beats,
            *ru_interval,
            final=group_index == len(groups) - 1,
        )
        en_centered = _beats_centered_in_interval(
            en_beats,
            *en_interval,
            final=group_index == len(groups) - 1,
        )
        count = max(
            1,
            len(ru_centered),
            len(en_centered),
            math.ceil((ru_interval[1] - ru_interval[0]) / visual_max_seconds),
            math.ceil((en_interval[1] - en_interval[0]) / visual_max_seconds),
        )
        ru_partitions = _partition_interval(*ru_interval, count)
        en_partitions = _partition_interval(*en_interval, count)
        en_only_ids = set(int(value) for value in group["en_only_ids"])
        for position, (ru_time, en_time) in enumerate(
            zip(ru_partitions, en_partitions)
        ):
            canonical_ordinal += 1
            canonical_id = f"ch{chapter:02d}-s{canonical_ordinal:03d}"
            ru_overlaps = _beats_overlapping_interval(ru_beats, *ru_time)
            en_overlaps = _beats_overlapping_interval(en_beats, *en_time)
            if not ru_overlaps or not en_overlaps:
                raise FastPipelineError(
                    f"canonical slot {canonical_id} has no source beat overlap"
                )
            ru_midpoint = (ru_time[0] + ru_time[1]) / 2.0
            en_midpoint = (en_time[0] + en_time[1]) / 2.0
            ru_paragraph_id = _paragraph_for_time(
                ru_midpoint, ru_paragraphs, ru_times
            )
            en_paragraph_id = _paragraph_for_time(
                en_midpoint, en_paragraphs, en_times
            )
            query_en = _dominant_beat_text(en_overlaps, *en_time)
            if not query_en:
                query_en = str(
                    en_paragraphs[en_paragraph_id - 1].get("text") or ""
                ).strip()
            if not query_en:
                raise FastPipelineError(
                    f"canonical slot {canonical_id} has no English query"
                )
            source_ids = {
                "ru": [str(row["slot_id"]) for row in ru_overlaps],
                "en": [str(row["slot_id"]) for row in en_overlaps],
            }
            durations = {
                "ru": round(ru_time[1] - ru_time[0], 6),
                "en": round(en_time[1] - en_time[0], 6),
            }
            canonical = {
                "canonical_id": canonical_id,
                "shot_identity": canonical_id,
                "chapter": chapter,
                "ordinal": canonical_ordinal,
                "query_en": query_en,
                "paragraphs": {"ru": ru_paragraph_id, "en": en_paragraph_id},
                "en_only_anchor": en_paragraph_id in en_only_ids,
                "language_durations": durations,
                "source_beat_ids": source_ids,
                "required_visual_duration": max(durations.values()),
            }
            canonical_slots.append(canonical)
            for language, interval, paragraph_id, overlaps in (
                ("ru", ru_time, ru_paragraph_id, ru_overlaps),
                ("en", en_time, en_paragraph_id, en_overlaps),
            ):
                language_slots[language].append(
                    {
                        "canonical_id": canonical_id,
                        "shot_identity": canonical_id,
                        "query_en": query_en,
                        "start": round(interval[0], 6),
                        "end": round(interval[1], 6),
                        "duration": round(interval[1] - interval[0], 6),
                        "text": _slot_text(overlaps),
                        "source_beat_ids": [
                            str(row["slot_id"]) for row in overlaps
                        ],
                        "paragraph_id": paragraph_id,
                        "alignment_method": "paragraph_char_time_partition",
                    }
                )

    for language, source_beats in (("ru", ru_beats), ("en", en_beats)):
        slots = language_slots[language]
        source_ids = [str(row["slot_id"]) for row in source_beats]
        assigned: list[str] = []
        for row in slots:
            for source in row["source_beat_ids"]:
                if not assigned or assigned[-1] != source:
                    assigned.append(source)
        if assigned != source_ids:
            raise FastPipelineError(
                f"{language} beat alignment changed source order or coverage"
            )
        if slots:
            if abs(float(slots[0]["start"])) > 1e-6:
                raise FastPipelineError(f"{language} slots do not start at zero")
            duration = float((ru_payload if language == "ru" else en_payload)["audio_duration"])
            if abs(float(slots[-1]["end"]) - duration) > 1.0 / 25.0 + 1e-6:
                raise FastPipelineError(f"{language} slots do not cover narration end")
            for previous, current in zip(slots, slots[1:]):
                if abs(float(previous["end"]) - float(current["start"])) > 1e-6:
                    raise FastPipelineError(f"{language} slots have a gap or overlap")

    canonical_ids = [row["canonical_id"] for row in canonical_slots]
    for language in ("ru", "en"):
        slots = language_slots[language]
        if [row["canonical_id"] for row in slots] != canonical_ids:
            raise FastPipelineError(
                f"{language} canonical sequence differs from shared visual sequence"
            )
        if any(float(row["duration"]) <= 0 for row in slots):
            raise FastPipelineError(f"{language} contains a zero-duration visual slot")

    output = {
        "schema_version": FAST_PIPELINE_SCHEMA_VERSION,
        "chapter": chapter,
        "alignment": {
            "method": "paragraph_char_span_v1",
            "en_only_paragraphs": list(anchors),
            "ru_paragraph_count": len(ru_paragraphs),
            "en_paragraph_count": len(en_paragraphs),
            "char_span_fallback_beat_counts": {
                "ru": sum(
                    method == "time_fraction_fallback"
                    for method in ru_methods.values()
                ),
                "en": sum(
                    method == "time_fraction_fallback"
                    for method in en_methods.values()
                ),
            },
        },
        "canonical_slots": canonical_slots,
        "language_slots": language_slots,
        "language_audio_durations": {
            "ru": round(float(ru_payload["audio_duration"]), 6),
            "en": round(float(en_payload["audio_duration"]), 6),
        },
        "language_beat_counts": {"ru": len(ru_beats), "en": len(en_beats)},
    }
    if deadline is not None:
        deadline.checkpoint(
            "bilingual_plan_complete",
            data={
                "chapter": chapter,
                "canonical_slot_count": len(canonical_slots),
                "ru_beat_count": len(ru_beats),
                "en_beat_count": len(en_beats),
            },
        )
    return output


def _normalised_matrix(value: Any, label: str) -> np.ndarray:
    matrix = np.asarray(value, dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[0] < 1 or matrix.shape[1] < 1:
        raise FastPipelineError(f"{label} must be a non-empty 2D matrix")
    if not np.isfinite(matrix).all():
        raise FastPipelineError(f"{label} contains non-finite values")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if np.any(norms <= 0):
        raise FastPipelineError(f"{label} contains a zero vector")
    return matrix / norms


def cosine_score_matrix(query_vectors: Any, candidate_vectors: Any) -> np.ndarray:
    """Return all pairwise cosine scores in one matrix multiplication."""

    queries = _normalised_matrix(query_vectors, "query_vectors")
    candidates = _normalised_matrix(candidate_vectors, "candidate_vectors")
    if queries.shape[1] != candidates.shape[1]:
        raise FastPipelineError("query/candidate embedding dimensions differ")
    return queries @ candidates.T


def vectorized_top_k(
    query_vectors: Any,
    candidate_vectors: Any,
    candidates: Sequence[Mapping[str, Any]],
    *,
    top_k: int = MAX_TOP_K,
) -> list[list[dict[str, Any]]]:
    """Rank at most five catalog rows per query with deterministic tie order."""

    top_k = _bounded_integer(top_k, "top_k", MAX_TOP_K)
    rows = [dict(row) for row in candidates]
    if not rows:
        raise FastPipelineError("candidate catalog is empty")
    scores = cosine_score_matrix(query_vectors, candidate_vectors)
    if scores.shape[1] != len(rows):
        raise FastPipelineError("candidate/vector count mismatch")
    output: list[list[dict[str, Any]]] = []
    limit = min(top_k, len(rows))
    for score_row in scores:
        order = np.argsort(-score_row, kind="stable")[:limit]
        output.append(
            [
                {
                    **rows[int(index)],
                    "candidate_index": int(index),
                    "cosine_score": float(score_row[int(index)]),
                    "retrieval_rank": rank,
                }
                for rank, index in enumerate(order)
            ]
        )
    return output


def _source_id(row: Mapping[str, Any]) -> str:
    return str(
        row.get("source_id")
        or row.get("source_asset_id")
        or row.get("donor_id")
        or ""
    )


def _candidate_identity(row: Mapping[str, Any]) -> str:
    explicit = row.get("candidate_id") or row.get("clip_id")
    if explicit is not None and str(explicit):
        return str(explicit)
    source_time = row.get("source_time")
    if source_time is not None:
        try:
            normalized_time = float(source_time)
        except (TypeError, ValueError) as exc:
            raise FastPipelineError("candidate has invalid source_time") from exc
        if not math.isfinite(normalized_time) or not _source_id(row):
            raise FastPipelineError("candidate has invalid source_time identity")
        return f"{_source_id(row)}:{normalized_time:.6f}"
    try:
        return (
            f"{_source_id(row)}:{float(row['source_in']):.6f}-"
            f"{float(row['source_out']):.6f}"
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FastPipelineError("candidate lacks stable identity/source range") from exc


def _ranges_overlap(
    first: Mapping[str, Any], second: Mapping[str, Any], tolerance: float = 0.08
) -> bool:
    if _source_id(first) != _source_id(second):
        return False
    if not all(key in first and key in second for key in ("source_in", "source_out")):
        return _candidate_identity(first) == _candidate_identity(second)
    try:
        overlap = min(float(first["source_out"]), float(second["source_out"])) - max(
            float(first["source_in"]), float(second["source_in"])
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FastPipelineError("candidate has invalid source range") from exc
    return overlap > tolerance


def rank_diverse_top_k(
    query_vectors: Any,
    candidate_vectors: Any,
    candidates: Sequence[Mapping[str, Any]],
    *,
    canonical_ids: Sequence[str] | None = None,
    top_k: int = MAX_TOP_K,
    same_source_penalty: float = 0.04,
    anti_repeat_window: int = 3,
    deadline: Deadline | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return bounded shortlists with local source diversity and anti-repeat.

    Candidate embeddings are scored once.  Selection then proceeds in canonical
    order: exact identities and overlapping source ranges used by a recent
    primary are ineligible, while another range from the same donor receives a
    small score penalty.  The function never asks for a wider second search.
    """

    top_k = _bounded_integer(top_k, "top_k", MAX_TOP_K)
    if isinstance(anti_repeat_window, bool) or int(anti_repeat_window) != anti_repeat_window:
        raise FastPipelineError(
            f"anti_repeat_window must be an integer in 0..{MAX_ANTI_REPEAT_WINDOW}"
        )
    anti_repeat_window = int(anti_repeat_window)
    if not 0 <= anti_repeat_window <= MAX_ANTI_REPEAT_WINDOW:
        raise FastPipelineError(
            f"anti_repeat_window must be an integer in 0..{MAX_ANTI_REPEAT_WINDOW}"
        )
    try:
        same_source_penalty = float(same_source_penalty)
    except (TypeError, ValueError) as exc:
        raise FastPipelineError("same_source_penalty must be finite and non-negative") from exc
    if not math.isfinite(same_source_penalty) or same_source_penalty < 0:
        raise FastPipelineError("same_source_penalty must be finite and non-negative")

    rows = [dict(row) for row in candidates]
    scores = cosine_score_matrix(query_vectors, candidate_vectors)
    if scores.shape[1] != len(rows):
        raise FastPipelineError("candidate/vector count mismatch")
    if canonical_ids is None:
        ids = [f"s{index + 1:03d}" for index in range(scores.shape[0])]
    else:
        ids = [str(value) for value in canonical_ids]
    if len(ids) != scores.shape[0] or len(set(ids)) != len(ids) or any(not value for value in ids):
        raise FastPipelineError("canonical_ids must uniquely match query rows")

    recent_primaries: list[dict[str, Any]] = []
    output: dict[str, list[dict[str, Any]]] = {}
    for slot_index, canonical_id in enumerate(ids):
        if deadline is not None:
            deadline.check()
        scored: list[tuple[float, int, float]] = []
        recent = recent_primaries[-anti_repeat_window:] if anti_repeat_window else []
        recent_ids = {_candidate_identity(row) for row in recent}
        recent_sources = {_source_id(row) for row in recent}
        for candidate_index, candidate in enumerate(rows):
            if bool(candidate.get("blocked")):
                continue
            identity = _candidate_identity(candidate)
            if identity in recent_ids or any(
                _ranges_overlap(candidate, previous) for previous in recent
            ):
                continue
            penalty = (
                same_source_penalty if _source_id(candidate) in recent_sources else 0.0
            )
            score = float(scores[slot_index, candidate_index]) - penalty
            scored.append((score, candidate_index, penalty))
        # Candidate index is the stable tie breaker.  Build the bounded prefix
        # from the best row of every available donor *before* filling spare
        # positions globally.  Looking only at the global top five can return
        # five near-duplicate ranges from one donor even when the full scored
        # set contains clean alternatives.  Since exact selection tries only
        # primary + two fallbacks, the per-source prefix also makes those three
        # attempts source-diverse whenever at least three donors are eligible.
        scored.sort(key=lambda row: (-row[0], row[1]))
        per_source_maxima: dict[str, tuple[float, int, float]] = {}
        for scored_row in scored:
            source_id = _source_id(rows[scored_row[1]])
            if source_id not in per_source_maxima:
                per_source_maxima[source_id] = scored_row
        diverse_prefix = sorted(
            per_source_maxima.values(), key=lambda row: (-row[0], row[1])
        )[:top_k]
        selected_indices = {row[1] for row in diverse_prefix}
        ordered = list(diverse_prefix)
        if len(ordered) < top_k:
            ordered.extend(
                row
                for row in scored
                if row[1] not in selected_indices
            )
        ordered = ordered[:top_k]
        shortlist = []
        for rank, (selection_score, candidate_index, penalty) in enumerate(ordered):
            shortlist.append(
                {
                    **rows[candidate_index],
                    "candidate_index": candidate_index,
                    "candidate_id": _candidate_identity(rows[candidate_index]),
                    "cosine_score": float(scores[slot_index, candidate_index]),
                    "diversity_penalty": penalty,
                    "selection_score": selection_score,
                    "retrieval_rank": rank,
                }
            )
        if not shortlist:
            raise SelectionExhausted(
                f"canonical slot {canonical_id} has no non-repeating candidate"
            )
        output[canonical_id] = shortlist
        recent_primaries.append(shortlist[0])
    if deadline is not None:
        deadline.checkpoint(
            "vector_selection_complete",
            data={"slot_count": len(ids), "top_k": top_k},
        )
    return output


def _exact_result(value: Any) -> tuple[bool, dict[str, Any]]:
    if isinstance(value, (bool, np.bool_)):
        return bool(value), {"accepted": bool(value)}
    if not isinstance(value, Mapping):
        raise FastPipelineError("exact_checker must return bool or mapping")
    evidence = deepcopy(dict(value))
    flags = [key for key in ("accepted", "clean", "ok", "passed") if key in evidence]
    if not flags:
        raise FastPipelineError("exact_checker mapping lacks accepted/clean/ok/passed")
    accepted = bool(evidence[flags[0]])
    if any(bool(evidence[key]) != accepted for key in flags[1:]):
        raise FastPipelineError("exact_checker mapping contains contradictory verdicts")
    evidence["accepted"] = accepted
    return accepted, evidence


def _checked_candidate(
    candidate: Mapping[str, Any], evidence: Mapping[str, Any]
) -> dict[str, Any]:
    result = deepcopy(dict(candidate))
    replacement = evidence.get("candidate")
    if replacement is not None:
        if not isinstance(replacement, Mapping):
            raise FastPipelineError("exact_checker candidate replacement must be a mapping")
        result.update(deepcopy(dict(replacement)))
    for key in (
        "source_id",
        "source_asset_id",
        "source_path",
        "source_in",
        "source_out",
        "safe_source_out",
    ):
        if key in evidence:
            result[key] = deepcopy(evidence[key])
    try:
        source_in = float(result["source_in"])
        source_out = float(result["source_out"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FastPipelineError("accepted exact candidate has invalid source time") from exc
    if not math.isfinite(source_in) or not math.isfinite(source_out) or source_out <= source_in:
        raise FastPipelineError("accepted exact candidate has invalid source time")
    if not _source_id(result):
        raise FastPipelineError("accepted exact candidate has no source identity")
    result["source_in"] = source_in
    result["source_out"] = source_out
    result.setdefault("candidate_id", _candidate_identity(result))
    return result


def select_lazy_candidates(
    canonical_slots: Sequence[Mapping[str, Any]],
    ranked_candidates: Mapping[str, Sequence[Mapping[str, Any]]],
    exact_checker: Callable[[Mapping[str, Any], Mapping[str, Any]], Any],
    *,
    max_fallbacks: int = MAX_FALLBACKS,
    anti_repeat_window: int = MAX_ANTI_REPEAT_WINDOW,
    deadline: Deadline | None = None,
) -> dict[str, Any]:
    """Exact-check primary and at most two fallbacks for each canonical slot.

    ``exact_checker(slot, candidate)`` returns either a boolean or a mapping
    containing one of ``accepted``, ``clean``, ``ok`` or ``passed``.  Accepted
    evidence may override exact ``source_in``/``source_out`` or provide a
    replacement ``candidate`` mapping.  Exceptions are technical failures and
    propagate immediately; they are never converted into another search.
    """

    max_fallbacks = _bounded_integer(
        max_fallbacks + 1, "primary_plus_fallback_count", MAX_FALLBACKS + 1
    ) - 1
    if (
        isinstance(anti_repeat_window, bool)
        or int(anti_repeat_window) != anti_repeat_window
        or not 0 <= int(anti_repeat_window) <= MAX_ANTI_REPEAT_WINDOW
    ):
        raise FastPipelineError(
            f"anti_repeat_window must be an integer in 0..{MAX_ANTI_REPEAT_WINDOW}"
        )
    anti_repeat_window = int(anti_repeat_window)
    if not callable(exact_checker):
        raise FastPipelineError("exact_checker must be callable")
    slots = [dict(row) for row in canonical_slots]
    ids = [str(row.get("canonical_id") or "") for row in slots]
    if any(not value for value in ids) or len(set(ids)) != len(ids):
        raise FastPipelineError("canonical slots must have unique non-empty IDs")
    extra = sorted(set(str(key) for key in ranked_candidates) - set(ids))
    if extra:
        raise FastPipelineError(f"ranked candidates contain unknown slots: {extra[:3]}")

    selections: list[dict[str, Any]] = []
    used_exact: list[dict[str, Any]] = []
    exact_attempt_count = 0
    maximum_attempts = 1 + max_fallbacks
    for slot in slots:
        canonical_id = str(slot["canonical_id"])
        rows = [dict(row) for row in ranked_candidates.get(canonical_id, ())]
        if not rows:
            raise SelectionExhausted(f"canonical slot {canonical_id} has no shortlist")
        if len(rows) > MAX_TOP_K:
            raise FastPipelineError(
                f"canonical slot {canonical_id} shortlist exceeds hard top_k={MAX_TOP_K}"
            )
        attempts: list[dict[str, Any]] = []
        selected: dict[str, Any] | None = None
        selected_evidence: dict[str, Any] | None = None
        selected_rank = -1
        for rank, candidate in enumerate(rows[:maximum_attempts]):
            if deadline is not None:
                deadline.check()
            exact_attempt_count += 1
            accepted, evidence = _exact_result(exact_checker(slot, deepcopy(candidate)))
            attempt = {
                "rank": rank,
                "candidate_id": _candidate_identity(candidate),
                "accepted": accepted,
                "reason": str(evidence.get("reason") or ""),
            }
            attempts.append(attempt)
            if not accepted:
                continue
            checked = _checked_candidate(candidate, evidence)
            recent_exact = (
                used_exact[-anti_repeat_window:] if anti_repeat_window else []
            )
            if any(_ranges_overlap(checked, previous) for previous in recent_exact):
                attempt["accepted"] = False
                attempt["reason"] = "exact_range_repeats_previous_selection"
                continue
            required = float(slot.get("required_visual_duration") or 0.0)
            if required > 0 and float(checked["source_out"]) - float(checked["source_in"]) + 1e-6 < required:
                attempt["accepted"] = False
                attempt["reason"] = "exact_range_shorter_than_language_duration"
                continue
            selected = checked
            selected_evidence = evidence
            selected_rank = rank
            break
        if selected is None or selected_evidence is None:
            raise SelectionExhausted(
                f"canonical slot {canonical_id} exhausted primary + "
                f"{max_fallbacks} fallback(s)"
            )
        used_exact.append(selected)
        source_in = float(selected["source_in"])
        source_out = float(selected["source_out"])
        selections.append(
            {
                "canonical_id": canonical_id,
                "shot_identity": str(slot.get("shot_identity") or canonical_id),
                "selected_rank": selected_rank,
                "used_fallbacks": selected_rank,
                "candidate": selected,
                "source_time": {
                    "source_id": _source_id(selected),
                    "source_in": source_in,
                    "source_out": source_out,
                    "duration": round(source_out - source_in, 6),
                },
                "exact_check": deepcopy(selected_evidence),
                "attempts": attempts,
            }
        )
        if deadline is not None:
            deadline.checkpoint(
                "exact_slot_selected",
                data={
                    "canonical_id": canonical_id,
                    "selected_rank": selected_rank,
                    "attempt_count": len(attempts),
                },
            )
    return {
        "schema_version": FAST_PIPELINE_SCHEMA_VERSION,
        "max_top_k": MAX_TOP_K,
        "max_fallbacks": max_fallbacks,
        "anti_repeat_window": anti_repeat_window,
        "selection_count": len(selections),
        "exact_attempt_count": exact_attempt_count,
        "selections": selections,
        "selection_by_canonical_id": {
            row["canonical_id"]: deepcopy(row) for row in selections
        },
    }


__all__ = [
    "DEFAULT_EN_ONLY_PARAGRAPH_ANCHORS",
    "Deadline",
    "DeadlineExceeded",
    "FAST_PIPELINE_SCHEMA_VERSION",
    "FastPipelineError",
    "MAX_FALLBACKS",
    "MAX_ANTI_REPEAT_WINDOW",
    "MAX_TOP_K",
    "SelectionExhausted",
    "cosine_score_matrix",
    "load_and_plan_beats",
    "load_words_payload",
    "plan_bilingual_chapter",
    "rank_diverse_top_k",
    "select_lazy_candidates",
    "vectorized_top_k",
]
