#!/usr/bin/env python3
"""Real VGC worker/journal against local faulting HTTP, no paid calls or function mocks."""
import concurrent.futures
import contextlib
import hashlib
import http.server
import importlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent
CANDIDATE = Path(os.environ.get('VGC_TEST_CANDIDATE', str(HERE / 'worker_candidate2'))).resolve()
SOURCE = HERE.parents[1] / 'src'
sys.path[:0] = [str(CANDIDATE), str(SOURCE)]
import voicegencore_worker as worker
from remote_journal_vgc import VGCJournal, fingerprint, atomic_json

TMP = Path(tempfile.mkdtemp(prefix='vgc-c2-'))
RESULTS = []
LOCK = threading.Lock()
TASKS, CREATES = {}, []
OPTIONS = {}
COUNTS = {'get': 0, 'cancel': 0, 'start': 0, 'download': 0}
subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.5',
                '-c:a', 'libmp3lame', '-b:a', '64k', str(TMP / 'source.mp3')], check=True)
AUDIO = (TMP / 'source.mp3').read_bytes()

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, code, value, content_type='application/json', truncate=False):
        data = json.dumps(value).encode() if not isinstance(value, bytes) else value
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data[:len(data)//2] if truncate else data)
        self.wfile.flush()
        if truncate:
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()

    def do_POST(self):
        if self.path == '/client/tasks':
            data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            with LOCK:
                CREATES.append({'text': data['text'], 'key_sha256': fingerprint(self.headers['X-API-Key'])})
                option = OPTIONS.get(data['text'], {})
                if option.get('reject'):
                    return self.reply(option['reject'], {'detail': 'synthetic rejected'})
                task_id = str(len(TASKS)+1)
                TASKS[task_id] = {'task_id': task_id, 'text': data['text'], 'option': option,
                                 'key_sha256': fingerprint(self.headers['X-API-Key'])}
            if option.get('lost_create'):
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            return self.reply(200, {'task': {'task_id': task_id}})
        if self.path.endswith('/cancel'):
            with LOCK:
                COUNTS['cancel'] += 1
            task = TASKS[self.path.split('/')[3]]
            if task['option'].get('cancel_delay'):
                time.sleep(task['option']['cancel_delay'])
            task['option'].update(status='completed' if task['option'].get('complete_before_cancel') else 'cancelled')
            if task['option'].get('lost_cancel'):
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            return self.reply(200, {'ok': True})
        if self.path == '/client/queue/start':
            with LOCK:
                COUNTS['start'] += 1
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        self.reply(404, {})

    def do_GET(self):
        with LOCK:
            COUNTS['get'] += 1
        if self.path == '/client/me':
            return self.reply(200, {'has_active_subscription': False, 'max_threads': 3, 'free_remaining': OPTIONS.get('quota', 10000000)})
        if self.path == '/client/runtime':
            return self.reply(200, {'accounts': [1], 'queue': {'active': False, 'paused': False, 'waiting': 1}})
        task = TASKS.get(self.path.split('/')[3]) if self.path.startswith('/client/tasks/') else None
        if not task:
            return self.reply(404, {})
        if task['key_sha256'] != fingerprint(self.headers.get('X-API-Key', '')):
            return self.reply(403, {})
        option = task['option']
        if self.path.endswith('/download'):
            with LOCK:
                COUNTS['download'] += 1
            data = b'<html>not audio</html>' if option.get('bad_audio') else AUDIO
            return self.reply(200, data, 'audio/mpeg', option.get('truncate', False))
        if option.get('poll_error'):
            return self.reply(option['poll_error'], {'detail': 'synthetic temporary'})
        status = option.get('status', 'completed')
        return self.reply(200, {'task': {'task_id': task['task_id'], 'status': status, 'progress': 0 if status == 'queued' else 100}})

server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = 'http://127.0.0.1:' + str(server.server_port)
# Only endpoint and timing/retry configuration are adjusted; production functions execute unchanged.
worker.API_BASE = BASE
worker.MAX_RETRIES = 1
worker.POLL_INTERVAL = .02
worker.RETRY_DELAY_SECONDS = .01
worker.MAX_WAIT_TIME_MINUTES = 1
for name in ['VS_REMOTE_JOURNAL_DIR', 'VS_QUEUE_JOB_ID', 'VS_QUEUE_CANCEL_FILE', 'VS_CREDENTIAL_SOURCE']:
    os.environ.pop(name, None)

def project(name):
    root = TMP / name
    (root / worker.AUDIO_DIR_NAME).mkdir(parents=True)
    return root

def journal(root, text, key='test-A', filename='1.mp3'):
    return VGCJournal(root / worker.AUDIO_DIR_NAME, filename, key, text, 'voice-test', 'elevenLabsV3', .5, worker.TTS_SPEED)

def run(root, text, key='test-A', filename='1.mp3'):
    return worker.process_text(key, text, 'voice-test', 'elevenLabsV3', .5,
                               str(root / worker.AUDIO_DIR_NAME), filename, 1)

def ncreate(text):
    return sum(item['text'] == text for item in CREATES)

def pin(root):
    return json.loads((root / '.remote_jobs/voicegencore-standalone/credential_pin.json').read_text())

def record(root, text, key='test-A', filename='1.mp3'):
    j = journal(root, text, key, filename)
    return j.load()

def case(name, fn):
    started = time.monotonic()
    fn()
    RESULTS.append({'case': name, 'passed': True, 'seconds': round(time.monotonic()-started, 3)})
    print('PASS', name, flush=True)

def normal():
    p = project('normal')
    assert run(p, 'normal') is True
    r = record(p, 'normal')
    assert r['state'] == 'completed' and r['sha256'] == hashlib.sha256(AUDIO).hexdigest()
    assert pin(p)['state'] == 'released'
    before = COUNTS.copy()
    assert run(p, 'normal', 'test-B') is True
    assert ncreate('normal') == 1 and COUNTS == before
    assert 'test-A' not in json.dumps(r) and 'normal' not in json.dumps(r['identity'])

def resume_poll():
    p = project('poll')
    OPTIONS['poll'] = {'poll_error': 500}
    assert run(p, 'poll') is False
    r = record(p, 'poll')
    assert r['task_id'] and pin(p)['state'] == 'active'
    before = COUNTS.copy()
    assert run(p, 'poll', 'test-B') is False
    assert COUNTS == before and ncreate('poll') == 1
    OPTIONS['poll'].clear()
    assert run(p, 'poll') is True and ncreate('poll') == 1

def uncertain():
    p = project('uncertain')
    OPTIONS['uncertain'] = {'lost_create': True}
    assert run(p, 'uncertain') is False
    assert record(p, 'uncertain')['state'] == 'uncertain' and pin(p)['state'] == 'uncertain'
    assert run(p, 'uncertain') is False and run(p, 'uncertain', 'test-B') is False
    assert ncreate('uncertain') == 1

def rejected():
    p = project('reject')
    OPTIONS['reject'] = {'reject': 429}
    assert run(p, 'reject') is False and pin(p)['state'] == 'released'
    OPTIONS['reject'].clear()
    assert run(p, 'reject', 'test-B') is True and ncreate('reject') == 2

def bounded_create():
    p = project('bounded-create')
    OPTIONS['bounded-create'] = {'status': 'failed'}
    for _ in range(5):
        assert run(p, 'bounded-create') is False
    assert ncreate('bounded-create') == 3 and pin(p)['state'] == 'released'

def interrupted_download():
    p = project('download')
    out = p / worker.AUDIO_DIR_NAME / '1.mp3'
    out.write_bytes(b'previous-output-kept')
    OPTIONS['download'] = {'truncate': True}
    assert run(p, 'download') is False
    assert out.read_bytes() == b'previous-output-kept' and not list(out.parent.glob('*.part-*'))
    OPTIONS['download'].clear()
    assert run(p, 'download') is True and ncreate('download') == 1 and out.read_bytes() == AUDIO

def bad_audio():
    p = project('bad-audio')
    OPTIONS['bad-audio'] = {'bad_audio': True}
    assert run(p, 'bad-audio') is False
    assert not (p / worker.AUDIO_DIR_NAME / '1.mp3').exists()
    OPTIONS['bad-audio'].clear()
    assert run(p, 'bad-audio') is True and ncreate('bad-audio') == 1

def timeout_then_cancel():
    p = project('timeout')
    OPTIONS['timeout'] = {'status': 'queued'}
    before = COUNTS['cancel']
    worker.MAX_WAIT_TIME_MINUTES = .002
    try:
        assert run(p, 'timeout') is False
    finally:
        worker.MAX_WAIT_TIME_MINUTES = 1
    assert record(p, 'timeout')['state'] == 'pending' and COUNTS['cancel'] == before
    cancel_file = p / 'cancel.requested'
    cancel_file.touch()
    os.environ['VS_QUEUE_CANCEL_FILE'] = str(cancel_file)
    try:
        assert run(p, 'timeout') is False
        assert run(p, 'timeout') is False
        assert COUNTS['cancel'] == before+1 and ncreate('timeout') == 1
    finally:
        os.environ.pop('VS_QUEUE_CANCEL_FILE', None)
    # Server-authorized manual retry clears its marker only after a confirmed
    # terminal cancellation; the new request is bounded by persisted attempts.
    OPTIONS['timeout'].clear()
    assert run(p, 'timeout') is True and COUNTS['cancel'] == before+1 and ncreate('timeout') == 2

def queue_start_once():
    p = project('queue-start')
    OPTIONS['queue-start'] = {'status': 'queued'}
    before = COUNTS['start']
    worker.MAX_WAIT_TIME_MINUTES = .002
    try:
        assert run(p, 'queue-start') is False
        assert record(p, 'queue-start')['queue_start_attempted'] is True
        assert run(p, 'queue-start') is False
    finally:
        worker.MAX_WAIT_TIME_MINUTES = 1
    assert COUNTS['start'] == before+1 and ncreate('queue-start') == 1
    OPTIONS['queue-start'].clear()
    assert run(p, 'queue-start') is True and ncreate('queue-start') == 1

def corrupt_completed():
    p = project('corrupt')
    assert run(p, 'corrupt') is True
    out = p / worker.AUDIO_DIR_NAME / '1.mp3'
    out.write_bytes(b'corrupt')
    before = COUNTS.copy()
    assert run(p, 'corrupt', 'test-B') is False and COUNTS == before
    assert pin(p)['state'] == 'active' and pin(p)['sha256'] == fingerprint('test-A')
    assert run(p, 'corrupt') is True and ncreate('corrupt') == 1

def changed_identity():
    p = project('changed')
    assert run(p, 'changed') is True
    assert run(p, 'changed-v2') is False and ncreate('changed-v2') == 0

def source_pin():
    p = project('source')
    OPTIONS['source'] = {'poll_error': 500}
    assert run(p, 'source') is False
    before = COUNTS.copy()
    os.environ['VS_CREDENTIAL_SOURCE'] = 'server'
    try:
        assert run(p, 'source') is False and COUNTS == before
    finally:
        os.environ.pop('VS_CREDENTIAL_SOURCE', None)

def scope():
    p = project('scope')
    os.environ.update(VS_QUEUE_JOB_ID='123', VS_REMOTE_JOURNAL_DIR=str(p / '.remote_jobs/123'))
    try:
        assert run(p, 'scope') is True
        assert (p / '.remote_jobs/123/credential_pin.json').exists()
        os.environ['VS_REMOTE_JOURNAL_DIR'] = str(p / 'wrong/123')
        assert run(p, 'scope-new') is False and ncreate('scope-new') == 0
    finally:
        os.environ.pop('VS_QUEUE_JOB_ID', None)
        os.environ.pop('VS_REMOTE_JOURNAL_DIR', None)

def filesystem_failure():
    p = project('fs')
    (p / '.remote_jobs').write_text('controlled failure: file instead of directory')
    assert run(p, 'fs') is False and ncreate('fs') == 0
    (p / '.remote_jobs').unlink()
    assert run(p, 'fs') is True and ncreate('fs') == 1

def output_lock():
    p = project('lock')
    j = journal(p, 'lock')
    with j.lock():
        assert run(p, 'lock') is False and ncreate('lock') == 0
    assert run(p, 'lock') is True

def switched_key_after_cached_assets():
    p = project('asset-rotation')
    for i in range(1, 11):
        assert run(p, 'asset-' + str(i), filename=str(i)+'.mp3') is True
    before = len(CREATES)
    for i in range(1, 11):
        assert run(p, 'asset-' + str(i), 'test-B', str(i)+'.mp3') is True
    assert len(CREATES) == before
    assert run(p, 'asset-11', 'test-B', '11.mp3') is True
    assert pin(p)['state'] == 'released' and pin(p)['sha256'] == fingerprint('test-B')

def pin_only_crash():
    p = project('pin-only')
    root = p / '.remote_jobs/voicegencore-standalone'
    atomic_json(root / 'credential_pin.json', {'family': 'voicegencore', 'sha256': fingerprint('test-A'), 'source': 'byok', 'state': 'uncertain'})
    assert run(p, 'pin-only') is False and ncreate('pin-only') == 0

def accepted_record_before_pin_crash():
    p = project('accepted-before-pin')
    OPTIONS['accepted-before-pin'] = {'poll_error': 500}
    assert run(p, 'accepted-before-pin') is False
    root = p / '.remote_jobs/voicegencore-standalone'
    atomic_json(root / 'credential_pin.json', {'family': 'voicegencore', 'sha256': fingerprint('test-A'), 'source': 'byok', 'state': 'uncertain'})
    OPTIONS['accepted-before-pin'].clear()
    assert run(p, 'accepted-before-pin') is True and ncreate('accepted-before-pin') == 1

def malformed_journal():
    p = project('malformed')
    OPTIONS['malformed'] = {'poll_error': 500}
    assert run(p, 'malformed') is False
    j = journal(p, 'malformed')
    r = j.load()
    r['task_id'] = None
    atomic_json(j.path, r)
    assert run(p, 'malformed') is False and ncreate('malformed') == 1
    atomic_json(j.root / 'credential_pin.json', [])
    assert run(p, 'malformed') is False and ncreate('malformed') == 1

def invalid_speed_no_post():
    p = project('invalid-speed')
    worker.TTS_SPEED = float('nan')
    try:
        assert run(p, 'invalid-speed') is False and ncreate('invalid-speed') == 0
    finally:
        worker.TTS_SPEED = 1.0

def strict_speed():
    p = project('speed')
    worker.TTS_SPEED = .8
    try:
        assert run(p, 'speed') is True
        out = p / worker.AUDIO_DIR_NAME / '1.mp3'
        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(out)], capture_output=True, text=True, check=True)
        assert float(json.loads(probe.stdout)['format']['duration']) > .60
    finally:
        worker.TTS_SPEED = 1.0

CHILD = """import os,sys
sys.path[:0]=[os.environ['TEST_CANDIDATE'],os.environ['TEST_SOURCE']]
import voicegencore_worker as w
w.API_BASE=os.environ['TEST_API'];w.POLL_INTERVAL=.02;w.MAX_WAIT_TIME_MINUTES=1;w.MAX_RETRIES=1
if os.environ.get('TEST_MAIN'):
    raise SystemExit(w.main())
raise SystemExit(0 if w.process_text('test-A',os.environ['TEST_TEXT'],'voice-test','elevenLabsV3',.5,os.environ['TEST_OUTPUT'],'1.mp3',1) else 2)
"""

def child_env(p, text):
    return dict(os.environ, TEST_CANDIDATE=str(CANDIDATE), TEST_SOURCE=str(SOURCE), TEST_API=BASE,
                TEST_TEXT=text, TEST_OUTPUT=str(p / worker.AUDIO_DIR_NAME))

def kill_resume():
    p = project('kill')
    OPTIONS['kill'] = {'status': 'processing'}
    env = child_env(p, 'kill')
    before_cancel = COUNTS['cancel']
    with open(TMP / 'kill.log', 'w') as log:
        child = subprocess.Popen([sys.executable, '-c', CHILD], env=env, stdout=log, stderr=log)
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            paths = list((p / '.remote_jobs/voicegencore-standalone').glob('request-*.json'))
            if paths and json.loads(paths[0].read_text()).get('task_id'):
                break
            assert child.poll() is None
            time.sleep(.02)
        else:
            child.kill(); child.wait(); raise AssertionError('child did not persist remote ID')
        child.kill(); child.wait(timeout=5)
        assert COUNTS['cancel'] == before_cancel
        OPTIONS['kill'].clear()
        resumed = subprocess.run([sys.executable, '-c', CHILD], env=env, stdout=log, stderr=log, timeout=15)
    assert resumed.returncode == 0 and ncreate('kill') == 1

def full_text_resume_zero_quota():
    p = project('main')
    source = p / 'source.txt'
    source.write_text('cli full text', encoding='utf-8')
    env = dict(child_env(p, 'cli full text'), TEST_MAIN='1', VGC_API_TOKEN='test-A')
    args = [sys.executable, '-c', CHILD, '--source_file', str(source), '--project_dir', str(p), '--full_text', '--voice_id', 'voice-test']
    with open(TMP / 'main.log', 'w') as log:
        first = subprocess.run(args, env=env, stdout=log, stderr=log, timeout=15)
        assert first.returncode == 0
        out = p / worker.AUDIO_DIR_NAME / '1.mp3'
        before = out.stat().st_mtime_ns
        OPTIONS['quota'] = 0
        env['VGC_API_TOKEN'] = 'test-B'
        second = subprocess.run(args, env=env, stdout=log, stderr=log, timeout=15)
        OPTIONS.pop('quota')
    assert second.returncode == 0 and ncreate('cli full text') == 1 and out.stat().st_mtime_ns == before

def hundred_jobs():
    projects = [project('hundred-' + str(i)) for i in range(100)]
    previous_deadline = worker.MAX_WAIT_TIME_MINUTES
    worker.MAX_WAIT_TIME_MINUTES = 1  # Positive batch allows real VPS fsync latency; fault cases retain 120ms.
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(run, p, 'hundred-' + str(i), 'test-' + 'ABC'[i % 3]) for i, p in enumerate(projects)]
            assert all(f.result(timeout=60) is True for f in futures)
    finally:
        worker.MAX_WAIT_TIME_MINUTES = previous_deadline
    assert sum(r['text'].startswith('hundred-') for r in CREATES) == 100
    assert all(pin(p)['state'] == 'released' for p in projects)
    assert len({r['key_sha256'] for r in CREATES if r['text'].startswith('hundred-')}) == 3

def cancel_only_cli():
    p = project('cancel-cli')
    OPTIONS['cancel-cli'] = {'poll_error': 500}
    assert run(p, 'cancel-cli') is False
    OPTIONS['cancel-cli'].clear()
    OPTIONS['cancel-cli'].update(status='processing', lost_cancel=True)
    env = dict(child_env(p, 'cancel-cli'), TEST_MAIN='1', VGC_API_TOKEN='test-A')
    args = [sys.executable, '-c', CHILD, '--project_dir', str(p), '--cancel_pending']
    before = COUNTS['cancel']
    with open(TMP / 'cancel-cli.log', 'w') as log:
        for _ in range(2):
            result = subprocess.run(args, env=env, stdout=log, stderr=log, timeout=5)
            assert result.returncode == 0
    assert COUNTS['cancel'] == before+1 and ncreate('cancel-cli') == 1 and pin(p)['state'] == 'released'

def cancel_unknown_no_create():
    p = project('cancel-unknown')
    OPTIONS['cancel-unknown'] = {'lost_create': True}
    assert run(p, 'cancel-unknown') is False
    before = COUNTS['cancel']
    assert worker.cancel_pending_project(p, 'test-A') == 2
    assert COUNTS['cancel'] == before and ncreate('cancel-unknown') == 1 and pin(p)['state'] == 'uncertain'

def cancel_batch_deadline():
    p = project('cancel-budget')
    OPTIONS['cancel-budget'] = {'poll_error': 500}
    assert run(p, 'cancel-budget') is False
    OPTIONS['cancel-budget'].clear()
    OPTIONS['cancel-budget'].update(status='processing', cancel_delay=.3)
    before = COUNTS['cancel']
    started = time.monotonic()
    assert worker.cancel_pending_project(p, 'test-A', budget_seconds=.1) == 2
    assert time.monotonic()-started < .2 and ncreate('cancel-budget') == 1
    time.sleep(.4)
    assert worker.cancel_pending_project(p, 'test-A') == 0 and COUNTS['cancel'] == before+1

def cancel_orphan_and_terminal_pin():
    p = project('cancel-orphan')
    root = p / '.remote_jobs/voicegencore-standalone'
    atomic_json(root / 'credential_pin.json', {'family': 'voicegencore', 'sha256': fingerprint('test-A'), 'source': 'byok', 'state': 'uncertain'})
    assert worker.cancel_pending_project(p, 'test-A') == 2 and ncreate('cancel-orphan') == 0
    p = project('cancel-terminal-pin')
    assert run(p, 'cancel-terminal-pin') is True
    root = p / '.remote_jobs/voicegencore-standalone'
    atomic_json(root / 'credential_pin.json', {'family': 'voicegencore', 'sha256': fingerprint('test-A'), 'source': 'byok', 'state': 'active'})
    before = COUNTS['cancel']
    assert worker.cancel_pending_project(p, 'test-A') == 0 and pin(p)['state'] == 'released' and COUNTS['cancel'] == before

def late_cancel_completed_resume():
    p = project('late-cancel')
    OPTIONS['late-cancel'] = {'poll_error': 500}
    assert run(p, 'late-cancel') is False
    OPTIONS['late-cancel'].clear()
    OPTIONS['late-cancel'].update(status='completed', complete_before_cancel=True)
    assert worker.cancel_pending_project(p, 'test-A') == 0 and pin(p)['state'] == 'released'
    before = len(CREATES)
    assert run(p, 'late-cancel', 'test-B') is False
    assert pin(p)['state'] == 'active' and pin(p)['sha256'] == fingerprint('test-A')
    assert run(p, 'late-cancel') is True and len(CREATES) == before and ncreate('late-cancel') == 1

try:
    for name, fn in [('complete-cache-new-key', normal), ('poll-resume-key-pinning', resume_poll),
        ('uncertain-post-quarantine', uncertain), ('definitive-reject-key-rotation', rejected),
        ('persisted-create-limit', bounded_create), ('atomic-interrupted-download', interrupted_download),
        ('reject-html-audio', bad_audio), ('poll-timeout-no-cancel-explicit-cancel-once', timeout_then_cancel),
        ('queue-start-once-after-lost-response', queue_start_once), ('corrupt-completed-original-key-only', corrupt_completed),
        ('changed-output-identity-blocked', changed_identity), ('credential-source-no-fallback', source_pin),
        ('server-job-scope', scope), ('filesystem-failure-recovery', filesystem_failure),
        ('same-output-exclusive-lock', output_lock), ('ten-cached-assets-key-rotation', switched_key_after_cached_assets),
        ('pin-before-intent-crash-no-post', pin_only_crash), ('accepted-record-before-pin-crash', accepted_record_before_pin_crash),
        ('malformed-journal-no-duplicate', malformed_journal), ('invalid-speed-no-post', invalid_speed_no_post),
        ('strict-audio-speed-actual-ffmpeg', strict_speed),
        ('real-process-kill-resume', kill_resume), ('real-cli-fulltext-no-delete-no-quota-recharge', full_text_resume_zero_quota),
        ('real-cancel-only-cli-lost-ack-no-source', cancel_only_cli), ('cancel-unknown-no-create', cancel_unknown_no_create),
        ('cancel-batch-wall-clock-deadline', cancel_batch_deadline),
        ('cancel-orphan-and-terminal-pin-reconcile', cancel_orphan_and_terminal_pin),
        ('late-cancel-completed-resumes-paid-id', late_cancel_completed_resume),
        ('100-jobs-three-keys-six-parallel-workers', hundred_jobs)]:
        case(name, fn)
finally:
    payload = {'passed': len(RESULTS), 'results': RESULTS, 'temporary_evidence': str(TMP),
               'creates': len(CREATES), 'http_counts': COUNTS,
               'candidate_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in [CANDIDATE / 'voicegencore_worker.py', CANDIDATE / 'remote_journal_vgc.py']}}
    (HERE / 'vgc_verify_cycle2_result.json').write_text(json.dumps(payload, indent=2))
    server.shutdown()
print(json.dumps(payload, indent=2))
