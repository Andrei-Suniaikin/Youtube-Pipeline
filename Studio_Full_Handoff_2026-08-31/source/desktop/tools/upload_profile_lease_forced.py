#!/usr/bin/env python3
"""Narrow SSH forced-command wrapper for the upload profile coordinator."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys


COORDINATOR = "/opt/video-ops/upload_profile_lease.py"
LEASE_DB = "/var/lib/video-ops-profile-leases/profile-leases.sqlite3"
PAIR_NAMES = {
    "acquire": ("--profile", "--owner-token", "--holder", "--ttl-seconds"),
    "renew": ("--profile", "--owner-token", "--holder", "--ttl-seconds"),
    "release": ("--profile", "--owner-token", "--holder"),
    "inspect": ("--profile",),
}


class ForcedCommandError(ValueError):
    pass


def validated_argv(original: str, allowed_holder: str) -> list[str]:
    try:
        parts = shlex.split(str(original or ""), posix=True)
    except ValueError as exc:
        raise ForcedCommandError("original_command_invalid") from exc
    if len(parts) < 2 or parts[0] != COORDINATOR:
        raise ForcedCommandError("coordinator_command_invalid")
    action = parts[1]
    pair_names = PAIR_NAMES.get(action)
    if pair_names is None or len(parts) != 2 + 2 * len(pair_names):
        raise ForcedCommandError("coordinator_arguments_invalid")
    values: dict[str, str] = {}
    for index, expected in enumerate(pair_names):
        offset = 2 + index * 2
        if parts[offset] != expected or not parts[offset + 1]:
            raise ForcedCommandError("coordinator_arguments_invalid")
        values[expected] = parts[offset + 1]
    if action != "inspect" and values.get("--holder") != allowed_holder:
        raise ForcedCommandError("holder_not_authorized_for_key")
    return [sys.executable, COORDINATOR, "--db", LEASE_DB, *parts[1:]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allowed-holder", required=True, choices=("mac", "minsk"))
    args = parser.parse_args(argv)
    try:
        command = validated_argv(
            os.getenv("SSH_ORIGINAL_COMMAND", ""), args.allowed_holder
        )
    except ForcedCommandError as exc:
        print(
            json.dumps(
                {"schema_version": 1, "error": str(exc)},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    os.execv(command[0], command)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
