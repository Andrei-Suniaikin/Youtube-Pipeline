#!/usr/bin/env python3
"""Deterministic YouTube publication and VideoStudio infrastructure watchdog.

The watchdog deliberately uses public YouTube feeds only.  A missing public
entry is therefore reported as "not publicly observed", never as proof that a
video was not uploaded or scheduled privately.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import math
import re
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import receipt_io
except ModuleNotFoundError:  # importlib-based unit tests do not add this file's directory to sys.path
    import importlib.util

    _RECEIPT_SPEC = importlib.util.spec_from_file_location(
        "video_ops_watchdog_receipt_io", Path(__file__).with_name("receipt_io.py")
    )
    if _RECEIPT_SPEC is None or _RECEIPT_SPEC.loader is None:
        raise
    receipt_io = importlib.util.module_from_spec(_RECEIPT_SPEC)
    _RECEIPT_SPEC.loader.exec_module(receipt_io)


UTC = dt.timezone.utc
ATOM_NS = "http://www.w3.org/2005/Atom"
YT_NS = "http://www.youtube.com/xml/schemas/2015"
DEFAULT_HTTP_TIMEOUT = 15.0
DEFAULT_ALERT_AFTER = 3
DEFAULT_ALERT_COOLDOWN_HOURS = 6.0
MAX_TELEGRAM_CHARS = 3500
MASS_RSS_MIN_CHANNELS = 4
MASS_RSS_FAILURE_RATIO = 0.5


class ConfigError(ValueError):
    """The local configuration is invalid."""


def _utc_iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_error(exc: BaseException, secrets: Iterable[str] = ()) -> str:
    text = f"{type(exc).__name__}: {exc}"
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    # urllib exceptions can contain the complete Telegram URL.  Avoid keeping
    # any URL-shaped diagnostic in the report; the exception class/code is
    # sufficient for an operational monitor.
    words = []
    for word in text.split():
        if word.startswith(("http://", "https://")):
            words.append("[url]")
        else:
            words.append(word)
    return " ".join(words)[:240]


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"{label} not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read {label}: {_safe_error(exc)}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a JSON object")
    return value


def _parse_hhmm(value: Any, label: str) -> dt.time:
    if not isinstance(value, str):
        raise ConfigError(f"{label} must use HH:MM")
    try:
        parsed = dt.datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise ConfigError(f"{label} must use HH:MM") from exc
    return parsed


def load_config(path: str | os.PathLike[str]) -> dict[str, Any]:
    raw = _read_json(Path(path), "watchdog config")
    timezone_name = raw.get("timezone")
    if not isinstance(timezone_name, str) or not timezone_name:
        raise ConfigError("timezone is required")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"unknown timezone: {timezone_name}") from exc

    publication_day_start = str(raw.get("publication_day_start", "00:00"))
    _parse_hhmm(publication_day_start, "publication_day_start")

    telegram_path = raw.get("telegram_config")
    if not isinstance(telegram_path, str) or not telegram_path:
        raise ConfigError("telegram_config path is required")

    channels = raw.get("channels")
    if not isinstance(channels, list):
        raise ConfigError("channels must be a list")
    seen: set[str] = set()
    normalized_channels: list[dict[str, Any]] = []
    for index, item in enumerate(channels):
        if not isinstance(item, dict):
            raise ConfigError(f"channels[{index}] must be an object")
        uc = item.get("uc")
        if not isinstance(uc, str) or not uc.strip():
            raise ConfigError(f"channels[{index}].uc is required")
        uc = uc.strip()
        if uc in seen:
            raise ConfigError(f"duplicate channel id: {uc}")
        seen.add(uc)
        days = item.get("days", list(range(7)))
        if not isinstance(days, list) or any(not isinstance(day, int) or day < 0 or day > 6 for day in days):
            raise ConfigError(f"channels[{index}].days must contain weekday numbers 0..6")
        expected = item.get("expected_publications", 1)
        if not isinstance(expected, int) or expected < 1:
            raise ConfigError(f"channels[{index}].expected_publications must be a positive integer")
        deadline = item.get("deadline", "23:30")
        _parse_hhmm(deadline, f"channels[{index}].deadline")
        normalized_channels.append(
            {
                "uc": uc,
                "name": str(item.get("name") or uc),
                "niche": str(item.get("niche") or ""),
                "lang": str(item.get("lang") or ""),
                "days": sorted(set(days)),
                "expected_publications": expected,
                "deadline": deadline,
                "enabled": bool(item.get("enabled", True)),
            }
        )

    slots = raw.get("notifications", raw.get("notification_slots"))
    if not isinstance(slots, dict):
        raise ConfigError("notifications must be an object")
    normalized_slots: dict[str, Any] = {}
    for name in ("morning", "reminder", "final"):
        normalized_slots[name] = str(slots.get(name, {"morning": "10:00", "reminder": "18:00", "final": "23:45"}[name]))
        _parse_hhmm(normalized_slots[name], f"notifications.{name}")
    window = slots.get("slot_window_minutes", 20)
    if not isinstance(window, int) or window < 1 or window > 180:
        raise ConfigError("notifications.slot_window_minutes must be between 1 and 180")
    normalized_slots["slot_window_minutes"] = window
    # Режим публикационных сообщений. "full" — прежние пять сообщений в день;
    # "evening_only" — один вечерний итог. Без ключа поведение прежнее.
    mode = slots.get("mode", "full")
    if mode not in ("full", "evening_only"):
        raise ConfigError("notifications.mode must be full or evening_only")
    list_cap = slots.get("list_cap", 0)
    if isinstance(list_cap, bool) or not isinstance(list_cap, int) or list_cap < 0:
        raise ConfigError("notifications.list_cap must be a non-negative integer")
    check_interval = slots.get("check_interval_minutes", 15)
    if (
        isinstance(check_interval, bool)
        or not isinstance(check_interval, int)
        or not 1 <= check_interval <= 180
    ):
        raise ConfigError("notifications.check_interval_minutes must be between 1 and 180")
    normalized_slots["mode"] = mode
    normalized_slots["list_cap"] = list_cap
    normalized_slots["check_interval_minutes"] = check_interval

    infrastructure = raw.get("infrastructure", {})
    if not isinstance(infrastructure, dict):
        raise ConfigError("infrastructure must be an object")
    min_free_gb = infrastructure.get("min_free_gb", 5)
    stale_hours = infrastructure.get("queue_stale_hours", 3)
    queued_stale_hours = infrastructure.get("queue_queued_stale_hours", 6)
    pause_max_hours = infrastructure.get("queue_pause_max_hours", 6)
    if not isinstance(min_free_gb, (int, float)) or min_free_gb < 0:
        raise ConfigError("infrastructure.min_free_gb must be non-negative")
    if not isinstance(stale_hours, (int, float)) or stale_hours <= 0:
        raise ConfigError("infrastructure.queue_stale_hours must be positive")
    if not isinstance(queued_stale_hours, (int, float)) or queued_stale_hours <= 0:
        raise ConfigError("infrastructure.queue_queued_stale_hours must be positive")
    if not isinstance(pause_max_hours, (int, float)) or pause_max_hours <= 0:
        raise ConfigError("infrastructure.queue_pause_max_hours must be positive")
    normalized_infra = {
        "health_url": str(infrastructure.get("health_url") or ""),
        "vsgen_db": str(infrastructure.get("vsgen_db") or ""),
        "disk_path": str(
            infrastructure.get("disk_path")
            or Path(str(infrastructure.get("vsgen_db") or ".")).parent
        ),
        "min_free_gb": float(min_free_gb),
        "queue_stale_hours": float(stale_hours),
        "queue_queued_stale_hours": float(queued_stale_hours),
        "queue_pause_max_hours": float(pause_max_hours),
        "ytcortiez_service": str(infrastructure.get("ytcortiez_service") or ""),
    }
    ack_providers = infrastructure.get("acknowledged_pause_providers", [])
    ack_uids = infrastructure.get("acknowledged_pause_uids", [])
    if not isinstance(ack_providers, list) or not all(
        isinstance(item, str) and item for item in ack_providers
    ):
        raise ConfigError("infrastructure.acknowledged_pause_providers must be a list of provider names")
    if not isinstance(ack_uids, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in ack_uids
    ):
        raise ConfigError("infrastructure.acknowledged_pause_uids must be a list of integer user ids")
    credentials_max_age = infrastructure.get("credentials_max_age_hours", 36)
    task_window = infrastructure.get("task_failure_window_hours", 6)
    task_min_errors = infrastructure.get("task_failure_min_errors", 5)
    task_max_share = infrastructure.get("task_failure_max_share", 0.5)
    if not isinstance(credentials_max_age, (int, float)) or credentials_max_age <= 0:
        raise ConfigError("infrastructure.credentials_max_age_hours must be positive")
    if not isinstance(task_window, (int, float)) or task_window <= 0:
        raise ConfigError("infrastructure.task_failure_window_hours must be positive")
    if not isinstance(task_min_errors, int) or isinstance(task_min_errors, bool) or task_min_errors < 1:
        raise ConfigError("infrastructure.task_failure_min_errors must be a positive integer")
    if not isinstance(task_max_share, (int, float)) or not 0 < task_max_share <= 1:
        raise ConfigError("infrastructure.task_failure_max_share must be in (0, 1]")
    normalized_infra.update(
        {
            "acknowledged_pause_providers": sorted(set(ack_providers)),
            "acknowledged_pause_uids": sorted(set(ack_uids)),
            "credentials_report": str(infrastructure.get("credentials_report") or ""),
            "credentials_max_age_hours": float(credentials_max_age),
            "task_failure_window_hours": float(task_window),
            "task_failure_min_errors": int(task_min_errors),
            "task_failure_max_share": float(task_max_share),
        }
    )

    service_probes = infrastructure.get("service_probes", {})
    if not isinstance(service_probes, dict):
        raise ConfigError("infrastructure.service_probes must be an object")
    normalized_infra["service_probes"] = service_probes

    rss = raw.get("rss", {})
    if not isinstance(rss, dict):
        raise ConfigError("rss must be an object")
    timeout = rss.get("timeout_seconds", raw.get("http_timeout_seconds", DEFAULT_HTTP_TIMEOUT))
    workers = rss.get("workers", raw.get("rss_workers", 8))
    retries = rss.get("retries", 0)
    alert_after = slots.get(
        "failure_threshold",
        raw.get("failure_alert_after", DEFAULT_ALERT_AFTER),
    )
    if "failure_cooldown_seconds" in slots:
        cooldown = float(
            slots["failure_cooldown_seconds"]
        ) / 3600
    else:
        cooldown = raw.get("failure_cooldown_hours", DEFAULT_ALERT_COOLDOWN_HOURS)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError("http_timeout_seconds must be positive")
    if not isinstance(workers, int) or workers < 1 or workers > 32:
        raise ConfigError("rss_workers must be between 1 and 32")
    if not isinstance(retries, int) or retries < 0 or retries > 5:
        raise ConfigError("rss.retries must be between 0 and 5")
    if not isinstance(alert_after, int) or alert_after < 1:
        raise ConfigError("failure_alert_after must be a positive integer")
    if not isinstance(cooldown, (int, float)) or cooldown <= 0:
        raise ConfigError("failure_cooldown_hours must be positive")

    # Пер-компонентные пороги длительности: тревога только после min_failure_minutes
    # непрерывного провала, «восстановилась» — только после min_recovery_minutes
    # без провалов. Пустой словарь (по умолчанию) = прежнее поведение.
    overrides_raw = slots.get("failure_overrides", {})
    if not isinstance(overrides_raw, dict):
        raise ConfigError("notifications.failure_overrides must be an object")
    failure_overrides: dict[str, dict[str, float]] = {}
    override_fields = ("min_failure_minutes", "min_recovery_minutes")
    for check_key, entry in overrides_raw.items():
        if not isinstance(check_key, str) or not check_key:
            raise ConfigError("notifications.failure_overrides keys must be check names")
        if not isinstance(entry, dict):
            raise ConfigError(f"notifications.failure_overrides[{check_key}] must be an object")
        unknown_fields = sorted(set(entry) - set(override_fields))
        if unknown_fields:
            raise ConfigError(
                f"notifications.failure_overrides[{check_key}] has unknown fields: {', '.join(unknown_fields)}"
            )
        normalized_entry: dict[str, float] = {}
        for field in override_fields:
            value = entry.get(field, 0)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise ConfigError(
                    f"notifications.failure_overrides[{check_key}].{field} must be a non-negative number"
                )
            normalized_entry[field] = float(value)
        failure_overrides[check_key] = normalized_entry

    receipts = raw.get("receipts", {})
    if not isinstance(receipts, dict):
        raise ConfigError("receipts must be an object")
    if type(receipts.get("enabled", False)) is not bool:
        raise ConfigError("receipts.enabled must be a boolean")
    receipts_enabled = receipts.get("enabled", False)
    inbox_dir = str(receipts.get("inbox_dir") or "")
    max_age_hours = receipts.get("max_age_hours", 72)
    future_skew_minutes = receipts.get("future_skew_minutes", 5)
    archive_retention_days = receipts.get("archive_retention_days", 30)
    audit_retention_days = receipts.get("audit_retention_days", 180)
    diagnostic_cap = receipts.get("diagnostic_cap", 25)
    rejected_alert_hours = receipts.get("rejected_alert_hours", 24)
    allowed_hosts = receipts.get("allowed_source_hosts", ["mac", "minsk"])
    if receipts_enabled and not inbox_dir:
        raise ConfigError("receipts.inbox_dir is required when receipts are enabled")
    if (
        isinstance(max_age_hours, bool)
        or not isinstance(max_age_hours, (int, float))
        or not math.isfinite(float(max_age_hours))
        or max_age_hours <= 0
    ):
        raise ConfigError("receipts.max_age_hours must be positive")
    if (
        isinstance(future_skew_minutes, bool)
        or not isinstance(future_skew_minutes, (int, float))
        or not math.isfinite(float(future_skew_minutes))
        or future_skew_minutes < 0
    ):
        raise ConfigError("receipts.future_skew_minutes must be non-negative")
    if (
        isinstance(archive_retention_days, bool)
        or not isinstance(archive_retention_days, (int, float))
        or not math.isfinite(float(archive_retention_days))
        or archive_retention_days <= 0
    ):
        raise ConfigError("receipts.archive_retention_days must be positive")
    if (
        isinstance(audit_retention_days, bool)
        or not isinstance(audit_retention_days, (int, float))
        or not math.isfinite(float(audit_retention_days))
        or audit_retention_days <= 0
    ):
        raise ConfigError("receipts.audit_retention_days must be positive")
    if (
        isinstance(diagnostic_cap, bool)
        or not isinstance(diagnostic_cap, int)
        or not 1 <= diagnostic_cap <= 100
    ):
        raise ConfigError("receipts.diagnostic_cap must be between 1 and 100")
    if (
        isinstance(rejected_alert_hours, bool)
        or not isinstance(rejected_alert_hours, (int, float))
        or not math.isfinite(float(rejected_alert_hours))
        or rejected_alert_hours <= 0
    ):
        raise ConfigError("receipts.rejected_alert_hours must be positive")
    if (
        not isinstance(allowed_hosts, list)
        or not allowed_hosts
        or any(
            not isinstance(item, str)
            or receipt_io.SOURCE_HOST_RE.fullmatch(item) is None
            for item in allowed_hosts
        )
    ):
        raise ConfigError("receipts.allowed_source_hosts must be a non-empty string list")
    normalized_receipts = {
        "enabled": receipts_enabled,
        "inbox_dir": inbox_dir,
        "max_age_hours": float(max_age_hours),
        "future_skew_minutes": float(future_skew_minutes),
        "archive_retention_days": float(archive_retention_days),
        "audit_retention_days": float(audit_retention_days),
        "diagnostic_cap": diagnostic_cap,
        "rejected_alert_hours": float(rejected_alert_hours),
        "allowed_source_hosts": sorted(set(allowed_hosts)),
    }

    return {
        "timezone": timezone_name,
        "publication_day_start": publication_day_start,
        "telegram_config": telegram_path,
        "channels": normalized_channels,
        "notifications": normalized_slots,
        "infrastructure": normalized_infra,
        "receipts": normalized_receipts,
        "http_timeout_seconds": float(timeout),
        "rss_workers": workers,
        "rss_retries": retries,
        "failure_alert_after": alert_after,
        "failure_cooldown_hours": float(cooldown),
        "failure_overrides": failure_overrides,
    }


def load_telegram_config(path: str | os.PathLike[str]) -> dict[str, Any]:
    raw = _read_json(Path(path), "Telegram config")
    telegram = raw.get("telegram")
    if not isinstance(telegram, dict):
        raise ConfigError("Telegram config has no telegram object")
    enabled = bool(telegram.get("enabled", False))
    token = telegram.get("botToken")
    chat_id = telegram.get("chatId")
    if enabled and (not isinstance(token, str) or not token or chat_id in (None, "")):
        raise ConfigError("enabled Telegram config requires botToken and chatId")
    return {
        "enabled": enabled,
        "bot_token": str(token or ""),
        "chat_id": str(chat_id or ""),
    }


class StateStore:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=15000")
        self._init_schema()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    local_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    report_json TEXT
                );
                CREATE TABLE IF NOT EXISTS channel_observations(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    observed_at TEXT NOT NULL,
                    local_date TEXT NOT NULL,
                    uc TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    public_count INTEGER,
                    videos_json TEXT NOT NULL,
                    error_class TEXT,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );
                CREATE INDEX IF NOT EXISTS idx_observations_channel_date
                    ON channel_observations(uc, local_date, observed_at);
                CREATE TABLE IF NOT EXISTS notification_dedup(
                    dedup_key TEXT PRIMARY KEY,
                    sent_at TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS failure_streaks(
                    failure_key TEXT PRIMARY KEY,
                    streak INTEGER NOT NULL DEFAULT 0,
                    active_alerted INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    last_alert_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipt_audit(
                    receipt_sha256 TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    reason TEXT,
                    uc TEXT,
                    video_id TEXT,
                    operational_date TEXT,
                    source_host TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_receipt_audit_last_seen
                    ON receipt_audit(last_seen_at);
                """
            )
            # Миграция: начало текущей полосы (провалов или успехов). Старые строки
            # остаются с NULL и получают отметку при первом касании.
            columns = {
                str(row["name"]) for row in self.conn.execute("PRAGMA table_info(failure_streaks)")
            }
            if "state_since" not in columns:
                self.conn.execute("ALTER TABLE failure_streaks ADD COLUMN state_since TEXT")

    def close(self) -> None:
        self.conn.close()

    def begin_run(self, now_utc: dt.datetime, local_date: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO runs(started_at, local_date, status) VALUES(?,?,?)",
                (_utc_iso(now_utc), local_date, "running"),
            )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, now_utc: dt.datetime, report: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET finished_at=?, status='complete', report_json=? WHERE id=?",
                (_utc_iso(now_utc), json.dumps(report, ensure_ascii=False, separators=(",", ":")), run_id),
            )

    def add_observation(
        self,
        run_id: int,
        now_utc: dt.datetime,
        local_date: str,
        uc: str,
        result: dict[str, Any],
    ) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO channel_observations
                   (run_id, observed_at, local_date, uc, ok, public_count, videos_json, error_class)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    _utc_iso(now_utc),
                    local_date,
                    uc,
                    1 if result["ok"] else 0,
                    result.get("public_count"),
                    json.dumps(result.get("videos", []), ensure_ascii=False, separators=(",", ":")),
                    result.get("error_class"),
                ),
            )

    def record_receipt_audit(
        self,
        records: Iterable[dict[str, Any]],
        now_utc: dt.datetime,
        retention_days: float,
    ) -> dict[str, int]:
        """Idempotently retain bounded receipt outcomes after payload rotation."""
        observed_at = _utc_iso(now_utc)
        rows = []
        for record in records:
            digest = record.get("receipt_sha256") if isinstance(record, dict) else None
            if not isinstance(digest, str) or receipt_io.SHA256_RE.fullmatch(digest) is None:
                continue
            rows.append(
                (
                    digest,
                    str(record.get("status") or "unknown")[:32],
                    str(record.get("reason") or "")[:96],
                    str(record.get("uc") or "")[:64],
                    str(record.get("video_id") or "")[:32],
                    str(record.get("operational_date") or "")[:16],
                    str(record.get("source_host") or "")[:64],
                    observed_at,
                    observed_at,
                )
            )
        cutoff = _utc_iso(now_utc - dt.timedelta(days=float(retention_days)))
        with self.conn:
            if rows:
                self.conn.executemany(
                    """INSERT INTO receipt_audit(
                         receipt_sha256,status,reason,uc,video_id,operational_date,
                         source_host,first_seen_at,last_seen_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(receipt_sha256) DO UPDATE SET
                         status=excluded.status,
                         reason=excluded.reason,
                         uc=excluded.uc,
                         video_id=excluded.video_id,
                         operational_date=excluded.operational_date,
                         source_host=excluded.source_host,
                         last_seen_at=excluded.last_seen_at""",
                    rows,
                )
            cursor = self.conn.execute(
                "DELETE FROM receipt_audit WHERE last_seen_at < ?", (cutoff,)
            )
        return {"recorded": len(rows), "pruned": max(int(cursor.rowcount), 0)}

    def notification_sent(self, key: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM notification_dedup WHERE dedup_key=?", (key,)
        ).fetchone() is not None

    def mark_notification_sent(self, key: str, text: str, now_utc: dt.datetime) -> None:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO notification_dedup(dedup_key,sent_at,payload_sha256) VALUES(?,?,?)",
                (key, _utc_iso(now_utc), digest),
            )

    def note_failure(self, key: str, detail: str, now_utc: dt.datetime) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT streak,active_alerted,last_alert_at,state_since FROM failure_streaks WHERE failure_key=?",
            (key,),
        ).fetchone()
        previous_streak = int(row["streak"]) if row else 0
        streak = previous_streak + 1
        active = bool(row["active_alerted"]) if row else False
        last_alert_at = row["last_alert_at"] if row else None
        # Начало полосы провалов: переход OK→FAIL, первая запись или строка,
        # созданная до появления колонки state_since.
        state_since = (
            _utc_iso(now_utc)
            if row is None or previous_streak == 0 or not row["state_since"]
            else str(row["state_since"])
        )
        with self.conn:
            self.conn.execute(
                """INSERT INTO failure_streaks
                   (failure_key,streak,active_alerted,last_error,last_alert_at,updated_at,state_since)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(failure_key) DO UPDATE SET
                     streak=excluded.streak,last_error=excluded.last_error,updated_at=excluded.updated_at,
                     state_since=excluded.state_since""",
                (key, streak, 1 if active else 0, detail, last_alert_at, _utc_iso(now_utc), state_since),
            )
        return {
            "streak": streak,
            "active_alerted": active,
            "last_alert_at": last_alert_at,
            "state_since": state_since,
        }

    def note_success(self, key: str, now_utc: dt.datetime) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT streak,active_alerted,last_alert_at,state_since FROM failure_streaks WHERE failure_key=?",
            (key,),
        ).fetchone()
        active = bool(row["active_alerted"]) if row else False
        last_alert_at = row["last_alert_at"] if row else None
        # Начало полосы успехов: переход FAIL→OK, первая запись или строка без отметки.
        state_since = (
            _utc_iso(now_utc)
            if row is None or int(row["streak"]) > 0 or not row["state_since"]
            else str(row["state_since"])
        )
        with self.conn:
            self.conn.execute(
                """INSERT INTO failure_streaks
                   (failure_key,streak,active_alerted,last_error,last_alert_at,updated_at,state_since)
                   VALUES(?,0,?,NULL,?,?,?)
                   ON CONFLICT(failure_key) DO UPDATE SET
                     streak=0,last_error=NULL,updated_at=excluded.updated_at,
                     state_since=excluded.state_since""",
                (key, 1 if active else 0, last_alert_at, _utc_iso(now_utc), state_since),
            )
        return {"active_alerted": active, "state_since": state_since}

    def mark_failure_alerted(self, key: str, now_utc: dt.datetime) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE failure_streaks SET active_alerted=1,last_alert_at=?,updated_at=? WHERE failure_key=?",
                (_utc_iso(now_utc), _utc_iso(now_utc), key),
            )

    def mark_failure_recovered(self, key: str, now_utc: dt.datetime) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE failure_streaks SET active_alerted=0,streak=0,last_error=NULL,updated_at=? WHERE failure_key=?",
                (_utc_iso(now_utc), key),
            )

    def clear_failure_states(self, keys: Iterable[str], now_utc: dt.datetime) -> None:
        """Quietly reset obsolete failure states without recovery notifications."""
        rows = [(_utc_iso(now_utc), key) for key in keys]
        if not rows:
            return
        with self.conn:
            self.conn.executemany(
                "UPDATE failure_streaks "
                "SET active_alerted=0,streak=0,last_error=NULL,state_since=NULL,updated_at=? "
                "WHERE failure_key=?",
                rows,
            )


def fetch_bytes(url: str, timeout: float = DEFAULT_HTTP_TIMEOUT) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "VideoStudio-watchdog/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _atom_text(element: ET.Element, path: str) -> str:
    found = element.find(path)
    return (found.text or "").strip() if found is not None else ""


def publication_window(
    now_local: dt.datetime,
    day_start_text: str,
) -> tuple[dt.date, dt.datetime, dt.datetime]:
    """Return operational date and its half-open publication interval.

    For a non-midnight boundary the operational date names the calendar day on
    which the interval ends.  Thus the 2026-08-12 day with a 23:00 boundary is
    [2026-08-11 23:00, 2026-08-12 23:00).  At/after 23:00 the next operational
    day begins.  Midnight intentionally keeps conventional calendar-day labels
    for backwards-compatible default behaviour.
    """
    day_start = _parse_hhmm(day_start_text, "publication_day_start")
    calendar_date = now_local.date()
    if day_start == dt.time(0, 0):
        operational_date = calendar_date
        start_date = operational_date
        end_date = operational_date + dt.timedelta(days=1)
    else:
        current_clock = now_local.time().replace(tzinfo=None)
        operational_date = calendar_date + dt.timedelta(days=1) if current_clock >= day_start else calendar_date
        start_date = operational_date - dt.timedelta(days=1)
        end_date = operational_date
    window_start = dt.datetime.combine(start_date, day_start, tzinfo=now_local.tzinfo)
    window_end = dt.datetime.combine(end_date, day_start, tzinfo=now_local.tzinfo)
    return operational_date, window_start, window_end


def parse_youtube_feed(
    data: bytes,
    timezone: ZoneInfo,
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> list[dict[str, Any]]:
    root = ET.fromstring(data)
    videos: list[dict[str, Any]] = []
    seen: set[str] = set()
    start_utc = window_start.astimezone(UTC)
    end_utc = window_end.astimezone(UTC)
    ns = {"atom": ATOM_NS, "yt": YT_NS}
    for entry in root.findall("atom:entry", ns):
        published_text = _atom_text(entry, f"{{{ATOM_NS}}}published")
        published = _parse_iso(published_text)
        if published is None or not (start_utc <= published < end_utc):
            continue
        video_id = _atom_text(entry, f"{{{YT_NS}}}videoId")
        if not video_id:
            video_id = _atom_text(entry, f"{{{ATOM_NS}}}id").rsplit(":", 1)[-1]
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        title = _atom_text(entry, f"{{{ATOM_NS}}}title")
        videos.append(
            {
                "video_id": video_id,
                "title": title,
                "published_at": _utc_iso(published),
                "url": f"https://www.youtube.com/watch?v={video_id}",
            }
        )
    videos.sort(key=lambda item: item["published_at"])
    return videos


def fetch_channel_result(
    channel: dict[str, Any],
    timezone: ZoneInfo,
    window_start: dt.datetime,
    window_end: dt.datetime,
    timeout: float,
    retries: int = 0,
) -> dict[str, Any]:
    uc = channel["uc"]
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=" + urllib.parse.quote(uc, safe="")
    last_error: BaseException | None = None
    for _attempt in range(retries + 1):
        try:
            videos = parse_youtube_feed(fetch_bytes(url, timeout), timezone, window_start, window_end)
            return {"ok": True, "public_count": len(videos), "videos": videos}
        except (OSError, ET.ParseError, ValueError) as exc:
            last_error = exc
    return {
        "ok": False,
        "public_count": None,
        "videos": [],
        "error_class": "rss_fetch",
        "error": _safe_error(last_error or RuntimeError("RSS check failed")),
    }


def fetch_health_result(url: str, timeout: float) -> dict[str, Any]:
    if not url:
        return {"ok": True, "skipped": True, "summary": "not configured"}
    try:
        payload = json.loads(fetch_bytes(url, timeout).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("health response is not an object")
        bad_status = str(payload.get("status", "")).lower() in {"error", "failed", "unhealthy", "down"}
        scheduler = payload.get("scheduler")
        scheduler_bad = isinstance(scheduler, dict) and scheduler.get("ok") is False
        ok = payload.get("ok") is not False and not bad_status and not scheduler_bad
        return {
            "ok": ok,
            "summary": "healthy" if ok else "health endpoint reports unhealthy state",
        }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return {"ok": False, "summary": "health endpoint unavailable", "error": _safe_error(exc)}


def _nearest_existing(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def disk_free_gb(path: str | os.PathLike[str]) -> float:
    usage = shutil.disk_usage(_nearest_existing(Path(path)))
    return usage.free / (1024 ** 3)


def check_disk(path: str, min_free_gb: float) -> dict[str, Any]:
    if not path:
        return {"ok": True, "skipped": True, "summary": "not configured"}
    try:
        free_gb = disk_free_gb(path)
        ok = free_gb >= min_free_gb
        return {
            "ok": ok,
            "free_gb": round(free_gb, 2),
            "minimum_gb": min_free_gb,
            "summary": f"free disk {free_gb:.1f} GB" if ok else f"only {free_gb:.1f} GB free",
        }
    except OSError as exc:
        return {"ok": False, "summary": "disk check failed", "error": _safe_error(exc)}


def _open_queue_readonly(db_path: str) -> tuple[sqlite3.Connection, str]:
    """Open a live WAL database without granting the monitor production writes.

    The writer creates/removes WAL companions in short bursts.  A read-only
    mount cannot join during some of those transitions, so retry briefly and
    only use immutable mode in a moment with no WAL companions.
    """
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(12):
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
            # Force a real database-page read; SELECT 1 can succeed before WAL
            # shared-memory setup and only fail on the first actual table query.
            connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()
            return connection, "read_only"
        except sqlite3.OperationalError as exc:
            last_error = exc
            if connection is not None:
                connection.close()

        wal = Path(db_path + "-wal")
        shm = Path(db_path + "-shm")
        if not wal.exists() and not shm.exists():
            try:
                connection = sqlite3.connect(
                    f"file:{db_path}?mode=ro&immutable=1", uri=True, timeout=2
                )
                connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()
                if not wal.exists() and not shm.exists():
                    return connection, "immutable_no_wal"
                connection.close()
            except sqlite3.OperationalError as exc:
                last_error = exc
                if connection is not None:
                    connection.close()
        if attempt < 11:
            time.sleep(0.25)
    raise last_error or sqlite3.OperationalError("queue database unavailable")


def _provider_pause_ages(
    connection: sqlite3.Connection, now_utc: dt.datetime
) -> list[dict[str, Any]]:
    """Какие провайдеры очереди стоят на админ-паузе и сколько времени.

    Пауза провайдера — единственное состояние кабинета, которое останавливает
    задачи бессрочно и молча: планировщик пропускает провайдера без записи в
    журнал задачи, и снаружи это неотличимо от «ждёт своей очереди». Сторож
    обязан видеть её сам, а не узнавать от человека.

    Возраст берём двумя источниками, от точного к грубому:
      * проигрываем ``admin_audit`` (pause_queue/resume_queue) и получаем момент,
        когда конкретный провайдер был поставлен на паузу;
      * если в аудите следа нет — ``admin_state.updated``, то есть «список пауз
        не менялся с этого момента». Это нижняя оценка возраста, и она честно
        помечена источником ``state``.
    Второй путь не запасной, а обязательный: аудит доказуемо неполон — 14.08 в
    нём оказалась строка resume_queue, которую код кабинета произвести не мог.
    """
    try:
        row = connection.execute(
            "SELECT v, updated FROM admin_state WHERE k='queue_pause'"
        ).fetchone()
    except sqlite3.Error:
        # admin_state — таблица кабинета, а не очереди: её отсутствие означает
        # «про паузы ничего не известно», но не должно ронять проверку очереди.
        return []
    if row is None:
        return []
    try:
        state = json.loads(row["v"])
    except (TypeError, ValueError):
        return []
    if not isinstance(state, dict):
        return []
    providers = [str(p) for p in (state.get("providers") or []) if str(p)]
    if bool(state.get("all")):
        providers = ["*все*"] + providers
    if not providers:
        return []

    started: dict[str, float] = {}
    try:
        for audit in connection.execute(
            "SELECT ts, action, target FROM admin_audit"
            " WHERE action IN ('pause_queue','resume_queue') ORDER BY id"
        ):
            try:
                target = str((json.loads(audit["target"]) or {}).get("target") or "")
            except (TypeError, ValueError):
                continue
            if not target:
                continue
            key = "*все*" if target == "all" else target
            if audit["action"] == "pause_queue":
                started.setdefault(key, float(audit["ts"]))
            elif key == "*все*":
                started.clear()
            else:
                started.pop(key, None)
    except sqlite3.Error:
        started = {}

    fallback = float(row["updated"] or 0.0)
    now_ts = now_utc.timestamp()
    result: list[dict[str, Any]] = []
    for provider in providers:
        since = started.get(provider)
        source = "audit"
        if since is None:
            since, source = fallback, "state"
        if not since:
            continue
        result.append(
            {
                "provider": provider,
                "age_hours": round((now_ts - since) / 3600, 1),
                "since_source": source,
            }
        )
    return sorted(result, key=lambda item: -item["age_hours"])


def _queue_pause_sets(connection: sqlite3.Connection) -> tuple[set[str], set[int]]:
    """Кого сейчас держит админ-пауза кабинета: провайдеров и пользователей (prompt_uids).

    Нужна, чтобы отличать паузу, признанную в конфиге сторожа осознанной, от случайной:
    задачи под признанной паузой не «зависли», они ждут решения человека. Глобальная
    пауза «все» здесь не раскрывается — признать её списком нельзя.
    """
    try:
        row = connection.execute("SELECT v FROM admin_state WHERE k='queue_pause'").fetchone()
    except sqlite3.Error:
        return set(), set()
    if row is None:
        return set(), set()
    try:
        state = json.loads(row[0])
    except (TypeError, ValueError):
        return set(), set()
    if not isinstance(state, dict):
        return set(), set()
    providers = {str(item) for item in (state.get("providers") or []) if str(item)}
    uids: set[int] = set()
    for raw_uid in state.get("prompt_uids") or []:
        try:
            uids.add(int(raw_uid))
        except (TypeError, ValueError):
            continue
    return providers, uids


def check_queue(
    db_path: str,
    stale_hours: float,
    now_utc: dt.datetime,
    queued_stale_hours: float = 6.0,
    pause_max_hours: float = 6.0,
    acknowledged_providers: Any = (),
    acknowledged_uids: Any = (),
) -> dict[str, Any]:
    if not db_path:
        return {"ok": True, "skipped": True, "summary": "not configured", "counts": {}}
    try:
        connection, snapshot_mode = _open_queue_readonly(db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(queue_tasks)")}
        if not columns:
            raise sqlite3.OperationalError("queue_tasks table is missing")
        counts = {
            str(row["status"]): int(row["amount"])
            for row in connection.execute("SELECT status,COUNT(*) AS amount FROM queue_tasks GROUP BY status")
        }
        timestamp_columns = [name for name in ("started", "claimed", "created") if name in columns]
        select_columns = ["job_id"]
        if "kind" in columns:
            select_columns.append("kind")
        select_columns.extend(timestamp_columns)
        stale: list[dict[str, Any]] = []
        cutoff = now_utc.timestamp() - stale_hours * 3600
        for row in connection.execute(
            f"SELECT {','.join(select_columns)} FROM queue_tasks WHERE status='running'"
        ):
            reference = None
            for name in ("claimed", "started", "created"):
                if name in row.keys() and row[name] is not None:
                    reference = float(row[name])
                    break
            if reference is None or reference < cutoff:
                stale.append(
                    {
                        "job_id": int(row["job_id"]),
                        "kind": str(row["kind"] if "kind" in row.keys() else ""),
                        "age_hours": None if reference is None else round((now_utc.timestamp() - reference) / 3600, 1),
                    }
                )
        # Задача, которую НИ РАЗУ не взяли в работу, прежней проверке была не видна:
        # она сканировала только status='running'. Именно так 24 задачи kind=video
        # простояли 3.7 суток при infra:queue=ok — провайдер стоял на админ-паузе,
        # планировщик их молча пропускал, и ни одного следа нигде не оставалось.
        paused_providers, paused_uids = _queue_pause_sets(connection)
        ack_providers = {str(item) for item in acknowledged_providers if str(item)}
        ack_uids: set[int] = set()
        for raw_uid in acknowledged_uids:
            try:
                ack_uids.add(int(raw_uid))
            except (TypeError, ValueError):
                continue
        acknowledged_queued = 0
        queued_stale: list[dict[str, Any]] = []
        queued_cutoff = now_utc.timestamp() - queued_stale_hours * 3600
        queued_columns = ["job_id"] + [
            name for name in ("kind", "provider", "uid", "created") if name in columns
        ]
        for row in connection.execute(
            f"SELECT {','.join(queued_columns)} FROM queue_tasks WHERE status='queued'"
        ):
            created = row["created"] if "created" in row.keys() else None
            if created is None or float(created) >= queued_cutoff:
                continue
            provider_name = (
                str(row["provider"])
                if "provider" in row.keys() and row["provider"] is not None
                else ""
            )
            owner_uid = None
            if "uid" in row.keys() and row["uid"] not in (None, ""):
                try:
                    owner_uid = int(row["uid"])
                except (TypeError, ValueError):
                    owner_uid = None
            if (
                provider_name
                and provider_name in paused_providers
                and provider_name in ack_providers
            ) or (
                owner_uid is not None
                and owner_uid in paused_uids
                and owner_uid in ack_uids
            ):
                # Задача ждёт осознанной паузы, признанной в конфиге сторожа, — это не сбой.
                acknowledged_queued += 1
                continue
            queued_stale.append(
                {
                    "job_id": int(row["job_id"]),
                    "kind": str(row["kind"] if "kind" in row.keys() else ""),
                    "provider": str(row["provider"] if "provider" in row.keys() else ""),
                    "age_hours": round((now_utc.timestamp() - float(created)) / 3600, 1),
                }
            )
        queued_stale.sort(key=lambda item: -item["age_hours"])
        pauses = _provider_pause_ages(connection, now_utc)
        stuck_pauses = [
            p for p in pauses
            if p["age_hours"] > pause_max_hours and p["provider"] not in ack_providers
        ]
        acknowledged_pauses = [p for p in pauses if p["provider"] in ack_providers]
        connection.close()

        problems: list[str] = []
        if stale:
            problems.append(f"зависших running: {len(stale)}")
        if queued_stale:
            oldest = queued_stale[0]
            problems.append(
                f"задач в queued дольше {queued_stale_hours:g} ч: {len(queued_stale)}"
                f" (старшая #{oldest['job_id']} {oldest['kind']}/{oldest['provider']},"
                f" {oldest['age_hours']} ч)"
            )
        if stuck_pauses:
            problems.append(
                "провайдеры на админ-паузе дольше"
                f" {pause_max_hours:g} ч: "
                + ", ".join(f"{p['provider']} ({p['age_hours']} ч)" for p in stuck_pauses)
            )
        ok = not problems
        return {
            "ok": ok,
            "counts": counts,
            "snapshot_mode": snapshot_mode,
            "stale_running": stale[:20],
            "stale_queued": queued_stale[:20],
            "provider_pauses": pauses,
            "acknowledged_pauses": acknowledged_pauses,
            "acknowledged_queued": acknowledged_queued,
            "summary": "queue has no stale running jobs" if ok else "; ".join(problems),
        }
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return {
            "ok": False,
            "counts": {},
            "stale_running": [],
            "stale_queued": [],
            "provider_pauses": [],
            "summary": "queue database unavailable",
            "error": _safe_error(exc),
        }


_TASK_KIND_LABELS = {
    "prompts": "раскадровка",
    "images": "картинки",
    "regen": "перегенерация кадров",
    "tts": "озвучка",
    "video": "видео",
    "regen_video": "перегенерация видео",
    "render": "сборка ролика",
    "chapters": "главы",
    "cards": "карточки",
}


def _parse_report_moment(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if moment.tzinfo is None:
        # Утренний отчёт пишет время машины без пояса: это местное время VPS.
        moment = moment.astimezone()
    return moment.astimezone(dt.timezone.utc)


def check_credentials(
    report_path: str, now_utc: dt.datetime, max_age_hours: float = 36.0
) -> dict[str, Any]:
    """Доступы провайдеров по утреннему отчёту /root/ops/check_credentials.py.

    Сам отчёт никого не будит — он пишется в файл. С 02.09.2026 он каждое утро фиксировал
    истёкший ключ VeoNonStop, а видео не генерировалось двенадцать дней, потому что файл
    никто не читал. Сторож превращает строку «МЁРТВ» в тревогу. Устаревший или нечитаемый
    отчёт — тоже тревога: значит, перестала работать сама утренняя проверка.
    """
    if not report_path:
        return {"ok": True, "skipped": True, "summary": "not configured", "dead": []}
    try:
        raw = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"ok": False, "dead": [], "summary": "отчёт проверки доступов не найден"}
    except (OSError, ValueError) as exc:
        return {
            "ok": False,
            "dead": [],
            "summary": "отчёт проверки доступов не читается",
            "error": _safe_error(exc),
        }
    rows = raw.get("rows") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return {"ok": False, "dead": [], "summary": "отчёт проверки доступов повреждён"}
    checked = _parse_report_moment(raw.get("checked_at"))
    dead = [
        str(row.get("name") or "?")
        for row in rows
        if isinstance(row, dict) and str(row.get("state") or "").strip().upper() in ("МЁРТВ", "DEAD")
    ]
    problems: list[str] = []
    age_hours = None
    if checked is None:
        problems.append("в отчёте проверки доступов нет времени проверки")
    else:
        age_hours = round((now_utc - checked).total_seconds() / 3600, 1)
        if age_hours > max_age_hours:
            problems.append(f"проверка доступов не обновлялась {age_hours:g} ч")
    if dead:
        problems.append("мёртвые доступы: " + ", ".join(dead))
    return {
        "ok": not problems,
        "dead": dead,
        "checked_at": raw.get("checked_at"),
        "age_hours": age_hours,
        "summary": "; ".join(problems) if problems else f"все доступы живы ({len(rows)} проверено)",
    }


def _failure_signal(text):
    text = str(text or '').lower()
    if re.search(r'\b(nameerror|assertionerror|typeerror|attributeerror|importerror|modulenotfounderror|permissionerror)\b', text) or any(x in text for x in ('sqlite3.databaseerror', 'sqlite3.operationalerror', 'no space left on device', 'code_defect', 'service_defect')):
        return 'internal'
    if any(x in text for x in ('provider_remote_recovery_required', 'maximum confirmed generation attempts reached', 'результат удалённого запроса не подтверждён', 'связь оборвалась при создании задачи', 'uncertain_submit', 'recovery_required')):
        return 'recovery'
    if any(x in text for x in ('план сцен отклонён', 'контракт референсов отклонён', 'content_contract', 'схема не исправлена после', 'required story reference failed')):
        return 'contract'
    if any(x in text for x in ('rate limit', 'quota exceeded', 'insufficient quota', 'provider unavailable', 'external_provider')) or re.search(r'http(?:error)?[ :]*(?:429|503|502|504)\b', text) or ('voicer' in text and 'превышен лимит запросов' in text and '(429)' in text):
        return 'external'
    return None


def _classify_task_failure(connection, row, has_logs):
    signals = {_failure_signal(row['error_text'])}
    if has_logs:
        lower = max(float(row['created_at'] or 0), float(row['started_at'] or 0))
        upper = float(row['finished_at'] or 0) + 5
        # Indexed by job_id/id. No raw log content is included in reports.
        for entry in connection.execute(
            'SELECT ts,substr(line,-1024) FROM job_logs WHERE job_id=? ORDER BY id DESC LIMIT 32',
            (row['job_id'],),
        ):
            if not lower <= float(entry[0]) <= upper:
                continue
            if str(entry[1]).lstrip().startswith('▶ старт:'):
                break
            signals.add(_failure_signal(entry[1]))
    # A transient external warning must not hide a defect/recovery requirement.
    for category in ('internal','recovery','contract','external'):
        if category in signals:
            return category
    return 'unknown'


def check_task_failures(
    db_path: str,
    now_utc: dt.datetime,
    window_hours: float = 6.0,
    min_errors: int = 5,
    max_share: float = 0.5,
) -> dict[str, Any]:
    """Этапы конвейера, где задачи массово падают.

    Проверка очереди видит только зависшие и не взятые задачи. Упавшая задача очередь
    не держит, поэтому 12.09.2026 раскадровка падала 79 раз из 109, а видео — во всех
    задачах из-за истёкшего ключа, и при этом infra:queue оставалась в порядке. Здесь
    считается доля ошибок каждого этапа за окно. Служебные пометки «этап оборван»
    (ошибка без владельца) не считаются: это следствие чужого сбоя, а не новый.
    """
    if not db_path:
        return {"ok": True, "skipped": True, "summary": "not configured", "stages": []}
    try:
        connection, snapshot_mode = _open_queue_readonly(db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + 8
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(queue_tasks)")}
        ts_column = "finished" if "finished" in columns else ("created" if "created" in columns else "")
        if not ts_column or "kind" not in columns or "status" not in columns:
            connection.close()
            return {"ok": True, "skipped": True, "summary": "в очереди нет полей для подсчёта", "stages": []}
        marker_filter = " AND NOT (status='error' AND uid IS NULL)" if "uid" in columns else ""
        cutoff = now_utc.timestamp() - window_hours * 3600
        counts: dict[str, dict[str, int]] = {}
        error_expr = "COALESCE(error,'')" if "error" in columns else "''"
        # Aggregate all outcomes, then classify a bounded number of actual failed jobs.
        # Rows beyond the bounded read remain visible as unknown, never green.
        for row in connection.execute(
            f"SELECT kind,status,COUNT(*) AS amount FROM queue_tasks "
            f"WHERE status IN ('done','error') AND {ts_column} >= ?{marker_filter} GROUP BY kind,status", (cutoff,),
        ):
            bucket = counts.setdefault(str(row['kind']), {})
            bucket['unknown' if row['status']=='error' else 'done'] = int(row['amount'])
        log_columns = {r[1] for r in connection.execute('PRAGMA table_info(job_logs)')}
        has_logs = {'id','job_id','ts','line'}.issubset(log_columns)
        created_expr = 'COALESCE(created,0)' if 'created' in columns else '0'
        started_expr = 'COALESCE(started,0)' if 'started' in columns else '0'
        if 'job_id' in columns:
            for row in connection.execute(
                f"SELECT job_id,kind,{error_expr} AS error_text,{created_expr} AS created_at,"
                f"{started_expr} AS started_at,{ts_column} AS finished_at FROM queue_tasks "
                f"WHERE status='error' AND {ts_column}>=?{marker_filter} ORDER BY {ts_column} DESC LIMIT 400", (cutoff,),
            ):
                category = _classify_task_failure(connection, row, has_logs)
                bucket = counts[str(row['kind'])]
                bucket['unknown'] -= 1
                bucket[category] = bucket.get(category,0)+1
                if {'project_id','uid','created'}.issubset(columns):
                    later = connection.execute(
                        f"SELECT 1 FROM queue_tasks newer JOIN queue_tasks failed ON failed.job_id=? "
                        f"WHERE failed.project_id IS NOT NULL AND failed.project_id!='' "
                        f"AND newer.project_id=failed.project_id AND newer.uid=failed.uid AND newer.kind=failed.kind "
                        f"AND newer.status='done' AND newer.created>=failed.created AND newer.{ts_column}>failed.{ts_column} LIMIT 1",
                        (row['job_id'],),
                    ).fetchone()
                    if later:bucket['later_success'] = bucket.get('later_success',0)+1
        connection.close()
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return {
            "ok": False,
            "stages": [],
            "summary": "очередь недоступна для подсчёта падений",
            "error": _safe_error(exc),
        }
    stages = []
    for kind, by_status in sorted(counts.items()):
        errors = sum(by_status.get(k,0) for k in ("internal","recovery","contract","unknown"))
        external = by_status.get("external", 0)
        total = errors + external + by_status.get("done", 0)
        failing = errors >= min_errors
        stages.append({"kind": kind, "errors": errors, "internal_errors":by_status.get("internal",0),
                       "recovery_errors":by_status.get("recovery",0), "contract_errors":by_status.get("contract",0),
                       "unknown_errors":by_status.get("unknown",0), "later_success_attempts":by_status.get("later_success",0), "external_errors": external, "total": total, "failing": failing})
    failing = [stage for stage in stages if stage["failing"]]
    return {
        "ok": not failing,
        "snapshot_mode": snapshot_mode,
        "window_hours": window_hours,
        "classification_limit":400,
        "failure_unit":"attempts; later_success means a later DONE on the same owner/project/stage",
        "stages": stages,
        "summary": (
            "; ".join(
                f"{_TASK_KIND_LABELS.get(stage['kind'], stage['kind'])}: "
                f"{stage['errors']} неудачных попыток из {stage['total']} требуют разбора за {window_hours:g} ч "
                f"(сервис {stage['internal_errors']}, восстановление {stage['recovery_errors']}, "
                f"контракт {stage['contract_errors']}, причина неизвестна {stage['unknown_errors']})"
                for stage in failing
            )
            if failing
            else "порог задач, требующих разбора, не превышен; внешних отказов: " + str(sum(stage["external_errors"] for stage in stages))
        ),
    }


def run_systemctl(service: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, _safe_error(exc)
    status = (result.stdout or result.stderr or "unknown").strip().splitlines()[0][:80]
    return result.returncode == 0 and status == "active", status


def check_service(service: str) -> dict[str, Any]:
    if not service:
        return {"ok": True, "skipped": True, "summary": "not configured"}
    ok, status = run_systemctl(service)
    return {"ok": ok, "status": status, "summary": f"service is {status}"}


def split_telegram_message(text: str, limit: int = MAX_TELEGRAM_CHARS) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        pieces = [line[index : index + limit] for index in range(0, max(len(line), 1), limit)] or [""]
        for piece in pieces:
            candidate = piece if not current else current + "\n" + piece
            if len(candidate) > limit:
                if current:
                    chunks.append(current)
                current = piece
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


def send_telegram(bot_token: str, chat_id: str, text: str, timeout: float = DEFAULT_HTTP_TIMEOUT) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}).encode()
    request = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError("Telegram API rejected the message")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Telegram API HTTP {exc.code}") from None
    except urllib.error.URLError:
        raise RuntimeError("Telegram API is unavailable") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Telegram API returned an invalid response") from None


def operational_moment(
    operational_date: dt.date,
    publication_day_start: str,
    clock_text: str,
    timezone: dt.tzinfo | None,
    *,
    label: str = "operational time",
) -> dt.datetime:
    """Map a wall-clock time into the calendar date inside an operational day.

    With a 23:00 boundary, 23:30 for operational date Aug 12 is Aug 11
    23:30, while 10:30 remains Aug 12 10:30.  Midnight keeps conventional
    calendar-day semantics.
    """
    day_start = _parse_hhmm(publication_day_start, "publication_day_start")
    clock = _parse_hhmm(clock_text, label)
    calendar_date = operational_date
    if day_start != dt.time(0, 0) and clock >= day_start:
        calendar_date -= dt.timedelta(days=1)
    return dt.datetime.combine(calendar_date, clock, tzinfo=timezone)


def _in_slot(
    now_local: dt.datetime,
    operational_date: dt.date,
    publication_day_start: str,
    slot: str,
    window_minutes: int,
) -> bool:
    start = operational_moment(
        operational_date,
        publication_day_start,
        slot,
        now_local.tzinfo,
        label="slot",
    )
    return start <= now_local < start + dt.timedelta(minutes=window_minutes)


def _deadline_passed(
    now_local: dt.datetime,
    operational_date: dt.date,
    publication_day_start: str,
    deadline: str,
) -> bool:
    point = operational_moment(
        operational_date,
        publication_day_start,
        deadline,
        now_local.tzinfo,
        label="deadline",
    )
    return now_local >= point


def _display_date(local_date: dt.date) -> str:
    return local_date.strftime("%d.%m.%Y")


def _channel_label(channel: dict[str, Any]) -> str:
    suffix = ", ".join(part for part in (channel.get("lang"), channel.get("niche")) if part)
    return f"{channel['name']} ({suffix})" if suffix else channel["name"]


def _channel_observation_line(channel: dict[str, Any], compact: bool = False) -> str:
    """Keep Studio upload proof and public RSS proof visibly independent."""
    label = _channel_label(channel)
    upload_status = channel.get("upload_status")
    upload_count = int(channel.get("scheduled_uploads") or 0)
    expected = int(channel.get("expected") or 0)
    public = channel.get("public_today")
    if compact:
        # Без жаргона: про Studio — только когда квитанция реально есть.
        public_text = "лента недоступна" if public is None else f"вышло {public} из {expected}"
        if upload_status == "scheduled":
            studio_text = f"; в Studio запланировано {upload_count} из {expected}"
        elif upload_status == "partial":
            studio_text = f"; в Studio запланировано только {upload_count} из {expected}"
        elif upload_status == "conflict":
            studio_text = "; квитанции Studio противоречат друг другу"
        else:
            studio_text = ""
        return f"• {label} — {public_text}{studio_text}"
    public_text = "RSS недоступен" if public is None else f"RSS {public}/{expected}"
    if upload_status == "scheduled":
        studio_text = f"Studio SCHEDULED {upload_count}/{expected}"
    elif upload_status == "partial":
        studio_text = f"Studio SCHEDULED только {upload_count}/{expected}"
    elif upload_status == "conflict":
        studio_text = "конфликт квитанций Studio"
    elif upload_status == "unconfirmed":
        studio_text = "нет точной квитанции Studio"
    elif upload_status == "unknown":
        studio_text = "проверка квитанций недоступна"
    else:
        studio_text = "контроль Studio не настроен"
    return f"• {label} — {studio_text}; {public_text}"


def _capped_channel_lines(
    items: list[dict[str, Any]], cap: int, *, compact: bool = False
) -> list[str]:
    """Единая точка ограничения списка каналов; cap<=0 — без ограничения."""
    shown = items if cap <= 0 or len(items) <= cap else items[:cap]
    lines = [_channel_observation_line(item, compact=compact) for item in shown]
    if len(shown) < len(items):
        lines.append(f"…и ещё {len(items) - len(shown)} каналов.")
    return lines


def _evening_summary_message(
    report: dict[str, Any],
    now_local: dt.datetime,
    operational_date: dt.date,
    publication_day_start: str,
    slots: dict[str, Any],
    *,
    overdue: list[dict[str, Any]],
    unknown: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Один вечерний итог для режима evening_only.

    При массовом сбое RSS итог откладывается, пока следующий прогон ещё попадает в
    окно финального слота; если это последний прогон окна — уходит одна строка
    «проверить не могу» без списка каналов.
    """
    due = report["channels"]
    date_text = _display_date(operational_date)
    key = f"schedule:final:{operational_date.isoformat()}"
    rss = report["infrastructure"]["youtube_rss"]
    if rss.get("mass_outage"):
        next_run = now_local + dt.timedelta(minutes=int(slots.get("check_interval_minutes", 15)))
        next_run_in_window = publication_window(next_run, publication_day_start)[0] == operational_date and _in_slot(
            next_run,
            operational_date,
            publication_day_start,
            slots["final"],
            slots["slot_window_minutes"],
        )
        if next_run_in_window:
            report["notifications"].setdefault("deferred", []).append(key)
            return None
        return {
            "key": key,
            "kind": "final",
            "text": (
                f"Итог публикаций за {date_text}\n"
                "Проверить публикации не могу: ленты YouTube недоступны у "
                f"{rss.get('failed_channels', 0)} из {rss.get('due_channels', len(due))} каналов."
            ),
        }

    expected = report["summary"]["expected_publications"]
    fulfilled = report["summary"]["fulfilled_publications"]
    receipts = report["upload_receipts"]
    cap = int(slots.get("list_cap", 0) or 0)
    lines = [f"Итог публикаций за {date_text}"]
    if fulfilled == 0 and report["summary"]["scheduled_uploads_confirmed"] == 0:
        lines.append(f"Публично вышло 0 из {expected} роликов по плану ({len(due)} каналов).")
        if receipts["enabled"]:
            lines.append(
                "Квитанций от загрузчика за день не было."
                if receipts["scan_ok"]
                else "Проверка квитанций недоступна."
            )
    else:
        lines.append(f"Публично вышло {fulfilled} из {expected} роликов по плану.")
        if overdue:
            lines.append(f"Не вышло после дедлайна ({len(overdue)} каналов):")
            lines.extend(_capped_channel_lines(overdue, cap, compact=True))
        else:
            lines.append("Просроченных выпусков нет.")
    if unknown:
        lines.append(f"Не удалось проверить ленту: {len(unknown)} каналов.")
    lines.append("Запланированные и приватные ролики до выхода не видны.")
    return {"key": key, "kind": "final", "text": "\n".join(lines)}


def _schedule_messages(
    report: dict[str, Any],
    now_local: dt.datetime,
    operational_date: dt.date,
    publication_day_start: str,
    slots: dict[str, Any],
    force_summary: bool,
) -> list[dict[str, Any]]:
    due = report["channels"]
    if not due:
        return []
    date_text = _display_date(operational_date)
    expected = report["summary"]["expected_publications"]
    fulfilled = report["summary"]["fulfilled_publications"]
    unknown = [item for item in due if item["status"] == "unknown"]
    missing = [item for item in due if item["status"] == "missing"]
    overdue = [item for item in missing if item["overdue"]]
    messages: list[dict[str, Any]] = []
    evening_only = slots.get("mode", "full") == "evening_only"
    cap = int(slots.get("list_cap", 0) or 0)
    morning_slot_active = _in_slot(
        now_local,
        operational_date,
        publication_day_start,
        slots["morning"],
        slots["slot_window_minutes"],
    )
    reminder_slot_active = _in_slot(
        now_local,
        operational_date,
        publication_day_start,
        slots["reminder"],
        slots["slot_window_minutes"],
    )
    final_slot_active = _in_slot(
        now_local,
        operational_date,
        publication_day_start,
        slots["final"],
        slots["slot_window_minutes"],
    )

    # Ручной --force-summary выдаёт утренний план в любом режиме: это разовый запуск руками.
    if force_summary or (morning_slot_active and not evening_only):
        lines = [
            f"План публикаций на {date_text}",
            f"Каналов по плану: {len(due)} · выпусков: {expected}",
        ]
        if report["upload_receipts"]["enabled"]:
            if not report["upload_receipts"]["scan_ok"]:
                lines.append("Studio: проверка квитанций недоступна.")
            elif not report["upload_receipts"]["ok"]:
                lines.append(
                    "Studio: квитанции требуют проверки "
                    "(невалидные, отклонённые или конфликтующие)."
                )
            else:
                lines.append(
                    "Studio: точное расписание подтверждено квитанциями у "
                    f"{report['summary']['upload_complete_channels']}/{len(due)} каналов."
                )
        else:
            lines.append("Studio: контроль exact-квитанций не настроен.")
        lines.append(f"RSS: публично обнаружено к текущему моменту {fulfilled}/{expected}.")
        if missing:
            lines.append(f"Пока ждём публичный выпуск: {len(missing)} каналов.")
        if unknown:
            lines.append(f"Не удалось проверить публичную ленту: {len(unknown)} каналов.")
        lines.append("Запланированные и приватные ролики до публикации в этой проверке не видны.")
        messages.append(
            {
                "key": f"schedule:morning:{operational_date.isoformat()}",
                "kind": "morning",
                "text": "\n".join(lines),
            }
        )

    # Alert close to each actual channel deadline instead of waiting for the
    # general evening reminder.  One stable key per deadline group prevents a
    # changing missing list from producing repeated Telegram messages.
    if overdue and not evening_only and not (
        force_summary or morning_slot_active or reminder_slot_active or final_slot_active
    ):
        by_deadline: dict[str, list[dict[str, Any]]] = {}
        for item in overdue:
            by_deadline.setdefault(item["deadline"], []).append(item)
        for deadline, items in sorted(by_deadline.items()):
            lines = [
                f"Публичный дедлайн {deadline} за {date_text} прошёл.",
                "Публичный выпуск пока не обнаружен:",
            ]
            lines.extend(_capped_channel_lines(items, cap))
            lines.append(
                "Это не доказывает отсутствие загрузки: приватный или отложенный ролик до выхода RSS не видит."
            )
            messages.append(
                {
                    "key": (
                        f"schedule:deadline:{operational_date.isoformat()}:"
                        f"{deadline.replace(':', '')}"
                    ),
                    "kind": "deadline",
                    "text": "\n".join(lines),
                }
            )

    if missing and reminder_slot_active and not evening_only:
        lines = [
            f"Напоминание по публикациям на {date_text}",
            "Публичный выпуск пока не обнаружен:",
        ]
        lines.extend(_capped_channel_lines(missing, cap))
        if unknown:
            lines.append(f"Отдельно не удалось проверить RSS: {len(unknown)} каналов.")
        lines.append("Это проверка публичной ленты: запланированный или приватный ролик до выхода здесь не виден.")
        messages.append(
            {
                "key": f"schedule:reminder:{operational_date.isoformat()}",
                "kind": "reminder",
                "text": "\n".join(lines),
            }
        )

    if final_slot_active and not evening_only:
        lines = [
            f"Итог публикаций за {date_text}",
            f"Публично подтверждено по плану: {fulfilled}/{expected}.",
        ]
        if overdue:
            lines.append("Публичный выпуск пока не обнаружен после дедлайна:")
            lines.extend(_capped_channel_lines(overdue, cap))
        else:
            lines.append("Просроченных публичных выпусков не обнаружено.")
        if unknown:
            lines.append(f"Не удалось проверить публичную ленту: {len(unknown)} каналов.")
        lines.append("Проверка не видит запланированные и приватные ролики до их публичного выхода.")
        messages.append(
            {
                "key": f"schedule:final:{operational_date.isoformat()}",
                "kind": "final",
                "text": "\n".join(lines),
            }
        )
    elif final_slot_active:
        evening = _evening_summary_message(
            report,
            now_local,
            operational_date,
            publication_day_start,
            slots,
            overdue=overdue,
            unknown=unknown,
        )
        if evening is not None:
            messages.append(evening)
    return messages


def _failure_component(key: str, channel_names: dict[str, str]) -> str:
    if key.startswith("rss:"):
        uc = key.split(":", 1)[1]
        return f"YouTube RSS — {channel_names.get(uc, uc)}"
    return {
        "infra:health": "vsgen /healthz",
        "infra:disk": "диск VPS",
        "infra:queue": "очередь vsgen",
        "infra:ytcortiez": "YTCortiez bot",
        "infra:youtube_rss": "YouTube RSS — массовая доступность",
        "infra:receipt_inbox": "квитанции загрузчика Studio",
        "infra:credentials": "ключи провайдеров",
        "infra:task_failures": "массовые падения задач",
        "infra:minsk": "рендер-нода Минск",
        "infra:service_rewrite": "сервис рерайтов",
        "infra:service_cover": "сервис обложек",
        "infra:report_backup": "ночной бэкап",
        "infra:report_storage": "уборка диска",
    }.get(key, key)


def _format_duration(minutes: int) -> str:
    days, remainder = divmod(max(int(minutes), 0), 1440)
    hours, mins = divmod(remainder, 60)
    if days:
        return f"{days} сут {hours} ч"
    if hours:
        return f"{hours} ч {mins} мин"
    return f"{mins} мин"


def _failure_notification(
    failure_key: str,
    component: str,
    detail: str,
    streak: int,
    now_utc: dt.datetime,
    since: str | None = None,
) -> dict[str, Any]:
    since_at = _parse_iso(since)
    if since_at is not None and now_utc >= since_at:
        minutes = int((now_utc - since_at).total_seconds() // 60)
        duration_line = f"Длится: {_format_duration(minutes)} (проверок подряд: {streak})"
    else:
        duration_line = f"Повторяется проверок подряд: {streak}"
    text = (
        "⚠️ VideoStudio: устойчивая проблема мониторинга\n"
        f"Компонент: {component}\n"
        f"{duration_line}\n"
        f"Деталь: {detail}"
    )
    return {
        "key": f"failure-alert:{failure_key}:{int(now_utc.timestamp())}",
        "kind": "failure_alert",
        "failure_key": failure_key,
        "text": text,
    }


def _recovery_notification(failure_key: str, component: str, now_utc: dt.datetime) -> dict[str, Any]:
    return {
        "key": f"failure-recovery:{failure_key}:{int(now_utc.timestamp())}",
        "kind": "failure_recovery",
        "failure_key": failure_key,
        "text": f"✅ VideoStudio: проверка восстановилась\nКомпонент: {component}",
    }


def _write_latest_report(state_path: Path, report: dict[str, Any]) -> Path:
    destination = state_path.parent / "latest_report.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".latest_report.", suffix=".tmp", dir=state_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, destination)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    return destination


def run_check(
    config_path: str | os.PathLike[str],
    state_db_path: str | os.PathLike[str],
    *,
    no_send: bool = False,
    force_summary: bool = False,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    telegram = load_telegram_config(config["telegram_config"])
    timezone = ZoneInfo(config["timezone"])
    now_utc = now or dt.datetime.now(UTC)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    now_utc = now_utc.astimezone(UTC)
    now_local = now_utc.astimezone(timezone)
    operational_date, window_start, window_end = publication_window(
        now_local, config["publication_day_start"]
    )
    state_path = Path(state_db_path)
    store = StateStore(state_path)
    run_id = store.begin_run(now_utc, operational_date.isoformat())
    try:
        due_channels = [
            channel
            for channel in config["channels"]
            if channel["enabled"] and operational_date.weekday() in channel["days"]
        ]
        rss_results: dict[str, dict[str, Any]] = {}
        if due_channels:
            workers = min(config["rss_workers"], len(due_channels))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="youtube-rss") as pool:
                futures = {
                    pool.submit(
                        fetch_channel_result,
                        channel,
                        timezone,
                        window_start,
                        window_end,
                        config["http_timeout_seconds"],
                        config["rss_retries"],
                    ): channel
                    for channel in due_channels
                }
                for future in concurrent.futures.as_completed(futures):
                    channel = futures[future]
                    try:
                        rss_results[channel["uc"]] = future.result()
                    except Exception as exc:  # an isolated feed failure must not abort checks for other channels
                        rss_results[channel["uc"]] = {
                            "ok": False,
                            "public_count": None,
                            "videos": [],
                            "error_class": "rss_worker",
                            "error": _safe_error(exc),
                        }

        rss_failure_count = sum(not bool(result["ok"]) for result in rss_results.values())
        mass_rss_outage = (
            len(due_channels) >= MASS_RSS_MIN_CHANNELS
            and rss_failure_count / len(due_channels) >= MASS_RSS_FAILURE_RATIO
        )

        receipt_config = config["receipts"]
        receipt_scan_ok = True
        if receipt_config["enabled"]:
            enabled_receipt_channels = {
                channel["uc"]: channel["expected_publications"]
                for channel in config["channels"]
                if channel["enabled"]
            }
            try:
                receipt_scan = receipt_io.scan_inbox(
                    receipt_config["inbox_dir"],
                    enabled_channels=enabled_receipt_channels,
                    timezone=config["timezone"],
                    publication_day_start=config["publication_day_start"],
                    allowed_source_hosts=set(receipt_config["allowed_source_hosts"]),
                    max_age_hours=receipt_config["max_age_hours"],
                    future_skew_minutes=receipt_config["future_skew_minutes"],
                    now=now_utc,
                    rotate=True,
                    archive_retention_days=receipt_config["archive_retention_days"],
                    diagnostic_cap=receipt_config["diagnostic_cap"],
                    rejected_alert_hours=receipt_config["rejected_alert_hours"],
                )
            except (OSError, receipt_io.ReceiptError) as exc:
                receipt_scan_ok = False
                receipt_scan = {
                    "receipts": [],
                    "valid_files": 0,
                    "semantic_duplicates": 0,
                    "invalid_count": 1,
                    "expired_count": 0,
                    "conflict_count": 0,
                    "invalid": [{"file": "inbox", "error": getattr(exc, "code", "inbox_unavailable")}],
                    "expired": [],
                    "conflicts": [],
                    "rotated_expired": 0,
                    "rotated_rejected": 0,
                    "archive_pruned": 0,
                    "unresolved_rejected_count": 0,
                    "rejected_archive": [],
                    "rejected_recent_count": 0,
                    "rejected_recent": [],
                    "audit_records": [],
                }
            receipt_audit = store.record_receipt_audit(
                receipt_scan["audit_records"],
                now_utc,
                receipt_config["audit_retention_days"],
            )
        else:
            receipt_scan = {
                "receipts": [],
                "valid_files": 0,
                "semantic_duplicates": 0,
                "invalid_count": 0,
                "expired_count": 0,
                "conflict_count": 0,
                "invalid": [],
                "expired": [],
                "conflicts": [],
                "rotated_expired": 0,
                "rotated_rejected": 0,
                "archive_pruned": 0,
                "unresolved_rejected_count": 0,
                "rejected_archive": [],
                "rejected_recent_count": 0,
                "rejected_recent": [],
                "audit_records": [],
            }
            receipt_audit = {"recorded": 0, "pruned": 0}

        receipt_health_ok = (
            not receipt_config["enabled"]
            or (
                receipt_scan_ok
                and receipt_scan["invalid_count"] == 0
                and receipt_scan["conflict_count"] == 0
                # Здоровье — это «прямо сейчас пишется невалидное», а не «в архиве
                # что-то лежит»: архив живёт 30 суток и держал бы лампу красной всё
                # это время, заглушая собой настоящие тревоги.
                and receipt_scan["rejected_recent_count"] == 0
            )
        )

        channel_rows: list[dict[str, Any]] = []
        for channel in due_channels:
            result = rss_results[channel["uc"]]
            store.add_observation(run_id, now_utc, operational_date.isoformat(), channel["uc"], result)
            expected = channel["expected_publications"]
            public_count = None if mass_rss_outage else result.get("public_count")
            if mass_rss_outage or not result["ok"]:
                status = "unknown"
            elif int(public_count) >= expected:
                status = "complete"
            else:
                status = "missing"
            matching_receipts = [
                receipt
                for receipt in receipt_scan["receipts"]
                if receipt["uc"] == channel["uc"]
                and receipt["operational_date"] == operational_date.isoformat()
            ]
            usable_receipts_by_video = {
                receipt["video_id"]: receipt
                for receipt in matching_receipts
                if not receipt["conflict"]
            }
            usable_receipts = list(usable_receipts_by_video.values())
            if not receipt_config["enabled"]:
                upload_status = "not_configured"
            elif not receipt_scan_ok:
                upload_status = "unknown"
            elif any(receipt["conflict"] for receipt in matching_receipts):
                upload_status = "conflict"
            elif len(usable_receipts) >= expected:
                upload_status = "scheduled"
            elif usable_receipts:
                upload_status = "partial"
            else:
                upload_status = "unconfirmed"
            channel_rows.append(
                {
                    "uc": channel["uc"],
                    "name": channel["name"],
                    "niche": channel["niche"],
                    "lang": channel["lang"],
                    "expected": expected,
                    "deadline": channel["deadline"],
                    "public_today": public_count,
                    # Backwards-compatible ``status`` remains the RSS/publication
                    # status.  A receipt never changes it to complete.
                    "status": status,
                    "publication_status": status,
                    "upload_status": upload_status,
                    "scheduled_uploads": len(usable_receipts),
                    "upload_receipt_count": len(matching_receipts),
                    "upload_receipts": [
                        {
                            "receipt_sha256": receipt["receipt_sha256"],
                            "video_id": receipt["video_id"],
                            "scheduled_epoch": receipt["scheduled_epoch"],
                            "verified_at": receipt["verified_at"],
                            "manifest_sha256": receipt["manifest_sha256"],
                            "normalized_title_sha256": receipt["normalized_title_sha256"],
                            "source_host": receipt["source_host"],
                            "conflict": receipt["conflict"],
                        }
                        for receipt in matching_receipts[:receipt_config["diagnostic_cap"]]
                    ],
                    "overdue": status == "missing"
                    and _deadline_passed(
                        now_local,
                        operational_date,
                        config["publication_day_start"],
                        channel["deadline"],
                    ),
                    "videos": [] if mass_rss_outage else result.get("videos", []),
                    **(
                        {
                            "check_error": result.get(
                                "error",
                                "результат канала скрыт из-за массового сбоя YouTube RSS",
                            )
                        }
                        if mass_rss_outage or not result["ok"]
                        else {}
                    ),
                }
            )

        infra_config = config["infrastructure"]
        infrastructure = {
            "health": fetch_health_result(infra_config["health_url"], config["http_timeout_seconds"]),
            "disk": check_disk(infra_config["disk_path"], infra_config["min_free_gb"]),
            "queue": check_queue(
                infra_config["vsgen_db"],
                infra_config["queue_stale_hours"],
                now_utc,
                queued_stale_hours=infra_config["queue_queued_stale_hours"],
                pause_max_hours=infra_config["queue_pause_max_hours"],
                acknowledged_providers=infra_config["acknowledged_pause_providers"],
                acknowledged_uids=infra_config["acknowledged_pause_uids"],
            ),
            "ytcortiez": check_service(infra_config["ytcortiez_service"]),
            "credentials": check_credentials(
                infra_config["credentials_report"],
                now_utc,
                infra_config["credentials_max_age_hours"],
            ),
            "task_failures": check_task_failures(
                infra_config["vsgen_db"],
                now_utc,
                window_hours=infra_config["task_failure_window_hours"],
                min_errors=infra_config["task_failure_min_errors"],
                max_share=infra_config["task_failure_max_share"],
            ),
            "youtube_rss": {
                "ok": not mass_rss_outage,
                "mass_outage": mass_rss_outage,
                "due_channels": len(due_channels),
                "failed_channels": rss_failure_count,
                "summary": (
                    f"массово недоступны RSS-ленты YouTube: "
                    f"{rss_failure_count}/{len(due_channels)}"
                    if mass_rss_outage
                    else f"массового сбоя RSS нет: {rss_failure_count}/{len(due_channels)} ошибок"
                ),
            },
            "receipt_inbox": {
                "ok": receipt_health_ok,
                "skipped": not receipt_config["enabled"],
                "invalid_files": receipt_scan["invalid_count"],
                "unresolved_rejected": receipt_scan["unresolved_rejected_count"],
                "rejected_recent": receipt_scan["rejected_recent_count"],
                "conflicts": receipt_scan["conflict_count"],
                "summary": (
                    "контроль exact Studio-квитанций не настроен"
                    if not receipt_config["enabled"]
                    else (
                        "квитанции Studio валидны"
                        if receipt_health_ok
                        else (
                            "проверка квитанций недоступна"
                            if not receipt_scan_ok
                            else (
                                "конфликт квитанций Studio"
                                if receipt_scan["conflict_count"]
                                else "отклонённые или невалидные квитанции Studio"
                            )
                        )
                    )
                ),
            },
        }

        if infra_config.get("service_probes"):
            import service_health
            # Базы соседей и heartbeat открываем тем же способом, что и очередь vsgen:
            # sandbox сторожа read-only, а WAL-базы без -shm иначе не читаются.
            infrastructure.update(
                service_health.check_all(
                    infra_config["service_probes"],
                    now_utc.timestamp(),
                    open_db=_open_queue_readonly,
                )
            )

        fulfilled = sum(
            min(int(item["public_today"]), item["expected"])
            for item in channel_rows
            if item["public_today"] is not None
        )
        report: dict[str, Any] = {
            "schema_version": 1,
            "run_id": run_id,
            "generated_at": _utc_iso(now_utc),
            "local_time": now_local.isoformat(timespec="seconds"),
            "local_date": operational_date.isoformat(),
            "timezone": config["timezone"],
            "publication_day_start": config["publication_day_start"],
            "window_start": window_start.isoformat(timespec="seconds"),
            "window_end": window_end.isoformat(timespec="seconds"),
            "summary": {
                "due_channels": len(channel_rows),
                "expected_publications": sum(item["expected"] for item in channel_rows),
                "fulfilled_publications": fulfilled,
                "complete_channels": sum(item["status"] == "complete" for item in channel_rows),
                "missing_public_channels": sum(item["status"] == "missing" for item in channel_rows),
                "unknown_channels": sum(item["status"] == "unknown" for item in channel_rows),
                "overdue_channels": sum(bool(item["overdue"]) for item in channel_rows),
                "scheduled_uploads_confirmed": sum(
                    min(item["scheduled_uploads"], item["expected"])
                    for item in channel_rows
                    if item["upload_status"] != "conflict"
                ),
                "upload_complete_channels": sum(
                    item["upload_status"] == "scheduled" for item in channel_rows
                ),
                "upload_partial_channels": sum(
                    item["upload_status"] == "partial" for item in channel_rows
                ),
                "upload_unconfirmed_channels": sum(
                    item["upload_status"] == "unconfirmed" for item in channel_rows
                ),
                "upload_conflict_channels": sum(
                    item["upload_status"] == "conflict" for item in channel_rows
                ),
                "upload_unknown_channels": sum(
                    item["upload_status"] == "unknown" for item in channel_rows
                ),
            },
            "channels": channel_rows,
            "upload_receipts": {
                "enabled": receipt_config["enabled"],
                "ok": receipt_health_ok,
                "scan_ok": receipt_scan_ok,
                "skipped": not receipt_config["enabled"],
                "valid_files": receipt_scan["valid_files"],
                "semantic_duplicates": receipt_scan["semantic_duplicates"],
                "invalid_count": receipt_scan["invalid_count"],
                "expired_count": receipt_scan["expired_count"],
                "conflict_count": receipt_scan["conflict_count"],
                "unresolved_rejected_count": receipt_scan["unresolved_rejected_count"],
                "rejected_recent_count": receipt_scan["rejected_recent_count"],
                "invalid": receipt_scan["invalid"],
                "expired": receipt_scan["expired"],
                "conflicts": receipt_scan["conflicts"],
                "rejected_archive": receipt_scan["rejected_archive"],
                "rejected_recent": receipt_scan["rejected_recent"],
                "rotated_expired": receipt_scan["rotated_expired"],
                "rotated_rejected": receipt_scan["rotated_rejected"],
                "archive_pruned": receipt_scan["archive_pruned"],
                "audit_recorded": receipt_audit["recorded"],
                "audit_pruned": receipt_audit["pruned"],
            },
            "infrastructure": infrastructure,
            "failures": [],
            "notifications": {
                "telegram_enabled": telegram["enabled"],
                "no_send": no_send,
                "mode": config["notifications"]["mode"],
                "planned": [],
                "sent": [],
                "failed": [],
                "deduplicated": [],
                "deferred": [],
            },
        }

        notifications = _schedule_messages(
            report,
            now_local,
            operational_date,
            config["publication_day_start"],
            config["notifications"],
            force_summary,
        )
        channel_names = {channel["uc"]: channel["name"] for channel in due_channels}
        checks: list[tuple[str, bool, str, bool]] = []
        if mass_rss_outage:
            # A feed-wide outage must produce one infrastructure alert, not one
            # alert (and later one recovery) per channel.  Reset legacy states
            # silently so a recovered feed cannot flood Telegram.
            store.clear_failure_states(
                (f"rss:{channel['uc']}" for channel in config["channels"]),
                now_utc,
            )
        else:
            for channel in due_channels:
                result = rss_results[channel["uc"]]
                checks.append(
                    (
                        f"rss:{channel['uc']}",
                        bool(result["ok"]),
                        "YouTube RSS доступен"
                        if result["ok"]
                        else "публичную ленту канала не удалось прочитать",
                        True,
                    )
                )
        infra_failure_keys = {
            "health": "infra:health",
            "disk": "infra:disk",
            "queue": "infra:queue",
            "ytcortiez": "infra:ytcortiez",
            "youtube_rss": "infra:youtube_rss",
            "receipt_inbox": "infra:receipt_inbox",
            "credentials": "infra:credentials",
            "task_failures": "infra:task_failures",
        }
        for name, result in infrastructure.items():
            checks.append(
                (
                    infra_failure_keys.get(name, "infra:" + name),
                    bool(result["ok"]),
                    str(result.get("summary") or "operational check failed"),
                    not bool(result.get("skipped")),
                )
            )

        for failure_key, ok, detail, monitored in checks:
            if not monitored:
                continue
            component = _failure_component(failure_key, channel_names)
            override = config["failure_overrides"].get(failure_key, {})
            if ok:
                state = store.note_success(failure_key, now_utc)
                # «Восстановилась» только после тревоги и только когда полоса без
                # провалов длится не меньше min_recovery_minutes (по умолчанию 0).
                # max(…, 0): отметка из прошлого прогона может оказаться «в будущем» при
                # сдвиге часов назад — порог 0 по умолчанию должен оставаться пройденным.
                ok_for = max(now_utc - (_parse_iso(state["state_since"]) or now_utc), dt.timedelta(0))
                if state["active_alerted"] and ok_for >= dt.timedelta(
                    minutes=override.get("min_recovery_minutes", 0)
                ):
                    notifications.append(_recovery_notification(failure_key, component, now_utc))
                continue
            state = store.note_failure(failure_key, detail, now_utc)
            failing_for = max(now_utc - (_parse_iso(state["state_since"]) or now_utc), dt.timedelta(0))
            report["failures"].append(
                {
                    "key": failure_key,
                    "component": component,
                    "streak": state["streak"],
                    "since": state["state_since"],
                    "detail": detail,
                }
            )
            cooldown_elapsed = True
            last_alert = _parse_iso(state["last_alert_at"])
            if last_alert is not None:
                cooldown_elapsed = now_utc - last_alert >= dt.timedelta(hours=config["failure_cooldown_hours"])
            if (
                state["streak"] >= config["failure_alert_after"]
                and failing_for >= dt.timedelta(minutes=override.get("min_failure_minutes", 0))
                and (not state["active_alerted"] or cooldown_elapsed)
            ):
                notifications.append(
                    _failure_notification(
                        failure_key,
                        component,
                        detail,
                        state["streak"],
                        now_utc,
                        since=state["state_since"],
                    )
                )

        unique_notifications: list[dict[str, Any]] = []
        keys_seen: set[str] = set()
        for notification in notifications:
            key = notification["key"]
            if key in keys_seen:
                continue
            keys_seen.add(key)
            if store.notification_sent(key):
                report["notifications"]["deduplicated"].append(key)
                continue
            unique_notifications.append(notification)
            report["notifications"]["planned"].append(
                {"key": key, "kind": notification["kind"], "text": notification["text"]}
            )

        if not no_send and telegram["enabled"]:
            for notification in unique_notifications:
                try:
                    for chunk in split_telegram_message(notification["text"]):
                        send_telegram(
                            telegram["bot_token"],
                            telegram["chat_id"],
                            chunk,
                            config["http_timeout_seconds"],
                        )
                    store.mark_notification_sent(notification["key"], notification["text"], now_utc)
                    if notification["kind"] == "failure_alert":
                        store.mark_failure_alerted(notification["failure_key"], now_utc)
                    elif notification["kind"] == "failure_recovery":
                        store.mark_failure_recovered(notification["failure_key"], now_utc)
                    report["notifications"]["sent"].append(notification["key"])
                except Exception as exc:
                    report["notifications"]["failed"].append(
                        {"key": notification["key"], "error": _safe_error(exc, (telegram["bot_token"],))}
                    )

        _write_latest_report(state_path, report)
        store.finish_run(run_id, now_utc, report)
        return report
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="VideoStudio publication watchdog")
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check", help="run one publication and infrastructure check")
    check.add_argument("--config", required=True, help="watchdog JSON config")
    check.add_argument("--state-db", required=True, help="persistent watchdog SQLite database")
    check.add_argument("--no-send", action="store_true", help="perform checks without Telegram calls")
    check.add_argument("--force-summary", action="store_true", help="make today's summary eligible now")
    check.add_argument("--json", action="store_true", help="emit compact JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_check(
            args.config,
            args.state_db,
            no_send=args.no_send,
            force_summary=args.force_summary,
        )
    except (ConfigError, OSError, sqlite3.Error) as exc:
        print(f"watchdog configuration/program error: {_safe_error(exc)}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    else:
        summary = report["summary"]
        infra_ok = sum(bool(item.get("ok")) for item in report["infrastructure"].values())
        print(
            "video_ops_watchdog "
            f"date={report['local_date']} "
            f"public={summary['fulfilled_publications']}/{summary['expected_publications']} "
            f"missing={summary['missing_public_channels']} "
            f"unknown={summary['unknown_channels']} "
            f"infra={infra_ok}/{len(report['infrastructure'])} "
            f"sent={len(report['notifications']['sent'])} "
            f"send_failed={len(report['notifications']['failed'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
