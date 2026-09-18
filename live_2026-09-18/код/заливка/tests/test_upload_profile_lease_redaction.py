"""Exercise the real lease transport wrapper without starting any subprocess."""

import importlib.util
from pathlib import Path
import subprocess
import traceback
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "lease_redaction_tested", ROOT / "src/upload_coordination.py")
coordination = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(coordination)


class LeaseTransportRedactionTest(unittest.TestCase):
    def assert_transport_redacted(self, exception_type):
        secret = "SYNTHETIC_OWNER_TOKEN_MUST_NOT_APPEAR"
        lease = coordination.ProfileLease("54", {
            "schema_version": 1, "coordinator_command": ["never-executed-coordinator"],
            "holder": "minsk", "ttl_seconds": 120,
            "renew_interval_seconds": 10, "command_timeout_seconds": 1,
        })
        lease.owner_token = secret
        for action in ("acquire", "renew", "release"):
            with self.subTest(action=action):
                cmd = ["never-executed-coordinator", action, "--owner-token", secret]
                if exception_type is subprocess.TimeoutExpired:
                    original = exception_type(cmd, 1, output=secret, stderr=secret)
                else:
                    original = exception_type(1, cmd, output=secret, stderr=secret)
                # The input error really leaks; a vacuous test cannot pass here.
                self.assertIn(secret, str(original))
                with mock.patch.object(coordination.subprocess, "run", side_effect=original) as transport:
                    try:
                        lease._invoke(action)
                    except coordination.ProfileLeaseError as error:
                        self.assertEqual(
                            str(error),
                            f"profile lease {action} transport failed ({exception_type.__name__})",
                        )
                        rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
                        self.assertNotIn(secret, str(error))
                        self.assertNotIn(secret, rendered)
                        self.assertNotIn("--owner-token", rendered)
                        self.assertTrue(error.__suppress_context__)
                        self.assertIsNone(error.__cause__)
                    else:
                        self.fail("Transport error was not rejected")
                transport.assert_called_once()
                self.assertIn(secret, transport.call_args.args[0])

    def test_timeout_expired_redacts_command_and_traceback(self):
        self.assert_transport_redacted(subprocess.TimeoutExpired)

    def test_called_process_error_redacts_command_and_traceback(self):
        self.assert_transport_redacted(subprocess.CalledProcessError)


if __name__ == "__main__":
    unittest.main()
