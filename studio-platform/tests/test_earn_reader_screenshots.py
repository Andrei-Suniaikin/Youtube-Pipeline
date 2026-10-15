"""Background screenshots: bytes/files, lease failures and evidence records.

Uses fake CDP responses and real temporary files; never opens a browser.
"""
import base64
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


READER_PATH = Path(__file__).resolve().parents[1] / "workers/minsk/studio_reader.py"
SPEC = importlib.util.spec_from_file_location("earn_studio_reader", READER_PATH)
reader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reader)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6"
    "u5EAAAAASUVORK5CYII="
)
UC = "UC" + "a" * 22


class FakeDriver:
    """Deliberately has no WebDriver screenshot or window activation methods."""

    def __init__(self, data=PNG, error=None):
        self.data = data
        self.error = error
        self.commands = []
        self.scripts = []
        self.before_capture = None

    def execute_cdp_cmd(self, command, params):
        self.commands.append((command, params))
        if self.before_capture is not None:
            self.before_capture(len(self.commands))
        if self.error is not None:
            raise self.error
        return {"data": base64.b64encode(self.data).decode("ascii")}

    def execute_script(self, script):
        self.scripts.append(script)
        if script == reader.JS_ID:
            # Stop before any channel activation/click; exercise real failure evidence.
            return {"url": "https://accounts.google.com/", "ready": "complete"}
        if script == "return document.body ? document.body.innerText : '';":
            return "Sign in"
        raise AssertionError("Unexpected script")


class CaptureScreenshotTests(unittest.TestCase):
    def test_returns_png_bytes_via_background_cdp_command(self):
        driver = FakeDriver()
        self.assertEqual(reader.capture_screenshot(driver), PNG)
        self.assertEqual(driver.commands, [
            ("Page.captureScreenshot", {"format": "png", "fromSurface": True})
        ])

    def test_file_mode_writes_exact_png_and_returns_true(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence.png"
            self.assertIs(reader.capture_screenshot(FakeDriver(), str(output)), True)
            self.assertEqual(output.read_bytes(), PNG)

    def test_file_mode_returns_false_on_oserror(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "missing-parent" / "evidence.png"
            self.assertIs(reader.capture_screenshot(FakeDriver(), str(output)), False)
            self.assertFalse(output.exists())

    def test_protocol_error_is_not_swallowed_in_bytes_or_file_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            for filename in (None, str(Path(directory) / "evidence.png")):
                with self.subTest(filename=filename):
                    with self.assertRaisesRegex(RuntimeError, "CDP unavailable"):
                        reader.capture_screenshot(
                            FakeDriver(error=RuntimeError("CDP unavailable")), filename
                        )


class WakeRenderTests(unittest.TestCase):
    def test_lease_is_checked_before_and_after_capture(self):
        driver = FakeDriver()
        checks = []
        observations = []
        reader.wake_render(driver, lambda: checks.append(len(driver.commands)),
                           observations, "background-wait")
        self.assertEqual(checks, [0, 1])
        self.assertEqual(observations[0]["status"], "ok")
        self.assertEqual(observations[0]["reason"], "background-wait")
        self.assertIn("elapsed_seconds", observations[0])

    def test_empty_png_is_observed_without_claiming_success(self):
        observations = []
        reader.wake_render(FakeDriver(data=b""), lambda: None, observations, "empty")
        self.assertEqual(observations[0]["status"], "empty_screenshot")

    def test_cdp_error_is_recorded_and_final_lease_check_still_runs(self):
        checks = []
        observations = []
        reader.wake_render(FakeDriver(error=RuntimeError("capture failed")),
                           lambda: checks.append(True), observations, "retry")
        self.assertEqual(len(checks), 2)
        self.assertEqual(observations[0]["status"], "error")
        self.assertEqual(observations[0]["error"], "RuntimeError: capture failed")

    def test_lost_lease_before_capture_prevents_cdp_call(self):
        driver = FakeDriver()
        observations = []

        def lost_lease():
            raise RuntimeError("lease lost")

        with self.assertRaisesRegex(RuntimeError, "lease lost"):
            reader.wake_render(driver, lost_lease, observations, "pre-check")
        self.assertEqual(driver.commands, [])
        self.assertEqual(observations, [])

    def test_lost_lease_during_capture_propagates_after_recording(self):
        for capture_error in (None, RuntimeError("capture failed")):
            with self.subTest(capture_error=capture_error):
                driver = FakeDriver(error=capture_error)
                observations = []

                def lease_check():
                    if driver.commands:
                        raise RuntimeError("lease lost")

                with self.assertRaisesRegex(RuntimeError, "lease lost"):
                    reader.wake_render(driver, lease_check, observations, "post-check")
                self.assertEqual(len(observations), 1)
                self.assertEqual(observations[0]["status"],
                                 "error" if capture_error else "ok")


class EvidenceTests(unittest.TestCase):
    def run_reader(self, driver, directory):
        return reader.read_channel(driver, UC, directory, {
            "context": {"authuser": 0, "email": "test@example.invalid"}
        })

    def test_entry_failure_preserves_reason_and_saves_cdp_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            driver = FakeDriver()
            result = self.run_reader(driver, directory)
            root = Path(directory)
            self.assertEqual(result["reason"], "need_auth")
            self.assertEqual((root / "entry_failure.png").read_bytes(), PNG)
            record = json.loads((root / "entry_failure.json").read_text())
            self.assertEqual(record["screenshot"], "entry_failure.png")
            self.assertEqual(record["data"]["reason"], "need_auth")
            self.assertEqual(len(driver.commands), 2)  # Wake and evidence.
            self.assertEqual(json.loads((root / "reader_result.json").read_text()), result)

    def test_evidence_capture_error_is_saved_without_losing_reader_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_reader(FakeDriver(error=RuntimeError("capture failed")), directory)
            record = json.loads((Path(directory) / "entry_failure.json").read_text())
            self.assertEqual(result["reason"], "need_auth")
            self.assertEqual(record["screenshot_error"], "RuntimeError: capture failed")
            self.assertNotIn("screenshot", record)
            self.assertEqual(result["render_wake_attempts"][0]["status"], "error")

    def test_evidence_file_error_is_recorded_as_screenshot_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            driver = FakeDriver()

            def block_evidence_write(capture_number):
                if capture_number == 2:
                    (Path(directory) / "entry_failure.png").mkdir()

            driver.before_capture = block_evidence_write
            result = self.run_reader(driver, directory)
            record = json.loads((Path(directory) / "entry_failure.json").read_text())
            self.assertEqual(result["reason"], "need_auth")
            self.assertEqual(record["screenshot_error"], "RuntimeError: screenshot_returned_false")
            self.assertNotIn("screenshot", record)


if __name__ == "__main__":
    unittest.main()
