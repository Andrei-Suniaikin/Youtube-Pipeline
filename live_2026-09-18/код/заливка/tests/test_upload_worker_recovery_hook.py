"""The worker's account-recovery hook: inert by default, strict when it runs.

Loads the real upload_worker module and drives _try_account_recovery directly.
The recovery module itself is exercised by tests/test_upload_account_recovery.py;
here the subject is the hook's own contract — when it may run at all, what it
does with the routing error, and what it records as the proven account number.
"""
import importlib.util
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

UC = 'UClNGNGulV7gPOrMxgzEmL-w'
ROUTING_ERROR = 'Studio не отдала рабочее состояние канала (доступ не подтверждён)'


def load_worker():
    spec = importlib.util.spec_from_file_location('worker_under_test',
                                                  ROOT / 'src' / 'upload_worker.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExplodingDriver:
    def execute_script(self, *args, **kwargs):
        raise AssertionError('the hook must not touch the page here')

    def get(self, *args, **kwargs):
        raise AssertionError('the hook must not navigate here')


class HookTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.worker = load_worker()
        if getattr(cls.worker, 'ACCOUNT_RECOVERY', None) is None:
            raise unittest.SkipTest('worker copy is not patched with the recovery hook')

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in
                      ('VIDEO_UPLOAD_ACCOUNT_RECOVERY', 'VIDEO_UPLOAD_AUTO_AUTH',
                       'VIDEO_UPLOAD_PHASE_ACK', 'VIDEO_UPLOAD_PEER_ADSPOWER',
                       'VIDEO_UPLOAD_LEASE_CONTEXT')}
        self.worker._ACTIVE_AU.pop(UC, None)
        self.addCleanup(self.restore)

    def tearDown(self):
        self.worker._reset_upload_phase() if hasattr(self.worker, '_reset_upload_phase') else None

    def restore(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.worker._ACTIVE_AU.pop(UC, None)

    def supervised(self, enabled=True):
        os.environ['VIDEO_UPLOAD_AUTO_AUTH'] = '1'
        os.environ['VIDEO_UPLOAD_PHASE_ACK'] = '1'
        os.environ['VIDEO_UPLOAD_PEER_ADSPOWER'] = 'http://127.0.0.1:1'
        if enabled:
            os.environ['VIDEO_UPLOAD_ACCOUNT_RECOVERY'] = '1'
        else:
            os.environ.pop('VIDEO_UPLOAD_ACCOUNT_RECOVERY', None)

    def stub_recovery(self, outcome):
        """Replace only the *collaborator* module, never the hook under test."""
        recovery = self.worker.ACCOUNT_RECOVERY
        original = recovery.maybe_recover
        self.addCleanup(setattr, recovery, 'maybe_recover', original)

        def fake(driver, profile, channel_uc, lease_check, **kwargs):
            self.seen = {'profile': profile, 'channel_uc': channel_uc,
                         'phase_before_upload': kwargs.get('phase_before_upload')}
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        recovery.maybe_recover = fake

    def test_disabled_flag_keeps_the_original_routing_error(self):
        self.supervised(enabled=False)
        result = self.worker._try_account_recovery(ExplodingDriver(), '28', UC, ROUTING_ERROR)
        self.assertEqual(result, (None, ROUTING_ERROR))

    def fake_coordinator(self, granted=True):
        """A local stand-in for the shared lease coordinator (no ssh, no network)."""
        import json as _json
        import tempfile
        script = Path(tempfile.mkdtemp()) / 'coordinator.py'
        script.write_text(
            'import json,sys\n'
            'action=sys.argv[1]\n'
            'key={"acquire":"acquired","renew":"renewed","release":"released"}[action]\n'
            'print(json.dumps({"schema_version":1,key:%s,'
            '"lease":{"profile":"28","holder":"mac","acquired_at":0,'
            '"renewed_at":0,"expires_at":0}}))\n' % ('True' if granted else 'False'),
            encoding='utf-8')
        os.environ['VIDEO_UPLOAD_LEASE_CONTEXT'] = _json.dumps({
            'schema_version': 1, 'coordinator_command': [sys.executable, str(script)],
            'holder': 'mac', 'ttl_seconds': 900, 'renew_interval_seconds': 120,
            'command_timeout_seconds': 20})

    def unsupervised(self):
        os.environ['VIDEO_UPLOAD_ACCOUNT_RECOVERY'] = '1'
        os.environ['VIDEO_UPLOAD_PEER_ADSPOWER'] = 'http://127.0.0.1:1'
        os.environ.pop('VIDEO_UPLOAD_AUTO_AUTH', None)
        os.environ.pop('VIDEO_UPLOAD_PHASE_ACK', None)

    def test_puller_path_takes_its_own_lease_before_signing_in(self):
        # Production has no manager, so the hook must hold the same cross-host
        # lease itself — otherwise two processes could type into one profile.
        self.unsupervised()
        self.fake_coordinator(granted=True)
        self.stub_recovery({'recovered': True, 'channel_uc': UC, 'session_index': '2'})
        uc, error = self.worker._try_account_recovery(ExplodingDriver(), '28', UC, ROUTING_ERROR)
        self.assertEqual((uc, error), (UC, None))
        self.assertEqual(self.worker._ACTIVE_AU.get(UC), '2')

    def test_a_refused_lease_means_no_sign_in_at_all(self):
        self.unsupervised()
        self.fake_coordinator(granted=False)
        self.stub_recovery(AssertionError('recovery must not run without a lease'))
        result = self.worker._try_account_recovery(ExplodingDriver(), '28', UC, ROUTING_ERROR)
        self.assertEqual(result, (None, ROUTING_ERROR))

    def test_auto_auth_without_phase_ack_is_not_manager_supervision(self):
        self.unsupervised()
        os.environ['VIDEO_UPLOAD_AUTO_AUTH'] = '1'
        self.assertFalse(self.worker._recovery_manager_supervised())

    def test_ownership_cannot_be_proved_without_the_peer_machine(self):
        os.environ.pop('VIDEO_UPLOAD_PEER_ADSPOWER', None)
        with self.assertRaises(RuntimeError) as error:
            self.worker._recovery_ownership_check('28')
        self.assertIn('PEER', str(error.exception))

    def test_successful_recovery_records_only_a_proven_account_number(self):
        self.supervised()
        self.stub_recovery({'recovered': True, 'channel_uc': UC, 'session_index': '1'})
        result = self.worker._try_account_recovery(ExplodingDriver(), '28', UC, ROUTING_ERROR)
        self.assertEqual(result, (UC, None))
        self.assertEqual(self.worker._ACTIVE_AU.get(UC), '1')
        self.assertEqual(self.seen['channel_uc'], UC)
        self.assertIs(self.seen['phase_before_upload'], True)

    def test_recovery_without_a_proven_number_records_nothing(self):
        self.supervised()
        self.stub_recovery({'recovered': True, 'channel_uc': UC, 'session_index': ''})
        result = self.worker._try_account_recovery(ExplodingDriver(), '28', UC, ROUTING_ERROR)
        self.assertEqual(result, (UC, None))
        self.assertNotIn(UC, self.worker._ACTIVE_AU)

    def test_owner_help_replaces_the_error_and_never_claims_success(self):
        self.supervised()
        recovery = self.worker.ACCOUNT_RECOVERY
        self.stub_recovery(recovery.OwnerHelpNeeded('credential rejected; no automatic retry'))
        uc, error = self.worker._try_account_recovery(ExplodingDriver(), '28', UC, ROUTING_ERROR)
        self.assertIsNone(uc)
        self.assertIn('owner', error)
        self.assertNotIn(UC, self.worker._ACTIVE_AU)

    def test_unexpected_failure_keeps_the_original_error_and_leaks_nothing(self):
        self.supervised()
        self.stub_recovery(RuntimeError('boom with /secret/path and value'))
        uc, error = self.worker._try_account_recovery(ExplodingDriver(), '28', UC, ROUTING_ERROR)
        self.assertIsNone(uc)
        # The operator must still see what actually went wrong with the routing.
        self.assertIn(ROUTING_ERROR, error)
        self.assertIn('RuntimeError', error)
        self.assertNotIn('secret', error)

    def test_no_result_preserves_the_original_routing_error(self):
        self.supervised()
        self.stub_recovery(None)
        result = self.worker._try_account_recovery(ExplodingDriver(), '28', UC, ROUTING_ERROR)
        self.assertEqual(result, (None, ROUTING_ERROR))

    def test_phase_gate_closes_once_the_file_hand_off_started(self):
        self.worker._advance_upload_phase('profile_opened')
        self.assertTrue(self.worker._recovery_phase_allows_login())
        self.worker._advance_upload_phase('upload_started')
        self.assertFalse(self.worker._recovery_phase_allows_login())

    def test_after_hand_off_the_hook_refuses_regardless_of_supervision(self):
        self.supervised()
        self.stub_recovery(AssertionError('must never run after hand-off'))
        self.worker._advance_upload_phase('upload_started')
        result = self.worker._try_account_recovery(ExplodingDriver(), '28', UC, ROUTING_ERROR)
        self.assertEqual(result, (None, ROUTING_ERROR))

    def test_manager_identity_is_fixed_at_worker_start_not_at_hook_time(self):
        # An orphaned worker (parent reaped to init) must not be able to claim
        # that "its manager" is alive — the baseline is captured at import.
        self.assertEqual(self.worker._RECOVERY_PARENT_PID_AT_START, os.getppid())
        self.assertTrue(self.worker._recovery_manager_alive())
        saved = self.worker._RECOVERY_PARENT_PID_AT_START
        self.worker._RECOVERY_PARENT_PID_AT_START = 1
        try:
            self.assertFalse(self.worker._recovery_manager_alive())
        finally:
            self.worker._RECOVERY_PARENT_PID_AT_START = saved


if __name__ == '__main__':
    unittest.main()
