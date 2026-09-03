"""Deterministic stage runner with durable resume and immutable artifacts."""
from __future__ import annotations

import json
import os
import shutil
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .common import canonical_hash, sha256_file
from .project import ProjectConfig
from .state import JobState


class RunnerError(RuntimeError):
    pass


class StageBlocked(RunnerError):
    """A safe terminal that needs an external provider/input, never a fallback."""

    def __init__(
        self,
        status: str,
        detail: str,
        *,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        if not status.startswith("needs_") or not status.replace("_", "").isalnum():
            raise ValueError("StageBlocked status must be a needs_* code")
        super().__init__(detail)
        self.status = status
        self.detail = str(detail)
        self.result = dict(result) if result is not None else None


@dataclass(frozen=True)
class StageContext:
    project: ProjectConfig
    state: JobState
    run_id: str
    stage_name: str
    attempt: int
    attempt_root: Path
    upstream: Mapping[str, Mapping[str, Any]]
    runtime_mounts: Mapping[str, Path] = field(default_factory=dict)
    # Asset hashes are verified once per stage attempt.  A stage may hydrate
    # hundreds of candidate rows that all point at the same multi-gigabyte
    # donor; re-hashing that donor for every row turns a minute-long lookup
    # into terabytes of redundant I/O.
    resolved_assets: dict[str, Path] = field(default_factory=dict)


@dataclass(frozen=True)
class StageDefinition:
    name: str
    handler: Callable[[StageContext], Mapping[str, Any]]
    dependencies: tuple[str, ...] = ()
    version: str = "1"
    paid: bool = False


@dataclass
class StageRegistry:
    _stages: dict[str, StageDefinition] = field(default_factory=dict)

    def register(self, definition: StageDefinition) -> None:
        if not definition.name or definition.name in self._stages:
            raise RunnerError(f"duplicate or empty stage: {definition.name!r}")
        self._stages[definition.name] = definition

    def get(self, name: str) -> StageDefinition:
        try:
            return self._stages[name]
        except KeyError as exc:
            raise RunnerError(f"unknown stage: {name}") from exc

    def ordered(self, requested: Iterable[str]) -> list[StageDefinition]:
        result: list[StageDefinition] = []
        visited: set[str] = set()
        active: set[str] = set()

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in active:
                raise RunnerError(f"stage dependency cycle at {name}")
            active.add(name)
            definition = self.get(name)
            for dependency in definition.dependencies:
                visit(dependency)
            active.remove(name)
            visited.add(name)
            result.append(definition)

        for stage_name in requested:
            visit(stage_name)
        return result


def _portable_result(value: object, project_root: Path, location: str = "result") -> None:
    """Reject host paths from values persisted into portable run state."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            _portable_result(item, project_root, f"{location}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _portable_result(item, project_root, f"{location}[{index}]")
    elif isinstance(value, str):
        # Absolute paths are only allowed if converted to project-relative artifact
        # records by `_normalize_result` before this check.
        if Path(value).is_absolute():
            raise RunnerError(f"absolute path leaked into portable {location}")
        if value.startswith("file://") or value.startswith("/tmp/"):
            raise RunnerError(f"temporary path leaked into portable {location}")


def _promote_artifact(
    source: Path,
    *,
    project: ProjectConfig,
    run_id: str,
    stage_name: str,
    digest: str,
) -> Path:
    """Copy into a content-addressed run namespace, then atomically publish."""

    safe_name = source.name or "artifact.bin"
    target = project.work_root / "artifacts" / run_id / stage_name / digest / safe_name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        if sha256_file(target) != digest:
            raise RunnerError(f"immutable artifact collision: {target.name}")
        return target
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    shutil.copyfile(source, temporary)
    if sha256_file(temporary) != digest:
        temporary.unlink(missing_ok=True)
        raise RunnerError("artifact changed while being promoted")
    os.replace(temporary, target)
    return target


def _normalize_result(
    result: Mapping[str, Any],
    project: ProjectConfig,
    *,
    run_id: str,
    stage_name: str,
) -> dict[str, Any]:
    normalized = dict(result)
    artifacts_raw = normalized.pop("artifacts", []) or []
    if not isinstance(artifacts_raw, list):
        raise RunnerError("stage result artifacts must be an array")
    artifacts: list[dict[str, Any]] = []
    for index, row in enumerate(artifacts_raw):
        if not isinstance(row, Mapping):
            raise RunnerError(f"artifact {index} must be an object")
        raw_path = Path(str(row.get("path") or ""))
        path = raw_path if raw_path.is_absolute() else project.root / raw_path
        path = path.resolve()
        try:
            relative = path.relative_to(project.root).as_posix()
        except ValueError as exc:
            raise RunnerError(f"artifact is outside project root: {path}") from exc
        if not path.is_file():
            raise RunnerError(f"stage artifact is missing: {relative}")
        actual_hash = sha256_file(path)
        expected_hash = str(row.get("sha256") or actual_hash)
        if expected_hash != actual_hash:
            raise RunnerError(f"stage artifact hash mismatch: {relative}")
        path = _promote_artifact(
            path,
            project=project,
            run_id=run_id,
            stage_name=stage_name,
            digest=actual_hash,
        )
        relative = path.relative_to(project.root).as_posix()
        artifacts.append(
            {
                "artifact_id": str(row.get("artifact_id") or f"artifact-{index}"),
                "relative_path": relative,
                "sha256": actual_hash,
                "bytes": path.stat().st_size,
                "metadata": dict(row.get("metadata") or {}),
            }
        )
    normalized["artifacts"] = artifacts
    _portable_result(normalized, project.root)
    return normalized


def _cached_artifacts_valid(result: Mapping[str, Any], project: ProjectConfig) -> bool:
    rows = result.get("artifacts")
    if not isinstance(rows, list):
        return False
    for row in rows:
        if not isinstance(row, Mapping):
            return False
        relative = str(row.get("relative_path") or "")
        expected = str(row.get("sha256") or "")
        path = (project.root / relative).resolve()
        try:
            path.relative_to(project.root)
        except ValueError:
            return False
        if not path.is_file() or sha256_file(path) != expected:
            return False
    return True


class PipelineRunner:
    def __init__(
        self,
        project: ProjectConfig,
        registry: StageRegistry,
        *,
        runtime_mounts: Mapping[str, str | os.PathLike[str]] | None = None,
    ) -> None:
        self.project = project
        self.registry = registry
        self.state = JobState(project.state_path)
        self.runtime_mounts = {
            str(name): Path(value).expanduser().resolve()
            for name, value in (runtime_mounts or {}).items()
        }

    def run(self, *, run_id: str | None = None, requested: Iterable[str] | None = None) -> dict[str, Any]:
        stages = self.registry.ordered(tuple(requested or self.project.stages))
        if not stages:
            raise RunnerError("project has no stages")
        run_id = self.state.ensure_run(
            self.project.project_id, self.project.fingerprint(), run_id=run_id
        )
        completed: dict[str, Mapping[str, Any]] = {}
        for definition in stages:
            upstream = {}
            for dependency in definition.dependencies:
                row = self.state.stage(run_id, dependency)
                if not row or row.get("status") != "completed":
                    raise RunnerError(f"dependency {dependency} is not completed")
                upstream[dependency] = dict(row.get("result") or {})
            input_hash = canonical_hash(
                {
                    "project": self.project.fingerprint(),
                    "stage": definition.name,
                    "version": definition.version,
                    "upstream": {
                        key: {
                            "input_hash": self.state.stage(run_id, key).get("input_hash"),
                            "output_hash": self.state.stage(run_id, key).get("output_hash"),
                        }
                        for key in definition.dependencies
                    },
                }
            )
            claim = self.state.claim_stage(run_id, definition.name, input_hash)
            if claim["action"] == "cached":
                cached_result = dict(claim["row"].get("result") or {})
                if _cached_artifacts_valid(cached_result, self.project):
                    completed[definition.name] = cached_result
                    continue
                self.state.invalidate_stage(
                    run_id, definition.name, "cached artifact is missing or hash-mismatched"
                )
                claim = self.state.claim_stage(run_id, definition.name, input_hash)
            token = str(claim["claim_token"])
            attempt = int(claim["attempt"])
            attempt_root = (
                self.project.work_root / run_id / definition.name / f"attempt-{attempt:03d}"
            )
            attempt_root.mkdir(parents=True, exist_ok=True)
            context = StageContext(
                project=self.project,
                state=self.state,
                run_id=run_id,
                stage_name=definition.name,
                attempt=attempt,
                attempt_root=attempt_root,
                upstream=upstream,
                runtime_mounts=self.runtime_mounts,
            )
            try:
                raw = definition.handler(context)
                if not isinstance(raw, Mapping):
                    raise RunnerError(f"stage {definition.name} returned a non-object")
                result = _normalize_result(
                    raw,
                    self.project,
                    run_id=run_id,
                    stage_name=definition.name,
                )
                output_hash = canonical_hash(result)
                self.state.complete_stage(
                    run_id,
                    definition.name,
                    token,
                    output_hash=output_hash,
                    result=result,
                    artifacts=list(result.get("artifacts") or ()),
                )
                completed[definition.name] = result
            except StageBlocked as blocked:
                blocked_result = None
                output_hash = None
                artifacts: list[Mapping[str, Any]] = []
                if blocked.result is not None:
                    blocked_result = _normalize_result(
                        blocked.result,
                        self.project,
                        run_id=run_id,
                        stage_name=definition.name,
                    )
                    output_hash = canonical_hash(blocked_result)
                    artifacts = list(blocked_result.get("artifacts") or ())
                self.state.block_stage(
                    run_id,
                    definition.name,
                    token,
                    status=blocked.status,
                    detail=blocked.detail,
                    output_hash=output_hash,
                    result=blocked_result,
                    artifacts=artifacts,
                )
                response = {
                    "run_id": run_id,
                    "status": blocked.status,
                    "blocked_stage": definition.name,
                    "detail": blocked.detail,
                    "stages": completed,
                }
                if blocked_result is not None:
                    response["blocked_result"] = blocked_result
                return response
            except BaseException as error:
                detail = "".join(traceback.format_exception_only(type(error), error)).strip()
                self.state.fail_stage(run_id, definition.name, token, detail)
                raise
        self.state.mark_run_complete(run_id)
        return {"run_id": run_id, "status": "completed", "stages": completed}


__all__ = [
    "PipelineRunner",
    "RunnerError",
    "StageBlocked",
    "StageContext",
    "StageDefinition",
    "StageRegistry",
]
