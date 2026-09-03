"""Fail-closed project fingerprint rebinds for asset-only pipeline updates.

The regular runner deliberately binds every stage input to the complete project
fingerprint.  That is the safe default, but it also means that replacing an
audio catalogue would invalidate already-paid visual checkpoints.  This module
provides one narrow migration: prove that only explicitly allowed assets
changed, preserve a completed prefix by recomputing its input hashes, and leave
the rerun boundary (and everything after it) stale for the normal runner.

No provider is called here.  The apply operation is one ``BEGIN IMMEDIATE``
SQLite transaction and never mutates results, outputs, artifacts, attempts,
paid-operation receipts, or repair attempts.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .common import canonical_hash, sha256_file
from .project import AssetReference, ProjectConfig
from .runner import StageDefinition, StageRegistry
from .state import JobState


class ProjectRebindError(RuntimeError):
    """Raised when a fingerprint rebind cannot be proven safe."""


OPAQUE_RECORDS_HASH_CONTRACT = "ordered-typed-column-sha256-v1"


@dataclass(frozen=True)
class _Validation:
    disposition: str
    old_project_hash: str
    new_project_hash: str
    asset_changes: tuple[dict[str, Any], ...]
    ordered: tuple[StageDefinition, ...]
    preserved: tuple[str, ...]
    affected: tuple[str, ...]
    expected_old_inputs: Mapping[str, str]
    expected_new_preserved_inputs: Mapping[str, str]
    before_rows: Mapping[str, Any]
    preview_rows: Mapping[str, Any]


def _asset_map(project: ProjectConfig) -> dict[str, AssetReference]:
    rows = {asset.asset_id: asset for asset in project.assets}
    if len(rows) != len(project.assets):
        raise ProjectRebindError("project contains duplicate asset_id values")
    return rows


def _asset_record_hash(asset: AssetReference | None) -> str | None:
    return canonical_hash(asset.to_dict()) if asset is not None else None


def _validate_project_delta(
    old_project: ProjectConfig,
    new_project: ProjectConfig,
    allowed_audio_asset_ids: Collection[str],
) -> tuple[dict[str, Any], ...]:
    exact_fields = (
        ("project_id", old_project.project_id, new_project.project_id),
        ("schema_version", old_project.schema_version, new_project.schema_version),
        ("stages", tuple(old_project.stages), tuple(new_project.stages)),
        ("settings", dict(old_project.settings), dict(new_project.settings)),
        ("secrets", dict(old_project.secrets), dict(new_project.secrets)),
    )
    for field_name, before, after in exact_fields:
        if before != after:
            raise ProjectRebindError(
                f"project field changed outside the asset allowlist: {field_name}"
            )

    allowed = {str(asset_id).strip() for asset_id in allowed_audio_asset_ids}
    if not allowed or "" in allowed:
        raise ProjectRebindError("allowed_audio_asset_ids must contain non-empty asset IDs")

    old_assets = _asset_map(old_project)
    new_assets = _asset_map(new_project)
    changed = {
        asset_id
        for asset_id in old_assets.keys() | new_assets.keys()
        if old_assets.get(asset_id) != new_assets.get(asset_id)
    }
    unexpected = sorted(changed - allowed)
    if unexpected:
        raise ProjectRebindError(
            "unexpected asset changes: " + ", ".join(unexpected)
        )

    # Asset order contributes to ProjectConfig.fingerprint().  Removing the
    # explicitly mutable assets must leave the immutable sequence untouched.
    old_immutable_order = [row.asset_id for row in old_project.assets if row.asset_id not in allowed]
    new_immutable_order = [row.asset_id for row in new_project.assets if row.asset_id not in allowed]
    if old_immutable_order != new_immutable_order:
        raise ProjectRebindError("non-allowed asset ordering changed")

    changes: list[dict[str, Any]] = []
    for asset_id in sorted(changed):
        before = old_assets.get(asset_id)
        after = new_assets.get(asset_id)
        kind = "added" if before is None else "removed" if after is None else "changed"
        changes.append(
            {
                "asset_id": asset_id,
                "kind": kind,
                "before_record_hash": _asset_record_hash(before),
                "after_record_hash": _asset_record_hash(after),
            }
        )
    return tuple(changes)


def _text_sha256(value: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    for offset in range(0, len(value), 1024 * 1024):
        chunk = value[offset : offset + 1024 * 1024].encode("utf-8")
        byte_count += len(chunk)
        digest.update(chunk)
    return digest.hexdigest(), byte_count


def _opaque_record_hash(row: Mapping[str, Any]) -> str:
    columns: list[dict[str, Any]] = []
    for key in sorted(row):
        value = row[key]
        if value is None:
            binding: dict[str, Any] = {"type": "null"}
        elif isinstance(value, str):
            digest, byte_count = _text_sha256(value)
            binding = {
                "type": "text",
                "utf8_bytes": byte_count,
                "utf8_sha256": digest,
            }
        elif isinstance(value, bytes):
            binding = {
                "type": "blob",
                "bytes": len(value),
                "sha256": hashlib.sha256(value).hexdigest(),
            }
        elif isinstance(value, bool):
            binding = {"type": "boolean", "value": value}
        elif isinstance(value, int):
            binding = {"type": "integer", "value": value}
        elif isinstance(value, float):
            binding = {"type": "real", "value": value}
        else:  # SQLite rows should never expose another value type.
            raise ProjectRebindError(f"unsupported SQLite value type: {type(value).__name__}")
        columns.append({"name": str(key), "binding": binding})
    return canonical_hash(
        {
            "contract": OPAQUE_RECORDS_HASH_CONTRACT,
            "columns": columns,
        }
    )


def _summarize_rows(
    rows: Sequence[Mapping[str, Any]] | sqlite3.Cursor,
    *,
    status_field: str | None = None,
) -> dict[str, Any]:
    row_hashes: list[str] = []
    status_counts: Counter[str] = Counter()
    for raw in rows:
        row = dict(raw)
        row_hashes.append(_opaque_record_hash(row))
        if status_field is not None:
            status_counts[str(row.get(status_field))] += 1
    return {
        "count": len(row_hashes),
        "records_hash": canonical_hash(
            {
                "contract": OPAQUE_RECORDS_HASH_CONTRACT,
                "row_hashes": row_hashes,
            }
        ),
        "status_counts": dict(sorted(status_counts.items())),
    }


def _read_rows(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        raise ProjectRebindError(f"unknown run_id: {run_id}")
    stages = connection.execute(
        "SELECT * FROM stages WHERE run_id=? ORDER BY stage_name", (run_id,)
    ).fetchall()
    artifacts = connection.execute(
        "SELECT * FROM artifacts WHERE run_id=? ORDER BY stage_name,artifact_id", (run_id,)
    ).fetchall()
    paid = _summarize_rows(
        connection.execute(
            "SELECT * FROM paid_operations ORDER BY provider,idempotency_key"
        ),
        status_field="status",
    )
    repairs = _summarize_rows(
        connection.execute(
            "SELECT * FROM repair_attempts WHERE run_id=? ORDER BY slot_id,attempt", (run_id,)
        )
    )
    return {
        "run": dict(run),
        "stages": {str(row["stage_name"]): dict(row) for row in stages},
        "artifacts": [dict(row) for row in artifacts],
        "paid_operations": paid,
        "repair_attempts": repairs,
    }


def _registry_order(
    project: ProjectConfig,
    registry: StageRegistry,
    preserved_stage_names: Sequence[str],
    rerun_boundary: str,
) -> tuple[tuple[StageDefinition, ...], tuple[str, ...], tuple[str, ...]]:
    try:
        ordered = tuple(registry.ordered(project.stages))
    except Exception as exc:
        raise ProjectRebindError(f"stage registry cannot order the project: {exc}") from exc
    names = tuple(definition.name for definition in ordered)
    if len(set(names)) != len(names):  # defensive; StageRegistry already enforces this
        raise ProjectRebindError("stage registry contains duplicate names")
    if rerun_boundary not in names:
        raise ProjectRebindError(f"rerun boundary is not in the project DAG: {rerun_boundary}")
    boundary_index = names.index(rerun_boundary)
    preserved = tuple(str(name) for name in preserved_stage_names)
    expected_prefix = names[:boundary_index]
    if preserved != expected_prefix:
        raise ProjectRebindError(
            "preserved_stage_names must be the exact ordered prefix before the rerun boundary"
        )
    if not preserved:
        raise ProjectRebindError("the preserved stage prefix must not be empty")
    return ordered, preserved, names[boundary_index:]


def _expected_inputs(
    *,
    project_hash: str,
    ordered: Sequence[StageDefinition],
    stage_rows: Mapping[str, Mapping[str, Any]],
) -> dict[str, str]:
    expected: dict[str, str] = {}
    for definition in ordered:
        row = stage_rows.get(definition.name)
        if row is None:
            continue
        upstream: dict[str, dict[str, Any]] = {}
        for dependency in definition.dependencies:
            dependency_row = stage_rows.get(dependency)
            if dependency_row is None or dependency not in expected:
                raise ProjectRebindError(
                    f"stored stage {definition.name} is missing dependency {dependency}"
                )
            upstream[dependency] = {
                "input_hash": expected[dependency],
                "output_hash": dependency_row.get("output_hash"),
            }
        expected[definition.name] = canonical_hash(
            {
                "project": project_hash,
                "stage": definition.name,
                "version": definition.version,
                "upstream": upstream,
            }
        )
    return expected


def _artifact_rows_by_stage(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        result.setdefault(str(row.get("stage_name") or ""), []).append(dict(row))
    return result


def _validate_cached_artifacts(
    *,
    project: ProjectConfig,
    preserved: Sequence[str],
    stage_rows: Mapping[str, Mapping[str, Any]],
    artifact_rows: Sequence[Mapping[str, Any]],
) -> None:
    persisted = _artifact_rows_by_stage(artifact_rows)
    for stage_name in preserved:
        row = stage_rows[stage_name]
        raw_result = row.get("result_json")
        if not isinstance(raw_result, str):
            raise ProjectRebindError(f"preserved stage has no result JSON: {stage_name}")
        try:
            result = json.loads(raw_result)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProjectRebindError(f"invalid result JSON for preserved stage: {stage_name}") from exc
        if not isinstance(result, Mapping) or not isinstance(result.get("artifacts"), list):
            raise ProjectRebindError(
                f"preserved stage has no normalized artifact manifest: {stage_name}"
            )

        manifest: list[dict[str, Any]] = []
        for item in result["artifacts"]:
            if not isinstance(item, Mapping):
                raise ProjectRebindError(f"invalid artifact manifest row: {stage_name}")
            relative = str(item.get("relative_path") or "")
            expected_hash = str(item.get("sha256") or "")
            artifact_id = str(item.get("artifact_id") or "")
            path = (project.root / relative).resolve()
            try:
                path.relative_to(project.root)
            except ValueError as exc:
                raise ProjectRebindError(
                    f"cached artifact escapes the project root: {stage_name}:{artifact_id}"
                ) from exc
            if not path.is_file():
                raise ProjectRebindError(
                    f"cached artifact is missing: {stage_name}:{artifact_id}"
                )
            if sha256_file(path) != expected_hash:
                raise ProjectRebindError(
                    f"cached artifact hash mismatch: {stage_name}:{artifact_id}"
                )
            actual_bytes = path.stat().st_size
            if int(item.get("bytes", actual_bytes)) != actual_bytes:
                raise ProjectRebindError(
                    f"cached artifact size mismatch: {stage_name}:{artifact_id}"
                )
            manifest.append(
                {
                    "artifact_id": artifact_id,
                    "relative_path": relative,
                    "sha256": expected_hash,
                    "bytes": actual_bytes,
                    "metadata_json": json.dumps(
                        dict(item.get("metadata") or {}), ensure_ascii=False, sort_keys=True
                    ),
                }
            )

        db_manifest: list[dict[str, Any]] = []
        prefix = f"{stage_name}:"
        for item in persisted.get(stage_name, []):
            stored_id = str(item.get("artifact_id") or "")
            if not stored_id.startswith(prefix):
                raise ProjectRebindError(f"invalid persisted artifact ID prefix: {stage_name}")
            db_manifest.append(
                {
                    "artifact_id": stored_id[len(prefix):],
                    "relative_path": str(item.get("relative_path") or ""),
                    "sha256": str(item.get("sha256") or ""),
                    "bytes": int(item.get("bytes") or 0),
                    "metadata_json": str(item.get("metadata_json") or "{}"),
                }
            )
        manifest.sort(key=lambda item: item["artifact_id"])
        db_manifest.sort(key=lambda item: item["artifact_id"])
        if manifest != db_manifest:
            raise ProjectRebindError(f"artifact manifest disagrees with SQLite: {stage_name}")


def _opaque_rows_hash(rows: Sequence[Mapping[str, Any]]) -> str:
    return str(_summarize_rows(rows)["records_hash"])


def _snapshot(rows: Mapping[str, Any]) -> dict[str, Any]:
    run = dict(rows["run"])
    artifacts_by_stage = _artifact_rows_by_stage(rows["artifacts"])
    stages: list[dict[str, Any]] = []
    for stage_name, raw in sorted(rows["stages"].items()):
        row = dict(raw)
        immutable = {key: value for key, value in row.items() if key != "input_hash"}
        stages.append(
            {
                "stage_name": stage_name,
                "status": row.get("status"),
                "input_hash": row.get("input_hash"),
                "output_hash": row.get("output_hash"),
                "attempt": row.get("attempt"),
                "immutable_record_hash": _opaque_record_hash(immutable),
                "artifact_count": len(artifacts_by_stage.get(stage_name, [])),
                "artifact_records_hash": _opaque_rows_hash(
                    artifacts_by_stage.get(stage_name, [])
                ),
            }
        )
    run_immutable = {key: value for key, value in run.items() if key != "project_hash"}
    paid_operations = dict(rows["paid_operations"])
    repair_attempts = dict(rows["repair_attempts"])
    return {
        "opaque_records_hash_contract": OPAQUE_RECORDS_HASH_CONTRACT,
        "run": {
            "run_id": run.get("run_id"),
            "project_id": run.get("project_id"),
            "project_hash": run.get("project_hash"),
            "status": run.get("status"),
            "immutable_record_hash": _opaque_record_hash(run_immutable),
        },
        "stages": stages,
        "all_artifact_records_hash": _opaque_rows_hash(rows["artifacts"]),
        "paid_operations": {
            "count": int(paid_operations["count"]),
            "status_counts": dict(paid_operations["status_counts"]),
            "records_hash": str(paid_operations["records_hash"]),
        },
        "repair_attempts": {
            "count": int(repair_attempts["count"]),
            "records_hash": str(repair_attempts["records_hash"]),
        },
    }


def _receipt(
    validation: _Validation,
    *,
    mode: str,
    after_rows: Mapping[str, Any],
) -> dict[str, Any]:
    before_snapshot = _snapshot(validation.before_rows)
    after_snapshot = _snapshot(after_rows)
    body: dict[str, Any] = {
        "receipt_version": "project-audio-fingerprint-rebind-v2",
        "mode": mode,
        "disposition": validation.disposition,
        "run_id": str(validation.before_rows["run"]["run_id"]),
        "project_id": str(validation.before_rows["run"]["project_id"]),
        "old_project_hash": validation.old_project_hash,
        "new_project_hash": validation.new_project_hash,
        "asset_changes": [dict(row) for row in validation.asset_changes],
        "preserved_stages": list(validation.preserved),
        "rerun_stages": list(validation.affected),
        "before": before_snapshot,
        "after": after_snapshot,
        "before_snapshot_hash": canonical_hash(before_snapshot),
        "after_snapshot_hash": canonical_hash(after_snapshot),
    }
    body["receipt_hash"] = canonical_hash(body)
    return body


def _validate_with_connection(
    *,
    old_project: ProjectConfig,
    new_project: ProjectConfig,
    state: JobState,
    registry: StageRegistry,
    connection: sqlite3.Connection,
    run_id: str,
    allowed_audio_asset_ids: Collection[str],
    preserved_stage_names: Sequence[str],
    rerun_boundary: str,
) -> _Validation:
    del state  # The open connection is the sole state authority inside this snapshot.
    changes = _validate_project_delta(old_project, new_project, allowed_audio_asset_ids)
    ordered, preserved, affected = _registry_order(
        old_project, registry, preserved_stage_names, rerun_boundary
    )
    rows = _read_rows(connection, run_id)
    run = rows["run"]
    old_hash = old_project.fingerprint()
    new_hash = new_project.fingerprint()
    if run.get("project_id") != old_project.project_id:
        raise ProjectRebindError("run belongs to a different project_id")
    if run.get("project_hash") == old_hash:
        disposition = "ready"
    elif run.get("project_hash") == new_hash:
        disposition = "already_applied"
    else:
        raise ProjectRebindError("stored run fingerprint matches neither old nor new project")

    stage_rows: dict[str, dict[str, Any]] = rows["stages"]
    known = {definition.name for definition in ordered}
    unknown_rows = sorted(set(stage_rows) - known)
    if unknown_rows:
        raise ProjectRebindError("stored run has stages outside the current registry")
    running = sorted(
        name for name, row in stage_rows.items() if row.get("status") == "running"
    )
    if running:
        raise ProjectRebindError("run has active stage claims: " + ", ".join(running))
    if int(rows["paid_operations"]["status_counts"].get("claimed", 0)):
        raise ProjectRebindError("state has active claimed paid operations")

    for stage_name in preserved:
        row = stage_rows.get(stage_name)
        if row is None or row.get("status") != "completed":
            raise ProjectRebindError(
                f"preserved stage is not completed: {stage_name}"
            )

    expected_old = _expected_inputs(
        project_hash=old_hash, ordered=ordered, stage_rows=stage_rows
    )
    expected_new = _expected_inputs(
        project_hash=new_hash, ordered=ordered, stage_rows=stage_rows
    )
    # Only the completed prefix is being preserved across the project
    # fingerprint change, so only those input hashes must reproduce the
    # current registry exactly.  The rerun boundary and its descendants may
    # legitimately contain older completed/blocked checkpoints from an
    # earlier upstream result.  They are intentionally left byte-for-byte
    # unchanged here and will be replaced by the normal runner.
    for stage_name in preserved:
        row = stage_rows[stage_name]
        expected = (
            expected_new[stage_name]
            if disposition == "already_applied"
            else expected_old[stage_name]
        )
        if row.get("input_hash") != expected:
            raise ProjectRebindError(
                f"current registry does not reproduce stored input_hash: {stage_name}"
            )

    _validate_cached_artifacts(
        project=new_project,
        preserved=preserved,
        stage_rows=stage_rows,
        artifact_rows=rows["artifacts"],
    )

    preview = {
        "run": dict(rows["run"]),
        "stages": {name: dict(row) for name, row in rows["stages"].items()},
        "artifacts": rows["artifacts"],
        "paid_operations": rows["paid_operations"],
        "repair_attempts": rows["repair_attempts"],
    }
    if disposition == "ready":
        preview["run"]["project_hash"] = new_hash
        for stage_name in preserved:
            preview["stages"][stage_name]["input_hash"] = expected_new[stage_name]
    return _Validation(
        disposition=disposition,
        old_project_hash=old_hash,
        new_project_hash=new_hash,
        asset_changes=changes,
        ordered=ordered,
        preserved=preserved,
        affected=affected,
        expected_old_inputs=expected_old,
        expected_new_preserved_inputs={name: expected_new[name] for name in preserved},
        before_rows=rows,
        preview_rows=preview,
    )


def validate_project_fingerprint_rebind(
    old_project: ProjectConfig,
    new_project: ProjectConfig,
    state: JobState,
    registry: StageRegistry,
    *,
    run_id: str,
    allowed_audio_asset_ids: Collection[str],
    preserved_stage_names: Sequence[str],
    rerun_boundary: str = "sound_resolution",
) -> dict[str, Any]:
    """Validate and preview a rebind without changing SQLite."""

    connection = state.connect()
    try:
        connection.execute("BEGIN")
        validation = _validate_with_connection(
            old_project=old_project,
            new_project=new_project,
            state=state,
            registry=registry,
            connection=connection,
            run_id=run_id,
            allowed_audio_asset_ids=allowed_audio_asset_ids,
            preserved_stage_names=preserved_stage_names,
            rerun_boundary=rerun_boundary,
        )
        receipt = _receipt(validation, mode="dry_run", after_rows=validation.preview_rows)
        connection.rollback()
        return receipt
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def apply_project_fingerprint_rebind(
    old_project: ProjectConfig,
    new_project: ProjectConfig,
    state: JobState,
    registry: StageRegistry,
    *,
    run_id: str,
    allowed_audio_asset_ids: Collection[str],
    preserved_stage_names: Sequence[str],
    rerun_boundary: str = "sound_resolution",
) -> dict[str, Any]:
    """Atomically apply a validated asset-only fingerprint rebind.

    Repeating the call after a committed migration is a verified no-op and
    returns an ``already_applied`` receipt.
    """

    with state.transaction() as connection:  # JobState uses BEGIN IMMEDIATE.
        validation = _validate_with_connection(
            old_project=old_project,
            new_project=new_project,
            state=state,
            registry=registry,
            connection=connection,
            run_id=run_id,
            allowed_audio_asset_ids=allowed_audio_asset_ids,
            preserved_stage_names=preserved_stage_names,
            rerun_boundary=rerun_boundary,
        )
        if validation.disposition == "ready":
            cursor = connection.execute(
                "UPDATE runs SET project_hash=? WHERE run_id=? AND project_hash=?",
                (validation.new_project_hash, run_id, validation.old_project_hash),
            )
            if cursor.rowcount != 1:
                raise ProjectRebindError("run fingerprint changed during rebind")
            for stage_name in validation.preserved:
                cursor = connection.execute(
                    "UPDATE stages SET input_hash=? "
                    "WHERE run_id=? AND stage_name=? AND status='completed' AND input_hash=?",
                    (
                        validation.expected_new_preserved_inputs[stage_name],
                        run_id,
                        stage_name,
                        validation.expected_old_inputs[stage_name],
                    ),
                )
                if cursor.rowcount != 1:
                    raise ProjectRebindError(
                        f"preserved stage changed during rebind: {stage_name}"
                    )
        after_rows = _read_rows(connection, run_id)
        if _snapshot(after_rows) != _snapshot(validation.preview_rows):
            raise ProjectRebindError("post-update state does not match the validated preview")
        return _receipt(validation, mode="apply", after_rows=after_rows)


__all__ = [
    "ProjectRebindError",
    "apply_project_fingerprint_rebind",
    "validate_project_fingerprint_rebind",
]
