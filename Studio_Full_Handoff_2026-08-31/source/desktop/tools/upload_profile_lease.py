#!/usr/bin/env python3
"""Atomic coordinator for cross-host AdsPower profile leases.

Run this command on one shared SSH-reachable coordinator.  Each invocation is a
short SQLite transaction; callers renew a token while the upload is alive.  The
coordinator never opens AdsPower and never sees video/channel payloads.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any


SCHEMA_VERSION = 1
MAX_TTL_SECONDS = 6 * 3600
PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TOKEN_RE = re.compile(r"^[0-9a-f]{32,64}$")
HOLDER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
DEFAULT_DB = Path(
    os.getenv(
        "VIDEO_STUDIO_PROFILE_LEASE_DB",
        "~/.local/state/video-studio/profile-leases.sqlite3",
    )
).expanduser()


class LeaseError(ValueError):
    pass


def _validated_identity(profile: str, owner_token: str, holder: str) -> tuple[str, str, str]:
    profile = str(profile or "")
    owner_token = str(owner_token or "")
    holder = str(holder or "")
    if PROFILE_RE.fullmatch(profile) is None:
        raise LeaseError("profile_invalid")
    if TOKEN_RE.fullmatch(owner_token) is None:
        raise LeaseError("owner_token_invalid")
    if HOLDER_RE.fullmatch(holder) is None:
        raise LeaseError("holder_invalid")
    return profile, owner_token, holder


def _validated_ttl(value: Any) -> float:
    if isinstance(value, bool):
        raise LeaseError("ttl_invalid")
    try:
        ttl = float(value)
    except (TypeError, ValueError) as exc:
        raise LeaseError("ttl_invalid") from exc
    if not 60 <= ttl <= 6 * 3600:
        raise LeaseError("ttl_invalid")
    return ttl


class LeaseStore:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path).expanduser()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS profile_leases (
                profile TEXT PRIMARY KEY,
                owner_token TEXT NOT NULL,
                holder TEXT NOT NULL,
                acquired_at REAL NOT NULL,
                renewed_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                clock_id TEXT NOT NULL DEFAULT '',
                expires_monotonic REAL NOT NULL DEFAULT 0
            )
            """
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(profile_leases)")
        }
        if "clock_id" not in columns:
            connection.execute(
                "ALTER TABLE profile_leases ADD COLUMN clock_id TEXT NOT NULL DEFAULT ''"
            )
        if "expires_monotonic" not in columns:
            connection.execute(
                "ALTER TABLE profile_leases ADD COLUMN expires_monotonic REAL NOT NULL DEFAULT 0"
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return connection

    @staticmethod
    def _clock() -> tuple[str, float, float]:
        """One coordinator-host clock domain immune to wall-clock corrections."""
        try:
            clock_id = Path("/proc/sys/kernel/random/boot_id").read_text(
                encoding="ascii"
            ).strip()
        except OSError:
            # The production coordinator is Linux.  This deterministic fallback
            # keeps local/offline tests usable without pretending cross-boot
            # reclaim is safe on a platform that has no boot identifier.
            clock_id = f"platform-{sys.platform}"
        monotonic_now = (
            time.clock_gettime(time.CLOCK_BOOTTIME)
            if hasattr(time, "CLOCK_BOOTTIME")
            else time.monotonic()
        )
        return clock_id, monotonic_now, time.time()

    @staticmethod
    def _row(row: tuple[Any, ...] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "profile": row[0],
            "owner_token": row[1],
            "holder": row[2],
            "acquired_at": row[3],
            "renewed_at": row[4],
            "expires_at": row[5],
            "clock_id": row[6],
            "expires_monotonic": row[7],
        }

    @staticmethod
    def _public(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            key: row[key]
            for key in (
                "profile", "holder", "acquired_at", "renewed_at", "expires_at"
            )
        }

    @staticmethod
    def _active(
        row: dict[str, Any] | None,
        *,
        clock_id: str,
        monotonic_now: float,
    ) -> bool:
        if row is None:
            return False
        if row["clock_id"] == clock_id:
            return row["expires_monotonic"] > monotonic_now
        # After a coordinator reboot, never reclaim an old lease until one full
        # maximum TTL has elapsed in the new boot.  This avoids a forward wall-
        # clock jump creating two live owners.  The old owner may renew sooner.
        return monotonic_now < MAX_TTL_SECONDS

    def acquire(
        self,
        profile: str,
        owner_token: str,
        holder: str,
        ttl_seconds: float,
        *,
        now: float | None = None,
        monotonic_now: float | None = None,
        clock_id: str | None = None,
    ) -> dict[str, Any]:
        profile, owner_token, holder = _validated_identity(profile, owner_token, holder)
        ttl = _validated_ttl(ttl_seconds)
        current_clock, current_mono, current_wall = self._clock()
        current = current_wall if now is None else float(now)
        current_mono = current_mono if monotonic_now is None else float(monotonic_now)
        current_clock = current_clock if clock_id is None else str(clock_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            found = connection.execute(
                "SELECT profile, owner_token, holder, acquired_at, renewed_at, expires_at, "
                "clock_id, expires_monotonic "
                "FROM profile_leases WHERE profile = ?",
                (profile,),
            ).fetchone()
            row = self._row(found)
            row_active = self._active(
                row, clock_id=current_clock, monotonic_now=current_mono
            )
            if row and row["owner_token"] != owner_token and row_active:
                connection.execute("COMMIT")
                return {
                    "schema_version": SCHEMA_VERSION,
                    "acquired": False,
                    "lease": self._public(row),
                }
            acquired_at = (
                row["acquired_at"]
                if row and row["owner_token"] == owner_token and row_active
                else current
            )
            expires_at = current + ttl
            expires_monotonic = current_mono + ttl
            connection.execute(
                """
                INSERT INTO profile_leases(
                    profile, owner_token, holder, acquired_at, renewed_at, expires_at,
                    clock_id, expires_monotonic
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile) DO UPDATE SET
                    owner_token=excluded.owner_token,
                    holder=excluded.holder,
                    acquired_at=excluded.acquired_at,
                    renewed_at=excluded.renewed_at,
                    expires_at=excluded.expires_at,
                    clock_id=excluded.clock_id,
                    expires_monotonic=excluded.expires_monotonic
                """,
                (
                    profile, owner_token, holder, acquired_at, current, expires_at,
                    current_clock, expires_monotonic,
                ),
            )
            connection.execute("COMMIT")
            return {
                "schema_version": SCHEMA_VERSION,
                "acquired": True,
                "lease": self._public({
                    "profile": profile,
                    "owner_token": owner_token,
                    "holder": holder,
                    "acquired_at": acquired_at,
                    "renewed_at": current,
                    "expires_at": expires_at,
                    "clock_id": current_clock,
                    "expires_monotonic": expires_monotonic,
                }),
            }
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def renew(
        self,
        profile: str,
        owner_token: str,
        holder: str,
        ttl_seconds: float,
        *,
        now: float | None = None,
        monotonic_now: float | None = None,
        clock_id: str | None = None,
    ) -> dict[str, Any]:
        profile, owner_token, holder = _validated_identity(profile, owner_token, holder)
        ttl = _validated_ttl(ttl_seconds)
        current_clock, current_mono, current_wall = self._clock()
        current = current_wall if now is None else float(now)
        current_mono = current_mono if monotonic_now is None else float(monotonic_now)
        current_clock = current_clock if clock_id is None else str(clock_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            found = connection.execute(
                "SELECT profile, owner_token, holder, acquired_at, renewed_at, expires_at, "
                "clock_id, expires_monotonic "
                "FROM profile_leases WHERE profile = ?",
                (profile,),
            ).fetchone()
            row = self._row(found)
            if (
                row is None
                or row["owner_token"] != owner_token
                or row["holder"] != holder
                or not self._active(
                    row, clock_id=current_clock, monotonic_now=current_mono
                )
            ):
                connection.execute("COMMIT")
                return {
                    "schema_version": SCHEMA_VERSION,
                    "renewed": False,
                    "lease": self._public(row),
                }
            expires_at = current + ttl
            expires_monotonic = current_mono + ttl
            connection.execute(
                "UPDATE profile_leases SET renewed_at = ?, expires_at = ?, "
                "clock_id = ?, expires_monotonic = ? "
                "WHERE profile = ? AND owner_token = ?",
                (
                    current, expires_at, current_clock, expires_monotonic,
                    profile, owner_token,
                ),
            )
            connection.execute("COMMIT")
            row.update({
                "renewed_at": current,
                "expires_at": expires_at,
                "clock_id": current_clock,
                "expires_monotonic": expires_monotonic,
            })
            return {
                "schema_version": SCHEMA_VERSION,
                "renewed": True,
                "lease": self._public(row),
            }
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def release(
        self,
        profile: str,
        owner_token: str,
        holder: str,
    ) -> dict[str, Any]:
        profile, owner_token, holder = _validated_identity(profile, owner_token, holder)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            found = connection.execute(
                "SELECT owner_token, holder FROM profile_leases WHERE profile = ?",
                (profile,),
            ).fetchone()
            if found is None:
                connection.execute("COMMIT")
                return {"schema_version": SCHEMA_VERSION, "released": True, "absent": True}
            if found != (owner_token, holder):
                connection.execute("COMMIT")
                return {"schema_version": SCHEMA_VERSION, "released": False, "absent": False}
            connection.execute(
                "DELETE FROM profile_leases WHERE profile = ? AND owner_token = ?",
                (profile, owner_token),
            )
            connection.execute("COMMIT")
            return {"schema_version": SCHEMA_VERSION, "released": True, "absent": False}
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def inspect(
        self,
        profile: str,
        *,
        now: float | None = None,
        monotonic_now: float | None = None,
        clock_id: str | None = None,
    ) -> dict[str, Any]:
        if PROFILE_RE.fullmatch(str(profile or "")) is None:
            raise LeaseError("profile_invalid")
        current_clock, current_mono, _current_wall = self._clock()
        current_mono = current_mono if monotonic_now is None else float(monotonic_now)
        current_clock = current_clock if clock_id is None else str(clock_id)
        connection = self._connect()
        try:
            found = connection.execute(
                "SELECT profile, owner_token, holder, acquired_at, renewed_at, expires_at, "
                "clock_id, expires_monotonic "
                "FROM profile_leases WHERE profile = ?",
                (profile,),
            ).fetchone()
        finally:
            connection.close()
        row = self._row(found)
        active = self._active(
            row, clock_id=current_clock, monotonic_now=current_mono
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "active": active,
            "lease": self._public(row),
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("acquire", "renew"):
        command = commands.add_parser(name)
        command.add_argument("--profile", required=True)
        command.add_argument("--owner-token", required=True)
        command.add_argument("--holder", required=True)
        command.add_argument("--ttl-seconds", required=True, type=float)
    release = commands.add_parser("release")
    release.add_argument("--profile", required=True)
    release.add_argument("--owner-token", required=True)
    release.add_argument("--holder", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--profile", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = LeaseStore(args.db)
    try:
        if args.command == "acquire":
            result = store.acquire(
                args.profile, args.owner_token, args.holder, args.ttl_seconds
            )
            return_code = 0 if result["acquired"] else 3
        elif args.command == "renew":
            result = store.renew(
                args.profile, args.owner_token, args.holder, args.ttl_seconds
            )
            return_code = 0 if result["renewed"] else 3
        elif args.command == "release":
            result = store.release(args.profile, args.owner_token, args.holder)
            return_code = 0 if result["released"] else 3
        else:
            result = store.inspect(args.profile)
            return_code = 0
    except (LeaseError, sqlite3.Error, OSError, ValueError) as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "error": str(exc)[:160],
        }
        return_code = 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return return_code


if __name__ == "__main__":
    sys.exit(main())
