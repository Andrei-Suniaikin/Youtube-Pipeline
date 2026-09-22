"""Opt-in Google Verify rendezvous; no profile opening, file handoff or publication.

The manager owns the lease and performs the callback while its worker waits.
Only non-secret, strictly typed messages cross the worker's stdout/stdin pipes.
The live adapter reuses the inspected DOM in the existing auth_browser helper.
"""
import importlib.util
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlparse
import urllib.request
import uuid

REQUEST = '__UPLOAD_AUTH_REQUEST__|'
ACK = '__UPLOAD_AUTH_ACK__|'
TIMEOUT = 180.0
MAX_WAVE_SIZE = 8
QUEUE_TIMEOUT = (MAX_WAVE_SIZE - 1) * TIMEOUT
ACK_MARGIN = 10.0
SHARED_ENV = 'VIDEO_UPLOAD_AUTH_SHARED_SLOT'
WORKER_DEADLINE_ENV = 'VIDEO_UPLOAD_AUTH_WORKER_DEADLINE'
UC_RE = re.compile(r'UC[A-Za-z0-9_-]{22}')
VIDEO_RE = re.compile(r'[A-Za-z0-9_-]{11}')
TARGET_RE = re.compile(r'[A-Fa-f0-9]{32}')
PHASES = {'preflight', 'profile_opened', 'upload_started', 'video_id_known'}
ROOT = Path('/home/vova/ww2_upload_stress_20260914')
SLOT_ROOT = ROOT / '.auth_slots'


class AuthBlocked(RuntimeError):
    """Only constant, value-free diagnostics belong in this exception."""


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AuthBlocked('Duplicate auth protocol field')
        result[key] = value
    return result


def _decode(line, marker):
    try:
        if not isinstance(line, str) or len(line) > 4096 or not line.startswith(marker):
            raise ValueError
        value = json.loads(line[len(marker):], object_pairs_hook=_unique)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except Exception:
        raise AuthBlocked('Invalid auth protocol message') from None


def validate_request(value):
    keys = {'schema_version', 'request_id', 'profile', 'channel_uc', 'target_id',
            'phase', 'video_id', 'stage', 'frontend_id'}
    if (not isinstance(value, dict) or set(value) != keys
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or any(not isinstance(value[k], str) for k in keys - {'schema_version'})
            or not re.fullmatch(r'[a-f0-9]{32}', value['request_id'])
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value['profile'])
            or not UC_RE.fullmatch(value['channel_uc'])
            or not TARGET_RE.fullmatch(value['target_id'])
            or value['phase'] not in PHASES
            or bool(value['video_id']) != (value['phase'] == 'video_id_known')
            or (value['video_id'] and not VIDEO_RE.fullmatch(value['video_id']))
            or not re.fullmatch(r'[a-z0-9_]{1,80}', value['stage'])
            or not re.fullmatch(r'[A-Za-z0-9_:.\-]{0,160}', value['frontend_id'])):
        raise AuthBlocked('Invalid auth request schema')
    return dict(value)


def decode_request(line):
    return validate_request(_decode(line, REQUEST))


def encode_request(value):
    return REQUEST + json.dumps(validate_request(value), sort_keys=True, separators=(',', ':'))


def encode_ack(request, video_id):
    if video_id and not VIDEO_RE.fullmatch(video_id):
        raise AuthBlocked('Invalid auth ACK identity')
    if request['video_id'] and request['video_id'] != video_id:
        raise AuthBlocked('Auth cannot change the known video ID')
    return ACK + json.dumps({'schema_version': 1, 'request_id': request['request_id'],
                            'target_id': request['target_id'], 'auth_clear': True,
                            'video_id': video_id}, sort_keys=True, separators=(',', ':'))


def decode_ack(line, request):
    value = _decode(line, ACK)
    if (set(value) != {'schema_version', 'request_id', 'target_id', 'auth_clear', 'video_id'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['auth_clear'] is not True
            or value['request_id'] != request['request_id'] or value['target_id'] != request['target_id']
            or not isinstance(value['video_id'], str)
            or (value['video_id'] and not VIDEO_RE.fullmatch(value['video_id']))
            or (request['video_id'] and value['video_id'] != request['video_id'])):
        raise AuthBlocked('Auth ACK does not match the pending request')
    return value


def wait_ack(request, stream, timeout=None):
    # The manager's normal phase ACK is a different marker and cannot satisfy this read.
    if timeout is None:
        timeout = TIMEOUT + ACK_MARGIN + (QUEUE_TIMEOUT if os.environ.get(SHARED_ENV) == '1' else 0)
    deadline, data = time.monotonic() + timeout, b''
    if os.environ.get(WORKER_DEADLINE_ENV):
        try:
            limit = float(os.environ[WORKER_DEADLINE_ENV])
            if not math.isfinite(limit):
                raise ValueError
            deadline = min(deadline, limit)
        except ValueError:
            raise AuthBlocked('Invalid auth worker deadline') from None
    while b'\n' not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([stream], [], [], remaining)[0]:
            raise AuthBlocked('Auth ACK deadline exceeded')
        part = os.read(stream.fileno(), 4097 - len(data))
        if not part:
            raise AuthBlocked('Auth ACK pipe closed')
        data += part
        if len(data) > 4096:
            raise AuthBlocked('Auth ACK exceeds protocol limit')
    if data.count(b'\n') != 1 or not data.endswith(b'\n'):
        raise AuthBlocked('Unexpected additional auth pipe data')
    try:
        line = data.decode('utf-8').rstrip('\r\n')
    except UnicodeError:
        raise AuthBlocked('Invalid auth ACK encoding') from None
    value = decode_ack(line, request)
    # An unsolicited second ACK is never an authorization for the next action.
    if select.select([stream], [], [], 0)[0]:
        raise AuthBlocked('Unexpected additional auth pipe data')
    return value


def expected_from_argv(argv):
    def one(flag):
        found = []
        for index, part in enumerate(argv):
            if part == flag and index + 1 < len(argv):
                found.append(argv[index + 1])
            elif part.startswith(flag + '='):
                found.append(part[len(flag) + 1:])
        if len(found) != 1:
            raise AuthBlocked('Auth requires one manager-owned profile and channel argument')
        return found[0]
    profile, route = one('--profile'), urlparse(one('--channel-url'))
    match = re.fullmatch(r'/channel/(UC[A-Za-z0-9_-]{22})/?', route.path)
    if (not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', profile)
            or route.scheme != 'https' or route.hostname != 'studio.youtube.com'
            or route.username or route.password or not match):
        raise AuthBlocked('Invalid manager-owned auth binding')
    return {'profile': profile, 'channel_uc': match[1]}


def save_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def process_identity(pid, proc_root=Path('/proc')):
    if Path(proc_root).exists():
        try:
            fields = (Path(proc_root) / str(pid) / 'stat').read_text().rsplit(') ', 1)[1].split()
            if fields[0] in ('Z', 'X', 'T', 't'):
                raise ValueError
            return (int(pid), int(fields[1]), fields[19])
        except Exception:
            raise AuthBlocked('Auth owner process unavailable') from None
    # Offline protocol tests may run on macOS. The live adapter separately requires Linux.
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'ppid=,lstart='],
                            capture_output=True, text=True, timeout=2)
    fields = result.stdout.split()
    if result.returncode or len(fields) < 2:
        raise AuthBlocked('Auth owner process unavailable')
    return (int(pid), int(fields[0]), ' '.join(fields[1:]))


class Context:
    def __init__(self, request, expected, phase, proc, lease, evidence_dir, *, clock=time.monotonic,
                 identity_reader=process_identity, wave=None, worker_deadline=None):
        self.request = validate_request(request)
        if (any(request[k] != expected[k] for k in ('profile', 'channel_uc'))
                or any(request[k] != phase[k] for k in ('phase', 'video_id'))):
            raise AuthBlocked('Auth request differs from manager identity or durable phase')
        self.proc, self.lease, self.clock = proc, lease, clock
        self.wave = validate_wave(wave, expected) if wave is not None else None
        now = clock()
        self.overall_deadline = now + TIMEOUT + (QUEUE_TIMEOUT if self.wave else 0)
        if worker_deadline is not None:
            if type(worker_deadline) not in (float, int) or not math.isfinite(worker_deadline):
                raise AuthBlocked('Invalid auth worker deadline')
            self.overall_deadline = min(self.overall_deadline, worker_deadline - ACK_MARGIN)
        self.deadline = min(now + (QUEUE_TIMEOUT if self.wave else TIMEOUT), self.overall_deadline)
        self.active = self.wave is None
        self.identity_reader = identity_reader
        self.worker_identity = identity_reader(proc.pid)
        self.parent_identity = identity_reader(os.getpid())
        if self.worker_identity[1] != os.getpid():
            raise AuthBlocked('Auth worker is not this manager child')
        self.evidence_dir = Path(evidence_dir)
        self.check()

    def remaining(self):
        left = self.deadline - self.clock()
        if left <= 0:
            raise AuthBlocked('Auth deadline exceeded')
        return left

    def check(self):
        self.remaining()
        self.lease.assert_held()
        if (self.proc.poll() is not None
                or self.identity_reader(self.proc.pid) != self.worker_identity
                or self.identity_reader(os.getpid()) != self.parent_identity):
            raise AuthBlocked('Auth worker or manager identity changed')

    def intent(self, stage, target):
        self.check()
        save_new(self.evidence_dir / ('auth_' + stage + '_' + target + '.intent.json'),
                 {'schema_version': 1, 'request': self.request, 'stage': stage,
                  'target_id': target, 'monotonic_seconds': self.clock()})

    def activate(self):
        self.check()
        if self.active:
            raise AuthBlocked('Auth slot already activated')
        self.deadline = min(self.clock() + TIMEOUT, self.overall_deadline)
        self.active = True
        self.check()


def validate_wave(value, expected):
    """Manager-owned, immutable cohort; the harness freezes wave admission."""
    if (not isinstance(value, dict) or set(value) != {'wave_id', 'visibility', 'members', 'attempt_id'}
            or value['visibility'] not in ('PUBLIC', 'PRIVATE')
            or not isinstance(value['wave_id'], str) or not re.fullmatch(r'[a-f0-9]{64}', value['wave_id'])
            or not isinstance(value['attempt_id'], str) or not re.fullmatch(r'[a-f0-9]{32}', value['attempt_id'])
            or not isinstance(value['members'], list) or not 1 <= len(value['members']) <= MAX_WAVE_SIZE):
        raise AuthBlocked('Invalid frozen auth wave')
    members = value['members']
    for item in members:
        if (not isinstance(item, dict) or set(item) != {'profile', 'channel_uc', 'project_id'}
                or not all(isinstance(v, str) for v in item.values())
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', item['profile'])
                or not UC_RE.fullmatch(item['channel_uc'])
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', item['project_id'])):
            raise AuthBlocked('Invalid frozen auth member')
    if (any(len({item[key] for item in members}) != len(members)
            for key in ('profile', 'channel_uc', 'project_id'))
            or sum(all(item[key] == expected[key] for key in ('profile', 'channel_uc')) for item in members) != 1):
        raise AuthBlocked('Auth target is not one exact frozen member')
    return {'wave_id': value['wave_id'], 'visibility': value['visibility'], 'attempt_id': value['attempt_id'],
            'members': sorted((dict(item) for item in members), key=lambda item: item['profile'])}


@contextmanager
def _checked_lock(path, context, *, pause=time.sleep):
    with Path(path).open('a') as handle:
        while True:
            context.check()
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                pause(min(0.5, context.remaining()))
        try:
            context.check()
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def shared_slot(context, *, root=None, pause=time.sleep):
    """One active callback across frozen waves; never steal a stopped owner's lock."""
    if context.wave is None:
        yield  # Legacy single-job caller retains the original 180s/190s contract.
        return
    directory = Path(root) if root is not None else SLOT_ROOT
    directory.mkdir(parents=True, exist_ok=True)
    wave = context.wave
    cohort = directory / wave['wave_id']
    with _checked_lock(directory / 'registry.lock', context, pause=pause):
        cohort.mkdir(exist_ok=True)
        path = cohort / 'members.json'
        document = {'schema_version': 1, 'wave_id': wave['wave_id'],
                    'visibility': wave['visibility'], 'members': wave['members']}
        if path.exists():
            if json.loads(path.read_text(), object_pairs_hook=_unique) != document:
                raise AuthBlocked('Frozen auth cohort changed')
        else:
            save_new(path, document)
        member = next(item for item in wave['members'] if item['profile'] == context.request['profile'])
        claim = hashlib.sha256(member['project_id'].encode()).hexdigest()
        save_new(cohort / (claim + '.claim.json'), {'schema_version': 1, 'member': member,
                 'attempt_id': wave['attempt_id'], 'request': context.request,
                 'worker_identity': context.worker_identity, 'parent_identity': context.parent_identity})
    context.intent('queued', context.request['request_id'])
    with _checked_lock(directory / 'active.lock', context, pause=pause):
        context.activate()
        context.intent('slot_acquired', context.request['request_id'])
        try:
            yield
        finally:
            context.active = False


# Existing live selectors: yt_studio._READ_WIZARD_JS; auth gate descendant dump
# 2026-09-14; auth_browser.require_original. No form values/body are returned.
ORIGINAL_JS = r"""(()=>{
function deep(r,o){for(const e of r.querySelectorAll('*')){o.push(e);if(e.shadowRoot)deep(e.shadowRoot,o)}return o}
const all=deep(document,[]),app=all.find(e=>e.tagName==='YTCP-APP');
const wizards=all.filter(e=>e.tagName==='YTCP-UPLOADS-DIALOG'),w=wizards.length===1?wizards[0]:null;
let frontends=[],ids=[],ambiguous=wizards.length>1;
if(w){
 const direct=w.videoId||'',active=Object.entries(w.uploadDetailsByFrontendId||{}).filter(([k,e])=>e&&e.local===true);
 if(active.length>1)ambiguous=true;
 else if(active.length===1){
  const [key,e]=active[0],mapped=e.videoId||'',draft=e.draftVideoId||'';
  if(w.uploadMode!=='NEW_UPLOAD'||e.frontendId!==key||!key||key.length>256
      ||[direct,mapped,draft].some(v=>typeof v!=='string'||(v&&!/^[A-Za-z0-9_-]{11}$/.test(v)))
      ||(direct&&direct!==mapped)||(draft&&draft!==(direct||mapped)))ambiguous=true;
  else {frontends=[key];ids=direct?[direct]:[];}
 }else if(direct)ambiguous=true;
}
const hosts=all.filter(e=>e.tagName==='YTCP-AUTH-CONFIRMATION-DIALOG');
const auth=hosts.some(h=>deep(h,[]).concat(h.shadowRoot?deep(h.shadowRoot,[]):[]).some(e=>{
const r=e.getBoundingClientRect(),s=getComputedStyle(e);return e.tagName==='TP-YT-PAPER-DIALOG'&&e.opened===true&&!e.hidden&&r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'}));
return {origin:location.origin,path:location.pathname,channel_uc:app&&app.channel&&app.channel.channelId||'',
auth:auth,wizard_present:!!w,frontend_ids:frontends,video_ids:ids,identity_ambiguous:ambiguous};})()"""


def validate_original(state, request, *, auth_clear=False):
    if (not isinstance(state, dict) or state.get('origin') != 'https://studio.youtube.com'
            or not state.get('path', '').startswith('/channel/' + request['channel_uc'] + '/')
            or state.get('channel_uc') != request['channel_uc'] or type(state.get('auth')) is not bool
            or (auth_clear and state['auth'])):
        raise AuthBlocked('Original Studio channel or auth state changed')
    ids, frontends = state.get('video_ids'), state.get('frontend_ids')
    if (state.get('identity_ambiguous') is not False
            or not isinstance(ids, list) or len(ids) > 1
            or any(not isinstance(v, str) or not VIDEO_RE.fullmatch(v) for v in ids)
            or not isinstance(frontends, list)
            or (request['frontend_id'] and (not state.get('wizard_present')
                                           or frontends != [request['frontend_id']]))
            or (request['video_id'] and ids != [request['video_id']])):
        raise AuthBlocked('Original upload identity changed or became ambiguous')
    return ids[0] if ids else ''


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configured_credentials(helper, path, profile, uc):
    try:
        st = path.stat()
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError
        document = json.loads(path.read_text(), object_pairs_hook=_unique)
        matches = [b for b in document.get('bindings', [])
                   if b.get('profile') == profile and b.get('channel_uc') == uc]
        if len(matches) != 1:
            raise ValueError
        return helper.load_credentials(path, matches[0]['login'])
    except Exception:
        raise AuthBlocked('Private exact account binding unavailable') from None


def _connect_cdp(context, port):
    """The read-only discovery and websocket startup share the active deadline."""
    import websocket
    context.check()
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version',
                                timeout=min(8, context.remaining())) as response:
        value = json.load(response)
    context.check()
    endpoint = value.get('webSocketDebuggerUrl', '')
    route = urlparse(endpoint)
    if (route.scheme != 'ws' or route.hostname not in ('localhost', '127.0.0.1')
            or route.port != port or route.username or route.password):
        raise AuthBlocked('Unexpected CDP endpoint')
    connection = websocket.create_connection(endpoint, timeout=min(15, context.remaining()), suppress_origin=True)
    try:
        context.check()
    except BaseException:
        connection.close(timeout=0)
        raise
    return SimpleNamespace(ws=connection, counter=0)


class Browser:
    """Guard every native input, including each part of a mouse click."""
    def __init__(self, context, reviewed, collector, credentials, *, connect=None):
        self.ctx, self.reviewed, self.collector, self.credentials = context, reviewed, collector, credentials
        context.check()
        if not context.active:
            raise AuthBlocked('Auth browser requires an acquired active slot')
        self.profile = context.request['profile']
        index = collector.profile_index('http://localhost:50325')
        self.uid = collector.uid_of(self.profile, index)
        if (not self.uid or self.uid not in index['by_uid']
                or index['by_serial'].get(index['by_uid'][self.uid]) != self.uid):
            raise AuthBlocked('Exact profile UID cannot be resolved')
        self.ctx.check()
        self.port = self.activity()
        self.identity = self.browser_identity()
        self.raw = (connect or _connect_cdp)(context, self.port)
        self.sessions = {}
        self.expression = self.condition = None

    def activity(self):
        self.ctx.check()
        hosts = {}
        for host, origin in (('minsk', 'http://localhost:50325'), ('mac', 'http://100.100.244.89:50325')):
            response = self.collector._requests().get(origin + '/api/v1/browser/local-active',
                                                       timeout=min(12, self.ctx.remaining()))
            value = response.json()
            entries = value.get('data', {}).get('list')
            if (response.status_code != 200 or type(value.get('code')) is not int or value['code'] != 0
                    or not isinstance(entries, list)
                    or any(not isinstance(e, dict) or not isinstance(e.get('user_id'), str)
                           or not e['user_id'] for e in entries)):
                raise AuthBlocked('Dual-host activity unavailable')
            hosts[host] = entries
        matches = [e for e in hosts['minsk'] if e.get('user_id') == self.uid]
        if (len(matches) != 1 or any(e.get('user_id') == self.uid for e in hosts['mac'])
                or not str(matches[0].get('debug_port', '')).isdigit()):
            raise AuthBlocked('Existing profile is not owned on Minsk only')
        self.ctx.check()
        return int(matches[0]['debug_port'])

    def browser_identity(self):
        return self.reviewed.browser_identity(self.profile, self.port, uid=self.uid,
                                             allowed_process=self.ctx.worker_identity)

    def call(self, method, params=None, session=None):
        self.ctx.check()
        self.raw.counter += 1
        identifier = self.raw.counter
        request = {'id': identifier, 'method': method, 'params': params or {}}
        if session:
            request['sessionId'] = session
        self.raw.ws.settimeout(min(10, self.ctx.remaining()))
        self.raw.ws.send(json.dumps(request))
        while True:
            self.ctx.check()
            self.raw.ws.settimeout(min(10, self.ctx.remaining()))
            result = json.loads(self.raw.ws.recv())
            self.ctx.check()  # Unrelated CDP events cannot extend the deadline.
            if result.get('id') == identifier:
                if 'error' in result:
                    raise AuthBlocked('Auth browser operation failed')
                return result['result']

    def session(self, target):
        if target not in self.sessions:
            self.sessions[target] = self.call('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
        return self.sessions[target]

    def evaluate(self, target, expression):
        result = self.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True}, self.session(target))
        if 'exceptionDetails' in result or 'value' not in result.get('result', {}):
            raise AuthBlocked('Auth DOM unreadable')
        return result['result']['value']

    def original(self, *, clear=False):
        state = self.evaluate(self.ctx.request['target_id'], ORIGINAL_JS)
        validate_original(state, self.ctx.request, auth_clear=clear)
        return state

    def pages(self):
        return [p for p in self.call('Target.getTargets')['targetInfos'] if p['type'] == 'page']

    def guard_ownership(self, target):
        self.ctx.check()
        if not self.ctx.active:
            raise AuthBlocked('Auth input requires the active slot')
        if self.activity() != self.port or self.browser_identity() != self.identity:
            raise AuthBlocked('Auth browser identity changed')
        self.original()
        page = next((p for p in self.pages() if p['targetId'] == target), None)
        if target != self.ctx.request['target_id']:
            if (not page or page.get('openerId') != self.ctx.request['target_id']
                    or urlparse(page.get('url', '')).hostname != 'accounts.google.com'):
                raise AuthBlocked('Auth popup ownership changed')

    def activate(self, target):
        self.guard_ownership(target)
        self.call('Target.activateTarget', {'targetId': target})

    def guard(self, target):
        self.guard_ownership(target)
        state = self.evaluate(target, self.expression)
        if not state or not self.condition(state):
            raise AuthBlocked('Auth input form changed')
        self.ctx.check()
        return state

    def click(self, target, point, expression, condition):
        self.expression, self.condition = expression, condition
        for kind in ('mouseMoved', 'mousePressed', 'mouseReleased'):
            self.guard(target)
            params = {'type': kind, 'x': point['x'], 'y': point['y']}
            if kind != 'mouseMoved':
                params.update(button='left', clickCount=1)
            self.call('Input.dispatchMouseEvent', params, self.session(target))

    def insert(self, target, text, expression, *, before_insert=None):
        self.expression, self.condition = expression, lambda s: s.get('empty') is True and s.get('focused') is True
        self.guard(target)
        session = self.session(target)
        if before_insert is not None:
            before_insert()  # Slow ownership/transport preparation must precede the TTL check.
        self.call('Input.insertText', {'text': text}, session)

    def close(self):
        # Only disconnect this helper's CDP sessions. Never close the profile/tab.
        self.raw.ws.close(timeout=0)


def run_flow(browser, context, credentials, secrets, *, pause=time.sleep, wall_clock=time.time):
    attempted = set()
    request, reviewed = context.request, browser.reviewed
    while True:
        context.check()
        original = browser.original()
        google = [p for p in browser.pages() if urlparse(p.get('url', '')).hostname == 'accounts.google.com']
        if any(p.get('openerId') != request['target_id'] for p in google) or len(google) > 1:
            raise AuthBlocked('Authentication popup is not unique and original-owned')
        if not google and original['auth'] is False:
            return validate_original(browser.original(clear=True), request, auth_clear=True)
        if google:
            # An original-owned popup proves Next already happened, including
            # requests that start mid-flow. Its closure may precede auth-clear.
            attempted.add('next')
            page, target = google[0], google[0]['targetId']
            stage = {'/v3/signin/accountchooser': 'account', '/v3/signin/challenge/pwd': 'password',
                     '/v3/signin/challenge/totp': 'totp'}.get(urlparse(page.get('url', '')).path)
            if stage is None:
                raise AuthBlocked('Unsupported authentication challenge')
            dom = browser.evaluate(target, reviewed.READ_DOM)
            signals = dom.get('signals', {}) if isinstance(dom, dict) else {}
            if any(signals.get(key) for key in ('wrong_password', 'wrong_code', 'rate_limit', 'phone', 'passkey')):
                raise AuthBlocked('Authentication challenge cannot continue automatically')
        else:
            stage, target = 'next', request['target_id']
        if stage in attempted:
            pause(min(0.5, context.remaining()))
            continue
        browser.activate(target)
        if stage == 'next':
            expression = reviewed.NEXT_POINT
        elif stage == 'account':
            expression = reviewed.account_choice_expression(credentials.login)
        else:
            expression = (reviewed.password_expression if stage == 'password' else reviewed.totp_expression)(credentials.login)
        state = browser.evaluate(target, expression)
        if not state:
            # A known route can precede its form's render. No input until the
            # exact inspected form is enabled; unknown routes remain blockers.
            pause(min(0.5, context.remaining()))
            continue
        if stage in ('password', 'totp') and state.get('empty') is not True:
            raise AuthBlocked('Auth secret field must be empty before the single attempt')
        context.intent(stage, target)
        attempted.add(stage)
        if stage in ('next', 'account'):
            browser.click(target, state, expression, lambda current, initial=state: current == initial)
            continue
        browser.click(target, state['input'], expression,
                      lambda current, initial=state: current.get('empty') is True
                      and current.get('input') == initial['input'] and current.get('next') == initial['next'])
        issued = None
        if stage == 'totp':
            synced = subprocess.run(['timedatectl', 'show', '-p', 'NTPSynchronized', '--value'],
                                    capture_output=True, text=True, timeout=min(5, context.remaining()))
            if synced.returncode or synced.stdout.strip() != 'yes':
                raise AuthBlocked('Synchronized clock required for Authenticator')
            while 30 - wall_clock() % 30 < 25:
                context.check()
                pause(min(0.5, context.remaining()))
            issued = wall_clock()
            value, remaining = secrets.totp_code(credentials.totp, timestamp=issued)
            if remaining < 25:
                raise AuthBlocked('Insufficient Authenticator validity')
        else:
            value = credentials.password
        def require_totp_validity(minimum):
            if issued is not None:
                now = wall_clock()
                if int(now // 30) != int(issued // 30) or 30 - now % 30 < minimum:
                    raise AuthBlocked('Authenticator window changed; no retry')
        browser.insert(target, value, expression,
                       before_insert=(lambda: require_totp_validity(15)) if issued is not None else None)
        value = None
        ready = browser.evaluate(target, expression)
        if not ready or ready.get('empty') is not False or ready.get('focused') is not True:
            raise AuthBlocked('Auth field input was not acknowledged')
        def ready_to_submit(current):
            require_totp_validity(5)
            # mousePressed on Next legitimately moves focus off the input.
            # Initial readback above requires focus; release retains every
            # ownership/form/TTL guard without requiring the old field focus.
            return (current.get('empty') is False and current.get('next') == ready['next']
                    and current.get('input') == ready['input'])
        browser.click(target, ready['next'], expression, ready_to_submit)


def runtime_callback(context):
    browser = None
    try:
        if sys.platform != 'linux' or socket.gethostname() != 'vova-System-Product-Name':
            raise AuthBlocked('Automatic auth is restricted to the reviewed Minsk host')
        if not os.environ.get('ADSPOWER_RATE_LIMIT_DIR'):
            raise AuthBlocked('Shared AdsPower rate limiter is required for automatic auth')
        context.check()
        if not context.active:
            raise AuthBlocked('Auth runtime requires an acquired active slot')
        reviewed = _load('upload_auth_reviewed', ROOT / 'auth_browser.py')
        secrets = _load('upload_auth_secrets', ROOT / 'google_auth_secrets.py')
        collector = _load('upload_auth_collector', Path(__file__).resolve().parents[1] / 'tools/collect_channel_emails.py')
        with collector._requests().deadline_scope(context.deadline, check=context.check):
            credentials = configured_credentials(secrets, ROOT / 'private/google_accounts.json',
                                                 context.request['profile'], context.request['channel_uc'])
            browser = Browser(context, reviewed, collector, credentials)
            return run_flow(browser, context, credentials, secrets)
    except Exception:
        # Transport/JSON/subprocess errors can contain argv or request bodies.
        raise AuthBlocked('Automatic account confirmation blocked; inspect non-secret intent') from None
    finally:
        if browser is not None:
            browser.close()
