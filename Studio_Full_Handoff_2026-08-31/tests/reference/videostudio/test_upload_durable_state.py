from __future__ import annotations

from contextlib import contextmanager, nullcontext
import io
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


contract = load_module("durable_upload_contract", ROOT / "src" / "upload_contract.py")
manager = load_module("durable_upload_manager", ROOT / "src" / "upload_manager.py")
worker = load_module("durable_upload_worker", ROOT / "src" / "upload_worker.py")
lease_tool = load_module(
    "durable_upload_profile_lease", ROOT / "tools" / "upload_profile_lease.py"
)
lease_forced = load_module(
    "durable_upload_profile_lease_forced",
    ROOT / "tools" / "upload_profile_lease_forced.py",
)

UC = "UC1234567890123456789012"
VIDEO_ID = "aBcDeFg_123"
SCHEDULE = "2026-08-14T10:30:00+07:00"


class UploadBoundaryStateTest(unittest.TestCase):
    def test_retry_safety_is_phase_exact(self):
        event = contract.initial_phase_event()
        self.assertFalse(contract.phase_requires_no_reupload(event["phase"]))
        event = contract.advance_phase_event(event, "profile_opened")
        self.assertFalse(contract.phase_requires_no_reupload(event["phase"]))
        event = contract.advance_phase_event(event, "upload_started")
        self.assertTrue(contract.phase_requires_no_reupload(event["phase"]))
        event = contract.advance_phase_event(event, "video_id_known", VIDEO_ID)
        self.assertTrue(contract.phase_requires_no_reupload(event["phase"]))

    def test_phase_regression_and_identity_change_are_rejected(self):
        event = contract.advance_phase_event(
            contract.initial_phase_event(), "video_id_known", VIDEO_ID
        )
        with self.assertRaisesRegex(ValueError, "regressed"):
            contract.advance_phase_event(event, "profile_opened")
        with self.assertRaisesRegex(ValueError, "changed"):
            contract.advance_phase_event(event, "video_id_known", "zYxWvUt_987")

    def test_worker_requires_durable_ack_before_file_input_command(self):
        events = []

        class Input:
            def __init__(self, error: Exception | None = None):
                self.error = error

            def send_keys(self, _path):
                events.append("send_keys")
                if self.error:
                    raise self.error

        expected = contract.advance_phase_event(
            contract.advance_phase_event(
                contract.initial_phase_event(), "profile_opened"
            ),
            "upload_started",
        )
        ack = io.StringIO(contract.encode_phase_ack(expected) + "\n")
        with (
            mock.patch("builtins.print"),
            mock.patch.dict(worker.os.environ, {"VIDEO_UPLOAD_PHASE_ACK": "1"}),
        ):
            worker._reset_upload_phase()
            worker._advance_upload_phase("profile_opened")
            worker._send_video_file_at_upload_boundary(
                Input(), "/tmp/video.mp4", ack_stream=ack
            )
            self.assertEqual(worker._current_upload_phase()["phase"], "upload_started")
        self.assertEqual(events, ["send_keys"])

    def test_worker_crash_injection_missing_ack_never_calls_send_keys(self):
        called = []

        class Input:
            def send_keys(self, _path):
                called.append(True)

        with (
            mock.patch("builtins.print"),
            mock.patch.dict(worker.os.environ, {"VIDEO_UPLOAD_PHASE_ACK": "1"}),
        ):
            worker._reset_upload_phase()
            worker._advance_upload_phase("profile_opened")
            with self.assertRaisesRegex(RuntimeError, "ACK missing"):
                worker._send_video_file_at_upload_boundary(
                    Input(), "/tmp/video.mp4", ack_stream=io.StringIO("")
                )
            self.assertEqual(worker._current_upload_phase()["phase"], "upload_started")
        self.assertEqual(called, [])

    def test_manager_acks_only_after_durable_phase_callback(self):
        events = []
        opened = contract.advance_phase_event(
            contract.initial_phase_event(), "profile_opened"
        )
        started = contract.advance_phase_event(opened, "upload_started")

        class Stdin:
            def write(self, value):
                events.append(("ack", value.strip()))

            def flush(self):
                events.append(("ack_flush", ""))

        class Proc:
            stdin = Stdin()
            stdout = iter([
                contract.encode_phase_event(opened) + "\n",
                contract.encode_phase_event(started) + "\n",
            ])

            def wait(self, timeout=None):
                return 1

            def kill(self):
                return None

        def persist(event):
            events.append(("persist", event["phase"]))

        with mock.patch.object(manager.subprocess, "Popen", return_value=Proc()):
            result = manager._invoke_worker(
                ["worker"], {}, lambda _line: None, phase_callback=persist
            )
        self.assertTrue(result["do_not_reupload"])
        self.assertEqual(
            events,
            [
                ("persist", "profile_opened"),
                ("persist", "upload_started"),
                ("ack", contract.encode_phase_ack(started)),
                ("ack_flush", ""),
            ],
        )

    def test_manager_crash_recovery_uses_last_valid_marker(self):
        class Proc:
            def __init__(self, lines):
                self.stdout = iter(lines)

            def wait(self, timeout=None):
                return 1

            def kill(self):
                return None

        def invoke(phase: str):
            event = contract.advance_phase_event(contract.initial_phase_event(), phase)
            with mock.patch.object(manager.subprocess, "Popen", return_value=Proc([
                contract.encode_phase_event(event) + "\n"
            ])):
                return manager._invoke_worker(["worker"], {}, lambda _line: None)

        self.assertFalse(invoke("profile_opened")["do_not_reupload"])
        self.assertTrue(invoke("upload_started")["do_not_reupload"])

    def test_manager_kills_regressing_phase_protocol(self):
        opened = contract.advance_phase_event(
            contract.initial_phase_event(), "profile_opened"
        )
        lines = [
            contract.encode_phase_event(opened) + "\n",
            contract.encode_phase_event(contract.initial_phase_event()) + "\n",
        ]

        class Proc:
            def __init__(self):
                self.stdout = iter(lines)
                self.killed = False

            def wait(self, timeout=None):
                return 1

            def kill(self):
                self.killed = True

        process = Proc()
        with mock.patch.object(manager.subprocess, "Popen", return_value=process):
            result = manager._invoke_worker(["worker"], {}, lambda _line: None)
        self.assertTrue(process.killed)
        self.assertFalse(result["success"])
        self.assertEqual(result["phase"], "profile_opened")
        self.assertFalse(result["do_not_reupload"])


class ManagerCoordinationTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def lease_context():
        return {
            "schema_version": 1,
            "coordinator_command": ["lease-coordinator"],
            "holder": "test-host",
            "ttl_seconds": 120,
            "renew_interval_seconds": 10,
            "command_timeout_seconds": 5,
        }

    def receipt_context(self, policy: str):
        return {
            "schema_version": 2,
            "manifest_sha256": "a" * 64,
            "manifest_name": "manifest.json",
            "operational_date": "2026-08-14",
            "expected_scheduled_at": SCHEDULE,
            "source_host": "minsk",
            "outbox_dir": str(self.root / "outbox"),
            "receipt_io_path": str(
                ROOT / "tools" / "video_ops_watchdog" / "receipt_io.py"
            ),
            "existing_upload_policy": policy,
            "push": {"enabled": False},
        }

    def test_lease_closes_check_to_start_race(self):
        events = []

        class Lease:
            lost_event = threading.Event()
            lost_error = ""
            release_error = ""

            def assert_held(self):
                events.append("lease_asserted")

        @contextmanager
        def reserved(profile, _context):
            events.append(f"lease_acquired:{profile}")
            yield Lease()
            events.append("lease_released")

        class Proc:
            stdout = iter([])

            def wait(self, timeout=None):
                return 1

            def kill(self):
                return None

        def popen(*_args, **_kwargs):
            events.append("worker_started")
            return Proc()

        with (
            mock.patch.object(manager.upload_coordination, "profile_lease", reserved),
            mock.patch.object(manager.subprocess, "Popen", side_effect=popen),
            mock.patch.object(manager, "_worker_path", return_value=__file__),
        ):
            result = manager.run_upload(
                self.root,
                self.root / "video.mp4",
                "Title",
                "Description",
                {"name": "Channel", "channel_url": f"https://youtube.com/channel/{UC}", "profile_id": "9002"},
                dedup=False,
                one_per_day=False,
                profile_lease_context=self.lease_context(),
                profile_guard=lambda: events.append("dual_host_guard"),
                profile_inactive_guard=lambda: events.append("post_worker_inactive"),
            )

        self.assertFalse(result["success"])
        self.assertEqual(
            events[:4],
            ["lease_acquired:9002", "dual_host_guard", "lease_asserted", "worker_started"],
        )
        self.assertEqual(events[-2:], ["post_worker_inactive", "lease_released"])

    def test_uncertain_post_worker_state_retains_lease_until_ttl(self):
        events = []

        class Lease:
            lost_event = threading.Event()
            lost_error = ""
            release_error = ""
            retained = False

            def assert_held(self):
                return None

            def retain_until_expiry(self, reason):
                self.retained = True
                events.append(("retain", str(reason)))

        lease = Lease()

        @contextmanager
        def reserved(_profile, _context):
            yield lease
            events.append("ttl_retained" if lease.retained else "released")

        with (
            mock.patch.object(manager.upload_coordination, "profile_lease", reserved),
            mock.patch.object(manager, "_worker_path", return_value=__file__),
            mock.patch.object(
                manager,
                "_invoke_worker",
                return_value=contract.failure_result(
                    "worker crashed",
                    contract.advance_phase_event(
                        contract.initial_phase_event(), "upload_started"
                    ),
                ),
            ),
        ):
            result = manager.run_upload(
                self.root,
                self.root / "video.mp4",
                "Title",
                "Description",
                {"name": "Channel", "channel_url": f"https://youtube.com/channel/{UC}", "profile_id": "9002"},
                dedup=False,
                one_per_day=False,
                profile_lease_context=self.lease_context(),
                profile_guard=lambda: None,
                profile_inactive_guard=lambda: (_ for _ in ()).throw(
                    RuntimeError("dual-host inactive proof unavailable")
                ),
            )
        self.assertFalse(result["success"])
        self.assertTrue(result["do_not_reupload"])
        self.assertEqual(events[-1], "ttl_retained")
        self.assertIn("inactive proof", events[0][1])

    def test_receipt_context_never_turns_existing_url_into_fresh_upload(self):
        exact_url = f"https://youtu.be/{VIDEO_ID}"
        with (
            mock.patch.object(manager, "load_project_target", return_value={"uploaded_url": exact_url}),
            mock.patch.object(manager.subprocess, "Popen") as popen,
        ):
            skipped = manager.run_upload(
                self.root,
                self.root / "video.mp4",
                "Title",
                "Description",
                {"name": "Channel", "channel_url": f"https://youtube.com/channel/{UC}", "profile_id": "9002"},
                schedule_iso=SCHEDULE,
                receipt_context=self.receipt_context("skip"),
            )
        self.assertTrue(skipped["skipped"])
        popen.assert_not_called()

    def test_receipt_only_recovery_passes_exact_reconcile_id(self):
        captured = {}

        def invoke(argv, *_args, **_kwargs):
            captured["argv"] = argv
            return contract.failure_result(
                "offline stop", contract.initial_phase_event()
            )

        with (
            mock.patch.object(manager, "load_project_target", return_value={}),
            mock.patch.object(manager.UploadLog, "uploaded_today", return_value=None),
            mock.patch.object(manager, "_worker_path", return_value=__file__),
            mock.patch.object(manager, "_invoke_worker", side_effect=invoke),
            mock.patch.object(
                manager.upload_coordination,
                "profile_lease",
                return_value=nullcontext(None),
            ),
        ):
            result = manager.run_upload(
                self.root,
                self.root / "video.mp4",
                "Title",
                "Description",
                {"name": "Channel", "channel_url": f"https://youtube.com/channel/{UC}", "profile_id": "9002"},
                schedule_iso=SCHEDULE,
                receipt_context=self.receipt_context("reconcile_exact"),
                known_upload_url=f"https://youtu.be/{VIDEO_ID}",
                profile_lease_context=self.lease_context(),
                profile_guard=lambda: None,
                profile_inactive_guard=lambda: None,
            )
        self.assertFalse(result["success"])
        index = captured["argv"].index("--reconcile-video-id")
        self.assertEqual(captured["argv"][index + 1], VIDEO_ID)

    def test_receipt_context_cannot_disable_dedup_or_daily_limit(self):
        exact_url = f"https://youtu.be/{VIDEO_ID}"
        with mock.patch.object(manager.subprocess, "Popen") as popen:
            result = manager.run_upload(
                self.root,
                self.root / "video.mp4",
                "Title",
                "Description",
                {"name": "Channel", "channel_url": f"https://youtube.com/channel/{UC}", "profile_id": "9002"},
                schedule_iso=SCHEDULE,
                receipt_context=self.receipt_context("reconcile_exact"),
                dedup=False,
            )
        self.assertFalse(result["success"])
        self.assertIn("dedup", result["error"])
        popen.assert_not_called()

        with (
            mock.patch.object(manager, "load_project_target", return_value={"uploaded_url": exact_url}),
            mock.patch.object(
                manager.UploadLog,
                "uploaded_today",
                return_value={"project": "different", "url": "https://youtu.be/zYxWvUt_987"},
            ),
            mock.patch.object(manager.subprocess, "Popen") as popen,
        ):
            blocked = manager.run_upload(
                self.root,
                self.root / "video.mp4",
                "Title",
                "Description",
                {"name": "Channel", "channel_url": f"https://youtube.com/channel/{UC}", "profile_id": "9002"},
                schedule_iso=SCHEDULE,
                receipt_context=self.receipt_context("reconcile_exact"),
            )
        self.assertTrue(blocked["skipped"])
        self.assertEqual(blocked["reason"], "one_per_day")
        popen.assert_not_called()

    def test_upload_log_read_modify_write_is_not_lost(self):
        path = self.root / "uploads.json"
        with mock.patch.object(manager.UploadLog, "_PATH", str(path)):
            threads = [
                threading.Thread(
                    target=manager.UploadLog.record,
                    args=("Channel", "channel-url", f"project-{index}", f"url-{index}"),
                )
                for index in range(40)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            rows = json.loads(path.read_text(encoding="utf-8"))["uploads"]
        self.assertEqual(len(rows), 40)
        self.assertEqual({row["project"] for row in rows}, {f"project-{i}" for i in range(40)})

    def test_upload_log_lock_is_cross_process(self):
        path = self.root / "process-uploads.json"
        script = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location('process_upload_manager', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.UploadLog._PATH = sys.argv[2]
raise SystemExit(0 if module.UploadLog.record('Channel', 'UC', sys.argv[3], sys.argv[3]) else 1)
"""
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(ROOT / "src" / "upload_manager.py"),
                    str(path),
                    f"process-{index}",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for index in range(12)
        ]
        diagnostics = [process.communicate(timeout=10) for process in processes]
        self.assertEqual(
            [process.returncode for process in processes],
            [0] * len(processes),
            diagnostics,
        )
        rows = json.loads(path.read_text(encoding="utf-8"))["uploads"]
        self.assertEqual(len(rows), len(processes))


class ProfileLeaseStoreTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = lease_tool.LeaseStore(Path(self.tempdir.name) / "leases.sqlite3")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_atomic_competition_has_one_owner_and_hides_tokens(self):
        barrier = threading.Barrier(2)
        results = []

        def contender(token: str, holder: str):
            barrier.wait()
            results.append(
                self.store.acquire(
                    "9002", token, holder, 120,
                    now=1_000, monotonic_now=100, clock_id="boot-a",
                )
            )

        threads = [
            threading.Thread(target=contender, args=("a" * 48, "mac")),
            threading.Thread(target=contender, args=("b" * 48, "minsk")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(result["acquired"] for result in results), 1)
        self.assertTrue(all("owner_token" not in result["lease"] for result in results))

    def test_stale_reclaim_uses_monotonic_clock_not_wall_clock(self):
        first = self.store.acquire(
            "9004", "a" * 48, "mac", 60,
            now=1_000, monotonic_now=100, clock_id="boot-a",
        )
        self.assertTrue(first["acquired"])
        wall_jumped = self.store.acquire(
            "9004", "b" * 48, "minsk", 60,
            now=9_999_999, monotonic_now=120, clock_id="boot-a",
        )
        self.assertFalse(wall_jumped["acquired"])
        reclaimed = self.store.acquire(
            "9004", "b" * 48, "minsk", 60,
            now=10, monotonic_now=161, clock_id="boot-a",
        )
        self.assertTrue(reclaimed["acquired"])

    def test_same_owner_acquire_and_release_are_idempotent(self):
        token = "d" * 48
        first = self.store.acquire(
            "9003", token, "minsk", 60,
            now=1_000, monotonic_now=100, clock_id="boot-a",
        )
        second = self.store.acquire(
            "9003", token, "minsk", 60,
            now=1_010, monotonic_now=110, clock_id="boot-a",
        )
        self.assertTrue(first["acquired"])
        self.assertTrue(second["acquired"])
        self.assertEqual(first["lease"]["acquired_at"], second["lease"]["acquired_at"])
        self.assertTrue(self.store.release("9003", token, "minsk")["released"])
        again = self.store.release("9003", token, "minsk")
        self.assertTrue(again["released"])
        self.assertTrue(again["absent"])

    def test_reboot_clock_domain_has_full_ttl_safety_grace(self):
        self.store.acquire(
            "9001", "a" * 48, "mac", 60,
            now=1_000, monotonic_now=100, clock_id="boot-a",
        )
        early = self.store.acquire(
            "9001", "b" * 48, "minsk", 60,
            now=2_000, monotonic_now=100, clock_id="boot-b",
        )
        self.assertFalse(early["acquired"])
        late = self.store.acquire(
            "9001", "b" * 48, "minsk", 60,
            now=2_001,
            monotonic_now=lease_tool.MAX_TTL_SECONDS + 1,
            clock_id="boot-b",
        )
        self.assertTrue(late["acquired"])


class ProfileLeaseForcedCommandTest(unittest.TestCase):
    def test_exact_command_is_pinned_to_key_holder_and_database(self):
        command = (
            "/opt/video-ops/upload_profile_lease.py acquire "
            "--profile 9003 --owner-token " + "a" * 48 +
            " --holder minsk --ttl-seconds 3600"
        )
        argv = lease_forced.validated_argv(command, "minsk")
        self.assertEqual(argv[1:4], [
            lease_forced.COORDINATOR,
            "--db",
            lease_forced.LEASE_DB,
        ])
        self.assertEqual(argv[-2:], ["--ttl-seconds", "3600"])

    def test_key_cannot_claim_other_holder_or_inject_arguments(self):
        wrong_holder = (
            "/opt/video-ops/upload_profile_lease.py release "
            "--profile 9003 --owner-token " + "b" * 48 + " --holder mac"
        )
        with self.assertRaisesRegex(ValueError, "holder_not_authorized"):
            lease_forced.validated_argv(wrong_holder, "minsk")
        with self.assertRaisesRegex(ValueError, "arguments_invalid"):
            lease_forced.validated_argv(
                wrong_holder + " --db /tmp/attacker.sqlite3", "mac"
            )


if __name__ == "__main__":
    unittest.main()
