#!/usr/bin/env python3
"""Offline integrity, forbidden-file, secret-pattern and syntax checks."""

from __future__ import annotations

import ast
from datetime import datetime
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".py", ".js", ".ts", ".html", ".css", ".md", ".txt", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sql", ".csv", ".tsv",
    ".sh", ".ps1", ".bat", ".example",
}
FORBIDDEN_NAMES = {
    ".DS_Store", "auth.json", ".vs_session", ".xai_key",
    "video_studio_api_keys.json", "id_rsa", "id_ed25519",
}
FORBIDDEN_SUFFIXES = {
    ".db", ".sqlite", ".sqlite3", ".dump",
    ".pem", ".key", ".p12", ".pfx", ".crt", ".cer",
    ".log", ".bak", ".backup", ".orig", ".old",
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
}
FORBIDDEN_PARTS = {
    "__pycache__", ".pytest_cache", ".git", "node_modules",
    "receipt_inbox", "receipt_outbox",
}
REQUIRED_FILES = {
    "START_HERE.md",
    "MESSAGE_TO_DEVELOPER.md",
    "docs/00_SCOPE_AND_TRUTH.md",
    "docs/01_SYSTEM_IN_PLAIN_RUSSIAN.md",
    "docs/03_VIDEOSTUDIO.md",
    "docs/04_REWRITE_STUDIO.md",
    "docs/05_COVER_STUDIO.md",
    "docs/06_CONTRACTS_AND_INVARIANTS.md",
    "docs/09_TARGET_ARCHITECTURE.md",
    "docs/10_MIGRATION_PLAN.md",
    "docs/11_ACCEPTANCE.md",
    "docs/12_SOURCE_INDEX.md",
    "docs/13_SECURITY.md",
    "docs/14_KNOWN_GAPS_AND_QUESTIONS.md",
    "docs/16_LIVE_DELTA_2026-08-31.md",
    "contracts/openapi/vsgen.openapi.json",
    "contracts/openapi/rewrite.openapi.json",
    "contracts/openapi/cover.openapi.json",
    "contracts/schema/vsgen.schema.sql",
    "contracts/examples/schedule_revision.v1.json",
    "source/vsgen_server/main.py",
    "source/vsgen_server/src/tts_worker.py",
    "source/vsgen_server/ops/backup_db.sh",
    "source/rewrite/server.py",
    "source/cover/cover_server.py",
    "source/cover/cover_gen_settings.json",
    "source/desktop/src/app_main.py",
    "manifest/SOURCE_PROVENANCE.md",
    "manifest/SOURCE_MANIFEST.tsv",
    "config/runtime/current_env_names.example",
    "config/runtime/vsgen-rewrite.pip-list.txt",
    "config/runtime/cover.pip-list.txt",
    "tests/reference/videostudio/test_server_lumean_route.py",
    "tests/reference/videostudio/test_tts_worker_lumean.py",
}


def patterns() -> list[tuple[str, re.Pattern[str]]]:
    # Regex fragments are split so this checker does not flag its own source.
    return [
        ("private-key", re.compile("BEGIN (?:RSA |EC |OPENSSH )?PRIVATE" + " KEY")),
        ("anthropic-key", re.compile("sk" + r"-ant-[A-Za-z0-9_-]{20,}")),
        ("openai-style-key", re.compile("sk" + r"-[A-Za-z0-9_-]{24,}")),
        ("google-key", re.compile("AI" + r"za[0-9A-Za-z_-]{30,}")),
        ("github-token", re.compile("gh" + r"[pousr]_[A-Za-z0-9]{30,}")),
        ("telegram-token", re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")),
        ("bearer-token", re.compile("Bear" + r"er\s+[A-Za-z0-9._~+/-]{24,}")),
        ("aws-access-key", re.compile("AK" + r"IA[0-9A-Z]{16}")),
        ("jwt", re.compile("ey" + r"J[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
        ("studio-api-key", re.compile(r"\b(?:cvr|rwr)_(?!x{20,}\b)[A-Za-z0-9_-]{28,}\b", re.I)),
        ("raw-gist-resource", re.compile(r"gist\.githubusercontent\.com", re.I)),
        ("temporary-hostname", re.compile(r"(?:sslip\.io|hosted-by-vdsina\.com)", re.I)),
    ]


def iter_files():
    for path in sorted(ROOT.rglob("*")):
        if path.is_file():
            yield path


def verify_forbidden_files() -> list[str]:
    errors: list[str] = []
    for path in iter_files():
        rel = path.relative_to(ROOT)
        if path.is_symlink():
            errors.append(f"symlink is not allowed: {rel}")
        if any(part in FORBIDDEN_PARTS for part in rel.parts):
            errors.append(f"forbidden directory: {rel}")
        if path.name in FORBIDDEN_NAMES:
            errors.append(f"forbidden file: {rel}")
        if path.name.startswith(".env") and path.name != ".env.example":
            errors.append(f"forbidden environment file: {rel}")
        lower_name = path.name.lower()
        if (
            path.suffix.lower() in FORBIDDEN_SUFFIXES
            or ".bak." in lower_name
            or lower_name.endswith(("-wal", "-shm"))
        ):
            errors.append(f"forbidden file type: {rel}")
    return errors


def verify_required_files() -> list[str]:
    return [f"required file is missing: {rel}" for rel in sorted(REQUIRED_FILES) if not (ROOT / rel).is_file()]


def verify_secret_patterns() -> list[str]:
    errors: list[str] = []
    checks = patterns()
    for path in iter_files():
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {"Dockerfile", "Makefile"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(ROOT)
        for label, regex in checks:
            if regex.search(text):
                errors.append(f"suspicious {label}: {rel}")
    return errors


def verify_privacy_literals() -> list[str]:
    """Reject real-looking network/account literals without embedding private values here."""
    errors: list[str] = []
    ipv4 = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])")
    email = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
    user_path = re.compile(r"/(?:Users|home)/([A-Za-z0-9._-]+)/")
    admin_id = re.compile(
        r"ADMIN_USER_ID\s*=\s*[\x22\x27][0-9a-f]{10,32}[\x22\x27]",
        re.I,
    )
    youtube_uc = re.compile(r"\bUC[A-Za-z0-9_-]{22}\b")
    doc_networks = tuple(
        ipaddress.ip_network(value)
        for value in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
    )
    allowed_ips = {"0.0.0.0", "127.0.0.1", "169.254.169.254"}

    for path in iter_files():
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {"Dockerfile", "Makefile"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(ROOT)

        for match in ipv4.finditer(text):
            value = match.group(0)
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if value in allowed_ips or any(address in network for network in doc_networks):
                continue
            # Package versions such as opencv==4.14.0.94 are not network addresses.
            if text[max(0, match.start() - 2):match.start()] == "==":
                continue
            errors.append(f"non-example IPv4 literal: {rel}")
            break

        for match in email.finditer(text):
            domain = match.group(1).lower()
            if domain not in {"example.com", "example.invalid"}:
                errors.append(f"non-example email literal: {rel}")
                break

        for match in user_path.finditer(text):
            if match.group(1).lower() != "owner":
                errors.append(f"personal absolute path: {rel}")
                break

        if admin_id.search(text):
            errors.append(f"real-looking hardcoded admin id: {rel}")

        for match in youtube_uc.finditer(text):
            value = match.group(0)
            upper = value.upper()
            if upper.startswith("UCFAKE") or set(value[2:]) <= {"0"}:
                continue
            if value in {"UC1234567890123456789012", "UCabcdefghijklmnopqrstuv"}:
                continue
            errors.append(f"real-looking YouTube channel id: {rel}")
            break

    return errors


def verify_hashes() -> list[str]:
    manifest = ROOT / "SHA256SUMS"
    if not manifest.is_file():
        return ["SHA256SUMS is missing"]
    errors: list[str] = []
    listed: set[Path] = set()
    for line_no, raw in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            expected, relative = raw.split("  ", 1)
        except ValueError:
            errors.append(f"SHA256SUMS:{line_no}: malformed line")
            continue
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts:
            errors.append(f"SHA256SUMS:{line_no}: unsafe path")
            continue
        if rel in listed:
            errors.append(f"SHA256SUMS:{line_no}: duplicate path: {rel}")
            continue
        listed.add(rel)
        path = ROOT / rel
        if not path.is_file():
            errors.append(f"missing: {rel}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            errors.append(f"checksum mismatch: {rel}")
    actual_files = {
        path.relative_to(ROOT)
        for path in iter_files()
        if path.name != "SHA256SUMS"
    }
    for rel in sorted(actual_files - listed):
        errors.append(f"not listed in SHA256SUMS: {rel}")
    return errors


def verify_source_manifest() -> list[str]:
    """Require exact source-tree coverage and current byte/hash metadata."""
    manifest = ROOT / "manifest" / "SOURCE_MANIFEST.tsv"
    if not manifest.is_file():
        return ["manifest/SOURCE_MANIFEST.tsv is missing"]

    errors: list[str] = []
    lines = manifest.read_text(encoding="utf-8").splitlines()
    expected_header = ["path", "component", "origin", "captured_at", "bytes", "sha256"]
    if not lines or lines[0].split("\t") != expected_header:
        return ["SOURCE_MANIFEST.tsv has an invalid header"]

    listed: set[Path] = set()
    for line_no, raw in enumerate(lines[1:], 2):
        fields = raw.split("\t")
        if len(fields) != 6:
            errors.append(f"SOURCE_MANIFEST.tsv:{line_no}: expected 6 tab-separated fields")
            continue
        relative, component, origin, captured_at, byte_text, expected_hash = fields
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] != "source":
            errors.append(f"SOURCE_MANIFEST.tsv:{line_no}: unsafe/non-source path: {relative}")
            continue
        if rel in listed:
            errors.append(f"SOURCE_MANIFEST.tsv:{line_no}: duplicate path: {relative}")
            continue
        listed.add(rel)
        path = ROOT / rel
        if not path.is_file():
            errors.append(f"SOURCE_MANIFEST.tsv:{line_no}: missing source: {relative}")
            continue
        if not component or not origin or not captured_at:
            errors.append(f"SOURCE_MANIFEST.tsv:{line_no}: empty provenance field: {relative}")
        try:
            expected_bytes = int(byte_text)
        except ValueError:
            errors.append(f"SOURCE_MANIFEST.tsv:{line_no}: invalid byte count: {relative}")
        else:
            if path.stat().st_size != expected_bytes:
                errors.append(f"SOURCE_MANIFEST.tsv byte mismatch: {relative}")
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            errors.append(f"SOURCE_MANIFEST.tsv hash mismatch: {relative}")

    actual = {
        path.relative_to(ROOT)
        for path in (ROOT / "source").rglob("*")
        if path.is_file()
    }
    for rel in sorted(actual - listed):
        errors.append(f"source missing from SOURCE_MANIFEST.tsv: {rel}")
    for rel in sorted(listed - actual):
        errors.append(f"SOURCE_MANIFEST.tsv lists non-source file: {rel}")
    return errors


def verify_json_files() -> list[str]:
    errors: list[str] = []
    for path in sorted(ROOT.rglob("*.json")):
        rel = path.relative_to(ROOT)
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"json: {rel}: {type(exc).__name__}: {exc}")
    return errors


def verify_example_contracts() -> list[str]:
    """Check that the example files form one coherent synthetic release graph."""
    errors: list[str] = []
    example_dir = ROOT / "contracts" / "examples"
    try:
        release = json.loads((example_dir / "release_manifest.v1.json").read_text())
        schedule = json.loads((example_dir / "schedule_revision.v1.json").read_text())
        artifact = json.loads((example_dir / "artifact.v1.json").read_text())
        render = json.loads((example_dir / "render_manifest.v1.json").read_text())
        upload = json.loads((example_dir / "upload_receipt.v1.json").read_text())
    except Exception as exc:
        return [f"example contracts could not be loaded: {type(exc).__name__}: {exc}"]

    release_ids = {
        release.get("release_id"),
        schedule.get("release_id"),
        artifact.get("release_id"),
        render.get("release_id"),
        upload.get("release_id"),
    }
    if len(release_ids) != 1 or None in release_ids:
        errors.append("example contracts disagree on release_id")

    if release.get("channel", {}).get("youtube_uc") != upload.get("channel_uc"):
        errors.append("example release/upload disagree on exact channel UC")

    schedule_revision_ids = {
        release.get("initial_schedule_revision_id"),
        schedule.get("schedule_revision_id"),
        upload.get("schedule_revision_id"),
    }
    if len(schedule_revision_ids) != 1 or None in schedule_revision_ids:
        errors.append("example contracts disagree on schedule_revision_id")

    scheduled_at = schedule.get("resolved_scheduled_at")
    if scheduled_at != upload.get("expected_scheduled_at"):
        errors.append("example release/upload disagree on scheduled_at")
    try:
        expected_epoch = int(datetime.fromisoformat(str(scheduled_at)).timestamp())
        if expected_epoch != int(upload.get("observed_scheduled_epoch")):
            errors.append("example upload receipt epoch does not match scheduled_at")
    except (TypeError, ValueError):
        errors.append("example scheduled_at/epoch is invalid")

    try:
        resolved_utc = datetime.fromisoformat(str(schedule.get("resolved_instant_utc")).replace("Z", "+00:00"))
        resolved_local = datetime.fromisoformat(str(scheduled_at))
        if resolved_utc.timestamp() != resolved_local.timestamp():
            errors.append("example schedule local and UTC instants disagree")
    except (TypeError, ValueError):
        errors.append("example schedule UTC instant is invalid")

    expected_upload_key = (
        f"{release.get('release_id')}:{schedule.get('schedule_revision_id')}:upload"
    )
    if upload.get("idempotency_key") != expected_upload_key:
        errors.append("example upload idempotency key is not bound to release and schedule revision")

    timeline_duration = sum(
        int(item.get("duration_ms") or 0) for item in render.get("timeline", [])
    )
    if timeline_duration != int(render.get("expected_duration_ms") or -1):
        errors.append("example render timeline does not match expected_duration_ms")

    narration_ids = {
        item.get("artifact_id") for item in render.get("narration", [])
    }
    if artifact.get("artifact_id") not in narration_ids:
        errors.append("example render does not reference the narration artifact")
    artifact_duration = int(artifact.get("validation", {}).get("duration_ms") or -1)
    if artifact_duration != int(render.get("expected_duration_ms") or -2):
        errors.append("example narration duration does not match render duration")
    minimum = int(
        artifact.get("validation", {}).get("expected_min_duration_ms") or -1
    )
    if not 0 <= minimum <= artifact_duration:
        errors.append("example narration minimum duration is invalid")

    if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", str(upload.get("channel_uc") or "")):
        errors.append("example upload channel UC has an invalid shape")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(upload.get("video_id") or "")):
        errors.append("example upload video_id has an invalid shape")
    return errors


def verify_python_syntax() -> list[str]:
    errors: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        try:
            compile(path.read_text(encoding="utf-8"), str(rel), "exec")
        except Exception as exc:
            errors.append(f"syntax: {rel}: {type(exc).__name__}: {exc}")
    return errors


def verify_python_secret_literals() -> list[str]:
    """Catch credential defaults and comparisons that token regexes can miss."""
    errors: list[str] = []
    sensitive_name = re.compile(
        r"(?:^|_)(?:API_KEY|API_SECRET|SECRET|ACCESS_TOKEN|AUTH_TOKEN|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY|CLIENT_SECRET)$",
        re.I,
    )
    harmless_markers = ("REDACTED", "SANITIZED", "PLACEHOLDER", "EXAMPLE", "DUMMY", "SYNTHETIC")

    def is_sensitive_name(node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        else:
            return False
        return bool(sensitive_name.search(name))

    def suspicious_strings(node: ast.AST | None) -> list[ast.Constant]:
        if node is None:
            return []
        found: list[ast.Constant] = []
        for child in ast.walk(node):
            if not isinstance(child, ast.Constant) or not isinstance(child.value, str):
                continue
            value = child.value.strip()
            upper = value.upper()
            if len(value) < 16 or any(marker in upper for marker in harmless_markers):
                continue
            # Environment/config variable names are not credential values.
            if re.fullmatch(r"[A-Z][A-Z0-9_]{15,}", value):
                continue
            # A likely embedded credential is compact and contains mixed classes.
            classes = sum(
                bool(re.search(pattern, value))
                for pattern in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[_+=:/.-]")
            )
            if not re.search(r"\s", value) and classes >= 3:
                found.append(child)
        return found

    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            targets: list[ast.AST] = []
            value: ast.AST | None = None
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value

            if targets and any(is_sensitive_name(target) for target in targets):
                # Environment variable names are expected. A second non-empty
                # default in getenv/environ.get is not.
                if isinstance(value, ast.Call):
                    func_name = ""
                    if isinstance(value.func, ast.Attribute):
                        func_name = value.func.attr
                    if func_name in {"get", "getenv"} and len(value.args) >= 2:
                        default = value.args[1]
                        if isinstance(default, ast.Constant) and isinstance(default.value, str) and default.value:
                            errors.append(f"credential default literal: {rel}:{node.lineno}")
                    continue
                if suspicious_strings(value):
                    errors.append(f"credential assignment literal: {rel}:{node.lineno}")

            if isinstance(node, ast.Compare) and is_sensitive_name(node.left):
                if any(suspicious_strings(comparator) for comparator in node.comparators):
                    errors.append(f"credential comparison literal: {rel}:{node.lineno}")
    return errors


def main() -> int:
    errors = (
        verify_required_files()
        + verify_forbidden_files()
        + verify_secret_patterns()
        + verify_privacy_literals()
        + verify_source_manifest()
        + verify_hashes()
        + verify_json_files()
        + verify_example_contracts()
        + verify_python_syntax()
        + verify_python_secret_literals()
    )
    if errors:
        print("PACKAGE CHECK FAILED")
        for error in errors:
            print(f"- {error}")
        return 1
    print("OK: required files, checksums, JSON, forbidden-file scan, secret scan and Python syntax passed")
    print("No imports, network calls or external operations were executed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
