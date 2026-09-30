# -*- coding: utf-8 -*-
"""Two-phase migration to server-owned credentials and fair provider queues."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set, Tuple


SENSITIVE_JOB_FIELDS = ("api_key", "oauth_token", "oauth_tokens")
SENSITIVE_USER_KEY_COLUMNS = (
    "openai_api_key",
    "anthropic_api_key",
    "oauth_tokens",
)
MIGRATION_ID = "shared_keys_fair_queue_001"
_REDACTED_LEGACY_SECRET = "[REDACTED_LEGACY_CREDENTIAL]"


def shared_keys_enabled() -> bool:
    return os.environ.get("REWRITE_SHARED_KEYS_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _column_names(
    conn: sqlite3.Connection, table: str, schema: str = "main"
) -> set:
    return {
        row[1]
        for row in conn.execute(
            f"PRAGMA {schema}.table_info({table})"
        ).fetchall()
    }


def _sqlite_backup(source_path: str, destination_path: str) -> None:
    fd = os.open(
        destination_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    os.close(fd)
    source = sqlite3.connect(source_path, timeout=15.0)
    destination = sqlite3.connect(destination_path, timeout=15.0)
    try:
        source.execute("PRAGMA busy_timeout=15000")
        source.backup(destination)
    finally:
        destination.close()
        source.close()


def backup_shared_key_databases(
    sessions_db_path: str,
    jobs_db_path: str,
    *,
    timestamp: str = "",
) -> Dict[str, str]:
    """Create SQLite-consistent backups sharing one timestamp."""
    stamp = timestamp or (
        time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        + f"_{time.time_ns() % 1_000_000_000:09d}"
    )
    source_paths = [
        Path(raw_path).resolve()
        for raw_path in (sessions_db_path, jobs_db_path)
    ]
    unique_stamp = stamp
    collision_index = 0
    while any(
        path.with_name(
            f"{path.name}.bak.shared_keys_fair_queue_001.{unique_stamp}"
        ).exists()
        for path in source_paths
    ):
        collision_index += 1
        unique_stamp = f"{stamp}.{collision_index}"
    result: Dict[str, str] = {}
    completed_sources: Dict[str, str] = {}
    for label, raw_path in (
        ("sessions", sessions_db_path),
        ("jobs", jobs_db_path),
    ):
        path = Path(raw_path).resolve()
        canonical = str(path)
        if canonical in completed_sources:
            result[label] = completed_sources[canonical]
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            sqlite3.connect(str(path)).close()
        backup_path = path.with_name(
            f"{path.name}.bak.shared_keys_fair_queue_001.{unique_stamp}"
        )
        _sqlite_backup(str(path), str(backup_path))
        result[label] = str(backup_path)
        completed_sources[canonical] = str(backup_path)
    return result


def _contains_sensitive_job_field(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            key in SENSITIVE_JOB_FIELDS
            or _contains_sensitive_job_field(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_sensitive_job_field(child) for child in value)
    return False


def _collect_secret_strings(value: Any, result: Set[str]) -> None:
    """Collect plausible exact credentials without ever logging them."""
    if isinstance(value, str):
        candidate = value.strip()
        if len(candidate) >= 8:
            result.add(candidate)
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return
        if decoded != value:
            _collect_secret_strings(decoded, result)
        return
    if isinstance(value, dict):
        for child in value.values():
            _collect_secret_strings(child, result)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _collect_secret_strings(child, result)


def _collect_sensitive_job_secrets(value: Any, result: Set[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in SENSITIVE_JOB_FIELDS:
                _collect_secret_strings(child, result)
            else:
                _collect_sensitive_job_secrets(child, result)
    elif isinstance(value, list):
        for child in value:
            _collect_sensitive_job_secrets(child, result)


def _scrub_job_payload(value: Any, secrets: Set[str]) -> Tuple[Any, bool]:
    """Remove credential fields recursively and redact exact leaked values."""
    if isinstance(value, dict):
        clean: Dict[str, Any] = {}
        changed = False
        for key, child in value.items():
            if key in SENSITIVE_JOB_FIELDS:
                _collect_secret_strings(child, secrets)
                changed = True
                continue
            clean_child, child_changed = _scrub_job_payload(child, secrets)
            clean[key] = clean_child
            changed = changed or child_changed
        return clean, changed
    if isinstance(value, list):
        clean_list: List[Any] = []
        changed = False
        for child in value:
            clean_child, child_changed = _scrub_job_payload(child, secrets)
            clean_list.append(clean_child)
            changed = changed or child_changed
        return clean_list, changed
    if isinstance(value, str):
        clean = value
        for secret in sorted(secrets, key=len, reverse=True):
            clean = clean.replace(secret, _REDACTED_LEGACY_SECRET)
        return clean, clean != value
    return value, False


def _redact_exact_secrets(value: str, secrets: Iterable[str]) -> str:
    clean = value or ""
    for secret in sorted(set(secrets), key=len, reverse=True):
        clean = clean.replace(secret, _REDACTED_LEGACY_SECRET)
    return clean


def _checkpoint_truncate(conn: sqlite3.Connection, schema: str) -> None:
    result = conn.execute(f"PRAGMA {schema}.wal_checkpoint(TRUNCATE)").fetchone()
    if result and int(result[0] or 0) != 0:
        raise RuntimeError(
            f"shared-key migration cannot checkpoint active {schema} database"
        )


def _assert_no_legacy_credentials(
    sessions_db_path: str, jobs_db_path: str
) -> None:
    """Fail closed if credentials reappear after the durable marker."""
    residue_fields = 0
    with sqlite3.connect(sessions_db_path, timeout=15.0) as conn:
        key_cols = _column_names(conn, "user_keys")
        for column in ("openai_api_key", "anthropic_api_key"):
            if column in key_cols:
                residue_fields += conn.execute(
                    f"SELECT COUNT(*) FROM user_keys "
                    f"WHERE COALESCE(TRIM({column}),'')<>''"
                ).fetchone()[0]
        if "oauth_tokens" in key_cols:
            residue_fields += conn.execute(
                "SELECT COUNT(*) FROM user_keys "
                "WHERE LOWER(COALESCE(TRIM(oauth_tokens),'')) "
                "NOT IN ('','[]','null')"
            ).fetchone()[0]

    residue_jobs = 0
    with sqlite3.connect(jobs_db_path, timeout=15.0) as conn:
        if _column_names(conn, "jobs"):
            invalid_jobs = 0
            for (raw,) in conn.execute("SELECT data FROM jobs").fetchall():
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    invalid_jobs += 1
                    continue
                if not isinstance(data, dict):
                    invalid_jobs += 1
                    continue
                if _contains_sensitive_job_field(data):
                    residue_jobs += 1
            if invalid_jobs:
                raise RuntimeError(
                    "shared-key migration marker exists but invalid job JSON "
                    f"was detected (jobs={invalid_jobs})"
                )
    if residue_fields or residue_jobs:
        raise RuntimeError(
            "shared-key migration marker exists but legacy credential "
            f"residue was detected (user_fields={residue_fields}, "
            f"jobs={residue_jobs})"
        )


def migrate_shared_keys_fair_queue(
    sessions_db_path: str,
    jobs_db_path: str,
    *,
    enabled: bool = False,
) -> Tuple[int, int, str]:
    """Scrub legacy vendor credentials after pool preflight.

    Flag-off is deliberately a true no-op: no scrub and no backup. Callers must
    preflight the server credential pools before passing ``enabled=True``.
    """
    if not enabled:
        return (0, 0, "shared_keys_fair_queue_001: disabled; no changes")

    sessions_path = str(Path(sessions_db_path).resolve())
    jobs_path = str(Path(jobs_db_path).resolve())
    Path(sessions_path).parent.mkdir(parents=True, exist_ok=True)
    Path(jobs_path).parent.mkdir(parents=True, exist_ok=True)
    marker_conn = sqlite3.connect(sessions_path, timeout=15.0)
    try:
        marker_exists = marker_conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='rewrite_migration_markers'"
        ).fetchone()
        if marker_exists and marker_conn.execute(
            "SELECT 1 FROM rewrite_migration_markers WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone():
            _assert_no_legacy_credentials(sessions_path, jobs_path)
            return (0, 0, "shared_keys_fair_queue_001: already applied")
    finally:
        marker_conn.close()

    backups = backup_shared_key_databases(sessions_db_path, jobs_db_path)
    user_key_rows = 0
    job_rows = 0

    conn = sqlite3.connect(sessions_path, timeout=15.0, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        jobs_schema = "main"
        if jobs_path != sessions_path:
            conn.execute("ATTACH DATABASE ? AS jobsdb", (jobs_path,))
            jobs_schema = "jobsdb"

        # Parse every payload before the first logical write. A malformed row is
        # a hard stop: otherwise the durable marker could bless an unscrubbed DB.
        parsed_jobs: List[Tuple[str, Dict[str, Any]]] = []
        job_cols = _column_names(conn, "jobs", jobs_schema)
        if job_cols:
            invalid_job_ids: List[str] = []
            for job_id, raw in conn.execute(
                f"SELECT id,data FROM {jobs_schema}.jobs"
            ).fetchall():
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    invalid_job_ids.append(str(job_id))
                    continue
                if not isinstance(data, dict):
                    invalid_job_ids.append(str(job_id))
                    continue
                parsed_jobs.append((job_id, data))
            if invalid_job_ids:
                raise RuntimeError(
                    "shared-key migration aborted: invalid job JSON "
                    f"(count={len(invalid_job_ids)}, first_id={invalid_job_ids[0]})"
                )

        # secure_delete handles updated cells; the one-time offline VACUUM below
        # rebuilds active DB files and removes free-page remnants.
        conn.execute("PRAGMA main.secure_delete=ON")
        if jobs_schema != "main":
            conn.execute("PRAGMA jobsdb.secure_delete=ON")

        secrets: Set[str] = set()
        key_cols = _column_names(conn, "user_keys")
        present_key_cols = [
            column for column in SENSITIVE_USER_KEY_COLUMNS if column in key_cols
        ]
        if present_key_cols:
            select_cols = ",".join(present_key_cols)
            for row in conn.execute(
                f"SELECT {select_cols} FROM user_keys"
            ).fetchall():
                for value in row:
                    _collect_secret_strings(value, secrets)
        for _job_id, data in parsed_jobs:
            _collect_sensitive_job_secrets(data, secrets)

        conn.execute("BEGIN IMMEDIATE")
        try:
            user_cols = _column_names(conn, "users")
            if user_cols:
                if "status" not in user_cols:
                    conn.execute(
                        "ALTER TABLE users ADD COLUMN status TEXT NOT NULL "
                        "DEFAULT 'active'"
                    )
                if "disabled_at" not in user_cols:
                    conn.execute("ALTER TABLE users ADD COLUMN disabled_at REAL")
                conn.execute(
                    "UPDATE users SET status='active' "
                    "WHERE status IS NULL OR status=''"
                )

            for column in SENSITIVE_USER_KEY_COLUMNS:
                if column in key_cols:
                    cur = conn.execute(
                        f"UPDATE user_keys SET {column}='' "
                        f"WHERE COALESCE({column},'')<>''"
                    )
                    user_key_rows += max(0, cur.rowcount)

            if user_cols:
                if _column_names(conn, "sessions"):
                    conn.execute(
                        "DELETE FROM sessions WHERE user_id IN "
                        "(SELECT user_id FROM users WHERE status<>'active')"
                    )
                if _column_names(conn, "api_keys"):
                    conn.execute(
                        "UPDATE api_keys SET status='revoked' "
                        "WHERE status='active' AND user_id IN "
                        "(SELECT user_id FROM users WHERE status<>'active')"
                    )

            if job_cols:
                has_log = "log" in job_cols
                for job_id, data in parsed_jobs:
                    clean_data, changed = _scrub_job_payload(data, secrets)
                    if has_log:
                        raw_log = conn.execute(
                            f"SELECT COALESCE(log,'') FROM {jobs_schema}.jobs "
                            "WHERE id=?",
                            (job_id,),
                        ).fetchone()[0]
                        clean_log = _redact_exact_secrets(raw_log, secrets)
                        if clean_log != raw_log:
                            conn.execute(
                                f"UPDATE {jobs_schema}.jobs SET log=? WHERE id=?",
                                (clean_log, job_id),
                            )
                            changed = True
                    if changed:
                        conn.execute(
                            f"UPDATE {jobs_schema}.jobs SET data=? WHERE id=?",
                            (
                                json.dumps(
                                    clean_data,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ),
                                job_id,
                            ),
                        )
                        job_rows += 1
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rewrite_migration_markers (
                    migration_id TEXT PRIMARY KEY,
                    applied_at REAL NOT NULL
                )
                """
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

        # This migration is run during maintenance before workers start. VACUUM
        # is intentionally one-time and occurs before the marker is committed.
        # Any scrub/checkpoint/VACUUM failure therefore leaves startup fail-closed.
        _checkpoint_truncate(conn, "main")
        if jobs_schema != "main":
            _checkpoint_truncate(conn, jobs_schema)
        conn.execute("VACUUM main")
        if jobs_schema != "main":
            conn.execute(f"VACUUM {jobs_schema}")
        _checkpoint_truncate(conn, "main")
        if jobs_schema != "main":
            _checkpoint_truncate(conn, jobs_schema)

        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO rewrite_migration_markers(migration_id,applied_at) "
                "VALUES (?,?)",
                (MIGRATION_ID, time.time()),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        _checkpoint_truncate(conn, "main")
        if jobs_schema != "main":
            _checkpoint_truncate(conn, jobs_schema)
    finally:
        conn.close()

    message = (
        "shared_keys_fair_queue_001: complete; "
        f"user_key_fields_scrubbed={user_key_rows}, jobs_scrubbed={job_rows}; "
        f"backups={backups['sessions']},{backups['jobs']}"
    )
    return user_key_rows, job_rows, message


if __name__ == "__main__":
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    enabled = shared_keys_enabled()
    if enabled:
        from rewrite_core import preflight_server_credential_pools

        preflight_server_credential_pools()
    print(
        migrate_shared_keys_fair_queue(
            os.environ.get("SESSION_DB", os.path.join(base, "sessions.db")),
            os.environ.get("REWRITE_DB_PATH", os.path.join(base, "job_queue.db")),
            enabled=enabled,
        )[2]
    )
