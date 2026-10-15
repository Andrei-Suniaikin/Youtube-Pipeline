"""Native headless sizing with real evidence writes and guarded browser calls."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


COLLECTOR_PATH = Path(__file__).resolve().parents[1] / "workers/minsk/collector.py"
SPEC = importlib.util.spec_from_file_location("earn_collector_headless", COLLECTOR_PATH)
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)
TARGET = {"width": 1590, "height": 903}


class LeaseLost(RuntimeError):
    pass


class FakeDriver:
    def __init__(self, inner=None, resize_results=None, resize_error=None):
        self.inner = dict(inner or {"width": 800, "height": 513})
        self.rect = {"x": 0, "y": 0, "width": self.inner["width"],
                     "height": self.inner["height"] + 87}
        self.resize_results = list(resize_results or [])
        self.resize_error = resize_error
        self.calls = []
        self.after_action = None

    def observed(self, name):
        self.calls.append(name)
        if self.after_action:
            self.after_action(name)

    def execute_script(self, script):
        if script != "return {width:innerWidth,height:innerHeight}":
            raise AssertionError("Unexpected browser script")
        self.observed("inner")
        return dict(self.inner)

    def get_window_rect(self):
        self.observed("rect")
        return dict(self.rect)

    def set_window_rect(self, **requested):
        self.observed(("resize", requested))
        if self.resize_error:
            raise self.resize_error
        self.rect.update(requested)
        self.inner = (dict(self.resize_results.pop(0)) if self.resize_results else
                      {"width": requested["width"], "height": requested["height"] - 87})


class HeadlessViewportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.guards = 0

    def guard(self):
        self.guards += 1

    def run_helper(self, driver, guard=None):
        collector.configure_headless_viewport(driver, self.directory, guard or self.guard, enabled=True)

    def evidence(self):
        return json.loads((self.directory / "headless_viewport.json").read_text())

    def test_disabled_does_not_touch_browser_guard_or_evidence(self):
        def forbidden_guard():
            raise AssertionError("Headed mode must do nothing")

        collector.configure_headless_viewport(object(), self.directory, forbidden_guard, enabled=False)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_correct_viewport_needs_no_resize(self):
        driver = FakeDriver(inner=TARGET)
        self.run_helper(driver)
        self.assertEqual(driver.calls, ["inner"])
        self.assertEqual(self.guards, 2)
        result = self.evidence()
        self.assertEqual(result, {"before": TARGET, "after": TARGET, "target": TARGET,
                                  "attempts": [], "confirmed": True})

    def test_small_viewport_resized_to_full_png_dimensions(self):
        driver = FakeDriver()
        self.run_helper(driver)
        self.assertEqual(driver.calls, ["inner", "rect",
                                      ("resize", {"width": 1590, "height": 990}), "inner"])
        self.assertEqual(self.guards, 8)
        result = self.evidence()
        self.assertEqual(result["before"], {"width": 800, "height": 513})
        self.assertEqual(result["after"], TARGET)
        self.assertTrue(result["confirmed"])
        self.assertEqual(len(result["attempts"]), 1)

    def test_second_correction_uses_actual_inner_and_outer_dimensions(self):
        driver = FakeDriver(resize_results=[{"width": 1500, "height": 850}, TARGET])
        self.run_helper(driver)
        result = self.evidence()
        self.assertEqual(len(result["attempts"]), 2)
        self.assertEqual(result["attempts"][1]["requested"], {"width": 1680, "height": 1043})
        self.assertEqual(result["after"], TARGET)
        self.assertTrue(result["confirmed"])
        self.assertEqual(self.guards, 2 * len(driver.calls))

    def test_unconfirmed_size_stops_after_two_corrections(self):
        wrong = {"width": 1200, "height": 700}
        driver = FakeDriver(resize_results=[wrong, wrong])
        with self.assertRaisesRegex(collector.StopRun, "could not be matched"):
            self.run_helper(driver)
        result = self.evidence()
        self.assertEqual(len(result["attempts"]), 2)
        self.assertEqual(result["after"], wrong)
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["error"]["type"], "StopRun")
        self.assertEqual(len([call for call in driver.calls if isinstance(call, tuple)]), 2)

    def test_invalid_native_rect_stops_before_resize(self):
        driver = FakeDriver()
        driver.rect["width"] = "800"
        with self.assertRaisesRegex(collector.StopRun, "native window dimensions are invalid"):
            self.run_helper(driver)
        self.assertEqual(driver.calls, ["inner", "rect"])
        result = self.evidence()
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["attempts"], [])
        self.assertEqual(result["error"]["type"], "StopRun")

    def test_lease_loss_before_first_action_prevents_browser_access(self):
        loss = LeaseLost("lease no longer held")

        def lost():
            raise loss

        driver = FakeDriver()
        with self.assertRaises(LeaseLost) as raised:
            self.run_helper(driver, lost)
        self.assertIs(raised.exception, loss)
        self.assertEqual(driver.calls, [])
        result = self.evidence()
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["error"]["type"], "LeaseLost")

    def test_lease_loss_after_browser_action_prevents_next_action(self):
        loss = LeaseLost("lost while reading viewport")
        driver = FakeDriver()

        def guard():
            if driver.calls:
                raise loss

        with self.assertRaises(LeaseLost) as raised:
            self.run_helper(driver, guard)
        self.assertIs(raised.exception, loss)
        self.assertEqual(driver.calls, ["inner"])
        self.assertEqual(self.evidence()["error"]["type"], "LeaseLost")

    def test_resize_failure_stops_and_preserves_evidence(self):
        failure = ValueError("native resize failed")
        driver = FakeDriver(resize_error=failure)
        with self.assertRaisesRegex(collector.StopRun, "set_window_rect failed") as raised:
            self.run_helper(driver)
        self.assertIs(raised.exception.__cause__, failure)
        result = self.evidence()
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["after"], {"width": 800, "height": 513})
        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(self.guards, 2 * len(driver.calls))

    def test_lease_loss_during_failed_resize_is_not_wrapped(self):
        loss = LeaseLost("lease expired during resize")
        driver = FakeDriver(resize_error=ValueError("native resize failed"))

        def guard():
            if driver.calls and isinstance(driver.calls[-1], tuple):
                raise loss

        with self.assertRaises(LeaseLost) as raised:
            self.run_helper(driver, guard)
        self.assertIs(raised.exception, loss)
        self.assertEqual(self.evidence()["error"]["type"], "LeaseLost")

    def test_repeated_evidence_write_keeps_previous_record_backup(self):
        self.run_helper(FakeDriver())
        previous = (self.directory / "headless_viewport.json").read_bytes()
        self.run_helper(FakeDriver(inner=TARGET))
        self.assertEqual((self.directory / "headless_viewport.json.bak").read_bytes(), previous)
        self.assertEqual(self.evidence()["attempts"], [])


if __name__ == "__main__":
    unittest.main()
