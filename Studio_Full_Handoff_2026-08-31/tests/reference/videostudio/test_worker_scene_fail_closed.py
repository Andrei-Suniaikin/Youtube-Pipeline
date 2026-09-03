"""Fail-closed контракт планировщика сцен (полностью без сети)."""

import json
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import googler_worker as worker  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def scene(subject="a stone bridge", visible_text=None):
    return {
        "subject": subject,
        "visual_prompt": f"{subject} above a quiet river at dawn",
        "camera_angle": "Wide Shot",
        "subject_type": "environment",
        "visible_text": [] if visible_text is None else visible_text,
    }


def ok_response(value=None):
    value = scene() if value is None else value
    return FakeResponse(
        payload={
            "choices": [{
                "message": {"content": json.dumps(value)},
                "finish_reason": "stop",
            }]
        }
    )


@pytest.fixture(autouse=True)
def isolated_scene_planner(monkeypatch):
    """Убирает все вспомогательные LLM/cache/sleep эффекты из focused-тестов."""
    monkeypatch.setattr(worker, "_prompt_provider", lambda: "openai")
    monkeypatch.setattr(worker, "generate_visual_context", lambda _text: "")
    monkeypatch.setattr(worker, "_prompt_cache_get", lambda _key: None)
    monkeypatch.setattr(worker, "_prompt_cache_set", lambda _key, _scenes: None)
    monkeypatch.setattr(worker.time, "sleep", lambda _seconds: None)


def generate(text="Only one sentence.", **kwargs):
    return worker.generate_scene_plan(
        text,
        sentences_per_prompt="1",
        precomputed_subject={},
        **kwargs,
    )


@pytest.mark.parametrize("failure", ["timeout", "503", "429"])
def test_timeout_503_and_429_are_transient_and_stop_after_three(monkeypatch, failure):
    calls = []

    def fake_post(*_args, **_kwargs):
        calls.append(failure)
        if failure == "timeout":
            raise requests.ConnectTimeout("connect timed out")
        status = int(failure)
        return FakeResponse(status_code=status, payload={}, text="temporary provider failure")

    monkeypatch.setattr(worker, "_llm_post", fake_post)

    with pytest.raises(worker.ScenePlanTransientError):
        generate()

    assert len(calls) == worker.SCENE_MAX_RETRIES == 3
    assert not hasattr(worker, "_degraded_scene")


def test_invalid_json_gets_one_corrective_retry_on_same_model(monkeypatch):
    payloads = []

    def fake_post(*_args, **_kwargs):
        payloads.append(_kwargs["json"])
        if len(payloads) > 1:
            return ok_response(scene("repaired subject"))
        return FakeResponse(payload={
            "choices": [{"message": {"content": "{not-json"}, "finish_reason": "stop"}]
        })

    monkeypatch.setattr(worker, "_llm_post", fake_post)

    plan = generate()

    assert plan[0]["subject"] == "repaired subject"
    assert len(payloads) == 2
    assert payloads[0]["model"] == payloads[1]["model"]
    assert payloads[0]["response_format"] == payloads[1]["response_format"]
    assert "CORRECTIVE RETRY for часть 1" in payloads[1]["messages"][-1]["content"]


def test_missing_visual_prompt_gets_bounded_corrective_retry(monkeypatch):
    payloads = []

    def fake_post(*_args, **_kwargs):
        payloads.append(_kwargs["json"])
        if len(payloads) == 1:
            bad = scene("missing visual prompt")
            bad.pop("visual_prompt")
            return ok_response(bad)
        return ok_response(scene("fixed visual prompt"))

    monkeypatch.setattr(worker, "_llm_post", fake_post)

    plan = generate()

    assert plan[0]["subject"] == "fixed visual prompt"
    assert len(payloads) == 2
    assert "visual_prompt" in payloads[1]["messages"][-1]["content"]


def test_schema_retry_exhaustion_is_fail_closed_without_cache_or_placeholder(monkeypatch):
    payloads = []
    cache_writes = []

    def fake_post(*_args, **_kwargs):
        payloads.append(_kwargs["json"])
        return FakeResponse(payload={
            "choices": [{"message": {"content": "not-json"}, "finish_reason": "stop"}]
        })

    monkeypatch.setattr(worker, "_llm_post", fake_post)
    monkeypatch.setattr(worker, "_prompt_cache_set", lambda *args: cache_writes.append(args))

    with pytest.raises(worker.ScenePlanSchemaError, match="после 3 точечных повторов"):
        generate()

    assert len(payloads) == 1 + worker.SCENE_SCHEMA_MAX_RETRIES == 4
    assert {payload["model"] for payload in payloads} == {worker.SCENE_GPT_MODEL}
    assert cache_writes == []
    assert not hasattr(worker, "_degraded_scene")


def test_refusal_is_terminal_and_not_schema_retried(monkeypatch):
    calls = []

    def fake_post(*_args, **_kwargs):
        calls.append(_kwargs["json"])
        return FakeResponse(payload={
            "choices": [{
                "message": {"content": "", "refusal": "no"},
                "finish_reason": "refusal",
            }]
        })

    monkeypatch.setattr(worker, "_llm_post", fake_post)

    with pytest.raises(worker.ScenePlanTerminalError, match="отклонил сцену"):
        generate()

    assert len(calls) == 1


def test_successful_plan_has_exactly_one_valid_scene_per_chunk(monkeypatch):
    monkeypatch.setattr(worker, "_scene_batch_size", lambda: 1)

    def fake_post(*_args, **kwargs):
        prompt = kwargs["json"]["messages"][-1]["content"]
        if "First sentence" in prompt:
            return ok_response(scene("first subject"))
        if "Second sentence" in prompt:
            return ok_response(scene("second subject"))
        return ok_response(scene("third subject"))

    monkeypatch.setattr(worker, "_llm_post", fake_post)

    plan = generate("First sentence. Second sentence. Third sentence.")

    assert len(plan) == 3
    assert [item["text_segment"] for item in plan] == [
        "First sentence.", "Second sentence.", "Third sentence."
    ]
    assert all(item["visual_prompt"].strip() for item in plan)


def test_parallel_chunk_failure_aborts_instead_of_returning_partial_plan(monkeypatch):
    attempts = Counter()
    monkeypatch.setattr(worker, "_scene_batch_size", lambda: 1)

    def fake_post(*_args, **kwargs):
        prompt = kwargs["json"]["messages"][-1]["content"]
        if "Second sentence" in prompt:
            attempts["second"] += 1
            raise requests.ConnectTimeout("second chunk timed out")
        key = "first" if "First sentence" in prompt else "third"
        attempts[key] += 1
        return ok_response(scene(f"{key} subject"))

    monkeypatch.setattr(worker, "_llm_post", fake_post)

    with pytest.raises(worker.ScenePlanTransientError, match="часть 2"):
        generate("First sentence. Second sentence. Third sentence.")

    assert attempts["second"] == 3
    assert attempts["first"] == 1
    assert attempts["third"] <= 1


def test_batch_failure_propagates_and_never_falls_back_to_partial_singles(monkeypatch):
    calls = Counter()
    monkeypatch.setattr(worker, "_scene_batch_size", lambda: 3)

    def fake_post(*_args, **kwargs):
        schema_name = kwargs["json"]["response_format"]["json_schema"]["name"]
        calls[schema_name] += 1
        if schema_name == "scene_batch":
            return FakeResponse(status_code=503, payload={}, text="upstream unavailable")
        return ok_response(scene("warmup subject"))

    monkeypatch.setattr(worker, "_llm_post", fake_post)

    with pytest.raises(worker.ScenePlanTransientError, match="пакет частей"):
        generate("First sentence. Second sentence. Third sentence. Fourth sentence.")

    assert calls["scene"] == 1
    assert calls["scene_batch"] == 3


def test_batch_repairs_invalid_siblings_in_parallel_and_keeps_valid_cache(monkeypatch):
    calls = []
    cache_writes = []
    repair_barrier = threading.Barrier(2, timeout=2)
    monkeypatch.setattr(worker, "_prompt_provider", lambda: "claude")
    monkeypatch.setenv("VS_PROMPT_FALLBACK", "0")
    monkeypatch.setattr(worker, "_scene_batch_size", lambda: 4)
    monkeypatch.setattr(
        worker, "_prompt_cache_set",
        lambda key, scenes: cache_writes.append((key, scenes[0]["subject"])),
    )

    def fake_post(*_args, **kwargs):
        payload = kwargs["json"]
        schema_name = payload["response_format"]["json_schema"]["name"]
        calls.append((schema_name, payload))
        if schema_name == "scene_batch":
            broken_two = scene("second broken subject")
            broken_two.pop("visual_prompt")
            broken = scene("third broken subject")
            broken.pop("visual_prompt")
            return ok_response({"scenes": [
                broken_two,
                broken,
                scene("fourth valid subject"),
                scene("fifth valid subject"),
            ]})
        corrective = payload["messages"][-1]["content"]
        if "CORRECTIVE RETRY" not in corrective:
            return ok_response(scene("first valid subject"))
        assert "CORRECTIVE RETRY for часть " in corrective
        repair_barrier.wait()
        if "часть 2" in corrective:
            return ok_response(scene("second repaired subject"))
        assert "часть 3" in corrective
        return ok_response(scene("third repaired subject"))

    monkeypatch.setattr(worker, "_llm_post", fake_post)

    plan = generate(
        "First sentence. Second sentence. Third sentence. "
        "Fourth sentence. Fifth sentence."
    )

    assert [item["subject"] for item in plan] == [
        "first valid subject",
        "second repaired subject",
        "third repaired subject",
        "fourth valid subject",
        "fifth valid subject",
    ]
    assert Counter(name for name, _payload in calls) == {"scene_batch": 1, "scene": 3}
    # Валидные siblings сохраняются до параллельного ремонта двух битых.
    written_subjects = [subject for _key, subject in cache_writes]
    for valid_subject in ("fourth valid subject", "fifth valid subject"):
        assert written_subjects.index(valid_subject) < written_subjects.index(
            "second repaired subject"
        )
        assert written_subjects.index(valid_subject) < written_subjects.index(
            "third repaired subject"
        )
    assert set(written_subjects) == {
        "first valid subject", "second repaired subject",
        "third repaired subject", "fourth valid subject", "fifth valid subject",
    }
    assert len({key for key, _subject in cache_writes}) == 5


def test_scene_cache_key_is_bound_to_full_source_context_and_provider():
    schema = worker._scene_response_format([])
    digest_a = worker._scene_cache_context_digest(
        "Full source A", "system plus bible A", schema, "claude"
    )
    digest_b = worker._scene_cache_context_digest(
        "Full source B", "system plus bible A", schema, "claude"
    )
    digest_c = worker._scene_cache_context_digest(
        "Full source A", "system plus bible B", schema, "claude"
    )
    digest_d = worker._scene_cache_context_digest(
        "Full source A", "system plus bible A", schema, "codex"
    )

    keys = {
        worker._prompt_cache_key("same chunk", "same style", digest)
        for digest in (digest_a, digest_b, digest_c, digest_d)
    }
    assert len(keys) == 4
    with pytest.raises(ValueError, match="context digest"):
        worker._prompt_cache_key("same chunk", "same style", "")


def test_story_bible_source_hash_cache_is_strictly_validated(monkeypatch, tmp_path):
    bible = {
        "characters": [{
            "id": "hero",
            "names": ["Hero"],
            "role": "protagonist",
            "appearance": "elderly desert architect",
        }],
        "locations": [],
    }
    writes = []
    monkeypatch.setattr(worker, "_planning_artifact_cache_get", lambda _identity: bible)
    monkeypatch.setattr(
        worker, "_atomic_write_json", lambda path, data: writes.append((path, data))
    )
    monkeypatch.setattr(
        worker,
        "_analyze_text_chunk",
        lambda _chunk: pytest.fail("valid story cache called LLM"),
    )

    got = worker.analyze_story(
        "Stable source.", str(tmp_path), use_cache=True,
        cadence={"sentences_per_prompt": "1", "split_long": True},
    )

    assert got == bible
    assert writes and writes[0][0].endswith("story_bible.json")
    broken = json.loads(json.dumps(bible))
    broken["characters"][0]["role"] = "invented"
    with pytest.raises(worker.ScenePlanTerminalError, match="role"):
        worker._validate_story_bible(broken)


def test_visual_context_cache_hit_skips_llm_and_write(monkeypatch):
    cached = "Stable Ancient Egyptian visual world and period-accurate architecture."
    monkeypatch.setattr(
        worker, "_planning_artifact_cache_get", lambda _identity: f"  {cached}  "
    )
    monkeypatch.setattr(
        worker, "generate_visual_context",
        lambda _text: pytest.fail("valid visual_context cache called LLM"),
    )
    monkeypatch.setattr(
        worker, "_planning_artifact_cache_set",
        lambda *_args: pytest.fail("cache hit rewrote artifact"),
    )

    assert worker._get_visual_context("source", use_cache=True) == cached


def test_visual_context_cache_miss_calls_existing_generator_and_writes(monkeypatch):
    generated = "Stable hand-drawn historical world with period-accurate clothing."
    writes = []
    monkeypatch.setattr(worker, "_planning_artifact_cache_get", lambda _identity: None)
    monkeypatch.setattr(worker, "generate_visual_context", lambda _text: generated)
    monkeypatch.setattr(
        worker, "_planning_artifact_cache_set",
        lambda identity, value: writes.append((identity, value)),
    )

    assert worker._get_visual_context("source", use_cache=True) == generated
    assert len(writes) == 1
    identity, value = writes[0]
    assert identity["kind"] == "visual_context"
    assert identity["algorithm_version"] == 1
    assert identity["prompt_version"] == "visual-context-v1"
    assert identity["cadence"] == {"sample_chars": 4000}
    assert identity["model"] == worker.GPT_MODEL
    assert value == generated


def test_corrupt_visual_context_cache_is_regenerated(monkeypatch):
    generated = "Regenerated visual context that is safely longer than twenty chars."
    writes = []
    monkeypatch.setattr(worker, "_planning_artifact_cache_get", lambda _identity: "short")
    monkeypatch.setattr(worker, "generate_visual_context", lambda _text: generated)
    monkeypatch.setattr(
        worker, "_planning_artifact_cache_set",
        lambda identity, value: writes.append((identity, value)),
    )

    assert worker._get_visual_context("source", use_cache=True) == generated
    assert writes and writes[0][1] == generated


def test_visual_context_no_cache_neither_reads_nor_writes(monkeypatch):
    generated = "Fresh visual context longer than the strict minimum length."
    monkeypatch.setattr(
        worker, "_planning_artifact_cache_get",
        lambda _identity: pytest.fail("use_cache=False read visual cache"),
    )
    monkeypatch.setattr(
        worker, "_planning_artifact_cache_set",
        lambda *_args: pytest.fail("use_cache=False wrote visual cache"),
    )
    monkeypatch.setattr(worker, "generate_visual_context", lambda _text: generated)

    assert worker._get_visual_context("source", use_cache=False) == generated


def test_visual_context_cache_reuses_director_context_digest_and_scene_key(monkeypatch):
    source = "One complete sentence."
    visual = "Stable visual bible with exact era, setting, clothing, and architecture."
    artifact_cache = {}
    scene_cache = {}
    visual_calls = 0
    director_calls = 0
    cache_get_keys = []

    def identity_key(identity):
        return json.dumps(identity, sort_keys=True)

    monkeypatch.setattr(
        worker, "_planning_artifact_cache_get",
        lambda identity: artifact_cache.get(identity_key(identity)),
    )
    monkeypatch.setattr(
        worker, "_planning_artifact_cache_set",
        lambda identity, value: artifact_cache.__setitem__(identity_key(identity), value),
    )

    def generate_visual(_text):
        nonlocal visual_calls
        visual_calls += 1
        return visual

    def get_scene(key):
        cache_get_keys.append(key)
        return scene_cache.get(key)

    def set_scene(key, scenes):
        scene_cache[key] = scenes

    def director(*_args, **_kwargs):
        nonlocal director_calls
        director_calls += 1
        return ok_response(scene("stable subject"))

    monkeypatch.setattr(worker, "generate_visual_context", generate_visual)
    monkeypatch.setattr(worker, "_prompt_cache_get", get_scene)
    monkeypatch.setattr(worker, "_prompt_cache_set", set_scene)
    monkeypatch.setattr(worker, "_llm_post", director)

    first = worker.generate_scene_plan(
        source, sentences_per_prompt="1", precomputed_subject={}, use_cache=True
    )
    second = worker.generate_scene_plan(
        source, sentences_per_prompt="1", precomputed_subject={}, use_cache=True
    )

    assert first == second
    assert visual_calls == 1
    assert director_calls == 1
    assert len(cache_get_keys) == 2
    assert cache_get_keys[0] == cache_get_keys[1]


def test_story_character_and_location_consolidation_join_in_parallel(monkeypatch, tmp_path):
    extracted = {
        "characters": [
            {"id": "a", "names": ["A"], "role": "protagonist", "appearance": "older"},
            {"id": "b", "names": ["B"], "role": "major", "appearance": "younger"},
        ],
        "locations": [
            {"id": "x", "names": ["X"], "appearance": "stone room"},
            {"id": "y", "names": ["Y"], "appearance": "river bank"},
        ],
    }
    barrier = threading.Barrier(2, timeout=2)
    monkeypatch.setattr(worker, "_analyze_text_chunk", lambda _chunk: extracted)

    def consolidate(rows, is_char=True):
        assert is_char in (True, False)
        barrier.wait()
        return rows

    monkeypatch.setattr(worker, "_consolidate_characters", consolidate)
    monkeypatch.setattr(worker, "_atomic_write_json", lambda *_args, **_kwargs: None)

    bible = worker.analyze_story(
        "A and B walked from X to Y.", str(tmp_path), use_cache=False
    )

    assert len(bible["characters"]) == 2
    assert len(bible["locations"]) == 2


def test_matched_refs_follow_bible_order_regardless_of_match_insertion_order():
    bible = {
        "characters": [
            {"id": "bob", "names": ["Bob"], "role": "major", "appearance": "older"},
            {"id": "alice", "names": ["Alice"], "role": "major", "appearance": "younger"},
        ],
        "locations": [
            {"id": "temple", "names": ["Temple"], "appearance": "stone temple"},
            {"id": "alice_chamber", "names": ["Alice chamber"], "appearance": "side chamber"},
        ],
    }
    base = {
        "subject": "three known entities",
        "visual_prompt": "Bob, Alice, and the Temple in one scene",
        "camera_angle": "Wide Shot",
        "subject_type": "environment",
        "text_segment": "",
    }
    forward = dict(base, entities=["Temple", "Alice", "Bob"])
    reverse = dict(base, entities=["Bob", "Alice", "Temple"])

    expected = ["bob", "alice", "temple"]
    assert worker.match_refs_for_scene(forward, "", bible) == expected
    assert worker.match_refs_for_scene(reverse, "", bible) == expected


def test_global_llm_gate_caps_nested_planning_calls(monkeypatch):
    active = 0
    maximum = 0
    lock = threading.Lock()
    release = threading.Event()
    cap = max(1, worker.GPT_SCENE_PARALLEL_THREADS)

    def underlying(*_args, **_kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        release.wait(timeout=3)
        with lock:
            active -= 1
        return object()

    monkeypatch.setattr(worker, "_llm_post_ungated", underlying)
    total = cap + 4
    with ThreadPoolExecutor(max_workers=total) as executor:
        futures = [executor.submit(worker._llm_post, "unused") for _ in range(total)]
        deadline = time.monotonic() + 2
        while maximum < cap and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        release.set()
        for future in futures:
            future.result()

    assert maximum == cap


def test_semantic_director_keeps_contract_above_legacy_subject_hacks(monkeypatch):
    source = "First. Second. Third."
    units = worker._semantic_cadence_units(source, "1", True, 6.0)
    beats = []
    for index, unit in enumerate(units):
        start, end = unit["start"], unit["end"]
        beats.append({
            "text_segment": source[start:end],
            "semantic_claim": f"visible relationship {index + 1}",
            "visual_mode": "comparison" if index == 1 else "literal_action",
            "required_elements": [f"left element {index + 1}", f"right element {index + 1}"],
            "continuity_group": "sequence_001" if index < 2 else "sequence_002",
            "continuity_relation": "continue_previous" if index == 1 else "new",
            "character_start": start,
            "character_end": end,
            "sentence_index": unit["sentence_index"],
            "sentence_end_index": unit["sentence_end_index"],
            "sentence_count": unit["sentence_count"],
            "sentence_start": start,
            "sentence_end": end,
            "cadence_unit_index": unit["unit_index"],
            "beat_index": 0,
            "beat_count": 1,
        })

    payloads = []
    monkeypatch.setattr(worker, "_plan_semantic_beats", lambda *_args, **_kwargs: beats)
    monkeypatch.setattr(worker, "_scene_batch_size", lambda: 2)
    monkeypatch.setattr(
        worker,
        "_normalize_scene_subject",
        lambda *_args, **_kwargs: pytest.fail("semantic scene used legacy subject rewrite"),
    )
    monkeypatch.setattr(
        worker,
        "_anchor_subject",
        lambda *_args, **_kwargs: pytest.fail("semantic scene used large-and-centered anchor"),
    )

    def fake_post(*_args, **kwargs):
        payload = kwargs["json"]
        payloads.append(payload)
        schema_name = payload["response_format"]["json_schema"]["name"]
        if schema_name == "scene_batch":
            return ok_response({"scenes": [scene("second relation"), scene("third action")]})
        return ok_response(scene("first action"))

    monkeypatch.setattr(worker, "_llm_post", fake_post)

    plan = worker.generate_scene_plan(
        source,
        sentences_per_prompt="1",
        split_long=True,
        precomputed_subject={
            "single_subject": True,
            "main_subject": "global hero",
            "generic_aliases": ["hero"],
        },
    )

    assert [item["semantic_claim"] for item in plan] == [
        "visible relationship 1", "visible relationship 2", "visible relationship 3"
    ]
    batch_prompt = payloads[-1]["messages"][-1]["content"]
    assert "=== SOURCE SEGMENT 2 ===" in batch_prompt
    assert "=== SOURCE SEGMENT 3 ===" in batch_prompt
    assert "CONTINUITY RELATION TO PREVIOUS BEAT: continue_previous" in batch_prompt
    assert "PREVIOUS BEAT GROUP: sequence_001" in batch_prompt
    assert "large and centered" not in " ".join(item["visual_prompt"] for item in plan)


def test_pipeline_precomputes_story_semantic_and_visual_context_in_parallel(
        monkeypatch, tmp_path):
    args = pipeline_args(tmp_path, analyze_story=True)
    args.split_long_sentences = True
    source = Path(args.source_file).read_text(encoding="utf-8")
    barrier = threading.Barrier(3, timeout=2)
    beats = []
    for unit in worker._semantic_cadence_units(source, "1", True, 6.0):
        beats.append({
            "text_segment": unit["text"],
            "semantic_claim": f"visible completed claim {unit['unit_index'] + 1}",
            "visual_mode": "literal_action",
            "required_elements": ["complete visible action"],
            "continuity_group": f"sequence_{unit['unit_index'] + 1:03d}",
            "continuity_relation": "new",
            "character_start": unit["start"],
            "character_end": unit["end"],
            "sentence_index": unit["sentence_index"],
            "sentence_end_index": unit["sentence_end_index"],
            "sentence_count": unit["sentence_count"],
            "sentence_start": unit["start"],
            "sentence_end": unit["end"],
            "cadence_unit_index": unit["unit_index"],
            "beat_index": 0,
            "beat_count": 1,
        })

    def parallel_result(value):
        barrier.wait()
        return value

    monkeypatch.setattr(worker, "_assert_openai_key", lambda: None)
    monkeypatch.setattr(
        worker, "analyze_story",
        lambda *_args, **_kwargs: parallel_result({"characters": [], "locations": []}),
    )
    monkeypatch.setattr(
        worker, "_plan_semantic_beats",
        lambda *_args, **_kwargs: parallel_result(beats),
    )
    monkeypatch.setattr(
        worker, "generate_visual_context",
        lambda *_args, **_kwargs: parallel_result(
            "parallel visual bible longer than twenty characters"
        ),
    )
    monkeypatch.setattr(worker, "extract_main_subject", lambda *_args, **_kwargs: {})
    captured = {}

    def fake_generate(*_args, **kwargs):
        captured.update(kwargs)
        return [dict(scene("complete action"), **beat) for beat in beats]

    monkeypatch.setattr(worker, "generate_scene_plan", fake_generate)
    monkeypatch.setattr(worker, "_publish_scene_plan", lambda *_args, **_kwargs: {})

    worker.cmd_full_pipeline(args)

    assert captured["precomputed_semantic_beats"] == beats
    assert captured["precomputed_visual_context"] == (
        "parallel visual bible longer than twenty characters"
    )


def pipeline_args(tmp_path, *, analyze_story=False):
    source = tmp_path / "source.txt"
    source.write_text("First sentence. Second sentence.", encoding="utf-8")
    return SimpleNamespace(
        source_file=str(source),
        project_dir=str(tmp_path / "project"),
        style="cinematic",
        aspect_ratio="landscape",
        plan_only=True,
        use_plan=False,
        analyze_story=analyze_story,
        use_cache=False,
        auto_hero=False,
        smart_mode=False,
        sentences_per_prompt="1",
        split_long_sentences=False,
        max_solo_scene_seconds=worker.DEFAULT_MAX_SOLO_SCENE_SECONDS,
        image_threads=1,
        model="OPENAI",
    )


def test_plan_is_published_atomically_once_after_reference_matching(monkeypatch, tmp_path):
    args = pipeline_args(tmp_path, analyze_story=True)
    bible = {
        "characters": [{"id": "hero", "names": ["Hero"], "role": "major", "appearance": "an old pilot"}],
        "locations": [],
    }
    generated = [
        dict(scene("first subject"), text_segment="First sentence.", entities=[]),
        dict(scene("second subject"), text_segment="Second sentence.", entities=[]),
    ]
    monkeypatch.setattr(worker, "_assert_openai_key", lambda: None)
    monkeypatch.setattr(worker, "analyze_story", lambda *_args, **_kwargs: bible)
    monkeypatch.setattr(worker, "generate_scene_plan", lambda *_args, **_kwargs: generated)
    monkeypatch.setattr(worker, "match_refs_for_scene", lambda *_args, **_kwargs: ["Hero"])

    writes = []
    real_atomic_bytes = worker._atomic_write_bytes

    def tracked_bytes(path, data):
        writes.append((os.path.basename(path), json.loads(data.decode("utf-8"))))
        real_atomic_bytes(path, data)

    monkeypatch.setattr(worker, "_atomic_write_bytes", tracked_bytes)

    worker.cmd_full_pipeline(args)

    scene_writes = [
        (name, data) for name, data in writes
        if name in {"scenes.json", "scenes.meta.json"}
    ]
    assert [name for name, _data in scene_writes] == [
        "scenes.json", "scenes.meta.json"
    ]
    final_writes = [data for name, data in writes if name == "scenes.json"]
    assert len(final_writes) == 1
    assert all(item["matched_refs"] == ["Hero"] for item in final_writes[0])
    on_disk = json.loads(
        (Path(args.project_dir) / worker.GOOGLER_IMAGES_DIR / "scenes.json").read_text(encoding="utf-8")
    )
    assert on_disk == final_writes[0]


def test_meta_write_failure_leaves_no_accepted_split_brain_bundle(monkeypatch, tmp_path):
    args = pipeline_args(tmp_path)
    generated = [
        dict(scene("new one"), text_segment="First sentence."),
        dict(scene("new two"), text_segment="Second sentence."),
    ]
    monkeypatch.setattr(worker, "_assert_openai_key", lambda: None)
    monkeypatch.setattr(worker, "extract_main_subject", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(worker, "generate_scene_plan", lambda *_args, **_kwargs: generated)

    output_dir = Path(args.project_dir) / worker.GOOGLER_IMAGES_DIR
    old_scenes = [
        dict(scene("old one"), text_segment="First sentence."),
        dict(scene("old two"), text_segment="Second sentence."),
    ]
    worker._publish_scene_plan(
        str(output_dir), args, "First sentence. Second sentence.", old_scenes,
        {"subject_anchor": {}},
    )

    def fail_meta(path, _data):
        if path.endswith("scenes.meta.json"):
            raise OSError("disk full")
        return worker._atomic_write_bytes(path, worker._json_bytes(_data))

    monkeypatch.setattr(worker, "_atomic_write_json", fail_meta)

    with pytest.raises(OSError, match="disk full"):
        worker.cmd_full_pipeline(args)

    scenes_path = output_dir / "scenes.json"
    assert scenes_path.exists(), "scene data may be replaced before the commit-marker"
    with pytest.raises(worker.ScenePlanTerminalError, match="digest"):
        worker._load_plan_bundle(
            str(output_dir), args, "First sentence. Second sentence."
        )


def test_plan_only_cannot_return_success_for_an_empty_plan(monkeypatch, tmp_path):
    args = pipeline_args(tmp_path)
    monkeypatch.setattr(worker, "_assert_openai_key", lambda: None)
    monkeypatch.setattr(worker, "extract_main_subject", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(worker, "generate_scene_plan", lambda *_args, **_kwargs: [])

    with pytest.raises(worker.ScenePlanTerminalError, match="полный план"):
        worker.cmd_full_pipeline(args)


def test_smart_plan_has_exact_lossless_source_coverage():
    source = " First scene.\nSecond scene.  "
    scenes = [
        dict(scene("one"), text_segment="First scene.", includes_main_character=True),
        dict(scene("two"), text_segment="Second scene.", includes_main_character=False),
    ]

    checked = worker._validate_smart_scene_plan(scenes, source)

    assert "".join(item["text_segment"] for item in checked) == source
    assert checked[0]["character_start"] == 0
    assert checked[-1]["character_end"] == len(source)


def test_smart_plan_rejects_missing_source_content():
    scenes = [
        dict(scene("one"), text_segment="First.", includes_main_character=True),
        dict(scene("three"), text_segment="Third.", includes_main_character=False),
    ]

    with pytest.raises(worker.ScenePlanTerminalError, match="пропущен фрагмент"):
        worker._validate_smart_scene_plan(scenes, "First. Second. Third.")


def test_smart_plan_rejects_missing_subject():
    bad = dict(scene(), text_segment="Only scene.", includes_main_character=False)
    bad.pop("subject")

    with pytest.raises(worker.ScenePlanTerminalError, match="subject"):
        worker._validate_smart_scene_plan([bad], "Only scene.")


def test_smart_failure_does_not_fallback_to_standard(monkeypatch, tmp_path):
    args = pipeline_args(tmp_path)
    args.smart_mode = True
    monkeypatch.setattr(worker, "_assert_openai_key", lambda: None)
    monkeypatch.setattr(worker, "extract_main_subject", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        worker, "_internal_generate_smart_scene_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            worker.ScenePlanTerminalError("bad smart schema")
        ),
    )
    standard_calls = []
    monkeypatch.setattr(
        worker, "generate_scene_plan",
        lambda *_args, **_kwargs: standard_calls.append(1),
    )

    with pytest.raises(worker.ScenePlanTerminalError, match="bad smart schema"):
        worker.cmd_full_pipeline(args)
    assert standard_calls == []


def test_use_plan_rejects_incomplete_bundle_even_with_valid_digest(tmp_path):
    args = pipeline_args(tmp_path)
    args.use_plan = True
    args.plan_only = False
    output_dir = Path(args.project_dir) / worker.GOOGLER_IMAGES_DIR
    incomplete = [dict(scene(), text_segment="First sentence. Second sentence.")]
    worker._publish_scene_plan(
        str(output_dir), args, "First sentence. Second sentence.", incomplete,
        {"subject_anchor": {}},
    )

    with pytest.raises(worker.ScenePlanTerminalError, match="1 сцен для 2 частей"):
        worker._load_plan_bundle(
            str(output_dir), args, "First sentence. Second sentence."
        )


@pytest.mark.parametrize(
    "error,expected_rc",
    [
        (worker.ScenePlanTransientError("timeout"), worker.EX_TEMPFAIL),
        (worker.ScenePlanTerminalError("bad schema"), 1),
    ],
)
def test_cli_maps_scene_plan_errors_to_nonzero_exit(monkeypatch, tmp_path, error, expected_rc):
    source = tmp_path / "source.txt"
    source.write_text("Text.", encoding="utf-8")
    project = tmp_path / "project"

    def fail(_args):
        raise error

    monkeypatch.setattr(worker, "cmd_full_pipeline", fail)
    monkeypatch.setattr(sys, "argv", [
        "googler_worker.py", "pipeline",
        "--source_file", str(source),
        "--project_dir", str(project),
        "--style", "cinematic",
        "--plan_only",
    ])

    with pytest.raises(SystemExit) as caught:
        worker.main()

    assert caught.value.code == expected_rc
