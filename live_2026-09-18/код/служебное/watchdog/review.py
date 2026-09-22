#!/usr/bin/env python3
"""Read-only Codex review for the deterministic VideoStudio watchdog report."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.request


LOG = logging.getLogger("video_ops_review")
SAFE_DEDUP_KEY = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
CYRILLIC = re.compile(r"[А-яЁё]")
MARKDOWN = re.compile(r"(?:^|\n)\s*(?:#{1,6}|[-*>])\s|`|\[[^\]]+\]\([^)]*\)")
SECRET_LIKE = re.compile(
    r"(?:\b\d{6,12}:[A-Za-z0-9_-]{20,}\b|\b(?:sk-|ghp_|AIza)[A-Za-z0-9_-]{16,}\b)"
)
CODEX_WORKDIR = "/opt/video-ops-watchdog"
CODEX_SCHEMA = "/opt/video-ops-watchdog/review_output.schema.json"
LATEST_REPORT = "latest_report.json"
LATEST_RESULT = "latest_agent_result.json"
FAILURE_ALERT_KEY = "codex-review-failure"
RECOVERY_ALERT_KEY = "codex-review-recovery"
RECOVERY_ALERT_MESSAGE = "✅ Операционный обзор Codex снова работает."


def failure_alert_message(failures, error_code=""):
    """Текст тревоги строится по факту, а не по зашитому числу.

    Порог отказов настраивается (``agent.failure_threshold``), поэтому прежняя
    фраза «не выполнился 3 раза подряд» врала бы при любом другом значении. Код
    причины (``codex_nonzero_exit`` / ``codex_timeout`` / ``report_stale``)
    отличает «Codex упал» от «Codex не успел» — без него тревога не говорит,
    куда смотреть.
    """
    reason = str(error_code or "").strip()
    return (
        "⚠️ Операционный обзор Codex не отработал."
        f" Отказов подряд: {int(failures or 0)}."
        + (f" Причина: {reason}." if reason else "")
        + " Детерминированный сторож продолжает работать; проверь journalctl"
        " -u video-ops-review.service."
    )


class ReviewError(Exception):
    """Expected failure carrying only a safe machine-readable code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _utc_iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _load_json(path: Path, error_code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewError(error_code) from exc
    if not isinstance(value, dict):
        raise ReviewError(error_code)
    return value


def _parse_report_time(report: dict[str, Any]) -> float:
    value = report.get("generated_at")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        raise ReviewError("report_timestamp_missing")
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ReviewError("report_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _open_state_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS agent_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at INTEGER NOT NULL,
            finished_at INTEGER NOT NULL,
            status TEXT NOT NULL,
            report_generated_at INTEGER,
            send_requested INTEGER,
            severity TEXT,
            dedup_key TEXT,
            message_hash TEXT,
            error_code TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_agent_runs_status_id
            ON agent_runs(status, id DESC);

        CREATE TABLE IF NOT EXISTS agent_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            kind TEXT NOT NULL,
            dedup_key TEXT NOT NULL,
            message_hash TEXT NOT NULL,
            sent_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_notifications_dedup
            ON agent_notifications(kind, dedup_key, message_hash, sent_at DESC);
        """
    )
    conn.commit()
    return conn


def _message_hash(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _record_run(
    conn: sqlite3.Connection,
    *,
    started_at: int,
    finished_at: int,
    status: str,
    report_generated_at: int | None,
    result: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> int:
    result = result or {}
    message = result.get("message")
    cursor = conn.execute(
        """
        INSERT INTO agent_runs(
            started_at, finished_at, status, report_generated_at,
            send_requested, severity, dedup_key, message_hash, error_code
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            started_at,
            finished_at,
            status,
            report_generated_at,
            int(bool(result.get("send"))) if result else None,
            result.get("severity"),
            result.get("dedup_key"),
            _message_hash(message) if isinstance(message, str) else None,
            error_code,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def _consecutive_codex_failures(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT status FROM agent_runs
        WHERE status IN ('codex_success', 'codex_failure')
        ORDER BY id DESC
        """
    )
    count = 0
    for row in rows:
        if row["status"] == "codex_success":
            break
        count += 1
    return count


def _was_sent_recently(
    conn: sqlite3.Connection,
    *,
    kind: str,
    dedup_key: str,
    message: str | None,
    now: int,
    cooldown_seconds: int,
) -> bool:
    params: list[Any] = [kind, dedup_key, now - cooldown_seconds]
    sql = (
        "SELECT 1 FROM agent_notifications "
        "WHERE kind = ? AND dedup_key = ? AND sent_at >= ?"
    )
    if message is not None:
        sql += " AND message_hash = ?"
        params.append(_message_hash(message))
    sql += " LIMIT 1"
    return conn.execute(sql, params).fetchone() is not None


def _has_unrecovered_failure(conn: sqlite3.Connection) -> bool:
    failure_at = conn.execute(
        "SELECT MAX(sent_at) FROM agent_notifications WHERE kind = 'failure_alert'"
    ).fetchone()[0]
    if failure_at is None:
        return False
    recovery_at = conn.execute(
        "SELECT MAX(sent_at) FROM agent_notifications WHERE kind = 'recovery_alert'"
    ).fetchone()[0]
    return recovery_at is None or int(recovery_at) < int(failure_at)


def _record_notification(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    kind: str,
    dedup_key: str,
    message: str,
    sent_at: int,
) -> None:
    conn.execute(
        """
        INSERT INTO agent_notifications(run_id, kind, dedup_key, message_hash, sent_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (run_id, kind, dedup_key, _message_hash(message), sent_at),
    )
    conn.commit()


def _minimal_env() -> dict[str, str]:
    allowed = (
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TERM",
        "NO_COLOR",
        "CODEX_HOME",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    )
    env = {name: os.environ[name] for name in allowed if name in os.environ}
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    env.setdefault("LANG", "C.UTF-8")
    env["NO_COLOR"] = "1"
    return env


def _validate_codex_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "send",
        "severity",
        "dedup_key",
        "message",
    }:
        raise ReviewError("codex_output_invalid")
    if not isinstance(value["send"], bool):
        raise ReviewError("codex_output_invalid")
    if value["severity"] not in {"ok", "warning", "critical"}:
        raise ReviewError("codex_output_invalid")
    key = value["dedup_key"]
    if not isinstance(key, str) or SAFE_DEDUP_KEY.fullmatch(key) is None:
        raise ReviewError("codex_output_invalid")
    message = value["message"]
    if not isinstance(message, str) or len(message) > 1000:
        raise ReviewError("codex_output_invalid")
    message = message.strip()
    if message and CYRILLIC.search(message) is None:
        raise ReviewError("codex_output_not_russian")
    if message and MARKDOWN.search(message):
        raise ReviewError("codex_output_has_markdown")
    if message and SECRET_LIKE.search(message):
        raise ReviewError("codex_output_may_contain_secret")
    if value["send"] and not message:
        raise ReviewError("codex_output_invalid")
    value = dict(value)
    value["message"] = message
    return value


def _run_codex(
    *,
    config: dict[str, Any],
    report: dict[str, Any],
    state_dir: Path,
) -> dict[str, Any]:
    prompt_path = Path(__file__).with_name("REVIEW_PROMPT.md")
    try:
        prompt_rules = prompt_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReviewError("review_prompt_unavailable") from exc
    prompt = (
        prompt_rules.rstrip()
        + "\n\nДЕТЕРМИНИРОВАННЫЙ ОТЧЁТ СТОРОЖА (JSON):\n"
        + json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    agent_config = config.get("agent") if isinstance(config.get("agent"), dict) else {}
    timeout = int(agent_config.get("codex_timeout_seconds", 240))
    codex_bin = config.get("codex_bin", "codex")
    if not isinstance(codex_bin, str) or not codex_bin:
        raise ReviewError("codex_binary_invalid")

    state_dir.mkdir(parents=True, exist_ok=True)
    fd, output_name = tempfile.mkstemp(prefix=".codex-review-", suffix=".json", dir=state_dir)
    os.close(fd)
    output_path = Path(output_name)
    command = [
        codex_bin,
        "exec",
        "--ignore-user-config",
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--skip-git-repo-check",
        "-C",
        CODEX_WORKDIR,
        "--output-schema",
        CODEX_SCHEMA,
        "--output-last-message",
        str(output_path),
        "-",
    ]
    try:
        try:
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                env=_minimal_env(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ReviewError("codex_timeout") from exc
        except OSError as exc:
            raise ReviewError("codex_unavailable") from exc
        if completed.returncode != 0:
            raise ReviewError("codex_nonzero_exit")
        try:
            raw = output_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ReviewError("codex_output_missing") from exc
        if not raw.strip():
            raise ReviewError("codex_output_missing")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReviewError("codex_output_invalid") from exc
        return _validate_codex_result(value)
    finally:
        try:
            output_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            LOG.warning("Could not remove temporary Codex output")


def _telegram_settings(config: dict[str, Any], config_path: Path) -> dict[str, Any]:
    raw_path = config.get("telegram_config")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ReviewError("telegram_config_missing")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    telegram_file = _load_json(path, "telegram_config_invalid")
    telegram = telegram_file.get("telegram")
    if not isinstance(telegram, dict):
        raise ReviewError("telegram_config_invalid")
    return telegram


def _post_telegram(telegram: dict[str, Any], message: str) -> None:
    if len(message) > 3500:
        raise ReviewError("telegram_message_too_long")
    token = telegram.get("botToken")
    chat_id = telegram.get("chatId")
    if not isinstance(token, str) or not token.strip() or not isinstance(chat_id, (str, int)):
        raise ReviewError("telegram_config_invalid")
    body = json.dumps(
        {"chat_id": chat_id, "text": message}, ensure_ascii=False
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://api.telegram.org/bot" + token.strip() + "/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = response.read()
    except Exception as exc:  # urllib exposes the token in exception URLs; never log it.
        raise ReviewError("telegram_delivery_failed") from exc
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewError("telegram_response_invalid") from exc
    if not isinstance(decoded, dict) or decoded.get("ok") is not True:
        raise ReviewError("telegram_delivery_failed")


def _deliver(
    *,
    conn: sqlite3.Connection,
    run_id: int,
    config: dict[str, Any],
    config_path: Path,
    kind: str,
    dedup_key: str,
    message: str,
    now: int,
    cooldown_seconds: int,
    no_send: bool,
    match_message: bool = True,
) -> str:
    if no_send:
        return "no_send"
    if _was_sent_recently(
        conn,
        kind=kind,
        dedup_key=dedup_key,
        message=message if match_message else None,
        now=now,
        cooldown_seconds=cooldown_seconds,
    ):
        return "deduplicated"
    telegram = _telegram_settings(config, config_path)
    if telegram.get("enabled") is not True:
        return "telegram_disabled"
    _post_telegram(telegram, message)
    _record_notification(
        conn,
        run_id=run_id,
        kind=kind,
        dedup_key=dedup_key,
        message=message,
        sent_at=now,
    )
    return "sent"


def _write_atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def execute(
    config_path: str | os.PathLike[str],
    state_db: str | os.PathLike[str],
    *,
    no_send: bool = False,
    now: float | None = None,
) -> int:
    """Run one review. Return a process-style status code."""

    started = int(time.time() if now is None else now)
    config_path_obj = Path(config_path).expanduser()
    state_db_path = Path(state_db).expanduser()
    state_dir = state_db_path.parent
    result_path = state_dir / LATEST_RESULT

    try:
        config = _load_json(config_path_obj, "config_invalid")
        conn = _open_state_db(state_db_path)
    except ReviewError as exc:
        LOG.error("Review setup failed: %s", exc.code)
        return 2
    except sqlite3.Error:
        LOG.error("Review setup failed: state_db_unavailable")
        return 2

    report_generated_at: int | None = None
    try:
        agent_config = config.get("agent") if isinstance(config.get("agent"), dict) else {}
        if agent_config.get("enabled", True) is False:
            # Обзор выключен конфигом (agent.enabled=false): ни Codex, ни строки в
            # agent_runs, ни Telegram. Файл результата помечаем явно, чтобы выключение
            # было видно, а не выглядело как молчание. Страховка на случай, если таймер
            # снова включат деплой-скриптом или догоняющим запуском Persistent=true.
            _write_atomic_json(
                result_path,
                {
                    "generated_at": _utc_iso(time.time() if now is None else now),
                    "status": "disabled",
                },
            )
            LOG.info("Codex review disabled by agent.enabled=false; nothing to do")
            return 0
        try:
            report = _load_json(state_dir / LATEST_REPORT, "report_unavailable")
            report_generated_at = int(_parse_report_time(report))
            agent_config = config.get("agent") if isinstance(config.get("agent"), dict) else {}
            max_age_seconds = int(agent_config.get("report_max_age_minutes", 30)) * 60
            age = started - report_generated_at
            if age < -300:
                raise ReviewError("report_timestamp_in_future")
            if age > max_age_seconds:
                raise ReviewError("report_stale")
        except ReviewError as exc:
            run_id = _record_run(
                conn,
                started_at=started,
                finished_at=int(time.time() if now is None else now),
                status="report_error",
                report_generated_at=report_generated_at,
                error_code=exc.code,
            )
            _write_atomic_json(
                result_path,
                {
                    "generated_at": _utc_iso(time.time() if now is None else now),
                    "run_id": run_id,
                    "status": "report_error",
                    "error_code": exc.code,
                },
            )
            LOG.error("Codex review skipped: %s", exc.code)
            return 2

        try:
            codex_result = _run_codex(config=config, report=report, state_dir=state_dir)
        except ReviewError as exc:
            finished = int(time.time() if now is None else now)
            run_id = _record_run(
                conn,
                started_at=started,
                finished_at=finished,
                status="codex_failure",
                report_generated_at=report_generated_at,
                error_code=exc.code,
            )
            failures = _consecutive_codex_failures(conn)
            delivery = "below_threshold"
            threshold = int(agent_config.get("failure_threshold", 3))
            cooldown = int(float(agent_config.get("dedup_cooldown_hours", 12)) * 3600)
            if failures >= threshold:
                try:
                    delivery = _deliver(
                        conn=conn,
                        run_id=run_id,
                        config=config,
                        config_path=config_path_obj,
                        kind="failure_alert",
                        dedup_key=FAILURE_ALERT_KEY,
                        message=failure_alert_message(failures, exc.code),
                        now=finished,
                        cooldown_seconds=cooldown,
                        no_send=no_send,
                        match_message=False,
                    )
                except ReviewError:
                    delivery = "delivery_error"
                    LOG.error("Codex failure alert could not be delivered")
            _write_atomic_json(
                result_path,
                {
                    "consecutive_failures": failures,
                    "delivery": delivery,
                    "error_code": exc.code,
                    "generated_at": _utc_iso(time.time() if now is None else now),
                    "run_id": run_id,
                    "status": "codex_failure",
                },
            )
            LOG.error("Codex review failed: %s", exc.code)
            return 1

        finished = int(time.time() if now is None else now)
        needs_recovery = _has_unrecovered_failure(conn)
        run_id = _record_run(
            conn,
            started_at=started,
            finished_at=finished,
            status="codex_success",
            report_generated_at=report_generated_at,
            result=codex_result,
        )
        cooldown = int(float(agent_config.get("dedup_cooldown_hours", 12)) * 3600)
        recovery_delivery = "not_needed"
        delivery = "not_requested"
        delivery_failed = False
        if needs_recovery:
            try:
                recovery_delivery = _deliver(
                    conn=conn,
                    run_id=run_id,
                    config=config,
                    config_path=config_path_obj,
                    kind="recovery_alert",
                    dedup_key=RECOVERY_ALERT_KEY,
                    message=RECOVERY_ALERT_MESSAGE,
                    now=finished,
                    cooldown_seconds=cooldown,
                    no_send=no_send,
                    match_message=False,
                )
            except ReviewError:
                recovery_delivery = "delivery_error"
                delivery_failed = True
                LOG.error("Codex recovery alert could not be delivered")
        if codex_result["send"]:
            try:
                delivery = _deliver(
                    conn=conn,
                    run_id=run_id,
                    config=config,
                    config_path=config_path_obj,
                    kind="analysis",
                    dedup_key=codex_result["dedup_key"],
                    message=codex_result["message"],
                    now=finished,
                    cooldown_seconds=cooldown,
                    no_send=no_send,
                    match_message=True,
                )
            except ReviewError:
                delivery = "delivery_error"
                delivery_failed = True
                LOG.error("Codex review message could not be delivered")

        _write_atomic_json(
            result_path,
            {
                "analysis": codex_result,
                "delivery": delivery,
                "generated_at": _utc_iso(time.time() if now is None else now),
                "recovery_delivery": recovery_delivery,
                "report_generated_at": _utc_iso(report_generated_at),
                "run_id": run_id,
                "status": "ok" if not delivery_failed else "delivery_error",
            },
        )
        LOG.info(
            "Codex review completed: severity=%s delivery=%s",
            codex_result["severity"],
            delivery,
        )
        return 1 if delivery_failed else 0
    except (OSError, sqlite3.Error):
        LOG.error("Review state update failed")
        return 2
    finally:
        conn.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Path to watchdog JSON config")
    parser.add_argument("--state-db", required=True, help="Path to persistent SQLite state")
    parser.add_argument(
        "--no-send",
        action="store_true",
        help="Run Codex analysis without sending or recording Telegram notifications",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _build_parser().parse_args(argv)
    return execute(args.config, args.state_db, no_send=args.no_send)


if __name__ == "__main__":
    sys.exit(main())
