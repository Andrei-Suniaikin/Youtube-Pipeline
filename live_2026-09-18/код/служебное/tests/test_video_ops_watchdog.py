import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


WATCHDOG_PATH = Path(__file__).resolve().parents[1] / "tools" / "video_ops_watchdog" / "watchdog.py"
SPEC = importlib.util.spec_from_file_location("video_ops_watchdog", WATCHDOG_PATH)
watchdog = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(watchdog)
# run_check делает `import service_health` из своей папки — в тестах её нет в sys.path.
sys.path.insert(0, str(WATCHDOG_PATH.parent))
import service_health  # noqa: E402


def write_wal_heartbeat_db(path, now_epoch, *, fresh=True, ok=True):
    """WAL-база с heartbeat Минска; после закрытия -wal/-shm исчезают, как у vsgen на VPS."""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL)")
    stamp = now_epoch - (5 if fresh else 3600)
    connection.execute(
        "INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES('render_node_health',?,?)",
        (json.dumps({"generated_at": stamp, "ok": ok, "free_bytes": 500 * 1024**3}), now_epoch),
    )
    connection.commit()
    connection.close()
    return path


def atom_feed(entries):
    rows = []
    for video_id, title, published in entries:
        rows.append(
            f"""
  <entry>
    <id>yt:video:{video_id}</id>
    <yt:videoId>{video_id}</yt:videoId>
    <title>{title}</title>
    <published>{published}</published>
  </entry>"""
        )
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<feed xmlns='http://www.w3.org/2005/Atom' "
        "xmlns:yt='http://www.youtube.com/xml/schemas/2015'>"
        + "".join(rows)
        + "</feed>"
    ).encode("utf-8")


class WatchdogTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.state_db = self.root / "state" / "watchdog.db"
        self.vsgen_db = self.root / "vsgen.db"
        connection = sqlite3.connect(self.vsgen_db)
        connection.execute(
            """CREATE TABLE queue_tasks(
                job_id INTEGER PRIMARY KEY,
                kind TEXT,
                status TEXT,
                created REAL,
                started REAL,
                claimed REAL
            )"""
        )
        connection.commit()
        connection.close()
        self.telegram_path = self.root / "telegram.json"
        self.telegram_path.write_text(
            json.dumps(
                {
                    "telegram": {
                        "enabled": True,
                        "botToken": "SECRET_BOT_TOKEN",
                        "chatId": "12345",
                    }
                }
            ),
            encoding="utf-8",
        )
        self.config_path = self.root / "watchdog.json"
        self.write_config()

    def tearDown(self):
        self.tempdir.cleanup()

    def write_config(self, **overrides):
        config = {
            "timezone": "Europe/Moscow",
            "telegram_config": str(self.telegram_path),
            "channels": [
                {
                    "uc": "UC_TEST_CHANNEL",
                    "name": "Тестовый канал",
                    "niche": "ww2",
                    "lang": "ru",
                    "days": list(range(7)),
                    "expected_publications": 1,
                    "deadline": "09:00",
                    "enabled": True,
                }
            ],
            "notifications": {
                "morning": "10:00",
                "reminder": "18:00",
                "final": "23:00",
                "slot_window_minutes": 30,
            },
            "infrastructure": {
                "health_url": "https://health.invalid/healthz",
                "vsgen_db": str(self.vsgen_db),
                "min_free_gb": 1,
                "queue_stale_hours": 3,
                "ytcortiez_service": "ytcortiez-autobuyer.service",
            },
            "failure_alert_after": 3,
            "failure_cooldown_hours": 6,
            "http_timeout_seconds": 2,
            "rss_workers": 2,
        }
        for key, value in overrides.items():
            config[key] = value
        self.config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def healthy_fetch(feed):
        def fetch(url, timeout):
            if "feeds/videos.xml" in url:
                return feed
            if "health" in url:
                return b'{"ok": true, "status": "ok"}'
            raise AssertionError(url)

        return fetch

    @staticmethod
    def channel_list(count, *, deadline="23:00"):
        return [
            {
                "uc": f"UC_TEST_{index:03d}",
                "name": f"Тестовый канал {index:03d}",
                "niche": "ww2",
                "lang": "ru",
                "days": list(range(7)),
                "expected_publications": 1,
                "deadline": deadline,
                "enabled": True,
            }
            for index in range(count)
        ]

    def run_watchdog(self, now, fetch, *, no_send=True, force_summary=False, sender=None):
        sender = sender or mock.Mock()
        with (
            mock.patch.object(watchdog, "fetch_bytes", side_effect=fetch),
            mock.patch.object(watchdog, "disk_free_gb", return_value=100.0),
            mock.patch.object(watchdog, "run_systemctl", return_value=(True, "active")),
            mock.patch.object(watchdog, "send_telegram", sender),
        ):
            report = watchdog.run_check(
                self.config_path,
                self.state_db,
                no_send=no_send,
                force_summary=force_summary,
                now=now,
            )
        return report, sender

    def test_publication_uses_configured_timezone_date(self):
        # 23:30 UTC on Aug 11 is already Aug 12 in Europe/Moscow.
        feed = atom_feed([("video-1", "Ночной выпуск", "2026-08-11T23:30:00Z")])
        now = dt.datetime(2026, 8, 12, 7, 5, tzinfo=dt.timezone.utc)

        report, _ = self.run_watchdog(now, self.healthy_fetch(feed))

        self.assertEqual(report["local_date"], "2026-08-12")
        self.assertTrue(report["generated_at"].endswith("Z"))
        self.assertEqual(report["channels"][0]["public_today"], 1)
        self.assertEqual(report["channels"][0]["status"], "complete")
        self.assertEqual(report["summary"]["fulfilled_publications"], 1)
        latest = json.loads((self.state_db.parent / "latest_report.json").read_text(encoding="utf-8"))
        self.assertEqual(latest["generated_at"], report["generated_at"])

    def test_shifted_publication_day_counts_both_sides_of_midnight(self):
        self.write_config(timezone="Asia/Ho_Chi_Minh", publication_day_start="23:00")
        feed = atom_feed(
            [
                ("before", "До окна", "2026-08-11T15:59:00Z"),       # 22:59 local
                ("at-start", "На границе", "2026-08-11T16:00:00Z"), # 23:00 local, inclusive
                ("late", "Поздний", "2026-08-11T16:40:00Z"),         # 23:40 local
                ("after-midnight", "Ночной", "2026-08-11T17:47:00Z"),
                ("at-end-minus", "Перед концом", "2026-08-12T15:59:00Z"),
                ("at-end", "Следующий день", "2026-08-12T16:00:00Z"), # 23:00, exclusive
            ]
        )
        now = dt.datetime(2026, 8, 12, 5, 0, tzinfo=dt.timezone.utc)  # 12:00 local

        report, _ = self.run_watchdog(now, self.healthy_fetch(feed))

        self.assertEqual(report["local_date"], "2026-08-12")
        self.assertEqual(report["window_start"], "2026-08-11T23:00:00+07:00")
        self.assertEqual(report["window_end"], "2026-08-12T23:00:00+07:00")
        self.assertEqual(report["channels"][0]["public_today"], 4)
        self.assertEqual(
            [item["video_id"] for item in report["channels"][0]["videos"]],
            ["at-start", "late", "after-midnight", "at-end-minus"],
        )

    def test_after_shift_boundary_uses_next_operational_date_for_deadline_and_slots(self):
        self.write_config(timezone="Asia/Ho_Chi_Minh", publication_day_start="23:00")
        feed = atom_feed([("new-day", "Новый операционный день", "2026-08-12T16:05:00Z")])
        now = dt.datetime(2026, 8, 12, 16, 15, tzinfo=dt.timezone.utc)  # 23:15 local

        report, _ = self.run_watchdog(
            now, self.healthy_fetch(feed), no_send=True, force_summary=True
        )

        self.assertEqual(report["local_date"], "2026-08-13")
        self.assertEqual(report["window_start"], "2026-08-12T23:00:00+07:00")
        self.assertEqual(report["window_end"], "2026-08-13T23:00:00+07:00")
        self.assertFalse(report["channels"][0]["overdue"])
        self.assertEqual(
            report["notifications"]["planned"][0]["key"],
            "schedule:morning:2026-08-13",
        )

    def test_slot_and_deadline_after_day_start_use_previous_calendar_evening(self):
        channels = [
            {
                "uc": "UC_TEST_CHANNEL",
                "name": "Тестовый канал",
                "niche": "ww2",
                "lang": "ru",
                "days": list(range(7)),
                "expected_publications": 1,
                "deadline": "23:30",
                "enabled": True,
            }
        ]
        notifications = {
            "morning": "23:30",
            "reminder": "12:00",
            "final": "22:00",
            "slot_window_minutes": 30,
        }
        self.write_config(
            timezone="Asia/Ho_Chi_Minh",
            publication_day_start="23:00",
            channels=channels,
            notifications=notifications,
        )
        timezone = watchdog.ZoneInfo("Asia/Ho_Chi_Minh")
        mapped = watchdog.operational_moment(
            dt.date(2026, 8, 12), "23:00", "23:30", timezone
        )
        self.assertEqual(mapped.isoformat(), "2026-08-11T23:30:00+07:00")

        # Aug 11 at 23:35 is the opening evening of operational date Aug 12.
        first_now = dt.datetime(2026, 8, 11, 16, 35, tzinfo=dt.timezone.utc)
        first, _ = self.run_watchdog(first_now, self.healthy_fetch(atom_feed([])))
        self.assertEqual(first["local_date"], "2026-08-12")
        self.assertTrue(first["channels"][0]["overdue"])
        self.assertEqual(
            [item["key"] for item in first["notifications"]["planned"]],
            ["schedule:morning:2026-08-12"],
        )

        # The same wall-clock time on Aug 12 belongs to the next operational date.
        second_now = dt.datetime(2026, 8, 12, 16, 35, tzinfo=dt.timezone.utc)
        second, _ = self.run_watchdog(second_now, self.healthy_fetch(atom_feed([])))
        self.assertEqual(second["local_date"], "2026-08-13")
        self.assertEqual(
            [item["key"] for item in second["notifications"]["planned"]],
            ["schedule:morning:2026-08-13"],
        )

    def test_schedule_notification_is_deduplicated_across_restarts(self):
        feed = atom_feed([])
        now = dt.datetime(2026, 8, 12, 7, 5, tzinfo=dt.timezone.utc)
        sender = mock.Mock()

        self.run_watchdog(now, self.healthy_fetch(feed), no_send=False, sender=sender)
        second, _ = self.run_watchdog(now, self.healthy_fetch(feed), no_send=False, sender=sender)

        self.assertEqual(sender.call_count, 1)
        self.assertIn("schedule:morning:2026-08-12", second["notifications"]["deduplicated"])
        self.assertNotIn("SECRET_BOT_TOKEN", json.dumps(second, ensure_ascii=False))

    def test_infrastructure_alert_after_three_strikes_and_single_recovery(self):
        feed = atom_feed([("video-1", "Выпуск", "2026-08-12T06:00:00Z")])

        def failing_health(url, timeout):
            if "feeds/videos.xml" in url:
                return feed
            raise TimeoutError("health timeout")

        sender = mock.Mock()
        base = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.timezone.utc)
        first, _ = self.run_watchdog(base, failing_health, no_send=False, sender=sender)
        second, _ = self.run_watchdog(base + dt.timedelta(minutes=15), failing_health, no_send=False, sender=sender)
        third, _ = self.run_watchdog(base + dt.timedelta(minutes=30), failing_health, no_send=False, sender=sender)

        self.assertEqual(sender.call_count, 1)
        self.assertEqual(first["failures"][0]["streak"], 1)
        self.assertEqual(second["failures"][0]["streak"], 2)
        self.assertEqual(third["failures"][0]["streak"], 3)
        self.assertIn("устойчивая проблема", sender.call_args_list[0].args[2])

        recovered, _ = self.run_watchdog(
            base + dt.timedelta(minutes=45),
            self.healthy_fetch(feed),
            no_send=False,
            sender=sender,
        )
        self.run_watchdog(
            base + dt.timedelta(minutes=60),
            self.healthy_fetch(feed),
            no_send=False,
            sender=sender,
        )
        self.assertEqual(sender.call_count, 2)
        self.assertIn("восстановилась", sender.call_args_list[1].args[2])
        self.assertFalse(recovered["failures"])

    def test_rss_alert_waits_for_three_consecutive_failures(self):
        def failing_rss(url, timeout):
            if "feeds/videos.xml" in url:
                raise TimeoutError("rss timeout")
            return b'{"ok": true}'

        sender = mock.Mock()
        base = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.timezone.utc)
        for offset in (0, 15):
            report, _ = self.run_watchdog(
                base + dt.timedelta(minutes=offset), failing_rss, no_send=False, sender=sender
            )
            self.assertEqual(report["channels"][0]["status"], "unknown")
        self.assertEqual(sender.call_count, 0)

        report, _ = self.run_watchdog(
            base + dt.timedelta(minutes=30), failing_rss, no_send=False, sender=sender
        )
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(report["failures"][0]["streak"], 3)
        self.assertIn("YouTube RSS", sender.call_args.args[2])

    def test_mass_rss_outage_sends_one_alert_on_third_strike_and_one_recovery(self):
        self.write_config(channels=self.channel_list(41))

        def failing_rss(url, timeout):
            if "feeds/videos.xml" in url:
                raise TimeoutError("rss timeout")
            return b'{"ok": true}'

        sender = mock.Mock()
        base = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.timezone.utc)
        reports = []
        for offset in (0, 15, 30):
            report, _ = self.run_watchdog(
                base + dt.timedelta(minutes=offset),
                failing_rss,
                no_send=False,
                sender=sender,
            )
            reports.append(report)

        self.assertEqual(sender.call_count, 1)
        self.assertEqual(reports[-1]["summary"]["unknown_channels"], 41)
        self.assertEqual(reports[-1]["summary"]["missing_public_channels"], 0)
        self.assertFalse(reports[-1]["infrastructure"]["youtube_rss"]["ok"])
        self.assertEqual(
            [item["key"] for item in reports[-1]["failures"]],
            ["infra:youtube_rss"],
        )
        self.assertEqual(reports[-1]["failures"][0]["streak"], 3)
        self.assertIn("массовая доступность", sender.call_args_list[0].args[2])

        recovered, _ = self.run_watchdog(
            base + dt.timedelta(minutes=45),
            self.healthy_fetch(atom_feed([])),
            no_send=False,
            sender=sender,
        )
        self.run_watchdog(
            base + dt.timedelta(minutes=60),
            self.healthy_fetch(atom_feed([])),
            no_send=False,
            sender=sender,
        )
        self.assertEqual(sender.call_count, 2)
        self.assertEqual(len(recovered["notifications"]["sent"]), 1)
        self.assertIn("восстановилась", sender.call_args_list[1].args[2])

    def test_single_rss_failure_outside_mass_outage_keeps_per_channel_behavior(self):
        self.write_config(channels=self.channel_list(41))

        def one_failed_feed(url, timeout):
            if "feeds/videos.xml" in url:
                if "UC_TEST_000" in url:
                    raise TimeoutError("one feed failed")
                return atom_feed([])
            return b'{"ok": true}'

        sender = mock.Mock()
        base = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.timezone.utc)
        for offset in (0, 15, 30):
            report, _ = self.run_watchdog(
                base + dt.timedelta(minutes=offset),
                one_failed_feed,
                no_send=False,
                sender=sender,
            )

        self.assertEqual(sender.call_count, 1)
        self.assertTrue(report["infrastructure"]["youtube_rss"]["ok"])
        self.assertEqual(report["summary"]["unknown_channels"], 1)
        self.assertEqual(
            [item["key"] for item in report["failures"]],
            ["rss:UC_TEST_000"],
        )
        self.assertIn("Тестовый канал 000", sender.call_args.args[2])

    def test_exact_half_rss_failures_make_all_channels_unknown(self):
        self.write_config(channels=self.channel_list(4))

        def half_failed(url, timeout):
            if "feeds/videos.xml" in url:
                if "UC_TEST_000" in url or "UC_TEST_001" in url:
                    raise TimeoutError("feed failed")
                return atom_feed([])
            return b'{"ok": true}'

        report, _ = self.run_watchdog(
            dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.timezone.utc),
            half_failed,
        )

        self.assertFalse(report["infrastructure"]["youtube_rss"]["ok"])
        self.assertEqual(report["infrastructure"]["youtube_rss"]["failed_channels"], 2)
        self.assertEqual(report["summary"]["unknown_channels"], 4)
        self.assertEqual(report["summary"]["missing_public_channels"], 0)
        self.assertTrue(all(item["public_today"] is None for item in report["channels"]))

    def test_mass_outage_quietly_clears_legacy_per_channel_alerts(self):
        channels = self.channel_list(42)
        channels[-1]["enabled"] = False
        self.write_config(channels=channels)
        base = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.timezone.utc)
        store = watchdog.StateStore(self.state_db)
        try:
            for channel in channels:
                key = f"rss:{channel['uc']}"
                for _ in range(3):
                    store.note_failure(key, "legacy feed failure", base)
                store.mark_failure_alerted(key, base)
        finally:
            store.close()

        def failing_rss(url, timeout):
            if "feeds/videos.xml" in url:
                raise TimeoutError("rss timeout")
            return b'{"ok": true}'

        sender = mock.Mock()
        self.run_watchdog(base, failing_rss, no_send=False, sender=sender)
        recovered, _ = self.run_watchdog(
            base + dt.timedelta(minutes=15),
            self.healthy_fetch(atom_feed([])),
            no_send=False,
            sender=sender,
        )

        self.assertEqual(sender.call_count, 0)
        self.assertFalse(recovered["notifications"]["sent"])
        connection = sqlite3.connect(self.state_db)
        try:
            states = connection.execute(
                "SELECT streak,active_alerted FROM failure_streaks WHERE failure_key LIKE 'rss:%'"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(len(states), 42)
        self.assertTrue(all(streak == 0 and active == 0 for streak, active in states))

    def test_missing_message_does_not_claim_upload_is_absent(self):
        feed = atom_feed([])
        now = dt.datetime(2026, 8, 12, 20, 5, tzinfo=dt.timezone.utc)  # 23:05 MSK
        sender = mock.Mock()

        _, _ = self.run_watchdog(now, self.healthy_fetch(feed), no_send=False, sender=sender)

        self.assertEqual(sender.call_count, 1)
        text = sender.call_args.args[2]
        self.assertIn("Публичный выпуск пока не обнаружен", text)
        self.assertNotIn("не залит", text.lower())
        self.assertNotIn("не загружен", text.lower())
        self.assertIn("приватные ролики", text.lower())

    def test_no_send_does_not_consume_daily_dedup(self):
        feed = atom_feed([])
        now = dt.datetime(2026, 8, 12, 7, 5, tzinfo=dt.timezone.utc)
        sender = mock.Mock()

        dry_report, _ = self.run_watchdog(now, self.healthy_fetch(feed), no_send=True, sender=sender)
        live_report, _ = self.run_watchdog(now, self.healthy_fetch(feed), no_send=False, sender=sender)

        self.assertEqual(sender.call_count, 1)
        self.assertEqual(dry_report["notifications"]["sent"], [])
        self.assertEqual(live_report["notifications"]["sent"], ["schedule:morning:2026-08-12"])

    def test_missed_deadline_alerts_immediately_and_deduplicates(self):
        feed = atom_feed([])
        now = dt.datetime(2026, 8, 12, 7, 35, tzinfo=dt.timezone.utc)  # 10:35 MSK
        sender = mock.Mock()

        first, _ = self.run_watchdog(now, self.healthy_fetch(feed), no_send=False, sender=sender)
        second, _ = self.run_watchdog(
            now + dt.timedelta(minutes=15),
            self.healthy_fetch(feed),
            no_send=False,
            sender=sender,
        )

        key = "schedule:deadline:2026-08-12:0900"
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(first["notifications"]["sent"], [key])
        self.assertIn(key, second["notifications"]["deduplicated"])
        self.assertIn("Публичный выпуск пока не обнаружен", sender.call_args.args[2])
        self.assertNotIn("не загружен", sender.call_args.args[2].lower())

    def test_messages_are_split_below_telegram_limit(self):
        text = "\n".join(f"строка {index} " + "x" * 80 for index in range(100))
        chunks = watchdog.split_telegram_message(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 3500 for chunk in chunks))

    def test_nested_runtime_settings_and_rss_retry_are_applied(self):
        self.write_config(
            rss={"timeout_seconds": 7, "workers": 3, "retries": 2},
            notifications={
                "morning": "10:00",
                "reminder": "18:00",
                "final": "22:30",
                "slot_window_minutes": 30,
                "failure_threshold": 4,
                "failure_cooldown_seconds": 7200,
            },
        )
        loaded = watchdog.load_config(self.config_path)
        self.assertEqual(loaded["http_timeout_seconds"], 7)
        self.assertEqual(loaded["rss_workers"], 3)
        self.assertEqual(loaded["rss_retries"], 2)
        self.assertEqual(loaded["failure_alert_after"], 4)
        self.assertEqual(loaded["failure_cooldown_hours"], 2)

        calls = 0
        good_feed = atom_feed([])

        def flaky_fetch(_url, _timeout):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise TimeoutError("temporary")
            return good_feed

        start = dt.datetime(2026, 8, 12, tzinfo=dt.timezone.utc)
        with mock.patch.object(watchdog, "fetch_bytes", side_effect=flaky_fetch):
            result = watchdog.fetch_channel_result(
                {"uc": "UC_RETRY"},
                dt.timezone.utc,
                start,
                start + dt.timedelta(days=1),
                7,
                retries=2,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(calls, 3)

    # ------------------------------------------------------------------
    # Шум сторожа (17.09.2026): пер-компонентные длительности, суточный повтор,
    # миграция state_since, режим одного вечернего сообщения.
    # ------------------------------------------------------------------

    BASE_NOTIFICATIONS = {"morning": "10:00", "reminder": "18:00", "final": "23:00", "slot_window_minutes": 30}

    def base_infrastructure(self, **extra):
        infra = {
            "health_url": "https://health.invalid/healthz",
            "vsgen_db": str(self.vsgen_db),
            "min_free_gb": 1,
            "queue_stale_hours": 3,
            "ytcortiez_service": "ytcortiez-autobuyer.service",
        }
        infra.update(extra)
        return infra

    @staticmethod
    def failing_health(feed):
        def fetch(url, timeout):
            if "feeds/videos.xml" in url:
                return feed
            raise TimeoutError("health timeout")

        return fetch

    def test_failure_overrides_config_validation(self):
        self.write_config()
        self.assertEqual(watchdog.load_config(self.config_path)["failure_overrides"], {})

        self.write_config(
            notifications={
                **self.BASE_NOTIFICATIONS,
                "failure_overrides": {"infra:minsk": {"min_failure_minutes": 60}},
            }
        )
        self.assertEqual(
            watchdog.load_config(self.config_path)["failure_overrides"],
            {"infra:minsk": {"min_failure_minutes": 60.0, "min_recovery_minutes": 0.0}},
        )

        for bad in (
            {"infra:minsk": {"min_failure_minutes": True}},
            {"infra:minsk": {"min_failure_minutes": -1}},
            {"infra:minsk": {"min_failure_hours": 1}},
            {"infra:minsk": 60},
            ["infra:minsk"],
        ):
            self.write_config(notifications={**self.BASE_NOTIFICATIONS, "failure_overrides": bad})
            with self.assertRaises(watchdog.ConfigError, msg=repr(bad)):
                watchdog.load_config(self.config_path)

    def test_full_mode_defaults_without_new_keys(self):
        self.write_config()
        loaded = watchdog.load_config(self.config_path)["notifications"]
        self.assertEqual(loaded["mode"], "full")
        self.assertEqual(loaded["list_cap"], 0)
        self.assertEqual(loaded["check_interval_minutes"], 15)

        for bad in (
            {"mode": "evening"},
            {"list_cap": -1},
            {"list_cap": True},
            {"check_interval_minutes": 0},
        ):
            self.write_config(notifications={**self.BASE_NOTIFICATIONS, **bad})
            with self.assertRaises(watchdog.ConfigError, msg=repr(bad)):
                watchdog.load_config(self.config_path)

    def test_minsk_short_outage_is_silent_with_overrides(self):
        """Провал heartbeat 30 мин при min_failure 60 → ни тревоги, ни «восстановилась»."""
        heartbeat = self.root / "heartbeat.db"
        self.write_config(
            infrastructure=self.base_infrastructure(service_probes={"render_heartbeat_db": str(heartbeat)}),
            notifications={
                **self.BASE_NOTIFICATIONS,
                "failure_overrides": {"infra:minsk": {"min_failure_minutes": 60, "min_recovery_minutes": 60}},
            },
        )
        feed = atom_feed([("video-1", "Выпуск", "2026-08-12T06:00:00Z")])
        sender = mock.Mock()
        base = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.timezone.utc)
        for offset in (0, 15, 30):
            now = base + dt.timedelta(minutes=offset)
            write_wal_heartbeat_db(heartbeat, now.timestamp(), fresh=False)
            report, _ = self.run_watchdog(now, self.healthy_fetch(feed), no_send=False, sender=sender)
        self.assertEqual([item["key"] for item in report["failures"]], ["infra:minsk"])
        self.assertEqual(report["failures"][0]["streak"], 3)
        self.assertEqual(report["failures"][0]["component"], "рендер-нода Минск")
        for offset in (45, 60, 75, 90, 105, 120):
            now = base + dt.timedelta(minutes=offset)
            write_wal_heartbeat_db(heartbeat, now.timestamp(), fresh=True)
            report, _ = self.run_watchdog(now, self.healthy_fetch(feed), no_send=False, sender=sender)
        self.assertEqual(sender.call_count, 0)
        self.assertEqual(report["failures"], [])
        self.assertTrue(report["infrastructure"]["minsk"]["ok"])

    def test_minsk_long_outage_gives_one_alert_and_one_delayed_recovery(self):
        """Провал 75 мин → одна тревога на 60-й минуте; «восстановилась» — через 60 мин тишины."""
        heartbeat = self.root / "heartbeat.db"
        self.write_config(
            infrastructure=self.base_infrastructure(service_probes={"render_heartbeat_db": str(heartbeat)}),
            notifications={
                **self.BASE_NOTIFICATIONS,
                "failure_overrides": {"infra:minsk": {"min_failure_minutes": 60, "min_recovery_minutes": 60}},
            },
        )
        feed = atom_feed([("video-1", "Выпуск", "2026-08-12T06:00:00Z")])
        sender = mock.Mock()
        base = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.timezone.utc)
        sent_by_offset = {}
        for offset in (0, 15, 30, 45, 60, 75):
            now = base + dt.timedelta(minutes=offset)
            write_wal_heartbeat_db(heartbeat, now.timestamp(), fresh=False)
            report, _ = self.run_watchdog(now, self.healthy_fetch(feed), no_send=False, sender=sender)
            sent_by_offset[offset] = report["notifications"]["sent"]
        alert_key = f"failure-alert:infra:minsk:{int((base + dt.timedelta(minutes=60)).timestamp())}"
        self.assertEqual(
            sent_by_offset,
            {0: [], 15: [], 30: [], 45: [], 60: [alert_key], 75: []},
        )
        alert_text = sender.call_args_list[0].args[2]
        self.assertIn("рендер-нода Минск", alert_text)
        self.assertIn("Длится: 1 ч 0 мин", alert_text)
        self.assertNotIn("infra:minsk", alert_text)

        for offset in (90, 105, 120, 135, 150, 165):
            now = base + dt.timedelta(minutes=offset)
            write_wal_heartbeat_db(heartbeat, now.timestamp(), fresh=True)
            report, _ = self.run_watchdog(now, self.healthy_fetch(feed), no_send=False, sender=sender)
            sent_by_offset[offset] = report["notifications"]["sent"]
        recovery_key = f"failure-recovery:infra:minsk:{int((base + dt.timedelta(minutes=150)).timestamp())}"
        self.assertEqual(
            {offset: sent_by_offset[offset] for offset in (90, 105, 120, 135, 150, 165)},
            {90: [], 105: [], 120: [], 135: [], 150: [recovery_key], 165: []},
        )
        self.assertEqual(sender.call_count, 2)
        self.assertIn("восстановилась", sender.call_args_list[1].args[2])

    def test_failure_override_silences_short_recovery_and_reuses_incident(self):
        """Одна удачная проверка посреди аварии — не «восстановилась», а следующий провал — не новая тревога."""
        self.write_config(
            notifications={
                **self.BASE_NOTIFICATIONS,
                "failure_overrides": {"infra:health": {"min_recovery_minutes": 60}},
            }
        )
        feed = atom_feed([("video-1", "Выпуск", "2026-08-12T06:00:00Z")])
        sender = mock.Mock()
        base = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.timezone.utc)
        run = lambda minutes, fetch: self.run_watchdog(  # noqa: E731
            base + dt.timedelta(minutes=minutes), fetch, no_send=False, sender=sender
        )
        for offset in (0, 15, 30):
            report, _ = run(offset, self.failing_health(feed))
        self.assertEqual(len(report["notifications"]["sent"]), 1)

        recovered_once, _ = run(45, self.healthy_fetch(feed))
        self.assertEqual(recovered_once["notifications"]["sent"], [])
        connection = sqlite3.connect(self.state_db)
        try:
            active, streak = connection.execute(
                "SELECT active_alerted, streak FROM failure_streaks WHERE failure_key='infra:health'"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual((active, streak), (1, 0))

        for offset in (60, 75, 90):
            report, _ = run(offset, self.failing_health(feed))
            self.assertEqual(report["notifications"]["sent"], [], offset)
        self.assertEqual(report["failures"][0]["streak"], 3)

        sent = {}
        for offset in (105, 120, 135, 150, 165):
            report, _ = run(offset, self.healthy_fetch(feed))
            sent[offset] = report["notifications"]["sent"]
        self.assertEqual(
            sent,
            {
                105: [],
                120: [],
                135: [],
                150: [],
                165: [f"failure-recovery:infra:health:{int((base + dt.timedelta(minutes=165)).timestamp())}"],
            },
        )
        self.assertEqual(sender.call_count, 2)

    def test_recovery_is_not_sent_when_alert_was_never_sent(self):
        self.write_config(
            notifications={
                **self.BASE_NOTIFICATIONS,
                "failure_overrides": {"infra:health": {"min_failure_minutes": 120}},
            }
        )
        feed = atom_feed([("video-1", "Выпуск", "2026-08-12T06:00:00Z")])
        sender = mock.Mock()
        base = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.timezone.utc)
        for offset in (0, 15, 30, 45, 60):
            report, _ = self.run_watchdog(
                base + dt.timedelta(minutes=offset), self.failing_health(feed), no_send=False, sender=sender
            )
        self.assertEqual(report["failures"][0]["streak"], 5)
        self.run_watchdog(base + dt.timedelta(minutes=75), self.healthy_fetch(feed), no_send=False, sender=sender)
        self.assertEqual(sender.call_count, 0)
        connection = sqlite3.connect(self.state_db)
        try:
            active = connection.execute(
                "SELECT active_alerted FROM failure_streaks WHERE failure_key='infra:health'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(active, 0)

    def test_no_overrides_keep_legacy_behavior_on_old_schema(self):
        """state.db старой схемы (без state_since) с идущей аварией: миграция молча,
        список отправок — как раньше (тревога на 3-й проверке после cooldown, восстановление с первой удачной)."""
        self.state_db.parent.mkdir(parents=True)
        connection = sqlite3.connect(self.state_db)
        connection.execute(
            """CREATE TABLE failure_streaks(
                failure_key TEXT PRIMARY KEY,
                streak INTEGER NOT NULL DEFAULT 0,
                active_alerted INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                last_alert_at TEXT,
                updated_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO failure_streaks VALUES('infra:health',2,1,'health endpoint unavailable',"
            "'2026-08-12T04:00:00Z','2026-08-12T11:45:00Z')"
        )
        connection.commit()
        connection.close()

        feed = atom_feed([("video-1", "Выпуск", "2026-08-12T06:00:00Z")])
        sender = mock.Mock()
        base = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.timezone.utc)
        third, _ = self.run_watchdog(base, self.failing_health(feed), no_send=False, sender=sender)
        self.assertEqual(third["failures"][0]["streak"], 3)
        self.assertEqual(third["notifications"]["sent"], [f"failure-alert:infra:health:{int(base.timestamp())}"])

        connection = sqlite3.connect(self.state_db)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(failure_streaks)")}
            since = connection.execute(
                "SELECT state_since FROM failure_streaks WHERE failure_key='infra:health'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertIn("state_since", columns)
        self.assertEqual(since, "2026-08-12T12:00:00Z")

        recovered, _ = self.run_watchdog(
            base + dt.timedelta(minutes=15), self.healthy_fetch(feed), no_send=False, sender=sender
        )
        self.assertEqual(
            recovered["notifications"]["sent"],
            [f"failure-recovery:infra:health:{int((base + dt.timedelta(minutes=15)).timestamp())}"],
        )
        self.assertEqual(sender.call_count, 2)

    def test_state_since_in_future_does_not_suppress_legacy_alert(self):
        """Второй сценарий: строка уже есть, а её state_since оказался «в будущем» (часы
        сдвинули назад). Без overrides тревога на 3-й проверке уходит как раньше."""
        self.write_config()
        feed = atom_feed([("video-1", "Выпуск", "2026-08-12T06:00:00Z")])
        sender = mock.Mock()
        base = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.timezone.utc)
        self.run_watchdog(base, self.failing_health(feed), no_send=False, sender=sender)
        connection = sqlite3.connect(self.state_db)
        try:
            connection.execute(
                "UPDATE failure_streaks SET state_since='2026-08-12T14:00:00Z' WHERE failure_key='infra:health'"
            )
            connection.commit()
        finally:
            connection.close()
        for offset in (15, 30):
            report, _ = self.run_watchdog(
                base + dt.timedelta(minutes=offset), self.failing_health(feed), no_send=False, sender=sender
            )
        self.assertEqual(
            report["notifications"]["sent"],
            [f"failure-alert:infra:health:{int((base + dt.timedelta(minutes=30)).timestamp())}"],
        )
        self.assertIn("Повторяется проверок подряд: 3", sender.call_args.args[2])

    def test_daily_cooldown_repeats_unchanged_alert_once_per_day(self):
        self.write_config(
            notifications={**self.BASE_NOTIFICATIONS, "failure_threshold": 3, "failure_cooldown_seconds": 86400}
        )
        feed = atom_feed([("video-1", "Выпуск", "2026-08-12T06:00:00Z")])
        sender = mock.Mock()
        base = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.timezone.utc)
        offsets = [0, 15, 30] + [30 + 60 * hour for hour in range(1, 30)]  # до 29 ч 30 мин
        sent_keys = []
        for offset in offsets:
            report, _ = self.run_watchdog(
                base + dt.timedelta(minutes=offset), self.failing_health(feed), no_send=False, sender=sender
            )
            sent_keys.extend(report["notifications"]["sent"])
        first_alert = base + dt.timedelta(minutes=30)
        repeat_alert = first_alert + dt.timedelta(hours=24)
        # Прогон переваливает на 13.08 — там честно уходит дедлайн по каналу; сравниваем тревоги.
        self.assertEqual(
            [key for key in sent_keys if key.startswith("failure-")],
            [
                f"failure-alert:infra:health:{int(first_alert.timestamp())}",
                f"failure-alert:infra:health:{int(repeat_alert.timestamp())}",
            ],
        )
        self.assertEqual(
            [key for key in sent_keys if not key.startswith("failure-")],
            ["schedule:deadline:2026-08-13:0900"],
        )
        alerts = [call.args[2] for call in sender.call_args_list if "устойчивая проблема" in call.args[2]]
        self.assertEqual(len(alerts), 2)
        self.assertIn("Длится: 30 мин", alerts[0])
        self.assertIn("Длится: 1 сут 0 ч", alerts[1])

    # --- режим одного вечернего сообщения ---

    def evening_config(self, channels, **notifications):
        self.write_config(
            channels=channels,
            notifications={**self.BASE_NOTIFICATIONS, "mode": "evening_only", **notifications},
        )

    @staticmethod
    def feed_only_for(uc, feed):
        def fetch(url, timeout):
            if "feeds/videos.xml" in url:
                return feed if uc in url else atom_feed([])
            if "health" in url:
                return b'{"ok": true, "status": "ok"}'
            raise AssertionError(url)

        return fetch

    def test_evening_only_sends_single_final_message_per_day(self):
        self.evening_config(self.channel_list(41, deadline="09:00"))
        sender = mock.Mock()
        empty = self.healthy_fetch(atom_feed([]))
        day = dt.datetime(2026, 8, 12, tzinfo=dt.timezone.utc)
        reports = {}
        # 10:05 (утро), 10:35 (дедлайн прошёл, вне слотов), 18:05 (напоминание), 23:05 и 23:20 (итог) по Москве
        for label, hour, minute in (("morning", 7, 5), ("deadline", 7, 35), ("reminder", 15, 5), ("final", 20, 5), ("final_repeat", 20, 20)):
            reports[label], _ = self.run_watchdog(day.replace(hour=hour, minute=minute), empty, no_send=False, sender=sender)

        self.assertEqual(sender.call_count, 1)
        for label in ("morning", "deadline", "reminder"):
            self.assertEqual(reports[label]["notifications"]["planned"], [], label)
            self.assertEqual(reports[label]["notifications"]["mode"], "evening_only")
        self.assertEqual(reports["final"]["notifications"]["sent"], ["schedule:final:2026-08-12"])
        self.assertIn("schedule:final:2026-08-12", reports["final_repeat"]["notifications"]["deduplicated"])
        text = sender.call_args.args[2]
        self.assertIn("Итог публикаций за 12.08.2026", text)
        self.assertIn("0 из 41", text)
        self.assertNotIn("•", text)
        for forbidden in ("Публичный дедлайн", "Напоминание", "План публикаций", "нет точной квитанции"):
            self.assertNotIn(forbidden, text)

    def test_evening_only_mass_outage_defers_then_sends_one_line(self):
        self.evening_config(self.channel_list(41), check_interval_minutes=15)

        def failing_rss(url, timeout):
            if "feeds/videos.xml" in url:
                raise TimeoutError("rss timeout")
            return b'{"ok": true}'

        sender = mock.Mock()
        first, _ = self.run_watchdog(
            dt.datetime(2026, 8, 12, 20, 5, tzinfo=dt.timezone.utc), failing_rss, no_send=False, sender=sender
        )
        self.assertEqual(first["notifications"]["planned"], [])
        self.assertEqual(first["notifications"]["deferred"], ["schedule:final:2026-08-12"])

        last_in_window = dt.datetime(2026, 8, 12, 20, 20, tzinfo=dt.timezone.utc)
        second, _ = self.run_watchdog(last_in_window, failing_rss, no_send=False, sender=sender)
        self.assertEqual(second["notifications"]["sent"], ["schedule:final:2026-08-12"])
        text = sender.call_args.args[2]
        self.assertIn("Проверить публикации не могу: ленты YouTube недоступны у 41 из 41 каналов", text)
        self.assertNotIn("•", text)
        self.assertEqual(text.count("\n"), 1)

        repeat, _ = self.run_watchdog(last_in_window, failing_rss, no_send=False, sender=sender)
        self.assertIn("schedule:final:2026-08-12", repeat["notifications"]["deduplicated"])
        self.assertNotIn("schedule:final:2026-08-12", repeat["notifications"]["sent"])

    def test_evening_only_mass_outage_with_2300_day_boundary_sends_on_last_run(self):
        """Боевая раскладка: день с 23:00, итог 22:30, окно 45 мин, таймер :00/:15/:30/:45.
        В окно попадают только 22:30 и 22:45 — в 23:00 уже следующий операционный день.
        При массовом сбое RSS 22:30 откладывает, 22:45 обязан отправить одну строку,
        23:00 ничего не досылает."""
        self.write_config(
            timezone="Asia/Ho_Chi_Minh",
            publication_day_start="23:00",
            channels=self.channel_list(41, deadline="15:00"),
            notifications={
                "morning": "10:00",
                "reminder": "18:00",
                "final": "22:30",
                "slot_window_minutes": 45,
                "mode": "evening_only",
                "check_interval_minutes": 15,
            },
        )

        def failing_rss(url, timeout):
            if "feeds/videos.xml" in url:
                raise TimeoutError("rss timeout")
            return b'{"ok": true}'

        sender = mock.Mock()
        utc = dt.timezone.utc
        # 22:30:05 / 22:45:05 / 23:00:05 по Хошимину = 15:30 / 15:45 / 16:00 UTC
        first, _ = self.run_watchdog(dt.datetime(2026, 8, 12, 15, 30, 5, tzinfo=utc), failing_rss, no_send=False, sender=sender)
        self.assertEqual(first["local_date"], "2026-08-12")
        self.assertEqual(first["notifications"]["planned"], [])
        self.assertEqual(first["notifications"]["deferred"], ["schedule:final:2026-08-12"])

        second, _ = self.run_watchdog(dt.datetime(2026, 8, 12, 15, 45, 5, tzinfo=utc), failing_rss, no_send=False, sender=sender)
        self.assertEqual(second["notifications"]["sent"], ["schedule:final:2026-08-12"])
        self.assertEqual(second["notifications"]["deferred"], [])
        text = sender.call_args.args[2]
        self.assertIn("Итог публикаций за 12.08.2026", text)
        self.assertIn("недоступны у 41 из 41", text)
        self.assertEqual(text.count("\n"), 1)

        third, _ = self.run_watchdog(dt.datetime(2026, 8, 12, 16, 0, 5, tzinfo=utc), failing_rss, no_send=False, sender=sender)
        self.assertEqual(third["local_date"], "2026-08-13")
        # В 23:00 уходит только инфра-тревога про RSS (3-я неудача подряд) — публикационных сообщений нет.
        self.assertEqual(
            [item["key"] for item in third["notifications"]["planned"] if item["key"].startswith("schedule:")],
            [],
        )
        self.assertEqual(third["notifications"]["deferred"], [])
        publication_texts = [call.args[2] for call in sender.call_args_list if "Итог публикаций" in call.args[2]]
        self.assertEqual(len(publication_texts), 1)

    def test_evening_only_recovers_inside_window(self):
        self.evening_config(self.channel_list(41), check_interval_minutes=15)

        def failing_rss(url, timeout):
            if "feeds/videos.xml" in url:
                raise TimeoutError("rss timeout")
            return b'{"ok": true}'

        sender = mock.Mock()
        first, _ = self.run_watchdog(
            dt.datetime(2026, 8, 12, 20, 5, tzinfo=dt.timezone.utc), failing_rss, no_send=False, sender=sender
        )
        self.assertEqual(first["notifications"]["planned"], [])

        feed = atom_feed([("video-1", "Выпуск", "2026-08-12T06:00:00Z")])
        second, _ = self.run_watchdog(
            dt.datetime(2026, 8, 12, 20, 20, tzinfo=dt.timezone.utc),
            self.feed_only_for("UC_TEST_000", feed),
            no_send=False,
            sender=sender,
        )
        self.assertEqual(second["notifications"]["sent"], ["schedule:final:2026-08-12"])
        text = sender.call_args.args[2]
        self.assertIn("Публично вышло 1 из 41", text)
        self.assertIn("Не вышло после дедлайна (40 каналов):", text)
        self.assertNotIn("Тестовый канал 000", text)
        self.assertIn("Тестовый канал 001", text)
        self.assertNotIn("проверить не могу", text)
        self.assertEqual(text.count("•"), 40)

    def test_evening_only_list_cap_truncates_overdue(self):
        self.evening_config(self.channel_list(41), list_cap=8)
        feed = atom_feed([("video-1", "Выпуск", "2026-08-12T06:00:00Z")])
        sender = mock.Mock()
        report, _ = self.run_watchdog(
            dt.datetime(2026, 8, 12, 20, 5, tzinfo=dt.timezone.utc),
            self.feed_only_for("UC_TEST_000", feed),
            no_send=False,
            sender=sender,
        )
        self.assertEqual(report["notifications"]["sent"], ["schedule:final:2026-08-12"])
        text = sender.call_args.args[2]
        self.assertEqual(text.count("•"), 8)
        self.assertIn("…и ещё 32 каналов.", text)
        self.assertIn("вышло 0 из 1", text)
        self.assertNotIn("нет точной квитанции Studio", text)
        self.assertNotIn("RSS", text)

    def test_full_mode_list_cap_keeps_legacy_text_and_truncates(self):
        """В режиме full тексты прежние; list_cap лишь укорачивает список."""
        self.write_config(channels=self.channel_list(41, deadline="09:00"), notifications={**self.BASE_NOTIFICATIONS, "list_cap": 5})
        sender = mock.Mock()
        report, _ = self.run_watchdog(
            dt.datetime(2026, 8, 12, 7, 35, tzinfo=dt.timezone.utc),  # 10:35 MSK, дедлайн 09:00 прошёл
            self.healthy_fetch(atom_feed([])),
            no_send=False,
            sender=sender,
        )
        self.assertEqual(report["notifications"]["sent"], ["schedule:deadline:2026-08-12:0900"])
        text = sender.call_args.args[2]
        self.assertIn("Публичный дедлайн 09:00", text)
        self.assertIn("контроль Studio не настроен; RSS 0/1", text)
        self.assertEqual(text.count("•"), 5)
        self.assertIn("…и ещё 36 каналов.", text)

    def test_force_summary_in_evening_only_still_gives_morning_plan(self):
        self.evening_config(self.channel_list(3))
        report, _ = self.run_watchdog(
            dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.timezone.utc),
            self.healthy_fetch(atom_feed([])),
            no_send=True,
            force_summary=True,
        )
        self.assertEqual(
            [item["key"] for item in report["notifications"]["planned"]],
            ["schedule:morning:2026-08-12"],
        )
        self.assertIn("План публикаций", report["notifications"]["planned"][0]["text"])


class QueueVisibilityTests(unittest.TestCase):
    """Простой 12–16.08.2026: 24 задачи kind=video простояли 3.7 суток в status='queued'
    при infra:queue=ok, потому что проверка смотрела только на 'running', а причину —
    админ-паузу провайдера veo — сторож не читал вовсе."""

    NOW = dt.datetime(2026, 8, 16, 12, 0, tzinfo=dt.timezone.utc)

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Path(self.tempdir.name) / "vsgen.db"
        connection = sqlite3.connect(self.db)
        connection.executescript(
            """
            CREATE TABLE queue_tasks(job_id INTEGER PRIMARY KEY, kind TEXT, provider TEXT,
                                     status TEXT, created REAL, started REAL, claimed REAL);
            CREATE TABLE admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
            CREATE TABLE admin_audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,
                                     action TEXT, target TEXT);
            """
        )
        self.connection = connection

    def tearDown(self):
        self.connection.close()
        self.tempdir.cleanup()

    def add_task(self, job_id, *, status, age_hours, kind="video", provider="veo"):
        self.connection.execute(
            "INSERT INTO queue_tasks(job_id,kind,provider,status,created) VALUES (?,?,?,?,?)",
            (job_id, kind, provider, status, self.NOW.timestamp() - age_hours * 3600),
        )
        self.connection.commit()

    def set_pause(self, providers, *, updated_age_hours):
        self.connection.execute(
            "INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES ('queue_pause',?,?)",
            (
                json.dumps({"all": False, "providers": providers}),
                self.NOW.timestamp() - updated_age_hours * 3600,
            ),
        )
        self.connection.commit()

    def audit(self, action, target, *, age_hours):
        self.connection.execute(
            "INSERT INTO admin_audit(ts,action,target) VALUES (?,?,?)",
            (self.NOW.timestamp() - age_hours * 3600, action, json.dumps({"target": target})),
        )
        self.connection.commit()

    def check(self, **kwargs):
        return watchdog.check_queue(str(self.db), 3.0, self.NOW, **kwargs)

    def test_long_queued_task_breaks_ok(self):
        self.add_task(8310, status="queued", age_hours=79.2)
        result = self.check()
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["stale_queued"]), 1)
        self.assertEqual(result["stale_queued"][0]["job_id"], 8310)
        self.assertIn("queued", result["summary"])
        self.assertIn("8310", result["summary"])

    def test_fresh_queued_task_is_normal_waiting(self):
        """p90 ожидания рендера — 33 мин, максимум 78,9 мин. Порог 6 ч не должен
        превращать штатную пачку в тревогу."""
        self.add_task(9000, status="queued", age_hours=1.4)
        result = self.check()
        self.assertTrue(result["ok"])
        self.assertEqual(result["stale_queued"], [])

    def test_pause_age_taken_from_audit(self):
        self.audit("pause_queue", "veo", age_hours=89.0)
        self.set_pause(["veo"], updated_age_hours=2.0)
        result = self.check()
        self.assertFalse(result["ok"])
        pause = result["provider_pauses"][0]
        self.assertEqual(pause["provider"], "veo")
        self.assertEqual(pause["since_source"], "audit")
        self.assertAlmostEqual(pause["age_hours"], 89.0, places=1)
        self.assertIn("veo", result["summary"])

    def test_resumed_provider_is_not_reported(self):
        self.audit("pause_queue", "veo", age_hours=89.0)
        self.audit("resume_queue", "veo", age_hours=80.0)
        self.set_pause([], updated_age_hours=80.0)
        result = self.check()
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider_pauses"], [])

    def test_pause_without_audit_trace_falls_back_to_state(self):
        """Аудит доказуемо неполон: 14.08 в нём оказалась строка resume_queue, которую
        код кабинета произвести не мог. Пауза без следа обязана остаться видимой."""
        self.set_pause(["veo"], updated_age_hours=48.0)
        result = self.check()
        self.assertFalse(result["ok"])
        pause = result["provider_pauses"][0]
        self.assertEqual(pause["since_source"], "state")
        self.assertAlmostEqual(pause["age_hours"], 48.0, places=1)

    def test_short_pause_is_allowed(self):
        self.audit("pause_queue", "veo", age_hours=1.0)
        self.set_pause(["veo"], updated_age_hours=1.0)
        self.assertTrue(self.check()["ok"])

    def test_missing_admin_state_degrades_without_failing_queue_check(self):
        connection = sqlite3.connect(self.db)
        connection.execute("DROP TABLE admin_state")
        connection.commit()
        connection.close()
        result = self.check()
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider_pauses"], [])
        self.assertNotIn("error", result)


if __name__ == "__main__":
    unittest.main()


class AcknowledgedPauseTests(unittest.TestCase):
    """Осознанная пауза, признанная в конфиге, не тревожит; случайная — тревожит."""

    NOW = dt.datetime(2026, 9, 13, 8, 0, tzinfo=dt.timezone.utc)

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Path(self.tempdir.name) / "vsgen.db"
        connection = sqlite3.connect(self.db)
        connection.executescript(
            """
            CREATE TABLE queue_tasks(job_id INTEGER PRIMARY KEY, kind TEXT, provider TEXT, uid INTEGER,
                                     status TEXT, created REAL, started REAL, claimed REAL, finished REAL);
            CREATE TABLE admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
            CREATE TABLE admin_audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, action TEXT, target TEXT);
            """
        )
        connection.commit()
        connection.close()

    def tearDown(self):
        self.tempdir.cleanup()

    def execute(self, sql, args=()):
        connection = sqlite3.connect(self.db)
        connection.execute(sql, args)
        connection.commit()
        connection.close()

    def add_queued(self, job_id, *, provider, uid, age_hours):
        self.execute(
            "INSERT INTO queue_tasks(job_id,kind,provider,uid,status,created) VALUES (?,?,?,?,?,?)",
            (job_id, "prompts", provider, uid, "queued", self.NOW.timestamp() - age_hours * 3600),
        )

    def pause(self, *, providers=(), uids=(), updated_age_hours=300.0):
        self.execute(
            "INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES ('queue_pause',?,?)",
            (
                json.dumps({"all": False, "providers": list(providers), "prompt_uids": list(uids)}),
                self.NOW.timestamp() - updated_age_hours * 3600,
            ),
        )

    def check(self, **kwargs):
        return watchdog.check_queue(str(self.db), 3.0, self.NOW, **kwargs)

    def test_acknowledged_provider_pause_does_not_break_queue(self):
        self.pause(providers=["prompt_claude"])
        self.add_queued(10894, provider="prompt_claude", uid=3, age_hours=400)
        result = self.check(acknowledged_providers=("prompt_claude",))
        self.assertTrue(result["ok"], result["summary"])
        self.assertEqual(result["stale_queued"], [])
        self.assertEqual(result["acknowledged_queued"], 1)
        self.assertEqual(result["acknowledged_pauses"][0]["provider"], "prompt_claude")

    def test_unacknowledged_pause_still_breaks_queue(self):
        self.pause(providers=["prompt_claude"])
        self.add_queued(10894, provider="prompt_claude", uid=3, age_hours=400)
        result = self.check()
        self.assertFalse(result["ok"])
        self.assertIn("prompt_claude", result["summary"])

    def test_acknowledged_user_pause_hides_only_that_users_tasks(self):
        self.pause(uids=[3])
        self.add_queued(10894, provider="prompt_openai", uid=3, age_hours=400)
        self.add_queued(14574, provider="prompt_openai", uid=14, age_hours=400)
        result = self.check(acknowledged_uids=(3,))
        self.assertFalse(result["ok"])
        self.assertEqual([task["job_id"] for task in result["stale_queued"]], [14574])
        self.assertEqual(result["acknowledged_queued"], 1)

    def test_acknowledgement_without_real_pause_hides_nothing(self):
        self.add_queued(10894, provider="prompt_openai", uid=3, age_hours=400)
        result = self.check(acknowledged_uids=(3,), acknowledged_providers=("prompt_openai",))
        self.assertFalse(result["ok"])
        self.assertEqual([task["job_id"] for task in result["stale_queued"]], [10894])


class CredentialsCheckTests(unittest.TestCase):
    """Утренний отчёт доступов превращается в тревогу, а не лежит в файле."""

    NOW = dt.datetime(2026, 9, 13, 8, 0, tzinfo=dt.timezone.utc)

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "credentials_report.json"

    def tearDown(self):
        self.tempdir.cleanup()

    def write(self, rows, *, age_hours=2.0, checked_at="__auto__"):
        moment = (
            (self.NOW - dt.timedelta(hours=age_hours)).isoformat()
            if checked_at == "__auto__"
            else checked_at
        )
        payload = {"rows": rows}
        if moment is not None:
            payload["checked_at"] = moment
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def check(self):
        return watchdog.check_credentials(str(self.path), self.NOW, 36.0)

    def test_dead_credential_is_failure(self):
        self.write([
            {"name": "googler/FastGen 1", "state": "ok", "detail": "…"},
            {"name": "veo (VeoNonStop)", "state": "МЁРТВ", "detail": "API key has expired"},
        ])
        result = self.check()
        self.assertFalse(result["ok"])
        self.assertEqual(result["dead"], ["veo (VeoNonStop)"])
        self.assertIn("veo (VeoNonStop)", result["summary"])

    def test_fresh_healthy_report_is_ok(self):
        self.write([{"name": "lumean", "state": "ok"}, {"name": "кабинет · claude-пул", "state": "не проверялось"}])
        result = self.check()
        self.assertTrue(result["ok"], result["summary"])
        self.assertEqual(result["dead"], [])

    def test_stale_report_is_failure_even_if_all_alive(self):
        self.write([{"name": "lumean", "state": "ok"}], age_hours=50)
        result = self.check()
        self.assertFalse(result["ok"])
        self.assertIn("не обновлялась", result["summary"])

    def test_report_without_time_is_failure(self):
        self.write([{"name": "lumean", "state": "ok"}], checked_at=None)
        self.assertFalse(self.check()["ok"])

    def test_missing_report_is_failure(self):
        result = self.check()
        self.assertFalse(result["ok"])
        self.assertIn("не найден", result["summary"])

    def test_unconfigured_check_is_skipped(self):
        result = watchdog.check_credentials("", self.NOW)
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])


class TaskFailureCheckTests(unittest.TestCase):
    """Массовые падения этапа видны, даже когда очередь при этом «в порядке»."""

    NOW = dt.datetime(2026, 9, 13, 8, 0, tzinfo=dt.timezone.utc)

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Path(self.tempdir.name) / "vsgen.db"
        connection = sqlite3.connect(self.db)
        connection.execute(
            "CREATE TABLE queue_tasks(job_id INTEGER PRIMARY KEY, kind TEXT, provider TEXT, uid INTEGER,"
            " status TEXT, created REAL, started REAL, claimed REAL, finished REAL)"
        )
        connection.commit()
        connection.close()

    def tearDown(self):
        self.tempdir.cleanup()

    def add(self, count, *, kind, status, finished_age_hours=1.0, uid=7):
        connection = sqlite3.connect(self.db)
        for _ in range(count):
            connection.execute(
                "INSERT INTO queue_tasks(kind,status,uid,created,finished) VALUES (?,?,?,?,?)",
                (
                    kind,
                    status,
                    uid,
                    self.NOW.timestamp() - (finished_age_hours + 0.5) * 3600,
                    self.NOW.timestamp() - finished_age_hours * 3600,
                ),
            )
        connection.commit()
        connection.close()

    def check(self):
        return watchdog.check_task_failures(str(self.db), self.NOW, window_hours=6.0, min_errors=5, max_share=0.5)

    def test_mass_failures_of_one_stage_break_check(self):
        self.add(6, kind="prompts", status="error")
        self.add(2, kind="prompts", status="done")
        self.add(4, kind="tts", status="done")
        result = self.check()
        self.assertFalse(result["ok"])
        self.assertIn("раскадровка: 6 из 8", result["summary"])
        self.assertNotIn("озвучка", result["summary"])

    def test_few_errors_are_tolerated(self):
        self.add(3, kind="video", status="error")
        self.assertTrue(self.check()["ok"])

    def test_minority_of_failures_is_tolerated(self):
        self.add(6, kind="prompts", status="error")
        self.add(20, kind="prompts", status="done")
        self.assertTrue(self.check()["ok"])

    def test_failures_outside_window_are_ignored(self):
        self.add(10, kind="video", status="error", finished_age_hours=20)
        self.assertTrue(self.check()["ok"])

    def test_stage_markers_without_owner_are_not_counted(self):
        self.add(6, kind="images", status="error", uid=None)
        self.assertTrue(self.check()["ok"])


class NewInfrastructureWiringTests(unittest.TestCase):
    """Новые проверки доходят до отчёта и тревог; пустое имя службы глушит бот автопокупки."""

    NOW = dt.datetime(2026, 9, 13, 8, 0, tzinfo=dt.timezone.utc)

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.state_db = self.root / "state" / "watchdog.db"
        self.vsgen_db = self.root / "vsgen.db"
        connection = sqlite3.connect(self.vsgen_db)
        connection.execute(
            "CREATE TABLE queue_tasks(job_id INTEGER PRIMARY KEY, kind TEXT, provider TEXT, uid INTEGER,"
            " status TEXT, created REAL, started REAL, claimed REAL, finished REAL)"
        )
        connection.commit()
        connection.close()
        self.telegram_path = self.root / "telegram.json"
        self.telegram_path.write_text(
            json.dumps({"telegram": {"enabled": True, "botToken": "SECRET_BOT_TOKEN", "chatId": "12345"}}),
            encoding="utf-8",
        )
        self.report_path = self.root / "credentials_report.json"
        self.config_path = self.root / "watchdog.json"

    def tearDown(self):
        self.tempdir.cleanup()

    def write_config(self, **infrastructure):
        infra = {
            "health_url": "https://health.invalid/healthz",
            "vsgen_db": str(self.vsgen_db),
            "min_free_gb": 1,
            "queue_stale_hours": 3,
            "ytcortiez_service": "",
        }
        infra.update(infrastructure)
        config = {
            "timezone": "Europe/Moscow",
            "telegram_config": str(self.telegram_path),
            "channels": [
                {
                    "uc": "UC_TEST_CHANNEL",
                    "name": "Тестовый канал",
                    "niche": "ww2",
                    "lang": "ru",
                    "days": list(range(7)),
                    "expected_publications": 1,
                    "deadline": "09:00",
                    "enabled": True,
                }
            ],
            "notifications": {"morning": "10:00", "reminder": "18:00", "final": "23:00", "slot_window_minutes": 30},
            "infrastructure": infra,
            "failure_alert_after": 3,
            "failure_cooldown_hours": 6,
            "http_timeout_seconds": 2,
            "rss_workers": 2,
        }
        self.config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def run_once(self):
        feed = atom_feed([("video-1", "Выпуск", "2026-09-13T05:00:00Z")])

        def fetch(url, timeout):
            if "feeds/videos.xml" in url:
                return feed
            if "health" in url:
                return b'{"ok": true, "status": "ok"}'
            raise AssertionError(url)

        with (
            mock.patch.object(watchdog, "fetch_bytes", side_effect=fetch),
            mock.patch.object(watchdog, "disk_free_gb", return_value=100.0),
            mock.patch.object(watchdog, "run_systemctl", return_value=(False, "inactive")) as systemctl,
            mock.patch.object(watchdog, "send_telegram", mock.Mock()),
        ):
            report = watchdog.run_check(self.config_path, self.state_db, no_send=True, now=self.NOW)
        return report, systemctl

    def test_empty_service_name_silences_autobuyer_check(self):
        self.write_config()
        report, systemctl = self.run_once()
        self.assertTrue(report["infrastructure"]["ytcortiez"]["skipped"])
        systemctl.assert_not_called()
        self.assertNotIn("infra:ytcortiez", [failure["key"] for failure in report["failures"]])

    def test_dead_credential_reaches_report_failures(self):
        self.report_path.write_text(
            json.dumps(
                {
                    "checked_at": (self.NOW - dt.timedelta(hours=2)).isoformat(),
                    "rows": [{"name": "veo (VeoNonStop)", "state": "МЁРТВ"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.write_config(credentials_report=str(self.report_path))
        report, _ = self.run_once()
        self.assertFalse(report["infrastructure"]["credentials"]["ok"])
        self.assertIn("infra:credentials", [failure["key"] for failure in report["failures"]])

    def test_mass_task_failures_reach_report_failures(self):
        connection = sqlite3.connect(self.vsgen_db)
        for _ in range(6):
            connection.execute(
                "INSERT INTO queue_tasks(kind,status,uid,created,finished) VALUES ('prompts','error',14,?,?)",
                (self.NOW.timestamp() - 7200, self.NOW.timestamp() - 3600),
            )
        connection.commit()
        connection.close()
        self.write_config()
        report, _ = self.run_once()
        self.assertFalse(report["infrastructure"]["task_failures"]["ok"])
        self.assertIn("раскадровка", report["infrastructure"]["task_failures"]["summary"])
        self.assertIn("infra:task_failures", [failure["key"] for failure in report["failures"]])

    def test_invalid_acknowledged_uids_are_rejected(self):
        self.write_config(acknowledged_pause_uids=["3"])
        with self.assertRaises(watchdog.ConfigError):
            watchdog.run_check(self.config_path, self.state_db, no_send=True, now=self.NOW)

    @unittest.skipIf(os.geteuid() == 0, "root игнорирует права каталога")
    def test_service_probes_use_readonly_opener(self):
        """Пробы service_health открывают WAL-базу без -shm в read-only каталоге так же,
        как infra:queue (иначе на VPS под ProtectSystem=strict проба мигала OperationalError)."""
        ro_dir = self.root / "ro"
        ro_dir.mkdir()
        heartbeat = write_wal_heartbeat_db(ro_dir / "vsgen.db", self.NOW.timestamp(), fresh=True)
        self.assertFalse((ro_dir / "vsgen.db-wal").exists())
        self.write_config(service_probes={"render_heartbeat_db": str(heartbeat)})
        os.chmod(ro_dir, 0o555)
        try:
            report, _ = self.run_once()
        finally:
            os.chmod(ro_dir, 0o755)
        self.assertTrue(report["infrastructure"]["minsk"]["ok"], report["infrastructure"]["minsk"])
        self.assertNotIn("infra:minsk", [failure["key"] for failure in report["failures"]])


class ServiceHealthProbeTests(unittest.TestCase):
    """Базы соседей и heartbeat читаются без права записи в их каталог."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.ro_dir = self.root / "ro"
        self.ro_dir.mkdir()
        self.now = 1789635217.0

    def tearDown(self):
        os.chmod(self.ro_dir, 0o755)
        self.tempdir.cleanup()

    def make_cover_db(self):
        db = self.ro_dir / "cover_job_queue.db"
        connection = sqlite3.connect(db)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY, status TEXT, created_at REAL, data TEXT)")
        connection.execute(
            "INSERT INTO jobs(status,created_at,data) VALUES('completed',?,?)",
            (self.now - 7200, json.dumps({"provider": "grok_api", "finished_at": self.now - 7000})),
        )
        connection.execute(
            "INSERT INTO jobs(status,created_at,data) VALUES('failed',?,?)",
            (self.now - 3 * 86400, json.dumps({"error": "boom", "finished_at": self.now - 3 * 86400})),
        )
        connection.commit()
        connection.close()
        self.assertFalse((self.ro_dir / "cover_job_queue.db-shm").exists())
        return db

    @unittest.skipIf(os.geteuid() == 0, "root игнорирует права каталога")
    def test_wal_db_without_sidecars_in_readonly_dir(self):
        db = write_wal_heartbeat_db(self.ro_dir / "vsgen.db", self.now, fresh=True)
        os.chmod(self.ro_dir, 0o555)
        legacy = service_health.check_render_heartbeat(str(db), self.now)
        self.assertFalse(legacy["ok"])
        self.assertIn("OperationalError", legacy["summary"])
        self.assertIn("error", legacy)

        fixed = service_health.check_render_heartbeat(str(db), self.now, open_db=watchdog._open_queue_readonly)
        self.assertTrue(fixed["ok"], fixed)
        self.assertIn("heartbeat свежий", fixed["summary"])
        self.assertEqual(fixed["age_seconds"], 5)

    @unittest.skipIf(os.geteuid() == 0, "root игнорирует права каталога")
    def test_cover_like_jobs_db_readonly_dir_twice(self):
        db = self.make_cover_db()
        os.chmod(self.ro_dir, 0o555)
        cfg = {"name": "cover", "unit": "cover-server", "health_url": "http://127.0.0.1:8900/", "db": str(db)}
        urlopen = mock.MagicMock()
        urlopen.return_value.__enter__.return_value.status = 200
        with (
            mock.patch.object(
                service_health.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout="active\n", stderr=""),
            ),
            mock.patch.object(service_health.urllib.request, "urlopen", urlopen),
        ):
            results = [
                service_health.check_neighbor(cfg, self.now, open_db=watchdog._open_queue_readonly)
                for _ in range(2)
            ]
        for result in results:
            self.assertTrue(result["ok"], result)
            self.assertTrue(result["summary"].startswith("cover: зависших 0"), result["summary"])
            self.assertEqual(result["category"], "idle")

    def test_check_all_passes_opener_to_probes(self):
        db = write_wal_heartbeat_db(self.ro_dir / "vsgen.db", self.now, fresh=False)
        seen = []

        def opener(path):
            seen.append(path)
            return service_health._default_open(path)

        out = service_health.check_all({"render_heartbeat_db": str(db)}, self.now, open_db=opener)
        self.assertEqual([Path(item).resolve() for item in seen], [Path(db).resolve()])
        self.assertFalse(out["minsk"]["ok"])
        self.assertIn("heartbeat устарел", out["minsk"]["summary"])


class ReportIgnoreComponentsTests(unittest.TestCase):
    """storage.json: красный блок delivery_evidence с 14.09 держал тревогу каждые 6 ч.
    Его можно исключить конфигом, но не молча: он остаётся в summary и в данных отчёта."""

    NOW = 1789635217.0

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "storage.json"

    def tearDown(self):
        self.tempdir.cleanup()

    def write(self, components, *, age=100, ok=None):
        payload = {
            "generated_at": self.NOW - age,
            "ok": all(components.values()) if ok is None else ok,
            "components": {name: {"ok": value} for name, value in components.items()},
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    STORAGE_LIKE = {"cache": True, "delivery_evidence": False, "disk": True, "inventory": True, "retention": True}

    def test_without_ignore_report_fails_and_names_block(self):
        self.write(self.STORAGE_LIKE)
        result = service_health.check_report(str(self.path), 3600, self.NOW)
        self.assertFalse(result["ok"])
        self.assertIn("delivery_evidence", result["summary"])
        self.assertEqual(result["failed_components"], ["delivery_evidence"])
        self.assertEqual(result["ignored_failing"], [])

    def test_ignored_block_keeps_report_ok_but_visible(self):
        self.write(self.STORAGE_LIKE)
        result = service_health.check_report(
            str(self.path), 3600, self.NOW, ignore_components=["delivery_evidence"]
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["ignored_failing"], ["delivery_evidence"])
        self.assertIn("без учёта: delivery_evidence", result["summary"])
        self.assertTrue(result["summary"].startswith("проверка свежая и успешная"))

    def test_other_failing_block_still_breaks_with_ignore(self):
        self.write({**self.STORAGE_LIKE, "disk": False})
        result = service_health.check_report(
            str(self.path), 3600, self.NOW, ignore_components=["delivery_evidence"]
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_components"], ["disk"])
        self.assertIn("сбой блоков отчёта: disk", result["summary"])

    def test_top_level_failure_outside_blocks_is_not_masked_by_ignore(self):
        """Верхний ok=False при зелёных блоках (у backup так пишется offsite_manifest_error):
        ignore_components этого не покрывает, а деталь называет причину, а не «успешно»."""
        self.write({"cache": True, "delivery_evidence": True, "disk": True}, ok=False)
        with_ignore = service_health.check_report(
            str(self.path), 3600, self.NOW, ignore_components=["delivery_evidence"]
        )
        self.assertFalse(with_ignore["ok"], with_ignore)
        self.assertEqual(with_ignore["failed_components"], [])
        self.assertEqual(with_ignore["ignored_failing"], [])
        self.assertIn("ошибку вне блоков", with_ignore["summary"])
        self.assertNotIn("успешн", with_ignore["summary"])

        legacy = service_health.check_report(str(self.path), 3600, self.NOW)
        self.assertFalse(legacy["ok"])
        self.assertIn("ошибку вне блоков", legacy["summary"])

    def test_stale_report_fails_even_with_ignore(self):
        self.write(self.STORAGE_LIKE, age=7200)
        result = service_health.check_report(
            str(self.path), 3600, self.NOW, ignore_components=["delivery_evidence"]
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["summary"].startswith("отчёт устарел"), result["summary"])

    def test_all_green_report_keeps_legacy_summary(self):
        self.write({"disk": True, "archive": True})
        result = service_health.check_report(str(self.path), 3600, self.NOW)
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"], "проверка свежая и успешная")

    def test_ignore_components_must_be_list_via_check_all(self):
        self.write(self.STORAGE_LIKE)
        entry = {"name": "storage", "path": str(self.path), "max_age_seconds": 3600}
        bad = service_health.check_all({"reports": [{**entry, "ignore_components": "delivery_evidence"}]}, self.NOW)
        self.assertEqual(bad["report_storage"]["summary"], "проверка недоступна (ValueError)")
        good = service_health.check_all({"reports": [{**entry, "ignore_components": ["delivery_evidence"]}]}, self.NOW)
        self.assertTrue(good["report_storage"]["ok"], good)
        legacy = service_health.check_all({"reports": [entry]}, self.NOW)
        self.assertFalse(legacy["report_storage"]["ok"])


class FailureComponentLabelTests(unittest.TestCase):
    def test_service_probe_keys_have_human_labels(self):
        self.assertEqual(watchdog._failure_component("infra:minsk", {}), "рендер-нода Минск")
        self.assertEqual(watchdog._failure_component("infra:service_cover", {}), "сервис обложек")
        self.assertEqual(watchdog._failure_component("infra:service_rewrite", {}), "сервис рерайтов")
        self.assertEqual(watchdog._failure_component("infra:report_storage", {}), "уборка диска")
        self.assertEqual(watchdog._failure_component("infra:report_backup", {}), "ночной бэкап")
        self.assertEqual(watchdog._failure_component("infra:unknown_thing", {}), "infra:unknown_thing")

    def test_alert_text_shows_duration_when_since_known(self):
        now = dt.datetime(2026, 9, 17, 12, 0, tzinfo=dt.timezone.utc)
        with_since = watchdog._failure_notification(
            "infra:minsk", "рендер-нода Минск", "деталь", 15, now, since="2026-09-17T08:15:00Z"
        )
        self.assertIn("Длится: 3 ч 45 мин (проверок подряд: 15)", with_since["text"])
        self.assertIn("устойчивая проблема", with_since["text"])
        without = watchdog._failure_notification("infra:minsk", "рендер-нода Минск", "деталь", 15, now)
        self.assertIn("Повторяется проверок подряд: 15", without["text"])
