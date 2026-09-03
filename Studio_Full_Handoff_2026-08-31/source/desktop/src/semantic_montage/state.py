"""Durable SQLite/WAL checkpoints for autonomous montage runs."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping


class StateError(RuntimeError):
    pass


class JobState:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                  run_id TEXT PRIMARY KEY,
                  project_id TEXT NOT NULL,
                  project_hash TEXT NOT NULL,
                  status TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  terminal_error TEXT
                );
                CREATE TABLE IF NOT EXISTS stages (
                  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                  stage_name TEXT NOT NULL,
                  input_hash TEXT NOT NULL,
                  output_hash TEXT,
                  status TEXT NOT NULL,
                  attempt INTEGER NOT NULL DEFAULT 0,
                  claim_token TEXT,
                  started_at REAL,
                  finished_at REAL,
                  result_json TEXT,
                  error TEXT,
                  PRIMARY KEY (run_id, stage_name)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                  stage_name TEXT NOT NULL,
                  artifact_id TEXT NOT NULL,
                  relative_path TEXT NOT NULL,
                  sha256 TEXT NOT NULL,
                  bytes INTEGER NOT NULL,
                  metadata_json TEXT NOT NULL,
                  PRIMARY KEY (run_id, artifact_id)
                );
                CREATE TABLE IF NOT EXISTS paid_operations (
                  provider TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  operation_kind TEXT NOT NULL,
                  request_hash TEXT NOT NULL,
                  status TEXT NOT NULL,
                  claim_token TEXT,
                  provider_job_id TEXT,
                  receipt_json TEXT,
                  result_json TEXT,
                  error TEXT,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  PRIMARY KEY (provider, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS repair_attempts (
                  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                  slot_id TEXT NOT NULL,
                  attempt INTEGER NOT NULL,
                  reason_code TEXT NOT NULL,
                  banned_json TEXT NOT NULL,
                  result_json TEXT,
                  created_at REAL NOT NULL,
                  PRIMARY KEY (run_id, slot_id, attempt)
                );
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        for key in ("result_json", "metadata_json", "receipt_json", "banned_json"):
            if key in value and value[key] is not None:
                value[key[:-5] if key.endswith("_json") else key] = json.loads(value[key])
        return value

    def ensure_run(self, project_id: str, project_hash: str, run_id: str | None = None) -> str:
        now = time.time()
        with self.transaction() as connection:
            if run_id:
                row = connection.execute(
                    "SELECT * FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if row:
                    if row["project_id"] != project_id or row["project_hash"] != project_hash:
                        raise StateError("run_id belongs to a different project fingerprint")
                    return run_id
            run_id = run_id or f"run-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
            connection.execute(
                "INSERT INTO runs(run_id,project_id,project_hash,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (run_id, project_id, project_hash, "running", now, now),
            )
        return run_id

    def claim_stage(self, run_id: str, stage_name: str, input_hash: str) -> dict[str, Any]:
        now = time.time()
        token = uuid.uuid4().hex
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM stages WHERE run_id=? AND stage_name=?",
                (run_id, stage_name),
            ).fetchone()
            if row and row["input_hash"] == input_hash and row["status"] == "completed":
                return {"action": "cached", "row": self._row(row)}
            attempt = int(row["attempt"] if row else 0) + 1
            connection.execute(
                "INSERT INTO stages(run_id,stage_name,input_hash,status,attempt,claim_token,started_at,finished_at,result_json,error) "
                "VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL) "
                "ON CONFLICT(run_id,stage_name) DO UPDATE SET "
                "input_hash=excluded.input_hash,output_hash=NULL,status='running',attempt=excluded.attempt,"
                "claim_token=excluded.claim_token,started_at=excluded.started_at,finished_at=NULL,result_json=NULL,error=NULL",
                (run_id, stage_name, input_hash, "running", attempt, token, now),
            )
            connection.execute(
                "UPDATE runs SET status='running',updated_at=?,terminal_error=NULL WHERE run_id=?",
                (now, run_id),
            )
        return {"action": "run", "claim_token": token, "attempt": attempt}

    def complete_stage(
        self,
        run_id: str,
        stage_name: str,
        claim_token: str,
        *,
        output_hash: str,
        result: Mapping[str, Any],
        artifacts: list[Mapping[str, Any]] | None = None,
    ) -> None:
        now = time.time()
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE stages SET status='completed',output_hash=?,result_json=?,finished_at=?,error=NULL "
                "WHERE run_id=? AND stage_name=? AND claim_token=? AND status='running'",
                (output_hash, json.dumps(dict(result), ensure_ascii=False, sort_keys=True),
                 now, run_id, stage_name, claim_token),
            )
            if cursor.rowcount != 1:
                raise StateError(f"stale stage claim: {stage_name}")
            connection.execute(
                "DELETE FROM artifacts WHERE run_id=? AND stage_name=?",
                (run_id, stage_name),
            )
            for row in artifacts or ():
                artifact_id = str(row.get("artifact_id") or "").strip()
                if not artifact_id:
                    raise StateError("stage artifact_id must be non-empty")
                connection.execute(
                    "INSERT INTO artifacts(run_id,stage_name,artifact_id,relative_path,sha256,bytes,metadata_json) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        run_id,
                        stage_name,
                        f"{stage_name}:{artifact_id}",
                        str(row["relative_path"]),
                        str(row["sha256"]),
                        int(row["bytes"]),
                        json.dumps(dict(row.get("metadata") or {}), ensure_ascii=False, sort_keys=True),
                    ),
                )
            connection.execute("UPDATE runs SET updated_at=? WHERE run_id=?", (now, run_id))

    def fail_stage(self, run_id: str, stage_name: str, claim_token: str, error: str) -> None:
        now = time.time()
        message = str(error)[:4000]
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE stages SET status='failed',finished_at=?,error=? "
                "WHERE run_id=? AND stage_name=? AND claim_token=? AND status='running'",
                (now, message, run_id, stage_name, claim_token),
            )
            if cursor.rowcount != 1:
                raise StateError(f"stale stage failure claim: {stage_name}")
            connection.execute(
                "UPDATE runs SET status='failed',updated_at=?,terminal_error=? WHERE run_id=?",
                (now, message, run_id),
            )

    def block_stage(
        self,
        run_id: str,
        stage_name: str,
        claim_token: str,
        *,
        status: str,
        detail: str,
        output_hash: str | None = None,
        result: Mapping[str, Any] | None = None,
        artifacts: list[Mapping[str, Any]] | None = None,
    ) -> None:
        """Persist a discoverable fail-closed terminal such as ``needs_sound``."""

        if not status.startswith("needs_") or not status.replace("_", "").isalnum():
            raise StateError("blocked stage status must be a needs_* code")
        now = time.time()
        message = str(detail)[:4000]
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE stages SET status=?,finished_at=?,error=?,output_hash=?,result_json=? "
                "WHERE run_id=? AND stage_name=? AND claim_token=? AND status='running'",
                (
                    status,
                    now,
                    message,
                    output_hash,
                    (
                        json.dumps(dict(result), ensure_ascii=False, sort_keys=True)
                        if result is not None
                        else None
                    ),
                    run_id,
                    stage_name,
                    claim_token,
                ),
            )
            if cursor.rowcount != 1:
                raise StateError(f"stale blocked-stage claim: {stage_name}")
            connection.execute(
                "DELETE FROM artifacts WHERE run_id=? AND stage_name=?",
                (run_id, stage_name),
            )
            for row in artifacts or ():
                artifact_id = str(row.get("artifact_id") or "").strip()
                if not artifact_id:
                    raise StateError("blocked stage artifact_id must be non-empty")
                connection.execute(
                    "INSERT INTO artifacts(run_id,stage_name,artifact_id,relative_path,sha256,bytes,metadata_json) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        run_id,
                        stage_name,
                        f"{stage_name}:{artifact_id}",
                        str(row["relative_path"]),
                        str(row["sha256"]),
                        int(row["bytes"]),
                        json.dumps(dict(row.get("metadata") or {}), ensure_ascii=False, sort_keys=True),
                    ),
                )
            connection.execute(
                "UPDATE runs SET status=?,updated_at=?,terminal_error=? WHERE run_id=?",
                (status, now, message, run_id),
            )

    def invalidate_stage(self, run_id: str, stage_name: str, reason: str) -> None:
        """Invalidate a completed checkpoint whose immutable artifact disappeared."""

        now = time.time()
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE stages SET status='invalidated',output_hash=NULL,error=?,finished_at=? "
                "WHERE run_id=? AND stage_name=? AND status='completed'",
                (str(reason)[:4000], now, run_id, stage_name),
            )
            if cursor.rowcount != 1:
                raise StateError(f"cannot invalidate non-completed stage: {stage_name}")
            connection.execute(
                "DELETE FROM artifacts WHERE run_id=? AND stage_name=?",
                (run_id, stage_name),
            )
            connection.execute(
                "UPDATE runs SET status='running',updated_at=?,terminal_error=NULL WHERE run_id=?",
                (now, run_id),
            )

    def stage(self, run_id: str, stage_name: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM stages WHERE run_id=? AND stage_name=?",
                (run_id, stage_name),
            ).fetchone()
        return self._row(row)

    def stages(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM stages WHERE run_id=? ORDER BY started_at,stage_name", (run_id,)
            ).fetchall()
        return [self._row(row) or {} for row in rows]

    def artifacts(self, run_id: str, stage_name: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM artifacts WHERE run_id=?"
        parameters: tuple[Any, ...] = (run_id,)
        if stage_name is not None:
            query += " AND stage_name=?"
            parameters = (run_id, stage_name)
        query += " ORDER BY stage_name,artifact_id"
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        result = []
        for raw in rows:
            row = self._row(raw) or {}
            artifact_id = str(row.get("artifact_id") or "")
            prefix = f"{row.get('stage_name')}:"
            if artifact_id.startswith(prefix):
                row["artifact_id"] = artifact_id[len(prefix):]
            result.append(row)
        return result

    def mark_run_complete(self, run_id: str) -> None:
        now = time.time()
        with self.transaction() as connection:
            connection.execute(
                "UPDATE runs SET status='completed',updated_at=?,terminal_error=NULL WHERE run_id=?",
                (now, run_id),
            )

    def run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return self._row(row)

    def claim_paid_operation(
        self,
        *,
        provider: str,
        idempotency_key: str,
        operation_kind: str,
        request_hash: str,
    ) -> dict[str, Any]:
        now = time.time()
        token = uuid.uuid4().hex
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM paid_operations WHERE provider=? AND idempotency_key=?",
                (provider, idempotency_key),
            ).fetchone()
            if row:
                if row["request_hash"] != request_hash or row["operation_kind"] != operation_kind:
                    raise StateError("idempotency key reused for a different paid request")
                # Even `claimed` is resumable rather than executable again. A
                # crash may have happened after the provider accepted a paid
                # request but before its receipt reached SQLite. Re-submitting
                # would risk a duplicate charge; missing provider_job_id is an
                # explicit reconciliation state, never a silent retry.
                if row["status"] in {"claimed", "submitted", "completed"}:
                    return {"action": "resume", "row": self._row(row)}
            connection.execute(
                "INSERT INTO paid_operations(provider,idempotency_key,operation_kind,request_hash,status,claim_token,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(provider,idempotency_key) DO UPDATE SET status='claimed',claim_token=excluded.claim_token,error=NULL,updated_at=excluded.updated_at",
                (provider, idempotency_key, operation_kind, request_hash, "claimed", token, now, now),
            )
        return {"action": "execute", "claim_token": token}

    def record_paid_submission(
        self, provider: str, idempotency_key: str, claim_token: str, provider_job_id: str,
        receipt: Mapping[str, Any],
    ) -> None:
        now = time.time()
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE paid_operations SET status='submitted',provider_job_id=?,receipt_json=?,updated_at=? "
                "WHERE provider=? AND idempotency_key=? AND claim_token=? AND status='claimed'",
                (provider_job_id, json.dumps(dict(receipt), ensure_ascii=False, sort_keys=True),
                 now, provider, idempotency_key, claim_token),
            )
            if cursor.rowcount != 1:
                raise StateError("stale paid-operation claim")

    def complete_paid_operation(
        self, provider: str, idempotency_key: str, result: Mapping[str, Any]
    ) -> None:
        now = time.time()
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE paid_operations SET status='completed',result_json=?,updated_at=? "
                "WHERE provider=? AND idempotency_key=? AND status IN ('claimed','submitted')",
                (json.dumps(dict(result), ensure_ascii=False, sort_keys=True),
                 now, provider, idempotency_key),
            )
            if cursor.rowcount != 1:
                raise StateError("paid operation is not resumable")


__all__ = ["JobState", "StateError"]
