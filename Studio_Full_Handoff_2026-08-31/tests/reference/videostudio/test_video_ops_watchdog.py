import datetime as dt
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock


WATCHDOG_PATH = Path(__file__).resolve().parents[1] / "tools" / "video_ops_watchdog" / "watchdog.py"
SPEC = importlib.util.spec_from_file_location("video_ops_watchdog", WATCHDOG_PATH)
watchdog = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(watchdog)


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
