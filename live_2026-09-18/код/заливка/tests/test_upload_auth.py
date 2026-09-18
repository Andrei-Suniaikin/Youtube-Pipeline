"""Offline auth protocol, real child pipes, and deterministic auth state transitions."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import upload_auth as auth
import upload_manager as manager

UC, TARGET, POPUP, VIDEO = 'UC1234567890123456789012', 'A' * 32, 'B' * 32, 'aBcDeFg_123'


def request(**changes):
    return dict({'schema_version': 1, 'request_id': 'a' * 32, 'profile': '21', 'channel_uc': UC,
                 'target_id': TARGET, 'phase': 'upload_started', 'video_id': '',
                 'stage': 'before_thumbnail', 'frontend_id': 'fixture:1'}, **changes)


def original(**changes):
    return dict({'origin': 'https://studio.youtube.com', 'path': '/channel/' + UC + '/videos/upload',
                 'channel_uc': UC, 'auth': True, 'wizard_present': True,
                 'frontend_ids': ['fixture:1'], 'video_ids': [], 'identity_ambiguous': False}, **changes)


class Lease:
    def __init__(self):
        self.lost_event = threading.Event()
        self.lost_error = ''
    def assert_held(self):
        if self.lost_event.is_set():
            raise RuntimeError('Fixture lease lost')


class ProtocolTest(unittest.TestCase):
    def test_request_ack_roundtrip_and_different_phase_ack_rejected(self):
        item = request()
        self.assertEqual(auth.decode_request(auth.encode_request(item)), item)
        ack = auth.encode_ack(item, VIDEO)
        self.assertEqual(auth.decode_ack(ack, item)['video_id'], VIDEO)
        with self.assertRaises(auth.AuthBlocked):
            auth.decode_ack('__UPLOAD_PHASE_ACK__|{}', item)

    def test_unknown_duplicate_secret_and_mismatched_fields_are_redacted(self):
        for line in (auth.REQUEST + '{"secret":"FICTIONAL_SECRET"}',
                     auth.REQUEST + '{"schema_version":1,"schema_version":1}',
                     auth.encode_request(request()).replace('"profile":"21"', '"profile":true')):
            with self.subTest(line=line), self.assertRaises(auth.AuthBlocked) as error:
                auth.decode_request(line)
            self.assertNotIn('FICTIONAL_SECRET', str(error.exception))
        item = request(phase='video_id_known', video_id=VIDEO)
        with self.assertRaises(auth.AuthBlocked):
            auth.encode_ack(item, 'zYxWvUt_987')
        with self.assertRaises(auth.AuthBlocked):
            auth.decode_ack(auth.encode_ack(request(), VIDEO), request(request_id='b' * 32))

    def test_pipe_eof_timeout_partial_and_duplicate_ack_fail_closed(self):
        for payload, close in ((b'', True), (b'', False), (b'partial', False),
                               ((auth.encode_ack(request(), '') + '\n') .encode() * 2, False)):
            r, w = os.pipe()
            with os.fdopen(r, 'r') as stream:
                if payload:
                    os.write(w, payload)
                if close:
                    os.close(w)
                try:
                    with self.assertRaises(auth.AuthBlocked):
                        auth.wait_ack(request(), stream, timeout=0.01)
                finally:
                    if not close:
                        os.close(w)

    def test_manager_argv_binding_is_exact_not_claimed_by_worker(self):
        self.assertEqual(auth.expected_from_argv(['worker', '--profile', '21', '--channel-url',
                                                  'https://studio.youtube.com/channel/' + UC + '/']),
                         {'profile': '21', 'channel_uc': UC})
        for argv in (['--profile', '21'], ['--profile', '21', '--profile=22', '--channel-url',
                     'https://studio.youtube.com/channel/' + UC + '/']):
            with self.assertRaises(auth.AuthBlocked):
                auth.expected_from_argv(argv)

    def test_original_target_cannot_change_channel_upload_or_known_id(self):
        self.assertEqual(auth.validate_original(original(video_ids=[VIDEO], auth=False), request(), auth_clear=True), VIDEO)
        for changes in ({'channel_uc': 'UC' + 'x' * 22}, {'frontend_ids': ['other']},
                        {'video_ids': [VIDEO, 'zYxWvUt_987']}, {'wizard_present': False}):
            with self.assertRaises(auth.AuthBlocked):
                auth.validate_original(original(**changes), request())
        with self.assertRaises(auth.AuthBlocked):
            auth.validate_original(original(), request(phase='video_id_known', video_id=VIDEO))


class ContextTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 100.0
        self.lease = Lease()
        self.proc = type('Proc', (), {'pid': 987654321, 'poll': lambda self: None})()
        self.write_stat(self.proc.pid, os.getpid(), '123')
        self.write_stat(os.getpid(), os.getppid(), '456')
        self.context = auth.Context(request(), {'profile': '21', 'channel_uc': UC},
                                    {'phase': 'upload_started', 'video_id': ''}, self.proc, self.lease,
                                    self.root / 'evidence', clock=lambda: self.now,
                                    identity_reader=lambda pid: auth.process_identity(pid, self.root))

    def write_stat(self, pid, ppid, start, state='S'):
        target = self.root / str(pid)
        target.mkdir(exist_ok=True)
        fields = [state, str(ppid)] + ['0'] * 17 + [start]
        (target / 'stat').write_text(str(pid) + ' (fixture process) ' + ' '.join(fields))

    def test_exact_child_reuse_pause_lease_loss_and_deadline_prevent_action(self):
        self.context.check()
        self.write_stat(self.proc.pid, os.getpid(), '124')
        with self.assertRaises(auth.AuthBlocked):
            self.context.check()
        self.write_stat(self.proc.pid, os.getpid(), '123', 'T')
        with self.assertRaises(auth.AuthBlocked):
            self.context.check()
        self.write_stat(self.proc.pid, os.getpid(), '123')
        self.lease.lost_event.set()
        with self.assertRaises(RuntimeError):
            self.context.check()
        self.lease.lost_event.clear()
        self.now += 181
        with self.assertRaises(auth.AuthBlocked):
            self.context.check()

    def test_durable_intent_is_exclusive_and_contains_no_credentials(self):
        self.context.intent('password', POPUP)
        proof = next((self.root / 'evidence').iterdir()).read_text()
        self.assertNotIn('FICTIONAL_SECRET', proof)
        with self.assertRaises(FileExistsError):
            self.context.intent('password', POPUP)


CHILD = r'''
import json,os,sys
sys.path.insert(0,sys.argv[1])
import upload_auth as a, upload_contract as c
item=json.loads(os.environ['FIXTURE_AUTH_REQUEST'])
phase=c.advance_phase_event(c.initial_phase_event(),'upload_started')
print(c.encode_phase_event(phase),flush=True)
c.decode_phase_ack(sys.stdin.readline().strip())
print(a.REQUEST+json.dumps(item),flush=True)
ack=a.wait_ack(item,sys.stdin,timeout=10)
if ack['video_id']: phase=c.advance_phase_event(phase,'video_id_known',ack['video_id'])
result=c.failure_result('fixture preserved',phase,{'visibility':'DRAFT'})
print(c.RESULT_MARKER+json.dumps(result),flush=True)
print('__UPLOAD_ERROR__|fixture preserved',flush=True)
raise SystemExit(1)
'''

WORKER_CHILD = r'''
import json,sys
sys.path.insert(0,sys.argv[1])
import upload_worker as w
import upload_contract as c
uc='UC1234567890123456789012';vid='aBcDeFg_123'
w._reset_upload_phase()
w._UPLOAD_AUTH_CONTEXT={'profile':'21','channel_uc':uc}
w._advance_upload_phase('upload_started')
c.decode_phase_ack(sys.stdin.readline().strip())
class Driver:
 def __init__(self): self.reads=0
 def execute_cdp_cmd(self, method, params):
  assert method=='Target.getTargetInfo'
  return {'targetInfo':{'targetId':'A'*32}}
 def execute_script(self, script):
  self.reads+=1
  return {'origin':'https://studio.youtube.com','path':'/channel/'+uc+'/videos/upload',
    'channel_uc':uc,'auth':self.reads==1,'wizard_present':True,'frontend_ids':['fixture:1'],'identity_ambiguous':False,
    'video_ids':[] if self.reads==1 else [vid]}
w._managed_auth_request(Driver(),stage='before_thumbnail')
result=c.failure_result('fixture preserved',w._current_upload_phase(),{'visibility':'DRAFT'})
print(c.RESULT_MARKER+json.dumps(result),flush=True)
print('__UPLOAD_ERROR__|fixture preserved',flush=True)
raise SystemExit(1)
'''

FRESH_WORKER_CHILD = r'''
import json,sys,os
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import upload_worker as w, upload_contract as c, upload_auth as a
uc='UC1234567890123456789012';vid='aBcDeFg_123';frontend='fixture:1'
w._reset_upload_phase();w._UPLOAD_AUTH_CONTEXT={'profile':'21','channel_uc':uc}
before={'observed_ms':1000,'route_ok':True,'channel_uc':uc,'entity_id':uc,'entity_type':'CHANNEL',
 'wizard_count':1,'opened_count':1,'input_in_wizard':True,'input_file_count':0,'videoId':'',
 'entries':[{'key':'old','videoId':'oldOldOld01','local':False}]}
w._FRESH_UPLOAD_BINDING=w.YS.fresh_upload_baseline(before,uc,'fixture.mp4',1234)
w._advance_upload_phase('upload_started');c.decode_phase_ack(sys.stdin.readline().strip())
after=dict(before,observed_ms=2000,videoId=vid,uploadMode='NEW_UPLOAD',entries=before['entries']+[
 {'key':frontend,'frontendId':frontend,'videoId':vid,'draftVideoId':'','local':True,'filename':'fixture.mp4',
  'fileSelectedTimeMs':1500,'transferStartTimeMs':1600,'media':{'durationMs':600000,'width':1280,'height':720}}])
class Driver:
 def __init__(self):self.original_reads=0
 def execute_cdp_cmd(self,method,params):
  assert method=='Target.getTargetInfo';return {'targetInfo':{'targetId':'A'*32}}
 def execute_script(self,script,*args):
  if script.endswith(w.YS._READ_WIZARD_JS):return {'present':True,'videoId':vid}
  if script.endswith(w.YS._FRESH_UPLOAD_STATE_JS):return after
  assert script=='return '+a.ORIGINAL_JS
  self.original_reads+=1
  return {'origin':'https://studio.youtube.com','path':'/channel/'+uc+'/videos/upload','channel_uc':uc,
   'auth':self.original_reads==1,'wizard_present':True,'frontend_ids':[frontend],
   'video_ids':[] if self.original_reads==1 else [vid],'identity_ambiguous':False}
w._managed_auth_request(Driver(),stage='before_thumbnail')
assert w._FRESH_UPLOAD_BINDING['frontend_id']==frontend and w._FRESH_UPLOAD_BINDING['video_id']==vid
proof=Path(os.path.splitext(os.environ['VIDEO_UPLOAD_THUMBNAIL_PROOF_PATH'])[0]+'.fresh_bound.json')
bound=json.loads(proof.read_text());assert bound['binding']['video_id']==vid
assert bound['studio_state']['videoId']==vid and bound['event']=='fresh_upload_identity_bound'
print('FRESH_ACK_PINNED_WITHOUT_FILE_HANDOFF',flush=True)
result=c.failure_result('fixture preserved',w._current_upload_phase(),{'visibility':'DRAFT'})
print(c.RESULT_MARKER+json.dumps(result),flush=True)
print('__UPLOAD_ERROR__|fixture preserved',flush=True)
raise SystemExit(1)
'''


class ManagerPipeTest(unittest.TestCase):
    def invoke(self, item, callback, *, lease=True, durable=True, child=CHILD, auth_wave=None, argv_extra=()):
        with tempfile.TemporaryDirectory() as directory:
            events, logs = [], []
            def phase(value):
                auth.save_new(Path(directory) / ('phase_' + str(len(events)) + '.json'), value)
                events.append(dict(value))
            env = dict(os.environ, VIDEO_UPLOAD_AUTO_AUTH='1',
                       VIDEO_UPLOAD_THUMBNAIL_PROOF_PATH=str(Path(directory)/'thumbnail.json'),
                       VIDEO_UPLOAD_AUTH_EVIDENCE_DIR=directory, FIXTURE_AUTH_REQUEST=json.dumps(item))
            argv = [sys.executable, '-c', child, str(ROOT / 'src'), '--profile', '21',
                    '--channel-url', 'https://studio.youtube.com/channel/' + UC + '/']
            if auth_wave is not None:
                argv.extend(['--visibility', auth_wave['visibility'].lower()])
            argv.extend(argv_extra)
            with mock.patch.object(auth, 'SLOT_ROOT', Path(directory) / 'slots'):
                result = manager._invoke_worker(argv, env, logs.append, lease=Lease() if lease else None,
                                                phase_callback=phase if durable else None,
                                                auth_callback=lambda ctx: callback(ctx, events), auth_wave=auth_wave)
            return result, events, logs

    def test_new_id_is_durable_before_auth_ack_and_no_second_file_handoff(self):
        def callback(context, events):
            context.check()
            self.assertEqual(events[-1]['phase'], 'upload_started')
            return VIDEO
        result, events, _ = self.invoke(request(), callback)
        self.assertEqual(events[-1]['video_id'], VIDEO)
        self.assertEqual(result['video_id'], VIDEO)
        self.assertTrue(result['do_not_reupload'])
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'fixture preserved')

    def test_real_worker_request_helper_resumes_same_target_with_durable_id(self):
        result, events, logs = self.invoke(request(), lambda ctx, events: VIDEO, child=WORKER_CHILD)
        self.assertEqual(result['error'], 'fixture preserved')
        self.assertEqual(result['video_id'], VIDEO)
        self.assertTrue(result['do_not_reupload'])
        self.assertEqual(events[-1]['phase'], 'video_id_known')
        self.assertEqual(sum('worker remains paused' in line for line in logs), 1)

    def test_pre_id_fresh_worker_persists_identity_before_adopting_auth_ack(self):
        result,events,logs=self.invoke(request(),lambda ctx,events:VIDEO,child=FRESH_WORKER_CHILD)
        self.assertEqual(result['error'],'fixture preserved')
        self.assertEqual(result['video_id'],VIDEO)
        self.assertTrue(result['do_not_reupload'])
        transitions=[e['phase'] for i,e in enumerate(events) if i==0 or events[i-1]['phase']!=e['phase']]
        self.assertEqual(transitions,['upload_started','video_id_known'])
        self.assertTrue(any('FRESH_ACK_PINNED_WITHOUT_FILE_HANDOFF' in line for line in logs))

    def test_no_actual_lease_or_durable_callback_does_not_launch_auth(self):
        def forbidden(*_):
            self.fail('Callback must not execute')
        for lease, durable in ((False, True), (True, False)):
            result, _, _ = self.invoke(request(), forbidden, lease=lease, durable=durable)
            self.assertFalse(result['success'])
            self.assertEqual(result['phase'], 'preflight')

    def test_untrusted_request_and_callback_errors_never_expose_secret_or_ack(self):
        for item in (request(profile='22'), dict(request(), password='FICTIONAL_SECRET')):
            result, _, logs = self.invoke(item, lambda *_: self.fail('Callback must not execute'))
            self.assertTrue(result['do_not_reupload'])
            self.assertNotIn('FICTIONAL_SECRET', json.dumps([result, logs]))
        def failed(*_):
            raise RuntimeError('FICTIONAL_SECRET')
        result, _, logs = self.invoke(request(), failed)
        self.assertTrue(result['do_not_reupload'])
        self.assertNotIn('FICTIONAL_SECRET', json.dumps([result, logs]))

    def test_dead_worker_stops_callback_before_ack(self):
        def stopped(context, _events):
            context.proc.kill()
            context.proc.wait()
            context.check()
            self.fail('A killed worker must stop the callback')
        result, _, _ = self.invoke(request(), stopped)
        self.assertTrue(result['do_not_reupload'])
        self.assertFalse(result['success'])


class FlowTest(ContextTest):
    def browser(self, *, unsupported=False, stay_password=False):
        spec = importlib.util.spec_from_file_location('auth_reviewed_fixture', ROOT / 'research/ww2_upload_stress_20260914/auth_browser.py')
        reviewed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reviewed)
        parent = self
        class FakeBrowser:
            def __init__(self):
                self.reviewed = reviewed
                self.index, self.focused, self.empty = 0, False, True
                self.inputs = []
            def original(self, clear=False):
                return original(auth=self.index < 4, video_ids=[VIDEO] if self.index == 4 else [])
            def activate(self, target):
                parent.context.check()
            def pages(self):
                paths = ('', '/v3/signin/accountchooser', '/v3/signin/challenge/pwd', '/v3/signin/challenge/totp')
                if self.index in (0, 4): return []
                path = '/v3/signin/challenge/phone' if unsupported else paths[self.index]
                return [{'targetId': POPUP, 'openerId': TARGET, 'url': 'https://accounts.google.com' + path}]
            def evaluate(self, target, expression):
                if expression == reviewed.READ_DOM: return {'signals': {}}
                if self.index < 2: return {'x': 1, 'y': 1}
                return {'input': {'x': 2, 'y': 2}, 'next': {'x': 3, 'y': 3},
                        'empty': self.empty, 'focused': self.focused, 'account_matches': True}
            def click(self, target, point, expression, condition):
                parent.context.check()
                if not condition(self.evaluate(target, expression)): raise auth.AuthBlocked('Fixture form changed')
                if self.index < 2:
                    self.index += 1
                elif point['x'] == 2:
                    self.focused = True
                elif not (stay_password and self.index == 2):
                    self.index += 1
                    self.focused, self.empty = False, True
            def insert(self, target, text, expression, *, before_insert=None):
                parent.context.check()
                if before_insert is not None:
                    before_insert()
                self.inputs.append(('password' if self.index == 2 else 'totp', len(text)))
                self.empty = False
        return FakeBrowser()

    def run_flow(self, browser):
        credentials = type('Credentials', (), {'login': 'fixture@example.test', 'password': 'FICTIONAL_SECRET', 'totp': 'FIXTURE'})()
        secrets = type('Secrets', (), {'totp_code': staticmethod(lambda *a, **k: ('123456', 30))})()
        def pause(amount): self.now += amount
        # Only the external clock-status subprocess is faked; the flow/guards/protocol run unchanged.
        with mock.patch.object(auth.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'yes\n', '')):
            return auth.run_flow(browser, self.context, credentials, secrets, pause=pause, wall_clock=lambda: 60.0)

    def test_ordered_existing_wizard_auth_one_password_one_totp(self):
        browser = self.browser()
        self.assertEqual(self.run_flow(browser), VIDEO)
        self.assertEqual(browser.inputs, [('password', 16), ('totp', 6)])

    def test_unsupported_factor_and_unchanged_password_do_not_retry(self):
        browser = self.browser(unsupported=True)
        with self.assertRaises(auth.AuthBlocked): self.run_flow(browser)
        self.assertEqual(browser.inputs, [])
        # A separate target avoids reusing the exclusive Next intent from the first run.
        with tempfile.TemporaryDirectory() as evidence:
            self.context.evidence_dir = Path(evidence)
            browser = self.browser(stay_password=True)
            with self.assertRaises(auth.AuthBlocked): self.run_flow(browser)
            self.assertEqual(browser.inputs, [('password', 16)])


class BrowserIOTest(ContextTest):
    """Real Browser guard/native click, with only HTTP/CDP/process IO fixtures."""
    def browser(self, *, bad_code=None, delayed_clear=False, challenge='pwd', get_hook=None):
        spec = importlib.util.spec_from_file_location('auth_browser_io_fixture', ROOT / 'research/ww2_upload_stress_20260914/auth_browser.py')
        reviewed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reviewed)
        parent = self
        class Raw:
            def __init__(self, context, port):
                self.counter, self.focused, self.empty, self.done = 0, False, True, False
                self.events, self.pending = [], []
                self.auth_tail = 2 if delayed_clear else 0
                self.ws = self
                self.flood = False
            def settimeout(self, timeout): self.timeout = timeout
            def close(self): pass
            def send(self, data):
                request = json.loads(data)
                method, params = request['method'], request['params']
                self.events.append(method)
                if method == 'Target.attachToTarget':
                    result = {'sessionId': params['targetId']}
                elif method == 'Target.getTargets':
                    result = {'targetInfos': [{'type': 'page', 'targetId': TARGET,
                                               'url': 'https://studio.youtube.com/channel/' + UC + '/videos/upload'}]}
                    if not self.done:
                        result['targetInfos'].append({'type': 'page', 'targetId': POPUP, 'openerId': TARGET,
                                                      'url': 'https://accounts.google.com/v3/signin/challenge/' + challenge})
                elif method == 'Runtime.evaluate':
                    if params['expression'] == auth.ORIGINAL_JS:
                        value = original(auth=not self.done or self.auth_tail > 0,
                                         video_ids=[VIDEO] if self.done else [])
                        if self.done and self.auth_tail > 0:
                            self.auth_tail -= 1
                    elif params['expression'] == reviewed.READ_DOM:
                        value = {'signals': {}}
                    else:
                        value = {'input': {'x': 2, 'y': 2}, 'next': {'x': 3, 'y': 3},
                                 'focused': self.focused, 'empty': self.empty, 'account_matches': True}
                    result = {'result': {'value': value}}
                elif method == 'Input.insertText':
                    parent.assertTrue(self.focused)
                    parent.assertTrue(self.empty)
                    self.empty = False
                    result = {}
                elif method == 'Input.dispatchMouseEvent':
                    if params['type'] == 'mousePressed':
                        self.focused = params['x'] == 2  # Actual Next mouseDown steals input focus.
                    if params['type'] == 'mouseReleased' and params['x'] == 3:
                        parent.assertFalse(self.focused)
                        self.done = True
                    result = {}
                elif method == 'Target.activateTarget':
                    result = {}
                else:
                    raise AssertionError('Unexpected browser operation')
                self.pending.append(json.dumps({'id': request['id'], 'result': result}))
            def recv(self):
                if self.flood:
                    parent.now += 181
                    return json.dumps({'method': 'unrelated.event'})
                return self.pending.pop(0)
        class Collector:
            @staticmethod
            def profile_index(api): return {'by_serial': {'21': 'fixture_uid'}, 'by_uid': {'fixture_uid': '21'}}
            @staticmethod
            def uid_of(profile, index): return index['by_serial'][profile]
            @staticmethod
            def _requests(): return Collector
            @staticmethod
            def get(url, **kwargs):
                if get_hook is not None:
                    get_hook()
                entries = [{'user_id': 'fixture_uid', 'debug_port': '9222'}] if 'localhost' in url else []
                return type('Response', (), {'status_code': 200, 'json': lambda self: {
                    'code': 0 if bad_code is None else bad_code, 'data': {'list': entries}}})()
        reviewed.browser_identity = lambda *a, **k: {'pid': 1, 'start_ticks': 'fixture'}
        credentials = type('Credentials', (), {'login': 'fixture@example.test', 'password': 'FICTIONAL_SECRET',
                                                'totp': 'FIXTURE'})()
        return auth.Browser(self.context, reviewed, Collector, credentials, connect=Raw), credentials

    def run_totp(self, browser, credentials, issued, *, wall_offset=-35, pauses=None):
        def code(secret, *, timestamp):
            issued.append(timestamp)
            return '123456', 30 - timestamp % 30
        def pause(amount):
            self.now += amount
            if pauses is not None:
                pauses.append(amount)
        secrets = type('Secrets', (), {'totp_code': staticmethod(code)})()
        with mock.patch.object(auth.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'yes\n', '')):
            return auth.run_flow(browser, self.context, credentials, secrets,
                                 pause=pause, wall_clock=lambda: self.now + wall_offset)

    def test_totp_generation_twenty_five_second_boundary(self):
        for offset, must_wait in ((-35, False), (-34.999, True)):
            with self.subTest(must_wait=must_wait):
                self.now = 100.0
                self.context.evidence_dir = self.root / ('generation_' + str(must_wait))
                browser, credentials = self.browser(challenge='totp')
                issued, pauses = [], []
                self.assertEqual(self.run_totp(browser, credentials, issued, wall_offset=offset, pauses=pauses), VIDEO)
                self.assertEqual(len(issued), 1)
                self.assertGreaterEqual(30 - issued[0] % 30, 25)
                self.assertEqual(bool(pauses), must_wait)
                if must_wait:
                    self.assertGreaterEqual(issued[0], 90)
                else:
                    self.assertEqual(issued[0], 65)
                self.assertEqual(browser.raw.events.count('Input.insertText'), 1)

    def test_totp_insert_rechecks_fifteen_second_boundary_after_real_guard(self):
        for elapsed, allowed in ((10.0, True), (10.001, False), (30.0, False)):
            with self.subTest(elapsed=elapsed):
                self.now = 100.0
                self.context.evidence_dir = self.root / ('insert_' + str(elapsed))
                issued, delayed = [], []
                def slow_get():
                    if issued and not delayed:
                        self.now += elapsed  # The real post-generation guard consumes validity.
                        delayed.append(True)
                browser, credentials = self.browser(challenge='totp', get_hook=slow_get)
                output = io.StringIO()
                with redirect_stdout(output):
                    if allowed:
                        self.assertEqual(self.run_totp(browser, credentials, issued), VIDEO)
                    else:
                        with self.assertRaises(auth.AuthBlocked) as error:
                            self.run_totp(browser, credentials, issued)
                        self.assertNotIn('123456', str(error.exception))
                        self.assertNotIn('FIXTURE', str(error.exception))
                self.assertEqual(len(issued), 1)
                self.assertTrue(delayed)
                self.assertEqual(browser.raw.events.count('Input.insertText'), int(allowed))
                self.assertEqual(output.getvalue(), '')

    def test_totp_late_submit_stops_without_second_insert(self):
        issued, delayed = [], []
        holder = []
        def slow_get():
            if holder and 'Input.insertText' in holder[0].raw.events and not delayed:
                self.now += 20.001  # 25s at generation, now below the 5s submit floor.
                delayed.append(True)
        browser, credentials = self.browser(challenge='totp', get_hook=slow_get)
        holder.append(browser)
        with self.assertRaises(auth.AuthBlocked):
            self.run_totp(browser, credentials, issued)
        self.assertEqual(len(issued), 1)
        self.assertEqual(browser.raw.events.count('Input.insertText'), 1)
        self.assertEqual(browser.raw.events.count('Input.dispatchMouseEvent'), 3)  # Focus only; no submit.

    def test_native_next_focus_change_still_releases_and_submits_once(self):
        browser, credentials = self.browser()
        self.assertEqual(auth.run_flow(browser, self.context, credentials, None), VIDEO)
        self.assertEqual(browser.raw.events.count('Input.insertText'), 1)
        self.assertEqual(browser.raw.events.count('Input.dispatchMouseEvent'), 6)
        self.assertEqual(browser.raw.events.count('Target.activateTarget'), 1)

    def test_event_flood_cannot_extend_callback_deadline(self):
        browser, _ = self.browser()
        browser.raw.flood = True
        with self.assertRaises(auth.AuthBlocked):
            browser.pages()
        self.assertNotIn('Input.insertText', browser.raw.events)

    def test_owned_password_popup_closure_waits_for_auth_clear_without_second_next(self):
        browser, credentials = self.browser(delayed_clear=True)
        self.assertEqual(auth.run_flow(browser, self.context, credentials, None), VIDEO)
        self.assertEqual(browser.raw.events.count('Input.insertText'), 1)
        self.assertEqual(browser.raw.events.count('Input.dispatchMouseEvent'), 6)
        self.assertEqual(browser.raw.events.count('Target.activateTarget'), 1)

    def test_boolean_adspower_success_code_is_rejected(self):
        with self.assertRaises(auth.AuthBlocked):
            self.browser(bad_code=False)


if __name__ == '__main__':
    unittest.main()
