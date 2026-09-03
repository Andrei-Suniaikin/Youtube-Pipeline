import json
import sys
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import render_worker as worker  # noqa: E402


def _words(tokens, *, start=0.0, step=0.35):
    return [
        {
            "word": token,
            "start": start + index * step,
            "end": start + index * step + step * 0.8,
        }
        for index, token in enumerate(tokens)
    ]


def _write_scenes(project, scene_texts):
    googler = project / "googler"
    googler.mkdir(parents=True)
    (googler / "scenes.json").write_text(
        json.dumps([{"text_segment": text} for text in scene_texts]),
        encoding="utf-8",
    )


def test_alignment_owns_initial_silence_and_reaches_exact_audio_end():
    scenes = [
        "amber falcon crosses canyon",
        "silver river bends northward",
        "evening stars cover temple",
    ]
    tokens = " ".join(scenes).split()

    result = worker._align_durations_v3(
        _words(tokens, start=2.0, step=0.5),
        scenes,
        audio_duration=12.0,
        min_interval=1 / 30,
    )

    assert result["ok"] is True
    assert result["boundaries"][0] == 0.0
    assert result["boundaries"][-1] == 12.0
    assert sum(result["durations"]) == pytest.approx(12.0, abs=1 / 30)
    assert all(duration >= 1 / 30 - 1e-8 for duration in result["durations"])
    # The two seconds before Whisper's first word belong to image 001.
    assert result["durations"][0] > 2.0


def test_noisy_but_complete_asr_keeps_enough_coverage_to_sync():
    scenes = [
        "amber falcon crosses canyon",
        "silver river bends northward",
        "workers raise limestone blocks",
        "evening stars cover temple",
    ]
    noisy = (
        "amber falcon crosses canyon "
        "silver stream bends northward "
        "workers loudly raise limestone blocks "
        "evening stars cover temple"
    ).split()

    result = worker._align_durations_v3(
        _words(noisy, start=0.4), scenes, audio_duration=9.0
    )

    assert result["ok"] is True
    assert result["metrics"]["script_match_coverage"] < 1.0
    assert result["metrics"]["script_reach"] >= 0.9
    assert sum(result["durations"]) == pytest.approx(9.0, abs=1 / 30)


def test_missing_middle_anchors_are_interpolated_by_text_weight():
    scenes = [
        "opening cedar signal",
        "tiny gap",
        (
            "enormous aqueduct construction carries many heavy stones across "
            "the desert while builders reinforce every arch"
        ),
        "closing obsidian marker",
    ]
    # Keep the ASR complete in length but deliberately corrupt the two middle
    # scenes. Only opening/closing provide exact anchors.
    noisy_middle = [
        "opening", "cedar", "signal",
        "brief", "noise",
        "massive", "structure", "assembly", "moves", "large", "material",
        "through", "arid", "ground", "as", "laborers", "support", "each", "span",
        "closing", "obsidian", "marker",
    ]

    result = worker._align_durations_v3(
        _words(noisy_middle, start=0.0, step=0.5),
        scenes,
        audio_duration=14.0,
    )

    assert result["ok"] is True
    # Equal-by-scene interpolation would give the two missing scenes comparable
    # time. Text-weighting must reserve much more for the long third scene.
    assert result["durations"][2] > result["durations"][1] * 3
    assert sum(result["durations"]) == pytest.approx(14.0, abs=1 / 30)


def test_truncated_egypt_shape_is_rejected_instead_of_stretched_to_tail():
    scenes = [f"unique{i:03d} sandstone worker action" for i in range(161)]
    truncated_tokens = " ".join(scenes[:91]).split()

    result = worker._align_durations_v3(
        _words(truncated_tokens, start=0.0, step=0.08),
        scenes,
        audio_duration=35.0,
    )

    assert result["ok"] is False
    assert result["durations"] == []
    assert result["metrics"]["scene_reach"] < worker._SYNC_MIN_SCENE_REACH
    assert (
        "охват" in result["reason"]
        or "составляет" in result["reason"]
    )


def test_eighty_percent_tail_truncation_is_rejected():
    scenes = [f"unique{i:03d} sandstone worker action" for i in range(100)]
    truncated_tokens = " ".join(scenes[:80]).split()

    result = worker._align_durations_v3(
        _words(truncated_tokens, start=0.0, step=0.08),
        scenes,
        audio_duration=28.0,
    )

    assert result["ok"] is False
    # A raw ASR/script length ratio of 80% is not enough: q95 sustained reach
    # shows that the real matching prefix ends well before the script tail.
    assert result["metrics"]["asr_char_ratio"] == pytest.approx(0.8)
    assert result["metrics"]["script_reach"] < worker._SYNC_MIN_SCRIPT_REACH


def test_short_script_truncation_still_requires_tail_and_length_coverage():
    scenes = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]

    result = worker._align_durations_v3(
        _words(["alpha"], start=0.0, step=0.5),
        scenes,
        audio_duration=12.0,
    )

    assert result["ok"] is False
    assert result["durations"] == []
    assert result["metrics"]["script_reach"] == pytest.approx(1 / 6)
    assert result["metrics"]["scene_reach"] == pytest.approx(1 / 6)
    assert result["metrics"]["asr_char_ratio"] < worker._SYNC_MIN_ASR_CHAR_RATIO


def test_short_complete_script_still_passes_coverage_contract():
    scenes = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]

    result = worker._align_durations_v3(
        _words(scenes, start=0.0, step=0.5),
        scenes,
        audio_duration=6.0,
    )

    assert result["ok"] is True
    assert result["metrics"]["script_match_coverage"] == pytest.approx(1.0)
    assert sum(result["durations"]) == pytest.approx(6.0, abs=1 / 30)


def test_complete_cjk_asr_accepts_character_or_phrase_timestamp_units():
    scenes = [
        "古代埃及",
        "沙漠炎热",
        "尼罗河水",
        "泥砖房屋",
        "白色亚麻",
        "夜间通风",
        "遮阳庭院",
        "储存粮食",
        "棕榈树荫",
        "智慧生存",
    ]
    # Whisper backends may emit CJK as individual characters even though the
    # script has no spaces and stores one phrase per scene.
    words = _words([char for scene in scenes for char in scene], step=0.2)

    result = worker._align_durations_v3(words, scenes, audio_duration=10.0)

    assert result["ok"] is True
    assert result["metrics"]["script_match_coverage"] == pytest.approx(1.0)
    assert result["metrics"]["script_reach"] >= 0.95
    assert sum(result["durations"]) == pytest.approx(10.0, abs=1 / 30)


def test_cjk_tokenizer_normalizes_decomposed_graphemes():
    assert worker._alignment_tokens("가") == ["가"]
    assert worker._alignment_tokens("가") == worker._alignment_tokens("가")


def test_renderer_does_not_fallback_to_count_sync_after_truncation(
        tmp_path, monkeypatch):
    scenes = [f"unique{i:03d} sandstone worker action" for i in range(20)]
    _write_scenes(tmp_path, scenes)
    truncated = _words(" ".join(scenes[:10]).split(), start=0.0, step=0.1)
    renderer = worker.VideoRenderer(str(tmp_path), {"target_fps": 30})
    monkeypatch.setattr(renderer, "_whisper_words", lambda *_args, **_kwargs: truncated)
    monkeypatch.setattr(renderer, "log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        worker,
        "_align_durations",
        lambda *_args, **_kwargs: pytest.fail("truncated ASR reached v1 fallback"),
    )
    timeline = [
        {"type": "image", "source": f"{index:03d}.jpg", "duration": 3.0}
        for index in range(1, 21)
    ]

    with pytest.raises(worker.NarrationCoverageError, match="не покрывает сценарий"):
        renderer._compute_synced_image_durations(
            "unused.mp3", timeline, audio_duration=12.0
        )


def test_pure_mapped_image_timeline_preserves_whisper_boundaries_and_exact_sum(
        tmp_path, monkeypatch):
    scenes = [
        "amber falcon crosses canyon",
        "silver river bends northward",
        "evening stars cover temple",
    ]
    _write_scenes(tmp_path, scenes)
    words = []
    starts = [2.0, 5.0, 8.0]
    for scene, start in zip(scenes, starts):
        words.extend(_words(scene.split(), start=start, step=0.2))

    renderer = worker.VideoRenderer(str(tmp_path), {"target_fps": 30})
    monkeypatch.setattr(renderer, "_whisper_words", lambda *_args, **_kwargs: words)
    monkeypatch.setattr(renderer, "log", lambda *_args, **_kwargs: None)
    timeline = [
        {
            "type": "image",
            "source": str(tmp_path / f"{index:03d}.jpg"),
            "orig_path": str(tmp_path / f"{index:03d}.jpg"),
            "duration": 99.0,
        }
        for index in range(1, 4)
    ]

    synced = renderer._compute_synced_image_durations(
        "unused.mp3", timeline, audio_duration=12.0
    )

    assert synced is not None
    durations = [segment["duration"] for segment in synced]
    assert durations == pytest.approx([5.0, 3.0, 4.0], abs=1 / 30)
    assert sum(durations) == pytest.approx(12.0, abs=1 / 30)
    assert all(segment.get("exact") is True for segment in synced)

    locked = renderer._lock_frames_to_timeline(synced)
    assert sum(segment["frames"] for segment in locked) == 12 * 30
    assert sum(segment["duration"] for segment in locked) == pytest.approx(12.0)


def test_unmapped_native_video_rejects_sync_instead_of_moving_whisper_anchors(
        tmp_path, monkeypatch):
    scenes = [
        "amber falcon crosses canyon",
        "silver river bends northward",
    ]
    _write_scenes(tmp_path, scenes)
    words = [
        {"word": "amber", "start": 0.0, "end": 0.2},
        {"word": "falcon", "start": 0.2, "end": 0.4},
        {"word": "crosses", "start": 0.4, "end": 0.6},
        {"word": "canyon", "start": 0.6, "end": 0.8},
        {"word": "silver", "start": 6.0, "end": 6.2},
        {"word": "river", "start": 6.2, "end": 6.4},
        {"word": "bends", "start": 6.4, "end": 6.6},
        {"word": "northward", "start": 6.6, "end": 6.8},
    ]
    renderer = worker.VideoRenderer(str(tmp_path), {"target_fps": 30})
    monkeypatch.setattr(renderer, "_whisper_words", lambda *_args, **_kwargs: words)
    messages = []
    monkeypatch.setattr(renderer, "log", lambda message: messages.append(message))
    timeline = [
        {"type": "image", "source": "001.jpg", "orig_path": "001.jpg", "duration": 99.0},
        {"type": "video", "source": "stock.mp4", "duration": 2.0},
        {"type": "image", "source": "002.jpg", "orig_path": "002.jpg", "duration": 99.0},
    ]

    synced = renderer._compute_synced_image_durations(
        "unused.mp3", timeline, audio_duration=10.0
    )

    assert synced is None
    assert [item["duration"] for item in timeline] == [99.0, 2.0, 99.0]
    assert any("native-видео" in message and "uniform" in message for message in messages)


@pytest.mark.parametrize("failure_mode", ["exception", "missing_output"])
def test_mapped_video_retime_failure_is_terminal_and_keeps_target_duration(
        tmp_path, monkeypatch, failure_mode):
    source = tmp_path / "001_video.mp4"
    source.write_bytes(b"x" * 2048)
    renderer = worker.VideoRenderer(str(tmp_path), {"target_fps": 30})
    monkeypatch.setattr(renderer, "log", lambda *_args, **_kwargs: None)

    def fail_ffmpeg(_command):
        if failure_mode == "exception":
            raise RuntimeError("ffmpeg failed")
        return None

    monkeypatch.setattr(renderer, "run_ffmpeg_command", fail_ffmpeg)
    timeline = [
        {
            "type": "video",
            "source": str(source),
            "orig_path": str(source),
            "duration": 6.0,
            "native_duration": 2.0,
            "retime": True,
            "exact": True,
        },
        {"type": "image", "source": "002.jpg", "duration": 4.0, "exact": True},
    ]

    expected = "ffmpeg failed" if failure_mode == "exception" else "не создал"
    with pytest.raises(worker.SyncedVideoRetimeError, match=expected):
        renderer._retime_synced_videos(timeline, str(tmp_path), {})

    assert timeline[0]["duration"] == 6.0
    assert sum(segment["duration"] for segment in timeline) == pytest.approx(10.0)


@pytest.mark.parametrize(
    "name",
    ["tmp", "render_tmp", "voice.spd.tmp.mp3", "scene.TMP.mp4", ".hidden.mp3"],
)
def test_render_scan_rejects_temporary_entries(name):
    assert worker._is_render_scan_temporary_name(name) is True


def test_render_scan_keeps_normal_media_names():
    assert worker._is_render_scan_temporary_name("001.mp3") is False
    assert worker._is_render_scan_temporary_name("001_video.mp4") is False
