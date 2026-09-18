import hashlib
import fcntl
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import adspower_rate_limit as rate


def take_slot(directory, url, output, crash=False, hold=0):
    os.environ[rate.ENV] = directory
    with rate.control_slot(url):
        Path(output).write_text(str(time.monotonic_ns()))
        if crash:
            os._exit(3)
        time.sleep(hold)


def take_legacy_slot(directory, url, output):
    """The old on-disk protocol: v1 dispatch only, same flock, full interval."""
    origin = rate.origin_key(url)
    key = hashlib.sha256(origin.encode()).hexdigest()
    root = Path(directory)
    with (root / (key + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state_path = root / (key + '.json')
        state = json.loads(state_path.read_text())
        if set(state) != {'schema_version', 'origin', 'boot_id', 'started_ns'}:
            raise AssertionError('new writer broke the legacy state shape')
        time.sleep(rate.INTERVAL_NS / 1_000_000_000)
        rate._write_state(state_path, {'schema_version': 1, 'origin': origin,
            'boot_id': rate.boot_id(), 'started_ns': time.monotonic_ns()})
        Path(output).write_text(str(time.monotonic_ns()))


class RateLimitTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {rate.ENV: str(self.root / 'pacing')})
        self.env.start()
        self.ctx = multiprocessing.get_context('spawn')

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def state_paths(self, url):
        key = hashlib.sha256(rate.origin_key(url).encode()).hexdigest()
        root = Path(os.environ[rate.ENV])
        return root / (key + '.json'), root / (key + '.completed.json')

    def spawn_slots(self, origins):
        processes = []
        for i, url in enumerate(origins):
            p = self.ctx.Process(target=take_slot, args=(os.environ[rate.ENV], url, str(self.root / str(i))))
            p.start(); processes.append(p)
        for p in processes:
            p.join(15)
            self.assertFalse(p.is_alive())
            self.assertEqual(p.exitcode, 0)
        return [int((self.root / str(i)).read_text()) for i in range(len(origins))]

    def test_four_real_processes_same_origin_are_paced(self):
        values = sorted(self.spawn_slots(['http://localhost:50325/api/v1/browser/active'] * 4))
        for before, after in zip(values, values[1:]):
            self.assertGreaterEqual(after - before, rate.INTERVAL_NS)

    def test_distinct_origin_is_independent(self):
        # Hold origin A's actual lock; B still enters without waiting for A.
        url = 'http://localhost:50325/api/v1/browser/active'
        with rate.control_slot(url):
            p = self.ctx.Process(target=take_slot, args=(os.environ[rate.ENV],
                'http://100.100.244.89:50325/api/v1/browser/local-active', str(self.root / 'other')))
            p.start(); p.join(5)
            self.assertEqual(p.exitcode, 0)

    def test_crashed_owner_releases_lock_but_retains_pacing(self):
        url = 'http://localhost:50325/api/v1/browser/start'
        p = self.ctx.Process(target=take_slot, args=(os.environ[rate.ENV], url, str(self.root / 'crash'), True))
        p.start(); p.join(5)
        self.assertEqual(p.exitcode, 3)
        before = int((self.root / 'crash').read_text())
        with rate.control_slot(url):
            self.assertGreaterEqual(time.monotonic_ns() - before, rate.INTERVAL_NS)

    def test_completed_idle_request_needs_no_extra_full_interval(self):
        url = 'http://localhost:50325/api/v1/browser/active'
        with rate.control_slot(url): pass
        path, receipt_path = self.state_paths(url)
        state = json.loads(path.read_text())
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(set(state), {'schema_version', 'origin', 'boot_id', 'started_ns'})
        self.assertEqual(receipt['started_ns'], state['started_ns'])
        self.assertGreaterEqual(receipt['completed_ns'], state['started_ns'])
        time.sleep(rate.INTERVAL_NS / 1_000_000_000 + 0.05)
        before = time.monotonic()
        with rate.deadline_scope(before + 0.5):
            with rate.control_slot(url):
                self.assertLess(time.monotonic() - before, 0.5)

    def test_partial_idle_waits_only_remainder_from_completion(self):
        url = 'http://localhost:50325/api/v1/browser/active'
        with rate.control_slot(url): pass
        _, receipt_path = self.state_paths(url)
        completed = json.loads(receipt_path.read_text())['completed_ns']
        time.sleep(0.65)
        before = time.monotonic()
        with rate.deadline_scope(before + 0.8):
            with rate.control_slot(url):
                self.assertGreaterEqual(time.monotonic_ns() - completed, rate.INTERVAL_NS)
                self.assertLess(time.monotonic() - before, 0.8)

    def test_spacing_is_from_request_completion_not_dispatch(self):
        url = 'http://localhost:50325/api/v1/browser/start'
        p = self.ctx.Process(target=take_slot, args=(os.environ[rate.ENV], url,
            str(self.root / 'slow'), False, 0.7))
        p.start(); p.join(5)
        self.assertEqual(p.exitcode, 0)
        _, receipt_path = self.state_paths(url)
        completed = json.loads(receipt_path.read_text())['completed_ns']
        with rate.control_slot(url):
            self.assertGreaterEqual(time.monotonic_ns() - completed, rate.INTERVAL_NS)

    def test_old_writer_and_crash_invalidate_stale_completion_receipt(self):
        url = 'http://localhost:50325/api/v1/browser/start'
        for legacy in (True, False):
            with self.subTest(legacy=legacy):
                with rate.control_slot(url): pass
                _, receipt_path = self.state_paths(url)
                previous = receipt_path.read_bytes()
                args = (os.environ[rate.ENV], url, str(self.root / 'previous'))
                p = self.ctx.Process(target=take_legacy_slot if legacy else take_slot,
                                     args=args if legacy else args + (True,))
                p.start(); p.join(5)
                self.assertEqual(p.exitcode, 0 if legacy else 3)
                self.assertEqual(receipt_path.read_bytes(), previous)
                before = time.monotonic_ns()
                with rate.control_slot(url):
                    self.assertGreaterEqual(time.monotonic_ns() - before, rate.INTERVAL_NS)

    def test_old_dispatch_only_record_waits_full_interval_even_after_idle(self):
        url = 'http://localhost:50325/api/v1/browser/active'
        path, _ = self.state_paths(url)
        path.parent.mkdir()
        rate._write_state(path, {'schema_version': 1, 'origin': rate.origin_key(url),
            'boot_id': rate.boot_id(), 'started_ns': time.monotonic_ns() - 10 * rate.INTERVAL_NS})
        before = time.monotonic_ns()
        with rate.control_slot(url):
            self.assertGreaterEqual(time.monotonic_ns() - before, rate.INTERVAL_NS)

    def test_invalid_or_future_completion_blocks_dispatch(self):
        url = 'http://localhost:50325/api/v1/browser/active'
        with rate.control_slot(url): pass
        _, receipt_path = self.state_paths(url)
        receipt = json.loads(receipt_path.read_text())
        for value in ({}, dict(receipt, completed_ns=True), dict(receipt, origin='foreign'),
                      dict(receipt, completed_ns=receipt['started_ns'] - 1),
                      dict(receipt, completed_ns=time.monotonic_ns() + 10**12)):
            with self.subTest(value=value):
                receipt_path.write_text(json.dumps(value))
                with self.assertRaises(rate.RateLimitError):
                    with rate.control_slot(url): self.fail('must not dispatch')
        receipt_path.write_text('{')
        with self.assertRaises(rate.RateLimitError):
            with rate.control_slot(url): self.fail('must not dispatch')

    def test_default_has_no_state_or_delay_and_preserves_transport(self):
        os.environ.pop(rate.ENV)
        with patch('requests.get', return_value='response') as transport:
            self.assertEqual(rate.get('http://localhost:50325/api/v1/user/list', timeout=12), 'response')
        transport.assert_called_once_with('http://localhost:50325/api/v1/user/list', timeout=12)
        self.assertFalse((self.root / 'pacing').exists())

    def test_transport_failure_never_retries_and_next_call_is_paced(self):
        url = 'http://localhost:50325/api/v1/browser/start'
        with patch('requests.get', side_effect=TimeoutError('fixture timeout')) as transport:
            with self.assertRaises(TimeoutError):
                rate.get(url, timeout=1)
        self.assertEqual(transport.call_count, 1)
        before = time.monotonic_ns()
        with rate.control_slot(url):
            self.assertGreater(time.monotonic_ns() - before, 900_000_000)

    def test_malformed_state_and_backwards_clock_block(self):
        url = 'http://localhost:50325/api/v1/browser/active'
        with rate.control_slot(url):
            pass
        path = Path(os.environ[rate.ENV]) / (hashlib.sha256(rate.origin_key(url).encode()).hexdigest()+'.json')
        original = json.loads(path.read_text())
        for state in ({}, dict(original, started_ns=True), dict(original, origin='foreign'),
                      dict(original, started_ns=time.monotonic_ns()+10**12)):
            with self.subTest(state=state):
                path.write_text(json.dumps(state))
                with self.assertRaises(rate.RateLimitError):
                    with rate.control_slot(url):
                        self.fail('must not dispatch')

    def test_reboot_uses_new_epoch_after_conservative_interval(self):
        url = 'http://localhost:50325/api/v1/browser/active'
        with rate.control_slot(url): pass
        path, _ = self.state_paths(url)
        state = json.loads(path.read_text()); state['boot_id'] = 'prior-boot'
        path.write_text(json.dumps(state)); before = time.monotonic_ns()
        with rate.control_slot(url):
            self.assertGreaterEqual(time.monotonic_ns() - before, rate.INTERVAL_NS)
        self.assertEqual(json.loads(path.read_text())['boot_id'], rate.boot_id())

    def test_loopback_aliases_and_no_secret_origin(self):
        self.assertEqual(rate.origin_key('http://localhost:50325/a'), rate.origin_key('http://127.0.0.1:50325/b'))
        for url in ('http://secret:secret@host/a', 'file:///api', 'http://host:bad'):
            with self.assertRaises(rate.RateLimitError) as error:
                rate.origin_key(url)
            self.assertNotIn('secret', str(error.exception))

    def test_collector_strict_response_schema(self):
        spec = importlib.util.spec_from_file_location('stress_collector_test', ROOT/'tools/collect_channel_emails.py')
        collector = importlib.util.module_from_spec(spec); spec.loader.exec_module(collector)
        class Response:
            status_code = 200
            def __init__(self, value): self.value = value
            def json(self): return self.value
        for value in ({'code': 0}, {'code': False, 'data': {'list': []}},
                      {'code': 0, 'data': {'list': None}}, {'code': 0, 'data': {'list': [{}]}}):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                collector._api_json(Response(value), listing=True)
        self.assertEqual(collector._api_json(Response({'code': 0, 'data': {'list': []}}), listing=True)['code'], 0)

    def test_deadline_while_actual_origin_lock_is_held_never_dispatches(self):
        url = 'http://localhost:50325/api/v1/browser/local-active'
        with rate.control_slot(url), patch('requests.get') as transport:
            started = time.monotonic()
            with self.assertRaises(rate.RateLimitError), rate.deadline_scope(started + 0.15):
                rate.get(url, timeout=12)
            self.assertLess(time.monotonic() - started, 0.7)
            transport.assert_not_called()

    def test_deadline_inside_full_pacing_interval_never_dispatches(self):
        url = 'http://localhost:50325/api/v1/browser/local-active'
        with rate.control_slot(url): pass
        with patch('requests.get') as transport:
            with self.assertRaises(rate.RateLimitError), rate.deadline_scope(time.monotonic() + 0.15):
                rate.get(url, timeout=12)
            transport.assert_not_called()

    def test_timeout_is_clamped_after_pacing_and_late_http_response_is_rejected(self):
        url = 'http://localhost:50325/api/v1/browser/local-active'
        with rate.control_slot(url): pass
        with patch('requests.get', return_value='response') as transport:
            with rate.deadline_scope(time.monotonic() + 1.5):
                self.assertEqual(rate.get(url, timeout=40), 'response')
            self.assertGreater(transport.call_args.kwargs['timeout'], 0)
            self.assertLess(transport.call_args.kwargs['timeout'], 0.5)
        os.environ.pop(rate.ENV)  # Budget also applies without an enabled pacing directory.
        def late_http(*args, **kwargs):
            time.sleep(0.06)
            return 'late'
        with patch('requests.get', side_effect=late_http) as transport:
            with self.assertRaises(rate.RateLimitError), rate.deadline_scope(time.monotonic() + 0.03):
                rate.get(url, timeout=12)
            self.assertEqual(transport.call_count, 1)

    def test_owner_guard_cancels_wait_and_scope_restores_default_behavior(self):
        url = 'http://localhost:50325/api/v1/browser/local-active'
        checks = []
        def guard():
            checks.append(True)
            if len(checks) > 2: raise RuntimeError('fixture owner lost')
        with rate.control_slot(url), patch('requests.get') as transport:
            with self.assertRaisesRegex(RuntimeError, 'fixture owner lost'), rate.deadline_scope(time.monotonic() + 5, check=guard):
                rate.get(url, timeout=12)
            transport.assert_not_called()
        os.environ.pop(rate.ENV)
        with patch('requests.get', return_value='ok') as transport:
            self.assertEqual(rate.get(url, timeout=12), 'ok')
            self.assertEqual(transport.call_args.kwargs['timeout'], 12)


if __name__ == '__main__': unittest.main()
