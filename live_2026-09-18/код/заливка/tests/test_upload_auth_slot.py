"""Shared auth admission uses real OS locks/processes; no browser or credentials."""
import fcntl
import io
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))
import upload_auth as auth
import test_upload_auth as fixtures
Lease, request, UC, VIDEO = fixtures.Lease, fixtures.request, fixtures.UC, fixtures.VIDEO


def members(count=1):
    return [{'profile': str(21 + i), 'channel_uc': UC if i == 0 else 'UC' + str(i) * 22,
             'project_id': 'private_fixture_' + str(i)} for i in range(count)]


def wave(index=0, count=1):
    return {'wave_id': 'c' * 64, 'visibility': 'PRIVATE', 'members': members(count), 'attempt_id': f'{index + 1:032x}'}


def enter_process(directory, index, count):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        member = members(count)[index]
        expected = {key: member[key] for key in ('profile', 'channel_uc')}
        item = request(**expected, request_id=f'{index + 1:032x}')
        ctx = auth.Context(item, expected, {'phase': 'upload_started', 'video_id': ''},
                           child, Lease(), Path(directory) / ('evidence_' + str(index)),
                           wave=wave(index, count), worker_deadline=time.monotonic() + 30)
        with auth.shared_slot(ctx, root=Path(directory) / 'slots'):
            entered = time.monotonic()
            time.sleep(0.1)
            left = time.monotonic()
            auth.save_new(Path(directory) / (str(index) + '.json'), {'entered': entered, 'left': left})
    finally:
        child.terminate()
        child.wait(timeout=5)


class SharedProcessTest(unittest.TestCase):
    def test_four_real_processes_have_one_active_callback_and_all_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            processes = [multiprocessing.get_context('spawn').Process(target=enter_process, args=(directory, i, 4))
                         for i in range(4)]
            for process in processes:
                process.start()
                self.addCleanup(lambda p=process: p.kill() if p.is_alive() else None)
            for process in processes:
                process.join(15)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
            spans = sorted((json.loads((Path(directory) / (str(i) + '.json')).read_text()) for i in range(4)),
                           key=lambda row: row['entered'])
            for first, second in zip(spans, spans[1:]):
                self.assertLessEqual(first['left'], second['entered'])
            self.assertEqual(len(list((Path(directory) / 'slots' / ('c' * 64)).glob('*.claim.json'))), 4)


class SharedSlotTest(unittest.TestCase):
    setUp = fixtures.ContextTest.setUp
    write_stat = fixtures.ContextTest.write_stat
    def queued(self, index=0, count=1, **kwargs):
        member = members(count)[index]
        expected = {key: member[key] for key in ('profile', 'channel_uc')}
        return auth.Context(request(**expected, request_id=f'{index + 1:032x}'), expected,
                            {'phase': 'upload_started', 'video_id': ''}, self.proc, self.lease,
                            self.root / ('evidence_' + str(index)), wave=wave(index, count),
                            clock=lambda: self.now,
                            identity_reader=lambda pid: auth.process_identity(pid, self.root), **kwargs)

    def test_held_slot_timeout_does_not_activate_or_retry_member(self):
        slot_root = self.root / 'slots'; slot_root.mkdir()
        context = self.queued()
        context.deadline = self.now + 0.1
        with (slot_root / 'active.lock').open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def pause(amount): self.now += amount
            with self.assertRaises(auth.AuthBlocked):
                with auth.shared_slot(context, root=slot_root, pause=pause):
                    self.fail('A queued timeout cannot run a callback')
            self.assertFalse(context.active)
        replacement = self.queued()
        with self.assertRaises(FileExistsError):
            with auth.shared_slot(replacement, root=slot_root):
                self.fail('Consumed member cannot issue a new auth request')

    def test_dead_queued_worker_and_lost_lease_prevent_activation(self):
        for reason in ('worker', 'lease'):
            self.now = 100.0
            self.lease.lost_event.clear()
            self.write_stat(self.proc.pid, os.getpid(), '123')
            context = self.queued()
            context.evidence_dir = self.root / ('evidence_' + reason)
            slot_root = self.root / reason; slot_root.mkdir()
            with (slot_root / 'active.lock').open('a') as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                def pause(amount):
                    self.now += amount
                    if reason == 'worker': self.write_stat(self.proc.pid, os.getpid(), '123', 'Z')
                    else: self.lease.lost_event.set()
                with self.assertRaises((auth.AuthBlocked, RuntimeError)):
                    with auth.shared_slot(context, root=slot_root, pause=pause):
                        self.fail('Lost ownership cannot reach callback')
            self.assertFalse(context.active)

    def test_active_exception_releases_lock_and_resets_only_active_budget(self):
        first, second = self.queued(0, 2), self.queued(1, 2)
        self.now += 200  # Queue is not constrained to the former 180-second active budget.
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with auth.shared_slot(first, root=self.root / 'slots'):
                self.assertEqual(first.remaining(), auth.TIMEOUT)
                raise RuntimeError('fixture')
        with auth.shared_slot(second, root=self.root / 'slots'):
            self.assertTrue(second.active)
            self.assertEqual(second.remaining(), auth.TIMEOUT)

    def test_watchdog_remainder_caps_queue_and_active_and_can_block_immediately(self):
        with self.assertRaises(auth.AuthBlocked):
            self.queued(worker_deadline=self.now + auth.ACK_MARGIN)
        context = self.queued(worker_deadline=self.now + 25)
        self.assertEqual(context.remaining(), 15)
        with auth.shared_slot(context, root=self.root / 'slots'):
            self.assertEqual(context.remaining(), 15)

    def test_frozen_wave_rejects_changed_members_and_oversized_or_foreign_scope(self):
        expected = {'profile': '21', 'channel_uc': UC}
        for bad in (wave(count=9), dict(wave(), members=members(2)[1:]),
                    dict(wave(), members=members() * 2)):
            with self.assertRaises(auth.AuthBlocked): auth.validate_wave(bad, expected)
        first, second = self.queued(0, 2), self.queued(1, 2)
        with auth.shared_slot(first, root=self.root / 'slots'): pass
        second.wave['members'][0]['project_id'] = 'changed'
        with self.assertRaises(auth.AuthBlocked):
            with auth.shared_slot(second, root=self.root / 'slots'):
                self.fail('Cannot reuse a wave ID for another immutable cohort')


class AckBudgetTest(unittest.TestCase):
    def test_shared_and_legacy_budgets_and_actual_worker_deadline_cap(self):
        for shared, cap, expected in ((False, None, 190), (True, None, 1450), (True, 2, 2)):
            env = {auth.SHARED_ENV: '1' if shared else '0'}
            if cap is not None: env[auth.WORKER_DEADLINE_ENV] = str(time.monotonic() + cap)
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(auth.select, 'select', return_value=([], [], [])) as poll:
                with self.assertRaises(auth.AuthBlocked): auth.wait_ack(request(), io.StringIO())
                self.assertGreater(poll.call_args.args[3], expected - 0.5)
                self.assertLessEqual(poll.call_args.args[3], expected)


class SharedManagerPipeTest(unittest.TestCase):
    invoke = fixtures.ManagerPipeTest.invoke
    def test_real_manager_runs_callback_only_inside_bound_slot(self):
        def callback(context, events):
            self.assertTrue(context.active)
            self.assertEqual(context.wave['attempt_id'], wave()['attempt_id'])
            self.assertEqual(events[-1]['phase'], 'upload_started')
            with (auth.SLOT_ROOT / 'active.lock').open('a') as contender:
                with self.assertRaises(BlockingIOError): fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return VIDEO
        for visibility in ('PRIVATE', 'PUBLIC'):
            result, events, _ = self.invoke(request(), callback, auth_wave=dict(wave(), visibility=visibility))
            self.assertEqual(events[-1]['video_id'], VIDEO)
            self.assertTrue(result['do_not_reupload'])

    def test_duplicate_mismatched_or_scheduled_intent_never_launches_auth(self):
        for extra in (['--visibility', 'public'], ['--visibility=unlisted'], ['--schedule', '2026-10-01T00:00:00Z']):
            result, events, _ = self.invoke(request(), lambda *_: self.fail('Invalid intent cannot run auth'),
                                            auth_wave=wave(), argv_extra=extra)
            self.assertFalse(result['success'])
            self.assertEqual(result['phase'], 'preflight')
            self.assertEqual(events, [])


class ConnectionBudgetTest(unittest.TestCase):
    setUp = fixtures.ContextTest.setUp
    write_stat = fixtures.ContextTest.write_stat

    def test_discovery_and_websocket_use_remaining_time_and_late_results_close(self):
        for http_delay, websocket_delay, accepted in ((4, 0, True), (6, 0, False), (4, 2, False)):
            with self.subTest(http_delay=http_delay, websocket_delay=websocket_delay):
                self.now = 100
                self.context.deadline = self.now + 5
                connection = mock.Mock()
                def http(*args, **kwargs):
                    self.assertLessEqual(kwargs['timeout'], 5)
                    self.now += http_delay
                    return io.StringIO(json.dumps({'webSocketDebuggerUrl': 'ws://127.0.0.1:9222/devtools/browser/fixture'}))
                def connect(*args, **kwargs):
                    self.assertLessEqual(kwargs['timeout'], 1)
                    self.now += websocket_delay
                    return connection
                with mock.patch.object(auth.urllib.request, 'urlopen', side_effect=http), \
                        mock.patch('websocket.create_connection', side_effect=connect) as transport:
                    if accepted:
                        self.assertIs(auth._connect_cdp(self.context, 9222).ws, connection)
                    else:
                        with self.assertRaises(auth.AuthBlocked): auth._connect_cdp(self.context, 9222)
                    if http_delay > 5: transport.assert_not_called()
                    if websocket_delay: connection.close.assert_called_once_with(timeout=0)

    def test_queued_runtime_cannot_load_credentials_or_connect(self):
        self.context.active = False
        with mock.patch.object(auth.sys, 'platform', 'linux'), \
                mock.patch.object(auth.socket, 'gethostname', return_value='vova-System-Product-Name'), \
                mock.patch.dict(os.environ, {'ADSPOWER_RATE_LIMIT_DIR': str(self.root)}), \
                mock.patch.object(auth, '_load') as load:
            with self.assertRaises(auth.AuthBlocked): auth.runtime_callback(self.context)
            load.assert_not_called()


if __name__ == '__main__': unittest.main()
