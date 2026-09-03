import datetime as dt
import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "yt_studio.py"
SPEC = importlib.util.spec_from_file_location("yt_studio_schedule_time", MODULE_PATH)
yt_studio = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(yt_studio)

UPLOAD_WORKER_PATH = Path(__file__).resolve().parents[1] / "src" / "upload_worker.py"
sys.path.insert(0, str(UPLOAD_WORKER_PATH.parent))
WORKER_SPEC = importlib.util.spec_from_file_location("upload_worker_schedule_time", UPLOAD_WORKER_PATH)
upload_worker = importlib.util.module_from_spec(WORKER_SPEC)
assert WORKER_SPEC.loader is not None
WORKER_SPEC.loader.exec_module(upload_worker)


class ScheduleTimeReadbackTest(unittest.TestCase):
    def test_accepts_12_and_24_hour_readback(self):
        expected = dt.datetime(2026, 8, 13, 18, 0)
        self.assertTrue(yt_studio._shown_time_matches("6:00 PM", expected))
        self.assertTrue(yt_studio._shown_time_matches("6:00\u202fPM", expected))
        self.assertTrue(yt_studio._shown_time_matches("18:00", expected))
        self.assertTrue(yt_studio._shown_time_matches("6:00 P.M.", expected))
        self.assertTrue(yt_studio._shown_time_matches("18.00", expected))

    def test_rejects_morning_value_from_24_hour_input(self):
        expected = dt.datetime(2026, 8, 13, 18, 0)
        self.assertFalse(yt_studio._shown_time_matches("06:00", expected))
        self.assertFalse(yt_studio._shown_time_matches("6:00 AM", expected))

    def test_rejects_unreadable_or_invalid_values(self):
        expected = dt.datetime(2026, 8, 13, 18, 0)
        self.assertFalse(yt_studio._shown_time_matches("", expected))
        self.assertFalse(yt_studio._shown_time_matches("25:00", expected))

    def test_handles_noon_and_midnight(self):
        noon = dt.datetime(2026, 8, 13, 12, 0)
        midnight = dt.datetime(2026, 8, 14, 0, 0)
        self.assertTrue(yt_studio._shown_time_matches("12:00 PM", noon))
        self.assertTrue(yt_studio._shown_time_matches("12:00 AM", midnight))
        self.assertFalse(yt_studio._shown_time_matches("12:00 AM", noon))
        self.assertFalse(yt_studio._shown_time_matches("12:00 PM", midnight))

    def test_legacy_parser_uses_same_fail_closed_semantics(self):
        expected = dt.datetime(2026, 8, 13, 18, 0)
        self.assertTrue(upload_worker._schedule_time_matches("18:00", expected))
        self.assertTrue(upload_worker._schedule_time_matches("6:00 P.M.", expected))
        self.assertFalse(upload_worker._schedule_time_matches("06:00", expected))
        self.assertFalse(upload_worker._schedule_time_matches(True, expected))


class AbsoluteScheduleTimezoneTest(unittest.TestCase):
    def test_direct_worker_rejects_naive_datetime_before_adspower(self):
        result = upload_worker.upload_to_youtube(
            video_path="/missing.mp4",
            title="Title",
            description="Description",
            profile_id="9002",
            schedule_date=dt.datetime(2026, 8, 14, 10, 30),
            channel_url="https://studio.youtube.com/channel/UC1234567890123456789012/",
        )
        self.assertFalse(result["success"])
        self.assertIn("timezone-aware", result["error"])

    def test_offset_iso_is_preserved_and_converted_to_browser_timezone(self):
        source = upload_worker._parse_schedule("2026-08-14T10:30:00+07:00")
        self.assertIsNotNone(source.utcoffset())

        picker, epoch, mode = upload_worker._schedule_for_browser_picker(
            source, "Europe/Amsterdam"
        )

        # 10:30 UTC+7 == 03:30 UTC == 05:30 CEST.
        self.assertEqual(picker, dt.datetime(2026, 8, 14, 5, 30))
        self.assertEqual(epoch, int(source.timestamp()))
        self.assertEqual(mode, "absolute")

    def test_zoneinfo_applies_dst_transition(self):
        before = upload_worker._parse_schedule("2026-03-29T00:30:00+00:00")
        after = upload_worker._parse_schedule("2026-03-29T01:30:00+00:00")

        before_picker, _, _ = upload_worker._schedule_for_browser_picker(
            before, "Europe/Amsterdam"
        )
        after_picker, _, _ = upload_worker._schedule_for_browser_picker(
            after, "Europe/Amsterdam"
        )

        self.assertEqual(before_picker, dt.datetime(2026, 3, 29, 1, 30))
        self.assertEqual(after_picker, dt.datetime(2026, 3, 29, 3, 30))

    def test_ambiguous_dst_fold_is_rejected_before_finalization(self):
        ambiguous = upload_worker._parse_schedule("2026-10-25T00:30:00+00:00")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            upload_worker._schedule_for_browser_picker(
                ambiguous, "Europe/Amsterdam"
            )

    def test_naive_input_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "UTC offset"):
            upload_worker._parse_schedule("2026-08-14T10:30:00")

    def test_aware_input_requires_valid_browser_timezone(self):
        source = upload_worker._parse_schedule("2026-08-14T10:30:00+07:00")
        for timezone_name in ("", "Not/A_Real_Timezone"):
            with self.subTest(timezone_name=timezone_name):
                with self.assertRaises(ValueError):
                    upload_worker._schedule_for_browser_picker(source, timezone_name)

    def test_exact_epoch_mismatch_is_not_tolerated(self):
        expected = 1786678200
        self.assertTrue(upload_worker._scheduled_epoch_matches(expected, expected))
        self.assertFalse(upload_worker._scheduled_epoch_matches(expected + 1, expected))
        self.assertFalse(upload_worker._scheduled_epoch_matches(expected + 3600, expected))

    def test_browser_timezone_comes_from_intl(self):
        class Driver:
            script = ""

            def execute_script(self, script):
                self.script = script
                return "Europe/Amsterdam"

        driver = Driver()
        self.assertEqual(upload_worker._browser_timezone(driver), "Europe/Amsterdam")
        self.assertIn("Intl.DateTimeFormat", driver.script)


if __name__ == "__main__":
    unittest.main()
