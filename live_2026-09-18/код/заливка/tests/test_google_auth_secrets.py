"""Offline tests use public RFC seeds and synthetic credentials only."""

import base64
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import time
import traceback
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "google_auth_secrets_tested",
    ROOT / "research/ww2_upload_stress_20260914/google_auth_secrets.py",
)
auth = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(auth)
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode("ascii")


class TotpTest(unittest.TestCase):
    def test_rfc6238_sha1_vectors_at_six_digits(self):
        # RFC 6238 Appendix B publishes eight digits; six-digit truncation
        # is the last six decimal digits of the same dynamic-truncated value.
        # https://www.rfc-editor.org/rfc/rfc6238#appendix-B
        vectors = [(59, "94287082"), (1111111109, "07081804"),
                   (1111111111, "14050471"), (1234567890, "89005924"),
                   (2000000000, "69279037"), (20000000000, "65353130")]
        for timestamp, expected in vectors:
            with self.subTest(timestamp=timestamp):
                code, remaining = auth.totp_code(RFC_SECRET, timestamp=timestamp)
                self.assertEqual(code, expected[-6:])
                self.assertEqual(remaining, 30 - timestamp % 30)

    def test_boundary_and_fractional_remaining(self):
        before, remaining = auth.totp_code(RFC_SECRET, timestamp=29.75)
        self.assertEqual(before, "755224")  # RFC 4226 counter zero.
        self.assertEqual(remaining, 0.25)
        at_boundary, remaining = auth.totp_code(RFC_SECRET, timestamp=30)
        self.assertEqual(at_boundary, "287082")
        self.assertEqual(remaining, 30)
        self.assertEqual(auth.totp_code(RFC_SECRET, timestamp=0), ("755224", 30))

    def test_lowercase_and_canonical_optional_padding(self):
        padded = base64.b32encode(b"synthetic seed").decode("ascii")
        expected = auth.totp_code(padded, timestamp=59)
        self.assertEqual(auth.totp_code(padded.lower(), timestamp=59), expected)
        self.assertEqual(auth.totp_code(padded.rstrip("="), timestamp=59), expected)

    def test_invalid_base32_rejected(self):
        for secret in (None, b"MY======", "", "A", "MZ", "MY=", "MY=======",
                       "M=Y=====", "M0", "MY======\n", "MY ======", "otpauth://totp/example"):
            with self.subTest(kind=type(secret).__name__):
                with self.assertRaises(auth.AuthSecretsError):
                    auth.totp_code(secret, timestamp=59)

    def test_invalid_timestamps_rejected_without_value(self):
        for timestamp in (-1, float("nan"), float("inf"), True, "59", 10**1000, 1e40):
            with self.assertRaisesRegex(auth.AuthSecretsError, "^Invalid TOTP timestamp$"):
                auth.totp_code(RFC_SECRET, timestamp=timestamp)

    def test_default_clock_without_replacing_generator(self):
        start = time.time()
        code, remaining = auth.totp_code(RFC_SECRET)
        end = time.time()
        possible = {auth.totp_code(RFC_SECRET, timestamp=value)[0] for value in (start, end)}
        self.assertIn(code, possible)
        self.assertGreater(remaining, 0)
        self.assertLessEqual(remaining, 30)


class CredentialsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "configured-only.json"
        self.record = {"login": "Exact.User@example.invalid", "password": "synthetic-password-marker",
                       "totp": RFC_SECRET}

    def write(self, accounts):
        self.path.write_text(json.dumps({"accounts": accounts}), encoding="utf-8")

    def test_exact_email_and_no_case_or_substring_fallback(self):
        self.write([self.record])
        credentials = auth.load_credentials(self.path, self.record["login"])
        self.assertEqual(credentials.login, self.record["login"])
        self.assertEqual(credentials.password, self.record["password"])
        self.assertEqual(credentials.totp, self.record["totp"])
        for login in ("exact.user@example.invalid", "User@example.invalid", "wrong@example.invalid"):
            with self.assertRaisesRegex(auth.AuthSecretsError, "^Requested account not found$"):
                auth.load_credentials(self.path, login)

    def test_duplicate_account_is_rejected_even_after_selected_match(self):
        self.write([self.record, dict(self.record)])
        with self.assertRaisesRegex(auth.AuthSecretsError, "^Duplicate account login$"):
            auth.load_credentials(self.path, self.record["login"])

    def test_missing_and_invalid_fields_rejected(self):
        for field in ("login", "password", "totp"):
            for value in (None, "", 123):
                changed = dict(self.record, **{field: value})
                self.write([changed])
                with self.assertRaises(auth.AuthSecretsError):
                    auth.load_credentials(self.path, self.record["login"])
            changed = dict(self.record)
            del changed[field]
            self.write([changed])
            with self.assertRaises(auth.AuthSecretsError):
                auth.load_credentials(self.path, self.record["login"])
        self.write([dict(self.record, totp="invalid!base32")])
        with self.assertRaises(auth.AuthSecretsError):
            auth.load_credentials(self.path, self.record["login"])

    def test_invalid_schema_empty_accounts_and_duplicate_json_keys(self):
        for document in ([], {}, {"accounts": {}}, {"accounts": [None]}, {"accounts": []}):
            self.path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(auth.AuthSecretsError):
                auth.load_credentials(self.path, self.record["login"])
        self.path.write_text('{"accounts": [], "accounts": []}', encoding="utf-8")
        with self.assertRaisesRegex(auth.AuthSecretsError, "^Duplicate JSON field$"):
            auth.load_credentials(self.path, self.record["login"])

    def test_repr_and_normal_operation_do_not_print_values(self):
        self.write([self.record])
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            credentials = auth.load_credentials(self.path, self.record["login"])
            auth.totp_code(credentials.totp, timestamp=59)
        self.assertEqual(repr(credentials), "Credentials()")
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        for value in self.record.values():
            self.assertNotIn(value, repr(credentials))

    def test_errors_hide_file_content_path_and_exception_chains(self):
        sentinel = "private-error-marker"
        self.path = Path(self.temp.name) / sentinel
        for data in (None, b"\xff" + sentinel.encode(), (sentinel + '{"password":').encode()):
            if data is not None:
                self.path.write_bytes(data)
            try:
                auth.load_credentials(self.path, self.record["login"])
            except auth.AuthSecretsError as exc:
                self.assertEqual(str(exc), "Cannot read credentials JSON")
                rendered = "".join(traceback.format_exception(exc))
                self.assertNotIn(sentinel, rendered)
                self.assertNotIn(self.record["password"], rendered)
                self.assertNotIn(RFC_SECRET, rendered)
            else:
                self.fail("Invalid credentials file accepted")


if __name__ == "__main__":
    unittest.main()
