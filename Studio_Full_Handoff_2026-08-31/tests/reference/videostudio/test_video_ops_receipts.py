import concurrent.futures
import contextlib
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RECEIPT_PATH = ROOT / "tools" / "video_ops_watchdog" / "receipt_io.py"
WATCHDOG_PATH = ROOT / "tools" / "video_ops_watchdog" / "watchdog.py"
UPLOAD_WORKER_PATH = ROOT / "src" / "upload_worker.py"
UPLOAD_MANAGER_PATH = ROOT / "src" / "upload_manager.py"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


receipt_io = load_module("video_ops_receipt_io", RECEIPT_PATH)
watchdog = load_module("video_ops_watchdog_receipt_tests", WATCHDOG_PATH)
upload_worker = load_module("video_ops_upload_worker_receipt_tests", UPLOAD_WORKER_PATH)
upload_manager = load_module("video_ops_upload_manager_receipt_tests", UPLOAD_MANAGER_PATH)

UC = "UC1234567890123456789012"
VIDEO_ID = "aBcDeFg_123"
MANIFEST_SHA = "a" * 64
MANIFEST_NAME = "wave20-upload-manifest.json"
NOW = dt.datetime(2026, 8, 13, 2, 0, tzinfo=dt.timezone.utc)
TITLE_SHA = "b" * 64
EXPECTED_SCHEDULE = "2026-08-13T10:30:00+07:00"


def exact_evidence(*, uc=UC, video_id=VIDEO_ID, scheduled_epoch=1786591800):
    return {
        "verified_channel_uc": uc,
        "row_video_id": video_id,
        "studio_state": "SCHEDULED",
        "visibility_user_set_visibility": "VIDEO_USER_SET_VISIBILITY_SCHEDULED",
        "scheduled_time_seconds": scheduled_epoch,
        "normalized_title_sha256": TITLE_SHA,
    }


def make_receipt(**overrides):
    evidence = overrides.pop("evidence", exact_evidence())
    return receipt_io.build_scheduled_receipt(
        evidence=evidence,
        manifest_sha256=overrides.pop("manifest_sha256", MANIFEST_SHA),
        manifest_name=overrides.pop("manifest_name", MANIFEST_NAME),
        expected_operational_date=overrides.pop("expected_operational_date", "2026-08-13"),
        source_host=overrides.pop("source_host", "minsk"),
        expected_scheduled_at=overrides.pop("expected_scheduled_at", EXPECTED_SCHEDULE),
        verified_at=overrides.pop("verified_at", NOW),
        **overrides,
    )


def structured_scheduled_result(*, success=True, error=""):
    exact = exact_evidence()
    phase = upload_worker.upload_contract.advance_phase_event(
        upload_worker.upload_contract.initial_phase_event(),
        "video_id_known",
        VIDEO_ID,
    )
    return upload_worker.upload_contract.build_result(
        {
            "success": success,
            "url": f"https://youtu.be/{VIDEO_ID}",
            "error": error,
            "visibility": "SCHEDULED",
            "video_id": VIDEO_ID,
            "do_not_reupload": True,
            "studio_row_evidence": {
                **exact,
                "observed_normalized_title_sha256": TITLE_SHA,
                "title_matches": True,
            },
            "exact_studio_evidence": exact if success else None,
        },
        phase,
    )


class ReceiptIOTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def policy(self, **overrides):
        value = {
            "enabled_channels": {UC: 1},
            "timezone": "Asia/Ho_Chi_Minh",
            "publication_day_start": "23:00",
            "allowed_source_hosts": {"mac", "minsk"},
            "max_age_hours": 72.0,
            "future_skew_minutes": 5.0,
        }
        value.update(overrides)
        return value

    def test_canonical_receipt_is_atomic_and_idempotent(self):
        receipt = make_receipt()
        self.assertEqual(
            receipt["receipt_sha256"], receipt_io.receipt_sha256(receipt)
        )

        first = receipt_io.write_outbox(receipt, self.root / "outbox")
        second = receipt_io.write_outbox(receipt, self.root / "outbox")

        self.assertEqual(first, second)
        self.assertEqual(json.loads(first.read_text(encoding="utf-8")), receipt)
        self.assertFalse(list((self.root / "outbox").glob("*.tmp")))

    def test_tamper_is_rejected_even_when_filename_is_unchanged(self):
        receipt = make_receipt()
        receipt["source_host"] = "mac"

        with self.assertRaisesRegex(receipt_io.ReceiptError, "sha256_mismatch"):
            receipt_io.validate_receipt(receipt, now=NOW, **self.policy())

    def test_schema_version_boolean_is_rejected(self):
        receipt = make_receipt()
        receipt["schema_version"] = True
        receipt["receipt_sha256"] = receipt_io.receipt_sha256(receipt)

        with self.assertRaisesRegex(receipt_io.ReceiptError, "schema_version_unsupported"):
            receipt_io.validate_receipt(receipt, now=NOW, **self.policy())

    def test_receipt_v2_binds_aware_instant_to_exact_epoch(self):
        receipt = make_receipt()
        self.assertEqual(receipt["schema_version"], 2)
        self.assertEqual(receipt["scheduled_at_utc"], "2026-08-13T03:30:00Z")
        receipt["scheduled_at_utc"] = "2026-08-13T03:30:01Z"
        receipt["receipt_sha256"] = receipt_io.receipt_sha256(receipt)
        with self.assertRaisesRegex(receipt_io.ReceiptError, "scheduled_at_utc_invalid"):
            receipt_io.validate_receipt_structure(receipt)

    def test_receipt_v2_rejects_uc_video_or_state_detached_from_evidence(self):
        mutations = (
            ("uc", "UCabcdefghijklmnopqrstuv", "studio_evidence_invalid"),
            ("video_id", "zYxWvUt_987", "studio_evidence_invalid"),
            ("studio_state", "PUBLIC", "studio_state_invalid"),
        )
        for field, value, error in mutations:
            with self.subTest(field=field):
                receipt = make_receipt()
                receipt[field] = value
                receipt["receipt_sha256"] = receipt_io.receipt_sha256(receipt)
                with self.assertRaisesRegex(receipt_io.ReceiptError, error):
                    receipt_io.validate_receipt_structure(receipt)

    def test_disabled_channel_and_stale_receipt_are_rejected(self):
        receipt = make_receipt(verified_at=NOW - dt.timedelta(hours=73))
        with self.assertRaisesRegex(receipt_io.ReceiptError, "stale"):
            receipt_io.validate_receipt(receipt, now=NOW, **self.policy())

        current = make_receipt()
        with self.assertRaisesRegex(receipt_io.ReceiptError, "channel_not_enabled"):
            receipt_io.validate_receipt(
                current,
                now=NOW,
                **self.policy(enabled_channels={}),
            )

    def test_exact_studio_evidence_is_required(self):
        evidence = exact_evidence()
        evidence["visibility_user_set_visibility"] = "VIDEO_USER_SET_VISIBILITY_PRIVATE"
        with self.assertRaisesRegex(receipt_io.ReceiptError, "studio_evidence"):
            make_receipt(evidence=evidence)

    def test_exact_title_proof_normalizes_nfc_whitespace_and_case_only(self):
        equal = upload_worker._exact_title_proof(
            "  Tajemství   Třetí říše ",
            "TAJEMSTVI\u0301 TŘETÍ ŘÍŠE",
        )
        mismatch = upload_worker._exact_title_proof(
            "Tajemství Třetí říše",
            "Tajemství Třetí říše — jiný díl",
        )

        self.assertTrue(equal["matches"])
        self.assertEqual(len(equal["normalized_title_sha256"]), 64)
        self.assertFalse(mismatch["matches"])

    def test_existing_scheduled_row_can_recover_receipt_without_reupload(self):
        row = {
            "videoId": VIDEO_ID,
            "title": "Tajemství Třetí říše",
            "draftStatus": "DRAFT_STATUS_NONE",
            "privacy": "VIDEO_PRIVACY_PRIVATE",
            "userSetVisibility": "VIDEO_USER_SET_VISIBILITY_SCHEDULED",
            "scheduledTime": 1786591800,
            "alteredDisclosed": True,
        }
        with self.assertLogs(upload_worker.logger, level="INFO") as captured:
            recovered = upload_worker._verified_existing_scheduled_result(
                UC,
                row,
                "  tajemství  třetí říše ",
                1786591800,
                studio_state="SCHEDULED",
                schedule_mode="absolute",
                picker_local=dt.datetime(2026, 8, 13, 10, 30),
                browser_timezone="Asia/Saigon",
            )
        mismatch = upload_worker._verified_existing_scheduled_result(
            UC,
            row,
            "Jiný titul",
            1786591800,
            studio_state="SCHEDULED",
            schedule_mode="absolute",
            picker_local=dt.datetime(2026, 8, 13, 10, 30),
            browser_timezone="Asia/Saigon",
        )
        altered_missing = upload_worker._verified_existing_scheduled_result(
            UC,
            {**row, "alteredDisclosed": False},
            "Tajemství Třetí říše",
            1786591800,
            studio_state="SCHEDULED",
            schedule_mode="absolute",
            picker_local=dt.datetime(2026, 8, 13, 10, 30),
            browser_timezone="Asia/Saigon",
        )

        self.assertTrue(recovered["success"])
        self.assertTrue(recovered["skipped"])
        self.assertTrue(recovered["do_not_reupload"])
        self.assertIsNotNone(recovered["exact_studio_evidence"])
        self.assertTrue(recovered["altered_disclosed"])
        joined = "\n".join(captured.output)
        self.assertIn("Состояние строки:", joined)
        self.assertIn("Отложка подтверждена машинно:", joined)
        self.assertIn("AI-дисклоуз подтверждён состоянием строки", joined)
        self.assertFalse(mismatch["success"])
        self.assertTrue(mismatch["do_not_reupload"])
        self.assertIn("do not reupload", mismatch["error"])
        self.assertFalse(altered_missing["success"])
        self.assertTrue(altered_missing["do_not_reupload"])
        self.assertIn("altered_content_unverified", altered_missing["error"])

    def test_worker_cli_emits_one_stable_result_on_pre_upload_terminal_paths(self):
        common = [
            sys.executable,
            str(UPLOAD_WORKER_PATH),
            "--video", str(self.root / "missing.mp4"),
            "--title", "Test",
            "--profile", "1",
            "--channel-url", f"https://www.youtube.com/channel/{UC}",
        ]
        commands = [
            common,
            common + ["--schedule", "not-an-iso-date"],
            common + [
                "--receipt-outbox", str(self.root / "outbox-preflight"),
                "--receipt-manifest-sha256", "bad",
                "--receipt-source-host", "minsk",
            ],
            common + ["--receipt-ssh-timeout", "10"],
            [sys.executable, str(UPLOAD_WORKER_PATH)],
        ]
        for command in commands:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                check=False,
            )
            markers = [
                line for line in completed.stdout.splitlines()
                if line.startswith("__UPLOAD_RESULT__|")
            ]
            self.assertEqual(len(markers), 1, completed.stdout)
            payload = json.loads(markers[0].split("|", 1)[1])
            self.assertEqual(payload["upload_result_schema_version"], 2)
            self.assertEqual(payload["phase"], "preflight")
            self.assertEqual(payload["success"], False)
            self.assertEqual(payload["video_id"], "")
            self.assertIsNone(payload["exact_studio_evidence"])

    def test_upload_manager_preserves_structured_exact_result(self):
        structured = structured_scheduled_result()

        class FakeProc:
            def __init__(self, lines):
                self.stdout = iter(lines)

            def wait(self, timeout=None):
                return 0

            def kill(self):
                return None

        lines = [
            "__UPLOAD_RESULT__|" + json.dumps(structured) + "\n",
            f"__UPLOAD_SUCCESS__|https://youtu.be/{VIDEO_ID}\n",
        ]
        with (
            mock.patch.object(upload_manager, "_worker_path", return_value=str(UPLOAD_WORKER_PATH)),
            mock.patch.object(upload_manager.subprocess, "Popen", return_value=FakeProc(lines)),
            mock.patch.object(upload_manager, "save_project_target"),
            mock.patch.object(upload_manager.UploadLog, "record"),
        ):
            result = upload_manager.run_upload(
                self.root,
                self.root / "video.mp4",
                "Title",
                "Description",
                {"channel_url": f"https://youtube.com/channel/{UC}", "profile_id": "1"},
                dedup=False,
                one_per_day=False,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["video_id"], VIDEO_ID)
        self.assertEqual(result["exact_studio_evidence"], exact_evidence())

    def test_upload_manager_rejects_truthy_malformed_result_without_saving(self):
        malicious = {
            "upload_result_schema_version": True,
            "success": "false",
            "url": f"https://youtu.be/{VIDEO_ID}",
            "error": 123,
            "visibility": [],
            "video_id": "NOTVALID",
            "do_not_reupload": "no",
            "studio_row_evidence": None,
            "exact_studio_evidence": None,
        }

        class FakeProc:
            stdout = iter(
                [
                    "__UPLOAD_RESULT__|" + json.dumps(malicious) + "\n",
                    f"__UPLOAD_SUCCESS__|https://youtu.be/{VIDEO_ID}\n",
                ]
            )

            def wait(self, timeout=None):
                return 0

            def kill(self):
                return None

        with (
            mock.patch.object(upload_manager, "_worker_path", return_value=str(UPLOAD_WORKER_PATH)),
            mock.patch.object(upload_manager.subprocess, "Popen", return_value=FakeProc()),
            mock.patch.object(upload_manager, "save_project_target") as save_target,
        ):
            result = upload_manager.run_upload(
                self.root,
                self.root / "video.mp4",
                "Title",
                "Description",
                {"channel_url": f"https://youtube.com/channel/{UC}", "profile_id": "1"},
                dedup=False,
                one_per_day=False,
            )

        self.assertFalse(result["success"])
        self.assertFalse(result["do_not_reupload"])
        save_target.assert_not_called()

    def test_upload_manager_receipt_context_is_all_or_none_and_propagated(self):
        invalid = {
            "manifest_sha256": MANIFEST_SHA,
            "manifest_name": MANIFEST_NAME,
        }
        with mock.patch.object(upload_manager.subprocess, "Popen") as popen:
            result = upload_manager.run_upload(
                self.root,
                self.root / "video.mp4",
                "Title",
                "Description",
                {"channel_url": f"https://youtube.com/channel/{UC}", "profile_id": "1"},
                schedule_iso="2026-08-13T10:30:00+07:00",
                receipt_context=invalid,
            )
        self.assertFalse(result["success"])
        popen.assert_not_called()

        for bad_context in (
            {
                "manifest_sha256": MANIFEST_SHA,
                "manifest_name": MANIFEST_NAME,
                "operational_date": "2026-08-13",
                "source_host": "minsk",
                "outbox_dir": str(self.root / "outbox"),
                "push": {"enabled": True, "ssh_target": 123, "timeout_seconds": 17},
            },
            {
                "manifest_sha256": MANIFEST_SHA,
                "manifest_name": MANIFEST_NAME,
                "operational_date": "2026-08-13",
                "source_host": "minsk",
                "outbox_dir": str(self.root / "outbox"),
                "push": {"enabled": False},
                "receipt_io_path": False,
            },
        ):
            with mock.patch.object(upload_manager.subprocess, "Popen") as bad_popen:
                rejected = upload_manager.run_upload(
                    self.root,
                    self.root / "video.mp4",
                    "Title",
                    "Description",
                    {"channel_url": f"https://youtube.com/channel/{UC}", "profile_id": "1"},
                    schedule_iso="2026-08-13T10:30:00+07:00",
                    receipt_context=bad_context,
                )
            self.assertFalse(rejected["success"])
            bad_popen.assert_not_called()

        structured = structured_scheduled_result()

        class FakeProc:
            stdout = iter(
                [
                    "__UPLOAD_RESULT__|" + json.dumps(structured) + "\n",
                    f"__UPLOAD_SUCCESS__|https://youtu.be/{VIDEO_ID}\n",
                ]
            )

            def wait(self, timeout=None):
                return 0

            def kill(self):
                return None

        context = {
            "schema_version": 2,
            "manifest_sha256": MANIFEST_SHA,
            "manifest_name": MANIFEST_NAME,
            "operational_date": "2026-08-13",
            "expected_scheduled_at": EXPECTED_SCHEDULE,
            "source_host": "minsk",
            "outbox_dir": str(self.root / "outbox"),
            "push": {
                "enabled": True,
                "ssh_target": "receipt-vps",
                "timeout_seconds": 17,
            },
            "receipt_io_path": str(RECEIPT_PATH),
            "existing_upload_policy": "reconcile_exact",
        }
        captured = {}

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs["env"]
            return FakeProc()

        with (
            mock.patch.object(upload_manager, "_worker_path", return_value=str(UPLOAD_WORKER_PATH)),
            mock.patch.object(upload_manager.subprocess, "Popen", side_effect=fake_popen),
            mock.patch.object(
                upload_manager,
                "load_project_target",
                return_value={"uploaded_url": f"https://youtu.be/{VIDEO_ID}"},
            ),
            mock.patch.object(
                upload_manager.UploadLog,
                "uploaded_today",
                return_value={
                    "project": self.root.name,
                    "url": f"https://youtu.be/{VIDEO_ID}",
                },
            ),
            mock.patch.object(upload_manager, "save_project_target"),
            mock.patch.object(upload_manager.UploadLog, "record"),
            mock.patch.object(
                upload_manager.upload_coordination,
                "profile_lease",
                return_value=contextlib.nullcontext(None),
            ),
        ):
            result = upload_manager.run_upload(
                self.root,
                self.root / "video.mp4",
                "Title",
                "Description",
                {"channel_url": f"https://youtube.com/channel/{UC}", "profile_id": "1"},
                schedule_iso="2026-08-13T10:30:00+07:00",
                receipt_context=context,
                profile_lease_context={
                    "schema_version": 1,
                    "coordinator_command": ["coordinator"],
                    "holder": "test",
                    "ttl_seconds": 120,
                    "renew_interval_seconds": 10,
                    "command_timeout_seconds": 5,
                },
                profile_guard=lambda: None,
                profile_inactive_guard=lambda: None,
            )
        self.assertTrue(result["success"])
        self.assertIn("--receipt-manifest-name", captured["argv"])
        self.assertIn("--reconcile-video-id", captured["argv"])
        self.assertEqual(captured["env"]["VIDEO_OPS_RECEIPT_MANIFEST_SHA256"], MANIFEST_SHA)
        self.assertEqual(captured["env"]["VIDEO_OPS_RECEIPT_SSH_TARGET"], "receipt-vps")

    def test_upload_manager_known_result_survives_missing_legacy_and_stream_error(self):
        structured = structured_scheduled_result(success=False, error="known failure")

        class MissingLegacyProc:
            stdout = iter(["__UPLOAD_RESULT__|" + json.dumps(structured) + "\n"])

            def wait(self, timeout=None):
                return 1

            def kill(self):
                return None

        with (
            mock.patch.object(upload_manager, "_worker_path", return_value=str(UPLOAD_WORKER_PATH)),
            mock.patch.object(upload_manager.subprocess, "Popen", return_value=MissingLegacyProc()),
        ):
            missing = upload_manager.run_upload(
                self.root, self.root / "video.mp4", "Title", "Description",
                {"channel_url": f"https://youtube.com/channel/{UC}", "profile_id": "1"},
                dedup=False, one_per_day=False,
            )
        self.assertFalse(missing["success"])
        self.assertEqual(missing["video_id"], VIDEO_ID)
        self.assertTrue(missing["do_not_reupload"])

        class RaisingStream:
            def __init__(self):
                self.first = True

            def __iter__(self):
                return self

            def __next__(self):
                if self.first:
                    self.first = False
                    return "__UPLOAD_RESULT__|" + json.dumps(structured) + "\n"
                raise OSError("pipe broke")

        class RaisingProc(MissingLegacyProc):
            stdout = RaisingStream()

        with (
            mock.patch.object(upload_manager, "_worker_path", return_value=str(UPLOAD_WORKER_PATH)),
            mock.patch.object(upload_manager.subprocess, "Popen", return_value=RaisingProc()),
        ):
            interrupted = upload_manager.run_upload(
                self.root, self.root / "video.mp4", "Title", "Description",
                {"channel_url": f"https://youtube.com/channel/{UC}", "profile_id": "1"},
                dedup=False, one_per_day=False,
            )
        self.assertEqual(interrupted["video_id"], VIDEO_ID)
        self.assertTrue(interrupted["do_not_reupload"])

    def test_upload_manager_crash_retry_safety_comes_from_actual_phase(self):
        class NoResultProc:
            stdout = iter([])

            def wait(self, timeout=None):
                return 1

            def kill(self):
                return None

        with (
            mock.patch.object(upload_manager, "_worker_path", return_value=str(UPLOAD_WORKER_PATH)),
            mock.patch.object(upload_manager.subprocess, "Popen", return_value=NoResultProc()),
            mock.patch.object(upload_manager, "save_project_target") as save_target,
        ):
            crashed = upload_manager.run_upload(
                self.root, self.root / "video.mp4", "Title", "Description",
                {"channel_url": f"https://youtube.com/channel/{UC}", "profile_id": "1"},
                dedup=False, one_per_day=False,
            )
        self.assertFalse(crashed["success"])
        self.assertFalse(crashed["do_not_reupload"])
        save_target.assert_not_called()

        stopped = threading.Event()

        class WaitForKillStream:
            def __iter__(self):
                return self

            def __next__(self):
                stopped.wait(timeout=2)
                raise StopIteration

        class TimedOutProc:
            stdout = WaitForKillStream()

            def wait(self, timeout=None):
                if timeout is not None:
                    raise subprocess.TimeoutExpired("upload_worker", timeout)
                return 1

            def kill(self):
                stopped.set()

        with (
            mock.patch.object(upload_manager, "_worker_path", return_value=str(UPLOAD_WORKER_PATH)),
            mock.patch.object(upload_manager.subprocess, "Popen", return_value=TimedOutProc()),
            mock.patch.object(upload_manager, "save_project_target") as save_timeout,
        ):
            timed_out = upload_manager.run_upload(
                self.root, self.root / "video.mp4", "Title", "Description",
                {"channel_url": f"https://youtube.com/channel/{UC}", "profile_id": "1"},
                dedup=False, one_per_day=False,
            )
        self.assertFalse(timed_out["success"])
        self.assertFalse(timed_out["do_not_reupload"])
        save_timeout.assert_not_called()

        with (
            mock.patch.object(upload_manager, "_worker_path", return_value=str(UPLOAD_WORKER_PATH)),
            mock.patch.object(upload_manager.subprocess, "Popen", side_effect=OSError("no fork")),
        ):
            not_started = upload_manager.run_upload(
                self.root, self.root / "video.mp4", "Title", "Description",
                {"channel_url": f"https://youtube.com/channel/{UC}", "profile_id": "1"},
                dedup=False, one_per_day=False,
            )
        self.assertFalse(not_started["success"])
        self.assertFalse(not_started["do_not_reupload"])

        phase = upload_manager.upload_contract.encode_phase_event(
            upload_manager.upload_contract.advance_phase_event(
                upload_manager.upload_contract.initial_phase_event(),
                "upload_started",
            )
        )

        class CrossedBoundaryProc(NoResultProc):
            stdout = iter([phase + "\n"])

        with (
            mock.patch.object(upload_manager, "_worker_path", return_value=str(UPLOAD_WORKER_PATH)),
            mock.patch.object(
                upload_manager.subprocess, "Popen", return_value=CrossedBoundaryProc()
            ),
        ):
            crossed = upload_manager.run_upload(
                self.root, self.root / "video.mp4", "Title", "Description",
                {"channel_url": f"https://youtube.com/channel/{UC}", "profile_id": "1"},
                dedup=False, one_per_day=False,
            )
        self.assertFalse(crossed["success"])
        self.assertEqual(crossed["phase"], "upload_started")
        self.assertTrue(crossed["do_not_reupload"])

    def test_operational_date_comes_from_actual_epoch_and_day_boundary(self):
        timezone = "Asia/Ho_Chi_Minh"
        before = int(dt.datetime(2026, 8, 13, 15, 59, tzinfo=dt.timezone.utc).timestamp())
        at_start = int(dt.datetime(2026, 8, 13, 16, 0, tzinfo=dt.timezone.utc).timestamp())

        self.assertEqual(
            receipt_io.operational_date_for_epoch(before, timezone, "23:00"),
            dt.date(2026, 8, 13),
        )
        self.assertEqual(
            receipt_io.operational_date_for_epoch(at_start, timezone, "23:00"),
            dt.date(2026, 8, 14),
        )

    def test_conflict_and_semantic_idempotency(self):
        inbox = self.root / "inbox"
        inbox.mkdir()
        first = make_receipt()
        repeated = make_receipt(verified_at=NOW + dt.timedelta(minutes=1))
        other = make_receipt(
            evidence=exact_evidence(video_id="zYxWvUt_987"),
            verified_at=NOW + dt.timedelta(minutes=2),
        )
        for item in (first, repeated, other):
            receipt_io.store_inbox(item, inbox)

        scan = receipt_io.scan_inbox(inbox, now=NOW + dt.timedelta(minutes=3), **self.policy())

        self.assertEqual(scan["valid_files"], 3)
        self.assertEqual(scan["semantic_duplicates"], 1)
        self.assertEqual(len(scan["receipts"]), 2)
        self.assertEqual(len(scan["conflicts"]), 1)
        self.assertTrue(all(item["conflict"] for item in scan["receipts"]))

    def test_manifest_label_cannot_double_count_same_video(self):
        inbox = self.root / "inbox"
        inbox.mkdir()
        receipt_io.store_inbox(make_receipt(), inbox)
        receipt_io.store_inbox(
            make_receipt(manifest_name="renamed-manifest.json"), inbox
        )

        scan = receipt_io.scan_inbox(
            inbox,
            now=NOW,
            **self.policy(enabled_channels={UC: 2}),
        )

        self.assertEqual(scan["valid_files"], 2)
        self.assertEqual(scan["semantic_duplicates"], 1)
        self.assertEqual(len(scan["receipts"]), 1)

    def test_same_video_with_changed_exact_title_hash_is_conflict(self):
        inbox = self.root / "inbox"
        inbox.mkdir()
        first = make_receipt()
        changed = exact_evidence()
        changed["normalized_title_sha256"] = "c" * 64
        second = make_receipt(evidence=changed, verified_at=NOW + dt.timedelta(minutes=1))
        receipt_io.store_inbox(first, inbox)
        receipt_io.store_inbox(second, inbox)

        scan = receipt_io.scan_inbox(
            inbox, now=NOW + dt.timedelta(minutes=2), **self.policy()
        )

        self.assertTrue(scan["conflicts"])
        self.assertTrue(all(item["conflict"] for item in scan["receipts"]))

    def test_concurrent_writers_leave_one_complete_file(self):
        receipt = make_receipt()
        outbox = self.root / "outbox"
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            paths = list(pool.map(lambda _: receipt_io.write_outbox(receipt, outbox), range(32)))

        self.assertEqual(len(set(paths)), 1)
        files = list(outbox.glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text(encoding="utf-8")), receipt)

    def test_failed_push_stays_in_retry_outbox(self):
        receipt = make_receipt()
        source = receipt_io.write_outbox(receipt, self.root / "outbox")
        failed = mock.Mock(returncode=255, stdout="", stderr="network down")
        with mock.patch.object(receipt_io.subprocess, "run", return_value=failed):
            result = receipt_io.push_outbox(self.root / "outbox", "receipt-vps")

        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertTrue(source.exists())
        self.assertFalse((self.root / "outbox" / "sent" / source.name).exists())

    def test_current_only_push_does_not_retry_backlog(self):
        outbox = self.root / "outbox"
        older = receipt_io.write_outbox(make_receipt(), outbox)
        current_receipt = make_receipt(
            evidence=exact_evidence(video_id="zYxWvUt_987"),
            verified_at=NOW + dt.timedelta(minutes=1),
        )
        current = receipt_io.write_outbox(current_receipt, outbox)
        ack = mock.Mock(
            returncode=0,
            stdout=f"OK {current_receipt['receipt_sha256']} stored\n",
            stderr="",
        )
        with mock.patch.object(receipt_io.subprocess, "run", return_value=ack) as runner:
            result = receipt_io.push_receipt(current, "receipt-vps")

        self.assertEqual(result["sent"], 1)
        self.assertTrue(older.exists())
        self.assertFalse(current.exists())
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(runner.call_args.args[0], ["ssh", "--", "receipt-vps"])

    def test_permanent_remote_reject_deadletters_but_infra_and_future_retry(self):
        outbox = self.root / "outbox"
        permanent = receipt_io.write_outbox(make_receipt(), outbox)
        nack = mock.Mock(
            returncode=2,
            stdout="",
            stderr="ERROR channel_not_enabled\n",
        )
        with mock.patch.object(receipt_io.subprocess, "run", return_value=nack):
            rejected = receipt_io.push_receipt(permanent, "receipt-vps")
        self.assertEqual(rejected["deadlettered"], 1)
        self.assertEqual(rejected["failed"], 0)
        self.assertFalse(permanent.exists())
        self.assertEqual(len(list((outbox / "deadletter").glob("*.json"))), 1)

        future_receipt = make_receipt(
            evidence=exact_evidence(video_id="zYxWvUt_987"),
            verified_at=NOW + dt.timedelta(minutes=1),
        )
        future = receipt_io.write_outbox(future_receipt, outbox)
        future_nack = mock.Mock(
            returncode=2,
            stdout="",
            stderr="ERROR verified_at_future\n",
        )
        with mock.patch.object(receipt_io.subprocess, "run", return_value=future_nack):
            transient = receipt_io.push_receipt(future, "receipt-vps")
        self.assertEqual(transient["failed"], 1)
        self.assertEqual(transient["deadlettered"], 0)
        self.assertTrue(future.exists())
        self.assertEqual(
            transient["errors"][0]["error"],
            "remote_transient:verified_at_future",
        )

        infra_receipt = make_receipt(
            evidence=exact_evidence(video_id="pQrStUv_456"),
            verified_at=NOW + dt.timedelta(minutes=2),
        )
        infra = receipt_io.write_outbox(infra_receipt, outbox)
        infra_nack = mock.Mock(
            returncode=2,
            stdout="",
            stderr="ERROR inbox_missing\n",
        )
        with mock.patch.object(receipt_io.subprocess, "run", return_value=infra_nack):
            infrastructure_failure = receipt_io.push_receipt(infra, "receipt-vps")
        self.assertEqual(infrastructure_failure["failed"], 1)
        self.assertEqual(infrastructure_failure["deadlettered"], 0)
        self.assertTrue(infra.exists())
        self.assertEqual(
            infrastructure_failure["errors"][0]["error"],
            "remote_transient:inbox_missing",
        )

    def test_push_rejects_option_like_ssh_target(self):
        source = receipt_io.write_outbox(make_receipt(), self.root / "outbox")
        with (
            self.assertRaisesRegex(receipt_io.ReceiptError, "ssh_target_invalid"),
            mock.patch.object(receipt_io.subprocess, "run") as runner,
        ):
            receipt_io.push_receipt(source, "-oProxyCommand=bad")
        runner.assert_not_called()

    def test_worker_delivery_failure_keeps_upload_success_and_local_receipt(self):
        outbox = self.root / "worker-outbox"
        result = {
            "success": True,
            "visibility": "SCHEDULED",
            "video_id": VIDEO_ID,
            "exact_studio_evidence": exact_evidence(),
        }
        args = SimpleNamespace(
            receipt_manifest_sha256=MANIFEST_SHA,
            receipt_manifest_name=MANIFEST_NAME,
            receipt_operational_date="2026-08-13",
            receipt_expected_scheduled_at=EXPECTED_SCHEDULE,
            receipt_source_host="minsk",
            receipt_outbox=str(outbox),
            receipt_ssh_target="receipt-vps",
            receipt_ssh_timeout=1,
        )
        with (
            mock.patch.object(
                receipt_io,
                "push_receipt",
                return_value={"pending": 1, "sent": 0, "failed": 1, "errors": []},
            ),
            mock.patch("builtins.print"),
            mock.patch.object(upload_worker.logger, "error"),
        ):
            upload_worker._persist_and_push_receipt(result, args, receipt_io)

        self.assertTrue(result["success"])
        self.assertEqual(len(list(outbox.glob("*.json"))), 1)

    def test_successful_push_moves_only_after_exact_ack(self):
        receipt = make_receipt()
        source = receipt_io.write_outbox(receipt, self.root / "outbox")
        ack = mock.Mock(
            returncode=0,
            stdout=f"OK {receipt['receipt_sha256']} stored\n",
            stderr="",
        )
        with mock.patch.object(receipt_io.subprocess, "run", return_value=ack):
            result = receipt_io.push_outbox(self.root / "outbox", "receipt-vps")

        self.assertEqual(result["sent"], 1)
        self.assertFalse(source.exists())
        self.assertTrue((self.root / "outbox" / "sent" / source.name).exists())

    def test_non_exact_ack_does_not_consume_outbox(self):
        receipt = make_receipt()
        source = receipt_io.write_outbox(receipt, self.root / "outbox")
        bad_ack = mock.Mock(
            returncode=0,
            stdout=f"OK {receipt['receipt_sha256']} stored trailing-data\n",
            stderr="",
        )
        with mock.patch.object(receipt_io.subprocess, "run", return_value=bad_ack):
            result = receipt_io.push_outbox(self.root / "outbox", "receipt-vps")

        self.assertEqual(result["failed"], 1)
        self.assertTrue(source.exists())

    def test_forced_key_source_identity_and_ingest_conflict(self):
        first = make_receipt()
        (self.root / "inbox").mkdir()
        with self.assertRaisesRegex(receipt_io.ReceiptError, "source_host_key_mismatch"):
            receipt_io.ingest_bytes(
                json.dumps(first).encode(),
                self.root / "inbox",
                policy=self.policy(),
                expected_source_host="mac",
                now=NOW,
            )

        _, disposition = receipt_io.ingest_bytes(
            json.dumps(first).encode(),
            self.root / "inbox",
            policy=self.policy(),
            expected_source_host="minsk",
            now=NOW,
        )
        self.assertEqual(disposition, "stored")

        other = make_receipt(
            evidence=exact_evidence(video_id="zYxWvUt_987"),
            verified_at=NOW + dt.timedelta(minutes=1),
        )
        _, disposition = receipt_io.ingest_bytes(
            json.dumps(other).encode(),
            self.root / "inbox",
            policy=self.policy(),
            expected_source_host="minsk",
            now=NOW + dt.timedelta(minutes=1),
        )
        self.assertEqual(disposition, "conflict")

    def test_ingest_does_not_auto_create_missing_inbox(self):
        missing = self.root / "missing-inbox"
        with self.assertRaisesRegex(receipt_io.ReceiptError, "inbox_missing"):
            receipt_io.ingest_bytes(
                json.dumps(make_receipt()).encode(),
                missing,
                policy=self.policy(),
                expected_source_host="minsk",
                now=NOW,
            )
        self.assertFalse(missing.exists())


class WatchdogReceiptIntegrationTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.state_db = self.root / "state" / "watchdog.db"
        self.inbox = self.root / "receipts" / "inbox"
        self.inbox.mkdir(parents=True)
        self.vsgen_db = self.root / "vsgen.db"
        conn = sqlite3.connect(self.vsgen_db)
        conn.execute(
            "CREATE TABLE queue_tasks(job_id INTEGER PRIMARY KEY, kind TEXT, status TEXT, created REAL, started REAL, claimed REAL)"
        )
        conn.commit()
        conn.close()
        self.telegram = self.root / "telegram.json"
        self.telegram.write_text(
            json.dumps({"telegram": {"enabled": False}}), encoding="utf-8"
        )
        self.config = self.root / "watchdog.json"
        self.config.write_text(
            json.dumps(
                {
                    "timezone": "Asia/Ho_Chi_Minh",
                    "publication_day_start": "23:00",
                    "telegram_config": str(self.telegram),
                    "channels": [
                        {
                            "uc": UC,
                            "name": "Receipt test",
                            "niche": "ww2",
                            "lang": "en",
                            "days": list(range(7)),
                            "expected_publications": 1,
                            "deadline": "10:30",
                            "enabled": True,
                        }
                    ],
                    "notifications": {
                        "morning": "10:00",
                        "reminder": "18:00",
                        "final": "22:30",
                        "slot_window_minutes": 30,
                    },
                    "infrastructure": {
                        "health_url": "",
                        "vsgen_db": str(self.vsgen_db),
                        "disk_path": str(self.root),
                        "min_free_gb": 0,
                        "queue_stale_hours": 4,
                        "ytcortiez_service": "",
                    },
                    "receipts": {
                        "enabled": True,
                        "inbox_dir": str(self.inbox),
                        "max_age_hours": 72,
                        "future_skew_minutes": 5,
                        "allowed_source_hosts": ["mac", "minsk"],
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def empty_feed(_url, _timeout):
        return (
            b"<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom' "
            b"xmlns:yt='http://www.youtube.com/xml/schemas/2015'></feed>"
        )

    def _run(self, *, now=NOW, force_summary=True):
        with (
            mock.patch.object(watchdog, "fetch_bytes", side_effect=self.empty_feed),
            mock.patch.object(watchdog, "disk_free_gb", return_value=100.0),
        ):
            return watchdog.run_check(
                self.config,
                self.state_db,
                no_send=True,
                force_summary=force_summary,
                now=now,
            )

    def test_receipt_marks_upload_only_and_never_rss_publication_complete(self):
        receipt_io.store_inbox(make_receipt(), self.inbox)
        with (
            mock.patch.object(watchdog, "fetch_bytes", side_effect=self.empty_feed),
            mock.patch.object(watchdog, "disk_free_gb", return_value=100.0),
        ):
            report = watchdog.run_check(
                self.config,
                self.state_db,
                no_send=True,
                force_summary=True,
                now=NOW,
            )

        channel = report["channels"][0]
        self.assertEqual(channel["upload_status"], "scheduled")
        self.assertEqual(channel["publication_status"], "missing")
        self.assertEqual(channel["status"], "missing")
        self.assertEqual(report["summary"]["fulfilled_publications"], 0)
        self.assertEqual(report["summary"]["upload_complete_channels"], 1)
        text = report["notifications"]["planned"][0]["text"]
        self.assertIn("Studio", text)
        self.assertIn("RSS", text)

    def test_rss_outage_keeps_scheduled_upload_evidence_separate(self):
        receipt_io.store_inbox(make_receipt(), self.inbox)
        channels = json.loads(self.config.read_text(encoding="utf-8"))
        base_channel = channels["channels"][0]
        channels["channels"] = [
            {**base_channel, "uc": f"UC{index:022d}", "name": f"Channel {index}"}
            for index in range(4)
        ]
        channels["channels"][0]["uc"] = UC
        self.config.write_text(json.dumps(channels), encoding="utf-8")

        with (
            mock.patch.object(watchdog, "fetch_bytes", side_effect=TimeoutError("RSS down")),
            mock.patch.object(watchdog, "disk_free_gb", return_value=100.0),
        ):
            report = watchdog.run_check(
                self.config, self.state_db, no_send=True, now=NOW
            )

        first = next(item for item in report["channels"] if item["uc"] == UC)
        self.assertEqual(first["upload_status"], "scheduled")
        self.assertEqual(first["publication_status"], "unknown")
        self.assertEqual(report["summary"]["complete_channels"], 0)
        self.assertEqual(report["summary"]["unknown_channels"], 4)

    def test_enabled_missing_and_non_directory_inbox_are_unhealthy_unknown(self):
        self.inbox.rmdir()
        report = self._run()
        self.assertFalse(report["upload_receipts"]["ok"])
        self.assertFalse(report["upload_receipts"]["scan_ok"])
        self.assertEqual(report["channels"][0]["upload_status"], "unknown")
        self.assertEqual(report["summary"]["upload_unknown_channels"], 1)
        self.assertIn(
            "Studio: проверка квитанций недоступна.",
            report["notifications"]["planned"][0]["text"],
        )
        self.assertIn(
            "проверка квитанций недоступна",
            watchdog._channel_observation_line(report["channels"][0]),
        )
        self.assertFalse(self.inbox.exists())

        self.inbox.write_text("not a directory", encoding="utf-8")
        second = self._run(now=NOW + dt.timedelta(minutes=1))
        self.assertEqual(
            second["upload_receipts"]["invalid"][0]["error"],
            "inbox_not_directory",
        )
        self.assertEqual(second["channels"][0]["upload_status"], "unknown")

    def test_empty_inbox_is_healthy_unconfirmed_and_disabled_is_not_configured(self):
        empty = self._run()
        self.assertTrue(empty["upload_receipts"]["ok"])
        self.assertTrue(empty["upload_receipts"]["scan_ok"])
        self.assertEqual(empty["channels"][0]["upload_status"], "unconfirmed")

        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["receipts"] = {"enabled": False}
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.inbox.rmdir()
        disabled = self._run(now=NOW + dt.timedelta(minutes=1))
        self.assertTrue(disabled["upload_receipts"]["ok"])
        self.assertTrue(disabled["upload_receipts"]["skipped"])
        self.assertEqual(disabled["channels"][0]["upload_status"], "not_configured")
        self.assertFalse(self.inbox.exists())

    def test_rotated_reject_stays_unhealthy_for_three_strikes_and_audited(self):
        invalid = self.inbox / "oversized.json"
        invalid.write_bytes(b"x" * (receipt_io.MAX_RECEIPT_BYTES + 1024))
        reports = [
            self._run(now=NOW + dt.timedelta(minutes=index), force_summary=False)
            for index in range(3)
        ]

        self.assertFalse(invalid.exists())
        self.assertEqual(reports[0]["upload_receipts"]["rotated_rejected"], 1)
        self.assertGreaterEqual(reports[1]["upload_receipts"]["unresolved_rejected_count"], 1)
        self.assertFalse(reports[2]["upload_receipts"]["ok"])
        failure = next(
            item for item in reports[2]["failures"]
            if item["key"] == "infra:receipt_inbox"
        )
        self.assertEqual(failure["streak"], 3)
        planned_keys = [item["key"] for item in reports[2]["notifications"]["planned"]]
        self.assertTrue(
            any(key.startswith("failure-alert:infra:receipt_inbox") for key in planned_keys)
        )
        connection = sqlite3.connect(self.state_db)
        try:
            audit = connection.execute(
                "SELECT status,reason FROM receipt_audit"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(audit, ("rejected", "receipt_too_large"))

    def test_receipt_numeric_bool_and_nan_config_are_rejected(self):
        for field, value in (("max_age_hours", True), ("future_skew_minutes", float("nan"))):
            config = json.loads(self.config.read_text(encoding="utf-8"))
            config["receipts"][field] = value
            self.config.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(watchdog.ConfigError):
                watchdog.load_config(self.config)
            config["receipts"][field] = 72 if field == "max_age_hours" else 5
            self.config.write_text(json.dumps(config), encoding="utf-8")




class LegacySchemaAndRejectedAgeTestCase(unittest.TestCase):
    """Инцидент 12–16.08.2026: подъём схемы квитанций до v2 (14.08 16:04 UTC) задним
    числом обнулил четыре квитанции v1 от 13.08 — по роликам, которые реально
    опубликовались. Они уехали в archive/rejected и держали infra:receipt_inbox
    красным 175 проверок подряд, заглушая собой обвал публикаций."""

    def _v1(self):
        receipt = make_receipt()
        body = {k: v for k, v in receipt.items()
                if k not in ("scheduled_at_utc", "receipt_sha256")}
        body["schema_version"] = receipt_io.LEGACY_SCHEMA_VERSION
        body["receipt_sha256"] = receipt_io.receipt_sha256(body)
        return body

    def test_legacy_v1_receipt_is_valid(self):
        receipt = self._v1()
        self.assertNotIn("scheduled_at_utc", receipt)
        receipt_io.validate_receipt_structure(receipt)

    def test_producer_still_writes_current_schema(self):
        self.assertEqual(make_receipt()["schema_version"], receipt_io.SCHEMA_VERSION)
        self.assertIn("scheduled_at_utc", make_receipt())

    def test_unknown_schema_version_names_the_version_not_the_fields(self):
        receipt = self._v1()
        receipt["schema_version"] = 99
        receipt["receipt_sha256"] = receipt_io.receipt_sha256(
            {k: v for k, v in receipt.items() if k != "receipt_sha256"}
        )
        with self.assertRaises(receipt_io.ReceiptError) as caught:
            receipt_io.validate_receipt_structure(receipt)
        self.assertEqual(caught.exception.code, "schema_version_unsupported")

    def test_v2_receipt_without_scheduled_at_is_still_rejected(self):
        """Совместимость распространяется ровно на v1, а не «на всё без поля»."""
        receipt = self._v1()
        receipt["schema_version"] = receipt_io.SCHEMA_VERSION
        receipt["receipt_sha256"] = receipt_io.receipt_sha256(
            {k: v for k, v in receipt.items() if k != "receipt_sha256"}
        )
        with self.assertRaises(receipt_io.ReceiptError) as caught:
            receipt_io.validate_receipt_structure(receipt)
        self.assertEqual(caught.exception.code, "receipt_fields_invalid")


class RejectedBacklogVersusHealthTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.inbox = root / "receipts" / "inbox"
        self.inbox.mkdir(parents=True)
        self.rejected = root / "receipts" / "archive" / "rejected"
        self.rejected.mkdir(parents=True)

    def tearDown(self):
        self.tempdir.cleanup()

    def add_rejected(self, name, *, age_hours):
        path = self.rejected / f"{name}.json"
        path.write_text("{}", encoding="utf-8")
        stamp = NOW.timestamp() - age_hours * 3600
        os.utime(path, (stamp, stamp))

    def scan(self):
        return receipt_io.scan_inbox(
            self.inbox,
            enabled_channels={UC},
            timezone="Asia/Ho_Chi_Minh",
            publication_day_start="23:00",
            allowed_source_hosts={"mac", "minsk"},
            max_age_hours=72,
            future_skew_minutes=5,
            now=NOW,
        )

    def test_old_rejection_stays_in_backlog_but_is_not_a_health_problem(self):
        self.add_rejected("old", age_hours=70)
        result = self.scan()
        self.assertEqual(result["unresolved_rejected_count"], 1)
        self.assertEqual(result["rejected_recent_count"], 0)

    def test_fresh_rejection_is_a_health_problem(self):
        self.add_rejected("fresh", age_hours=2)
        result = self.scan()
        self.assertEqual(result["unresolved_rejected_count"], 1)
        self.assertEqual(result["rejected_recent_count"], 1)
        self.assertEqual(result["rejected_recent"][0]["file"], "fresh.json")


if __name__ == "__main__":
    unittest.main()
