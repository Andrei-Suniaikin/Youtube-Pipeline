from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from semantic_montage.common import sha256_file
from semantic_montage.project import ProjectError, load_project
from semantic_montage.runner import PipelineRunner, RunnerError, StageDefinition, StageRegistry
from semantic_montage.state import JobState, StateError


def _project(root: Path) -> Path:
    source = root / "input" / "script.txt"
    source.parent.mkdir(parents=True)
    source.write_text("hello", encoding="utf-8")
    project = root / "project.json"
    project.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "project_id": "portable-test",
                "assets": [
                    {
                        "asset_id": "script",
                        "sha256": sha256_file(source),
                        "relative_path": "input/script.txt",
                        "media_type": "text/plain",
                    }
                ],
                "stages": ["first", "second"],
                "settings": {
                    "state_path": ".state/job.sqlite3",
                    "work_root": "work",
                    "output_root": "output",
                    "qa_root": "qa",
                },
                "secrets": {"openai": "OPENAI_API_KEY"},
            }
        ),
        encoding="utf-8",
    )
    return project


def test_project_moves_without_json_edits(tmp_path: Path):
    project = load_project(_project(tmp_path / "one"))
    assert project.resolve_asset("script").read_text() == "hello"
    moved = tmp_path / "two"
    shutil.copytree(project.root, moved)
    moved_project = load_project(moved / "project.json")
    assert moved_project.resolve_asset("script").read_text() == "hello"
    assert "/Users/" not in (moved / "project.json").read_text()


def test_project_rejects_absolute_persisted_paths(tmp_path: Path):
    path = _project(tmp_path)
    payload = json.loads(path.read_text())
    payload["assets"][0]["relative_path"] = "/tmp/script.txt"
    path.write_text(json.dumps(payload))
    with pytest.raises(ProjectError, match="portable relative"):
        load_project(path)


def test_paid_operation_is_idempotent_across_restart(tmp_path: Path):
    state = JobState(tmp_path / "state.sqlite3")
    first = state.claim_paid_operation(
        provider="stock", idempotency_key="abc", operation_kind="license", request_hash="r1"
    )
    state.record_paid_submission("stock", "abc", first["claim_token"], "job-1", {"cost": 1})
    restarted = JobState(tmp_path / "state.sqlite3")
    second = restarted.claim_paid_operation(
        provider="stock", idempotency_key="abc", operation_kind="license", request_hash="r1"
    )
    assert second["action"] == "resume"
    assert second["row"]["provider_job_id"] == "job-1"
    with pytest.raises(StateError, match="different paid request"):
        restarted.claim_paid_operation(
            provider="stock", idempotency_key="abc", operation_kind="license", request_hash="r2"
        )


def test_runner_checkpoints_and_invalidates_downstream(tmp_path: Path):
    project = load_project(_project(tmp_path))
    calls = {"first": 0, "second": 0}
    registry = StageRegistry()

    def first(context):
        calls["first"] += 1
        path = context.project.output_root / "first.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("one", encoding="utf-8")
        return {"value": 1, "artifacts": [{"artifact_id": "first", "path": path}]}

    def second(context):
        calls["second"] += 1
        path = context.project.output_root / "second.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("two", encoding="utf-8")
        return {"value": 2, "artifacts": [{"artifact_id": "second", "path": path}]}

    registry.register(StageDefinition("first", first, version="1"))
    registry.register(StageDefinition("second", second, dependencies=("first",), version="1"))
    runner = PipelineRunner(project, registry)
    first_run = runner.run()
    second_run = runner.run(run_id=first_run["run_id"])
    assert second_run["status"] == "completed"
    assert calls == {"first": 1, "second": 1}

    changed = StageRegistry()
    changed.register(StageDefinition("first", first, version="2"))
    changed.register(StageDefinition("second", second, dependencies=("first",), version="1"))
    PipelineRunner(project, changed).run(run_id=first_run["run_id"])
    assert calls == {"first": 2, "second": 2}


def test_runner_rejects_nonportable_result(tmp_path: Path):
    project_path = _project(tmp_path)
    payload = json.loads(project_path.read_text())
    payload["stages"] = ["bad"]
    project_path.write_text(json.dumps(payload))
    project = load_project(project_path)
    registry = StageRegistry()
    registry.register(StageDefinition("bad", lambda _: {"proof": "/tmp/proof.jpg"}))
    with pytest.raises(RunnerError, match="path leaked"):
        PipelineRunner(project, registry).run()
