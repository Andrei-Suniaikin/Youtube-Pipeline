from __future__ import annotations

import numpy as np
import pytest

from semantic_montage.pipeline import SelectionError, assemble_timeline, rank_beats


def _clip(index: int, start: float, end: float) -> dict:
    return {
        "clip_id": f"c{index}",
        "source_id": "donor",
        "source_path": "/tmp/donor.mp4",
        "source_in": start,
        "source_out": end,
        "thumbnail": f"/tmp/{index}.jpg",
    }


def test_assemble_timeline_honours_duration_and_no_overlap():
    beats = [
        {"slot_id": "b001", "start": 0.0, "end": 6.0, "text": "one"},
        {"slot_id": "b002", "start": 6.0, "end": 12.0, "text": "two"},
    ]
    briefs = [
        {"slot_id": "b001", "query_en": "water"},
        {"slot_id": "b002", "query_en": "forest"},
    ]
    first = _clip(1, 0.0, 8.0)
    collision = _clip(2, 4.0, 12.0)
    fallback = _clip(3, 20.0, 28.0)
    timeline = assemble_timeline(
        beats,
        briefs,
        [[first], [collision, fallback]],
        [
            {"chosen_index": 0, "status": "resolved", "verdict": "exact"},
            {"chosen_index": 0, "status": "resolved", "verdict": "relevant"},
        ],
    )
    assert timeline["slots"][0]["selected_clip"]["source_out"] == 6.0
    assert timeline["slots"][1]["selected_clip"]["clip_id"] == "c3"
    assert timeline["slots"][1]["status"] == "needs_review"


def test_assemble_rejects_missing_global_fallback():
    beat = [{"slot_id": "b001", "start": 0.0, "end": 8.0, "text": "one"}]
    brief = [{"slot_id": "b001", "query_en": "water"}]
    with pytest.raises(SelectionError, match="all candidates collide|duration"):
        assemble_timeline(
            beat,
            brief,
            [[_clip(1, 0.0, 4.0)]],
            [{"chosen_index": 0, "status": "resolved", "verdict": "exact"}],
        )


def test_assemble_marks_micro_tail_after_internal_cut_for_safe_hold():
    beat = [{"slot_id": "b001", "start": 0.0, "end": 6.0, "text": "one"}]
    brief = [{"slot_id": "b001", "query_en": "water"}]
    candidate = _clip(1, 0.0, 8.0)
    candidate["shot_cuts"] = [3.0, 5.98]

    timeline = assemble_timeline(
        beat,
        brief,
        [[candidate]],
        [{"chosen_index": 0, "status": "resolved", "verdict": "exact"}],
        fps=25.0,
    )

    selected = timeline["slots"][0]["selected_clip"]
    assert selected["source_out"] == 6.0
    assert selected["shot_end"] == 5.98
    assert selected["safe_source_out"] == 5.9
    assert selected["held_tail_seconds"] == 0.1
    assert selected["safe_end_reason"] == "micro_tail_after_shot_cut"


def test_assemble_keeps_meaningful_tail_after_internal_cut():
    beat = [{"slot_id": "b001", "start": 0.0, "end": 6.0, "text": "one"}]
    brief = [{"slot_id": "b001", "query_en": "water"}]
    candidate = _clip(1, 0.0, 8.0)
    candidate["shot_cuts"] = [5.1]

    timeline = assemble_timeline(
        beat,
        brief,
        [[candidate]],
        [{"chosen_index": 0, "status": "resolved", "verdict": "exact"}],
        fps=25.0,
    )

    selected = timeline["slots"][0]["selected_clip"]
    assert "safe_source_out" not in selected


def test_rank_beats_checks_slot_order(monkeypatch):
    beat = [{"slot_id": "b001", "start": 0.0, "end": 6.0}]
    brief = [{"slot_id": "wrong", "query_en": "water"}]
    with pytest.raises(SelectionError, match="slot order"):
        rank_beats(beat, brief, [_clip(1, 0.0, 8.0)], np.ones((1, 2)))
