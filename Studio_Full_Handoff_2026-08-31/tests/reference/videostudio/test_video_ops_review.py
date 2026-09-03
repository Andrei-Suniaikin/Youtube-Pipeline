import datetime as dt
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "video_ops_watchdog" / "review.py"
SPEC = importlib.util.spec_from_file_location("video_ops_review", MODULE_PATH)
review = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(review)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state_db = self.root / "state" / "state.db"
        self.state_db.parent.mkdir()
        self.telegram_path = self.root / "telegram.json"
        self.telegram_path.write_text(
            json.dumps(
                {
                    "telegram": {
                        "enabled": True,
                        "botToken": "test-token",
                        "chatId": 123,
                    }
                }
            ),
            encoding="utf-8",
        )
        self.config_path = self.root / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "telegram_config": str(self.telegram_path),
                    "agent": {
                        "report_max_age_minutes": 30,
                        "dedup_cooldown_hours": 12,
                        "failure_threshold": 3,
                        "codex_timeout_seconds": 240,
                    },
                }
            ),
            encoding="utf-8",
        )
        self.now = int(dt.datetime.now(tz=dt.timezone.utc).timestamp())
        self.write_report(self.now)

    def tearDown(self):
        self.temp.cleanup()

    def write_report(self, epoch):
        generated = dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).isoformat()
        (self.state_db.parent / "latest_report.json").write_text(
            json.dumps({"generated_at": generated, "summary": {"missing": 2}}),
            encoding="utf-8",
        )

    @staticmethod
    def codex_output(payload, returncode=0):
        def run(command, **kwargs):
            output_path = Path(command[command.index("--output-last-message") + 1])
            if returncode == 0:
                output_path.write_text(json.dumps(payload), encoding="utf-8")
            return subprocess.CompletedProcess(command, returncode, stdout="", stderr="")

        return run

    def notification_count(self):
        conn = sqlite3.connect(self.state_db)
        try:
            return conn.execute("SELECT COUNT(*) FROM agent_notifications").fetchone()[0]
        finally:
            conn.close()

    def test_valid_result_is_sent(self):
        payload = {
            "send": True,
            "severity": "warning",
            "dedup_key": "schedule.priority",
            "message": "Сначала проверь два канала с ранним дедлайном.",
        }
        with mock.patch.object(
            review.subprocess, "run", side_effect=self.codex_output(payload)
        ) as codex, mock.patch.object(review, "_post_telegram") as telegram:
            status = review.execute(self.config_path, self.state_db, now=self.now)

        self.assertEqual(status, 0)
        telegram.assert_called_once()
        self.assertEqual(self.notification_count(), 1)
        command = codex.call_args.args[0]
        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertIn("--ignore-user-config", command)
        self.assertEqual(command[-1], "-")
        self.assertIn("ДЕТЕРМИНИРОВАННЫЙ ОТЧЁТ", codex.call_args.kwargs["input"])

    def test_send_false_does_not_call_telegram(self):
        payload = {
            "send": False,
            "severity": "ok",
            "dedup_key": "no-new-judgment",
            "message": "",
        }
        with mock.patch.object(
            review.subprocess, "run", side_effect=self.codex_output(payload)
        ), mock.patch.object(review, "_post_telegram") as telegram:
            status = review.execute(self.config_path, self.state_db, now=self.now)

        self.assertEqual(status, 0)
        telegram.assert_not_called()
        self.assertEqual(self.notification_count(), 0)

    def test_identical_key_and_message_are_deduplicated_across_runs(self):
        payload = {
            "send": True,
            "severity": "warning",
            "dedup_key": "rss.pattern",
            "message": "Проверь общий сбой RSS у группы каналов.",
        }
        with mock.patch.object(
            review.subprocess, "run", side_effect=self.codex_output(payload)
        ), mock.patch.object(review, "_post_telegram") as telegram:
            first = review.execute(self.config_path, self.state_db, now=self.now)
            second = review.execute(self.config_path, self.state_db, now=self.now + 60)

        self.assertEqual((first, second), (0, 0))
        telegram.assert_called_once()
        self.assertEqual(self.notification_count(), 1)
        latest = json.loads(
            (self.state_db.parent / "latest_agent_result.json").read_text(encoding="utf-8")
        )
        self.assertEqual(latest["delivery"], "deduplicated")

    def test_stale_report_skips_codex(self):
        self.write_report(self.now - 31 * 60)
        with mock.patch.object(review.subprocess, "run") as codex, mock.patch.object(
            review, "_post_telegram"
        ) as telegram:
            status = review.execute(self.config_path, self.state_db, now=self.now)

        self.assertEqual(status, 2)
        codex.assert_not_called()
        telegram.assert_not_called()
        latest = json.loads(
            (self.state_db.parent / "latest_agent_result.json").read_text(encoding="utf-8")
        )
        self.assertEqual(latest["error_code"], "report_stale")

    def test_third_consecutive_codex_failure_sends_one_alert(self):
        with mock.patch.object(
            review.subprocess,
            "run",
            side_effect=self.codex_output({}, returncode=1),
        ), mock.patch.object(review, "_post_telegram") as telegram:
            statuses = [
                review.execute(self.config_path, self.state_db, now=self.now + offset)
                for offset in (0, 60, 120, 180)
            ]

        self.assertEqual(statuses, [1, 1, 1, 1])
        telegram.assert_called_once()
        self.assertEqual(self.notification_count(), 1)
        # Текст строится по факту: число отказов берётся из состояния, а не зашито
        # (порог настраивается), и называет код причины — иначе тревога не говорит,
        # куда смотреть: codex_nonzero_exit и codex_timeout лечатся по-разному.
        alert = telegram.call_args.args[1]
        self.assertIn("Отказов подряд: 3", alert)
        self.assertIn("codex_nonzero_exit", alert)

    def test_failure_alert_reports_actual_streak_and_reason(self):
        """Порог 1 (как на проде с 16.08) — сообщение обязано сказать «1», не «3»."""
        self.assertIn("Отказов подряд: 1", review.failure_alert_message(1, "codex_timeout"))
        self.assertIn("Причина: codex_timeout.", review.failure_alert_message(1, "codex_timeout"))
        self.assertNotIn("Причина:", review.failure_alert_message(2, ""))

    def test_no_send_analyzes_without_recording_notification(self):
        payload = {
            "send": True,
            "severity": "critical",
            "dedup_key": "queue.risk",
            "message": "Очередь требует ручной проверки.",
        }
        with mock.patch.object(
            review.subprocess, "run", side_effect=self.codex_output(payload)
        ) as codex, mock.patch.object(review, "_post_telegram") as telegram:
            status = review.execute(
                self.config_path, self.state_db, no_send=True, now=self.now
            )

        self.assertEqual(status, 0)
        codex.assert_called_once()
        telegram.assert_not_called()
        self.assertEqual(self.notification_count(), 0)
        latest = json.loads(
            (self.state_db.parent / "latest_agent_result.json").read_text(encoding="utf-8")
        )
        self.assertEqual(latest["delivery"], "no_send")

    def test_recovery_is_sent_only_once_after_alerted_failures(self):
        failure = self.codex_output({}, returncode=1)
        success_payload = {
            "send": False,
            "severity": "ok",
            "dedup_key": "recovered",
            "message": "",
        }
        with mock.patch.object(review, "_post_telegram") as telegram:
            with mock.patch.object(review.subprocess, "run", side_effect=failure):
                for offset in (0, 60, 120):
                    review.execute(self.config_path, self.state_db, now=self.now + offset)
            with mock.patch.object(
                review.subprocess,
                "run",
                side_effect=self.codex_output(success_payload),
            ):
                review.execute(self.config_path, self.state_db, now=self.now + 180)
                review.execute(self.config_path, self.state_db, now=self.now + 240)

        self.assertEqual(telegram.call_count, 2)
        self.assertIn("снова работает", telegram.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
