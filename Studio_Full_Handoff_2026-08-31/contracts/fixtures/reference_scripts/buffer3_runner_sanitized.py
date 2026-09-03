#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sanitized reference: idempotent, per-project buffer controller.

The controller advances every project independently.  Permanent evidence or
safety violations quarantine only that project; transport/service failures set
a durable ``next_attempt_at``.  The only client-submitted VideoStudio stages
are tts, images and render.  Render may be accepted by the server as a safe
deferred render while inputs are still being prepared.

This copy contains no manifest, channel/profile routing, hosts, sessions, or
credentials.  It is intentionally reference-only and imports a companion
``pipeline`` interface that is not included in the handoff fixtures.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pipeline as p


STATE = p.BASE / "runner_state.json"
LOCK = p.BASE / ".runner.lock"
LOG = p.BASE / "runner.log"  # compatibility with the existing operator tail
EVENTS = p.BASE / "controller_events.jsonl"
RUNTIME_GATES = p.BASE / "runtime_gates.json"
CONTROLLER_DEPENDENCY_KINDS = frozenset({"tts", "prompts", "images", "render"})


def _durable_append(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def emit(event: str, **details: object) -> dict[str, Any]:
    """Append a machine-readable event before returning it to the operator."""
    row: dict[str, Any] = {
        "at": p.now(),
        "event": event,
        "event_id": uuid.uuid4().hex,
        "message": event,
        **details,
    }
    _durable_append(EVENTS, row)
    if LOG != EVENTS:
        _durable_append(LOG, row)
    print(json.dumps(row, ensure_ascii=False, sort_keys=True), flush=True)
    return row


def state() -> dict[str, Any]:
    value = p.load_json(
        STATE,
        {"manifest_sha256": p.manifest_digest(), "waves": {}, "started_at": p.now()},
    )
    if not isinstance(value, dict):
        raise p.GuardError("runner state is not an object")
    value.setdefault("projects", {})
    return value


def save(value: dict[str, Any]) -> None:
    if value.get("manifest_sha256") != p.manifest_digest():
        raise p.GuardError("runner state belongs to a different manifest")
    p.atomic_json(STATE, value)


def runtime_gates() -> dict[str, bool]:
    """Load staged production gates without changing content-proof policy.

    This file is intentionally separate from ``controller_policy.json``: the
    latter is bound into rewrite result proofs, while this switch is an
    operational rollout decision that may change after a server canary.
    """
    value = p.load_json(RUNTIME_GATES)
    if not isinstance(value, dict):
        raise p.GuardError(f"runtime gates are not an object: {RUNTIME_GATES}")
    if value.get("schema_version") != 1:
        raise p.GuardError("runtime gates schema_version must be 1")
    enabled = value.get("prompt_images_enabled")
    if type(enabled) is not bool:
        raise p.GuardError("runtime gate prompt_images_enabled must be boolean")
    unknown = set(value) - {"schema_version", "prompt_images_enabled", "reason", "updated_at"}
    if unknown:
        raise p.GuardError(f"unknown runtime gates: {sorted(unknown)}")
    return {"prompt_images_enabled": enabled}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def project_control(value: dict[str, Any], key: str) -> dict[str, Any]:
    return value.setdefault("projects", {}).setdefault(key, {})


def is_quarantined(value: dict[str, Any], key: str) -> bool:
    return bool(project_control(value, key).get("quarantine"))


def is_due(value: dict[str, Any], key: str, *, at: datetime | None = None) -> bool:
    next_attempt = _parse_time(project_control(value, key).get("next_attempt_at"))
    return next_attempt is None or next_attempt <= (at or _utcnow())


def quarantine(
    value: dict[str, Any], key: str, stage: str, error: object, **evidence: object
) -> None:
    control = project_control(value, key)
    detail = str(error)[:2000]
    current = control.get("quarantine") or {}
    if current.get("stage") == stage and current.get("error") == detail:
        return
    record = {"at": p.now(), "stage": stage, "error": detail, **evidence}
    control["quarantine"] = record
    control.pop("transient", None)
    control.pop("next_attempt_at", None)
    save(value)
    p.append_journal(key, "controller_quarantined", **record)
    emit("project_quarantined", key=key, **record)


def mark_transient(value: dict[str, Any], key: str, stage: str, error: object) -> None:
    if is_quarantined(value, key):
        return
    control = project_control(value, key)
    detail = str(error)[:2000]
    old = control.get("transient") or {}
    existing_next = _parse_time(control.get("next_attempt_at"))
    if (old.get("stage") == stage and old.get("error") == detail
            and existing_next and existing_next > _utcnow()):
        return
    attempts = int(old.get("attempts") or 0) + 1 if old.get("stage") == stage else 1
    schedule = p.controller_policy()["retry"]["transient_backoff_seconds"]
    delay = int(schedule[min(attempts - 1, len(schedule) - 1)])
    next_attempt = (_utcnow() + timedelta(seconds=delay)).isoformat(timespec="seconds")
    record = {
        "attempts": attempts,
        "at": p.now(),
        "error": detail,
        "stage": stage,
    }
    control["transient"] = record
    control["next_attempt_at"] = next_attempt
    save(value)
    p.append_journal(key, "controller_transient", next_attempt_at=next_attempt, **record)
    emit("project_transient", key=key, next_attempt_at=next_attempt, **record)


def clear_transient(value: dict[str, Any], key: str, *, stage_prefix: str | None = None) -> None:
    control = project_control(value, key)
    if "transient" not in control and "next_attempt_at" not in control:
        return
    current = control.get("transient") or {}
    if stage_prefix is not None and not str(current.get("stage") or "").startswith(stage_prefix):
        return
    previous = control.pop("transient", None)
    control.pop("next_attempt_at", None)
    save(value)
    emit("project_retry_cleared", key=key, previous=previous)


def latest_tasks(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Collapse retries to newest task truth for every project/stage."""
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in sorted(
        rows,
        key=lambda item: (str(item.get("created") or ""), int(item.get("job_id") or 0)),
    ):
        latest[(str(row["project_id"]), str(row["kind"]))] = row
    return latest


def _settings_by_pid(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        pid = str(row.get("id") or "")
        try:
            settings = json.loads(row.get("settings") or "{}")
        except (TypeError, json.JSONDecodeError):
            settings = {}
        result[pid] = settings if isinstance(settings, dict) else {}
    return result


def stage_observed(
    kind: str,
    pid: str,
    latest: dict[tuple[str, str], dict[str, Any]],
    settings: dict[str, Any],
) -> bool:
    status = latest.get((pid, kind), {}).get("status")
    if status in {"queued", "running", "done"}:
        return True
    if kind == "images":
        prompt_status = latest.get((pid, "prompts"), {}).get("status")
        if prompt_status in {"queued", "running"} or settings.get("deferred_images_pending"):
            return True
    if kind == "render" and settings.get("deferred_pending"):
        return True
    return False


def stage_submission_allowed(
    kind: str,
    pid: str,
    latest: dict[tuple[str, str], dict[str, Any]],
    *,
    prompt_images_enabled: bool,
) -> bool:
    """Apply the staged provider gate without blocking safe independent work."""
    if kind == "images":
        return prompt_images_enabled
    if kind == "render" and not prompt_images_enabled:
        # While new prompt/image work is gated, never arm deferred render for an
        # immature project. Exact projects whose image task is already done may
        # proceed to the ordinary slideshow render immediately.
        return latest.get((pid, "images"), {}).get("status") == "done"
    return True


def task_failure_requires_quarantine(kind: str, status: str) -> bool:
    return kind in CONTROLLER_DEPENDENCY_KINDS and status in {"error", "failed", "cancelled"}


def submit_stage(
    kind: str,
    key: str,
    pid: str,
    vs_value: dict[str, Any],
) -> dict[str, Any]:
    """Submit one PID so response accounting is attributable to one project."""
    if kind not in p.SAFE_BATCH_KINDS or kind in p.FORBIDDEN_BATCH_KINDS:
        raise p.QuarantineError(f"unsafe controller stage: {kind}")
    emit("stage_submit_intent", key=key, pid=pid, kind=kind)
    result = p.vs_batch(kind, [pid])
    if result.get("kind") not in {None, kind}:
        raise p.QuarantineError(f"VS {kind} response kind differs: {result}")
    enqueued = int(result.get("enqueued") or 0)
    deferred = int(result.get("deferred") or 0)
    skipped = int(result.get("skipped") or 0)
    blocked = int(result.get("blocked") or 0)
    if blocked:
        raise p.TransientError(f"VS {kind} blocked: {result}")
    if kind != "render" and deferred:
        raise p.QuarantineError(f"VS {kind} unexpectedly deferred: {result}")
    accepted = enqueued + deferred + skipped
    if accepted != 1:
        exc = p.TransientError if accepted == 0 else p.QuarantineError
        raise exc(f"VS {kind} accounting mismatch for {pid}: {result}")
    project = vs_value["projects"][key]
    previous = project.setdefault("requested_stages", {}).get(kind) or {}
    project["requested_stages"][kind] = {
        "at": p.now(),
        "attempts": int(previous.get("attempts") or 0) + 1,
        "mode": "deferred" if deferred else "enqueued_or_existing",
        "response": result,
    }
    p.save_vs_state(vs_value)
    p.append_journal(key, "controller_stage_accepted", kind=kind, pid=pid, response=result)
    emit(
        "stage_accepted",
        key=key,
        pid=pid,
        kind=kind,
        mode=project["requested_stages"][kind]["mode"],
        response=result,
    )
    return result


def _handle_project_error(
    value: dict[str, Any], key: str, stage: str, exc: BaseException
) -> None:
    if isinstance(exc, p.QuarantineError):
        quarantine(value, key, stage, exc, error_type=type(exc).__name__)
    elif isinstance(exc, p.TransientError):
        mark_transient(value, key, stage, exc)
    else:
        # Unknown exceptions are code/config defects, not a licence for an
        # infinite project backoff. Let the oneshot fail loudly and preserve all
        # state so launchd and monitoring can surface one actionable incident.
        raise exc


def advance_rewrites(
    value: dict[str, Any], jobs: list[dict[str, Any]]
) -> tuple[dict[str, Any], Counter[str]]:
    rewrite = p.rewrite_state()
    due_keys = [job["key"] for job in jobs
                if not is_quarantined(value, job["key"]) and is_due(value, job["key"])]
    try:
        p.refresh_rewrite_state(rewrite, keys=due_keys)
    except p.TransientError as exc:
        for key in due_keys:
            mark_transient(value, key, "rewrite_poll", exc)
        return rewrite, Counter(row.get("status", "missing")
                                for row in rewrite.get("jobs", {}).values())

    for job in jobs:
        key = job["key"]
        if is_quarantined(value, key) or not is_due(value, key):
            continue
        row = rewrite.get("jobs", {}).get(key)
        if not isinstance(row, dict):
            continue
        if row.get("poll_error"):
            if row.get("poll_error_disposition") == "quarantine":
                quarantine(value, key, "rewrite_poll", row["poll_error"])
            else:
                mark_transient(value, key, "rewrite_poll", row["poll_error"])
            continue
        status = row.get("status")
        if status in {"failed", "cancelled", "interrupted", "partial"}:
            # This buffer requests exactly one language.  A partial aggregate is
            # not accepted without a separate requested-language completion proof.
            quarantine(value, key, "rewrite_terminal", f"status={status}: {row.get('error') or ''}")
            continue
        if status != "completed":
            clear_transient(value, key, stage_prefix="rewrite")
            continue
        try:
            result = p.fetch_rewrite_job(job, row, rewrite)
        except Exception as exc:  # noqa: BLE001 - disposition is per project
            _handle_project_error(value, key, "rewrite_fetch", exc)
            continue
        clear_transient(value, key, stage_prefix="rewrite")
        if result["status"] == "fetched":
            emit(
                "rewrite_output_verified",
                key=key,
                output_sha256=result["proof"]["output_sha256"],
                output_words=result["proof"]["output_words"],
                source_words=result["proof"]["source_words"],
                policy_version=result["proof"]["policy_version"],
            )
    return rewrite, Counter(row.get("status", "missing")
                            for row in rewrite.get("jobs", {}).values())


def advance_vs_projects(
    value: dict[str, Any], jobs: list[dict[str, Any]], rewrite: dict[str, Any]
) -> dict[str, Any]:
    vs_value = p.vs_state()
    candidates: list[dict[str, Any]] = []
    for job in jobs:
        key = job["key"]
        row = rewrite.get("jobs", {}).get(key, {})
        source = rewrite.get("sources", {}).get(job["source"], {})
        if (not is_quarantined(value, key) and is_due(value, key)
                and p.local_rewrite_proof_valid(job, row, source, p.TEXTS / f"{key}.txt")):
            candidates.append(job)
    if not candidates:
        return vs_value
    try:
        remote = p._vs_projects()
    except p.TransientError as exc:
        for job in candidates:
            mark_transient(value, job["key"], "vs_project_lookup", exc)
        return vs_value
    for job in candidates:
        key = job["key"]
        was_bound = key in vs_value.get("projects", {})
        if not was_bound and p.project_name(job) not in remote:
            emit(
                "vs_project_upload_intent",
                key=key,
                project_name=p.project_name(job),
                text_sha256=p.sha256_file(p.TEXTS / f"{key}.txt"),
            )
        try:
            bound = p.ensure_vs_project(job, remote, vs_value)
        except Exception as exc:  # noqa: BLE001 - preserve all other project IDs
            _handle_project_error(value, key, "vs_project", exc)
            continue
        clear_transient(value, key, stage_prefix="vs_project")
        if not was_bound:
            emit("vs_project_bound", key=key, pid=bound["pid"], project_name=bound["name"])
    return vs_value


def advance_vs_stages(
    value: dict[str, Any], jobs: list[dict[str, Any]], vs_value: dict[str, Any]
) -> tuple[bool, Counter[tuple[str, str]]]:
    by_key = {job["key"]: job for job in jobs}
    due_projects = {
        key: row for key, row in vs_value.get("projects", {}).items()
        if key in by_key and not is_quarantined(value, key) and is_due(value, key)
    }
    if not due_projects:
        return False, Counter()
    pid_to_key = {str(row["pid"]): key for key, row in due_projects.items()}
    pids = list(pid_to_key)
    try:
        tasks = p._vs_task_rows(pids)
        settings_rows = p._vs_settings_rows(pids)
    except p.TransientError as exc:
        for key in due_projects:
            mark_transient(value, key, "vs_observe", exc)
        return False, Counter()

    violations = p.classify_vs_safety(tasks, settings_rows)
    for pid, reasons in violations.items():
        key = pid_to_key.get(pid)
        if key:
            quarantine(value, key, "vs_safety", "; ".join(reasons), pid=pid)
    latest = latest_tasks(tasks)
    settings = _settings_by_pid(settings_rows)
    gates = runtime_gates()
    for key in due_projects:
        clear_transient(value, key, stage_prefix="vs_observe")
    for (pid, kind), task in latest.items():
        # Chapters may be created automatically by the server after render.
        # Their success/failure is outside this controller's submission and
        # completion contract, so they are observed but never quarantined here.
        if task_failure_requires_quarantine(kind, str(task.get("status") or "")):
            key = pid_to_key.get(pid)
            if key and not is_quarantined(value, key):
                quarantine(
                    value,
                    key,
                    "vs_task",
                    task.get("error") or f"{kind} status={task.get('status')}",
                    pid=pid,
                    kind=kind,
                    job_id=task.get("job_id"),
                )

    # One PID per request gives exact accounting and isolation.  Render is
    # intentionally submitted in the same cycle: the server may safely defer it.
    for key, project in due_projects.items():
        if is_quarantined(value, key) or not is_due(value, key):
            continue
        pid = str(project["pid"])
        project_settings = settings.get(pid, {})
        for kind in ("tts", "images", "render"):
            if stage_observed(kind, pid, latest, project_settings):
                continue
            if not stage_submission_allowed(
                kind,
                pid,
                latest,
                prompt_images_enabled=gates["prompt_images_enabled"],
            ):
                continue
            try:
                submit_stage(kind, key, pid, vs_value)
            except Exception as exc:  # noqa: BLE001 - no fail-all
                _handle_project_error(value, key, f"vs_submit_{kind}", exc)
                break
            clear_transient(value, key, stage_prefix=f"vs_submit_{kind}")

    counts: Counter[tuple[str, str]] = Counter(
        (str(row.get("kind")), str(row.get("status"))) for row in latest.values()
    )
    completed_keys = {
        pid_to_key[pid]
        for (pid, kind), row in latest.items()
        if kind == "render" and row.get("status") == "done" and pid in pid_to_key
    }
    all_done = len(completed_keys) == len(jobs)
    emit(
        "vs_snapshot",
        projects=len(vs_value.get("projects", {})),
        render_done=len(completed_keys),
        quarantined=sum(is_quarantined(value, job["key"]) for job in jobs),
        task_counts={f"{kind}:{status}": count for (kind, status), count in sorted(counts.items())},
        prompt_images_enabled=gates["prompt_images_enabled"],
    )
    return all_done, counts


def advance_once(value: dict[str, Any]) -> bool:
    p.validate_manifest()
    gates = runtime_gates()
    jobs = p.expand_jobs()
    rewrite, rewrite_counts = advance_rewrites(value, jobs)
    vs_value = advance_vs_projects(value, jobs, rewrite)
    complete, task_counts = advance_vs_stages(value, jobs, vs_value)
    value["last_cycle_at"] = p.now()
    save(value)
    emit(
        "controller_snapshot",
        jobs=len(jobs),
        rewrite_statuses=dict(rewrite_counts),
        projects=len(vs_value.get("projects", {})),
        quarantined=sum(is_quarantined(value, job["key"]) for job in jobs),
        transient=sum(bool(project_control(value, job["key"]).get("transient")) for job in jobs),
        task_counts={f"{kind}:{status}": count for (kind, status), count in sorted(task_counts.items())},
        prompt_images_enabled=gates["prompt_images_enabled"],
    )
    return complete


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--once", action="store_true")
    result.add_argument("--interval", type=int, default=60)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not 30 <= args.interval <= 180:
        raise SystemExit("--interval must be 30..180 seconds")
    LOCK.touch(exist_ok=True)
    with LOCK.open("r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another buffer3 runner is already active", file=sys.stderr)
            return 3
        value = state()
        save(value)
        policy = p.controller_policy()
        emit(
            "runner_start",
            once=args.once,
            interval=args.interval,
            manifest_sha256=p.manifest_digest(),
            policy_version=policy["policy_version"],
            policy_sha256=p.controller_policy_digest(),
        )
        while True:
            try:
                complete = advance_once(value)
            except Exception as exc:  # noqa: BLE001 - global config/code failure, not project evidence
                emit(
                    "controller_cycle_error",
                    error=str(exc)[:4000],
                    error_type=type(exc).__name__,
                )
                return 2
            if complete:
                if not value.get("completed_at"):
                    value["completed_at"] = p.now()
                    save(value)
                    emit("runner_complete", renders=len(p.expand_jobs()))
                return 0
            if args.once:
                return 0
            time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
