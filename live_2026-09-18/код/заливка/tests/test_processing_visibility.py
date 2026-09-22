"""Offline processing verdicts: real decision block, state reader, and contract."""

import ast
from contextlib import redirect_stdout
from datetime import datetime, timezone
import importlib.util
import io
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/upload_worker.py"
SPEC = importlib.util.spec_from_file_location("processing_visibility_worker", SOURCE)
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)
VIDEO_ID = "aBcDeFg_123"


def verdict_block():
    """Run the worker's actual four statements without browser/upload setup."""
    tree = ast.parse(SOURCE.read_text())
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for index, statement in enumerate(body):
            if (isinstance(statement, ast.Assign)
                    and len(statement.targets) == 1
                    and isinstance(statement.targets[0], ast.Name)
                    and statement.targets[0].id == "success"
                    and isinstance(statement.value, ast.Compare)
                    and isinstance(statement.value.left, ast.Name)
                    and statement.value.left.id == "actual_visibility"):
                return compile(ast.Module(body=body[index:index + 4], type_ignores=[]), str(SOURCE), "exec")
    raise AssertionError("Worker's visibility verdict block was not found")


class ProcessingVisibilityTest(unittest.TestCase):
    def setUp(self):
        worker._reset_upload_phase()
        with redirect_stdout(io.StringIO()):
            worker._mark_known_video_id(VIDEO_ID)
        self.addCleanup(worker._reset_upload_phase)
        self.row = {"videoId": VIDEO_ID, "title": "Exact title",
                    "draftStatus": "DRAFT_STATUS_NONE", "privacy": "VIDEO_PRIVACY_PUBLIC",
                    "userSetVisibility": "VIDEO_USER_SET_VISIBILITY_PUBLIC",
                    "effectiveStatus": "VIDEO_VISIBILITY_STATUS_UPLOADING_OR_PROCESSING"}

    def run_verdict(self, row, visibility, *, schedule_date=None, actual="PROCESSING"):
        scope = dict(vars(worker), _row=row, VISIBILITY=visibility, schedule_date=schedule_date,
                     actual_visibility=actual,
                     _want_states={"SCHEDULED"} if schedule_date is not None else {visibility.upper()},
                     video_url=f"https://youtu.be/{VIDEO_ID}")
        # Capture the actual logger; neither the gate nor the contract is replaced.
        with self.assertLogs(worker.logger, level="INFO"):
            exec(verdict_block(), scope)
        raw = {"success": scope["success"], "visibility": scope["actual_visibility"],
               "error": scope["error"], "url": scope["video_url"], "video_id": VIDEO_ID}
        result = worker.upload_contract.build_result(raw, worker._current_upload_phase())
        return worker.upload_contract.validate_result(result)

    def test_exact_public_processing_retains_existing_pending_success(self):
        self.assertEqual(worker.YS.classify(self.row), "PROCESSING")
        result = self.run_verdict(self.row, "PUBLIC")
        self.assertTrue(result["success"])
        self.assertEqual(result["visibility"], "PUBLIC_PENDING")
        self.assertEqual(result["error"], "")
        self.assertTrue(result["do_not_reupload"])

    def test_private_unlisted_and_unknown_are_not_public_success(self):
        for requested in ("PRIVATE", "UNLISTED", "UNKNOWN", "unknown"):
            with self.subTest(requested=requested):
                row = dict(self.row, privacy="VIDEO_PRIVACY_" + requested.upper(),
                           userSetVisibility="VIDEO_USER_SET_VISIBILITY_" + requested.upper())
                result = self.run_verdict(row, requested)
                self.assertFalse(result["success"])
                self.assertEqual(result["visibility"], "PROCESSING")
                self.assertIn(requested.upper(), result["error"])
                self.assertEqual(result["video_id"], VIDEO_ID)
                self.assertTrue(result["do_not_reupload"])

    def test_scheduled_intent_cannot_inherit_public_pending(self):
        result = self.run_verdict(self.row, "PUBLIC", schedule_date=datetime(2030, 1, 1, tzinfo=timezone.utc))
        self.assertFalse(result["success"])
        self.assertEqual(result["visibility"], "PROCESSING")
        self.assertIn("SCHEDULED", result["error"])
        self.assertTrue(result["do_not_reupload"])

    def test_public_request_rejects_mismatched_or_unknown_row_evidence(self):
        changes = ({"privacy": "VIDEO_PRIVACY_PRIVATE"},
                   {"userSetVisibility": "VIDEO_USER_SET_VISIBILITY_UNLISTED"},
                   {"userSetVisibility": "VIDEO_USER_SET_VISIBILITY_SCHEDULED"},
                   {"privacy": ""}, {"userSetVisibility": None},
                   {"draftStatus": "DRAFT_STATUS_PUBLIC"}, {"draftStatus": ""},
                   {"effectiveStatus": "VIDEO_VISIBILITY_STATUS_DRAFT"},
                   {"videoId": "zYxWvUt_987"}, {"videoId": ""})
        for changed in changes:
            with self.subTest(changed=changed):
                result = self.run_verdict(dict(self.row, **changed), "PUBLIC")
                self.assertFalse(result["success"])
                self.assertEqual(result["visibility"], "PROCESSING")
                self.assertEqual(result["video_id"], VIDEO_ID)
                self.assertTrue(result["do_not_reupload"])

    def test_private_request_cannot_accept_an_observed_public_intent(self):
        result = self.run_verdict(self.row, "PRIVATE")
        self.assertFalse(result["success"])
        self.assertEqual(result["visibility"], "PROCESSING")
        self.assertIn("PRIVATE", result["error"])

    def test_missing_row_unknown_id_and_lowercase_intent_fail_closed(self):
        for row, video_id, visibility in ((None, VIDEO_ID, "PUBLIC"),
                                           (self.row, "", "PUBLIC"),
                                           (self.row, None, "PUBLIC"),
                                           (self.row, 123, "PUBLIC"),
                                           (self.row, VIDEO_ID, "public")):
            with self.subTest(video_id=video_id, visibility=visibility):
                self.assertFalse(worker._processing_public_pending_allowed(
                    row, visibility=visibility, schedule_date=None, video_id=video_id))


if __name__ == "__main__":
    unittest.main()
