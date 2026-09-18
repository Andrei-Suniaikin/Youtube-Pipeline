"""Bounded operations on the task-owned existing Minsk authentication browser.

This tool never hands off a file, publishes, closes a browser, or clears cookies.
Inputs are added only after their exact live DOM has been inspected.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
from urllib.parse import urlparse
import urllib.request

BASE = Path('/home/vova/ww2_upload_stress_20260914')
NIGHT = Path('/home/vova/ww2_night_20260914')
SOURCE = Path('/home/vova/VideoStudio')
BINDINGS = {
    '21': ('<ADSPOWER_USER_ID>', 'UCmRLVcKRR0YaWQApNNZMzOA', '<TAB_ID>'),
    '55': ('<ADSPOWER_USER_ID>', 'UC6FO-4FFLg4wANGdECL9ssQ', '<TAB_ID>'),
}

class AuthBlocked(RuntimeError):
    pass

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def now():
    return datetime.now(timezone.utc).isoformat()

def save_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)

def browser_identity(profile, port, *, uid=None, allowed_process=None):
    """Read-only owner check. Process arguments are inspected, never returned."""
    uid = uid if uid is not None else BINDINGS[profile][0]
    manifest = json.loads((NIGHT / 'candidates.json').read_text())['candidates']
    projects = [r['project_id'] for r in manifest if str(r['profile_id']) == profile]
    ancestors = set()
    current = os.getpid()
    while current > 1 and current not in ancestors:
        ancestors.add(current)
        try:
            current = int(Path('/proc', str(current), 'stat').read_text().rsplit(') ', 1)[1].split()[1])
        except FileNotFoundError:
            break
    sockets = set()
    for protocol in ('tcp', 'tcp6'):
        for line in Path('/proc/net', protocol).read_text().splitlines()[1:]:
            fields = line.split()
            if int(fields[1].rsplit(':', 1)[1], 16) == port and fields[3] == '0A':
                sockets.add('socket:[' + fields[9] + ']')
    if not sockets:
        raise AuthBlocked('CDP listener absent')
    found = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        stage, process_class = 'stat', 'unknown'
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            stage = 'cmdline'
            raw = (proc / 'cmdline').read_bytes()
            command = ' '.join(raw.replace(b'\0', b' ').decode(errors='strict').split())
            scoped = (uid in command or str(port) in command or any(p in command for p in projects)
                      or re.search(r'--profile(?:=|\s+)' + re.escape(profile) + r'(?:\s|$)', command))
            automation = any(w in command.lower() for w in (
                'upload_worker', 'upload_manager', 'verify_public', 'auth_browser', 'native_next',
                'recover', 'inspect', 'probe_', 'ytdom.py', 'cancel_', 'close_owned'))
            allowed = False
            if allowed_process is not None and int(proc.name) == allowed_process[0]:
                stage = 'allowed_stat'
                fields = (proc / 'stat').read_text().rsplit(') ', 1)[1].split()
                if ((int(proc.name), int(fields[1]), fields[19]) != allowed_process
                        or fields[0] in ('Z', 'X', 'T', 't')):
                    raise AuthBlocked('Allowed worker process identity changed')
                allowed = True
            if int(proc.name) not in ancestors and not allowed and scoped and automation:
                raise AuthBlocked('Another scoped browser automation process exists')
            stage = 'comm'
            if (proc / 'comm').read_text().strip() != 'SunBrowser':
                continue
            # AdsPower may put the whole command in argv[0]. Only that item
            # is split; spaces in other argument values remain data. The API
            # UID -> actual CDP port -> socket/PID binding is authoritative.
            argv = [item for item in raw.split(b'\0') if item]
            if not argv:
                stage = 'empty_cmdline_stat'
                state = (proc / 'stat').read_text().rsplit(') ', 1)[1].split()[0]
                if state in ('Z', 'X'):
                    continue  # Confirmed terminal process cannot own the live listener.
                raise AuthBlocked('SunBrowser process class unknown: empty cmdline')
            argv = argv[0].split() + argv[1:]
            process_class = ('child' if any(item == b'--type' or item.startswith(b'--type=')
                                          for item in argv) else 'parent')
            if process_class == 'child':
                continue
            descriptors = set()
            stage = 'fd_iter'
            for fd in (proc / 'fd').iterdir():
                try:
                    stage = 'readlink'
                    descriptors.add(os.readlink(fd))
                except FileNotFoundError:
                    pass
            if descriptors & sockets:
                stage = 'stat_after'
                found.append({'pid': int(proc.name), 'start_ticks': (proc / 'stat').read_text().rsplit(') ', 1)[1].split()[19],
                              'port': port, 'socket_inodes': sorted(descriptors & sockets)})
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (PermissionError, UnicodeError) as exc:
            # Value-free failure evidence: never include command lines or paths.
            detail = {'stage': stage, 'pid': int(proc.name), 'process_class': process_class,
                      'errno': getattr(exc, 'errno', None), 'error_type': type(exc).__name__}
            raise AuthBlocked('Current-user process ownership unreadable: ' +
                              json.dumps(detail, sort_keys=True)) from None
    if len(found) != 1:
        raise AuthBlocked('Browser process ownership ambiguous')
    return found[0]

def activity(profile):
    uid = BINDINGS[profile][0]
    hosts = {}
    for label, origin in [('minsk', 'http://localhost:50325'), ('mac', 'http://100.100.244.89:50325')]:
        try:
            raw = json.load(urllib.request.urlopen(origin + '/api/v1/browser/local-active', timeout=12))
        except Exception:
            raise AuthBlocked('Dual-host activity unavailable') from None
        entries = raw.get('data', {}).get('list')
        if raw.get('code') != 0 or not isinstance(entries, list):
            raise AuthBlocked('Dual-host activity invalid')
        hosts[label] = [{k: e.get(k) for k in ('user_id', 'debug_port')} for e in entries]
    matches = [e for e in hosts['minsk'] if e['user_id'] == uid]
    if len(matches) != 1 or any(e['user_id'] == uid for e in hosts['mac']):
        raise AuthBlocked('Existing profile ownership is not Minsk only')
    port = str(matches[0]['debug_port'])
    if not port.isdigit():
        raise AuthBlocked('Existing CDP port unavailable')
    return int(port), hosts

class CDP:
    def __init__(self, port):
        import websocket
        raw = json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=8))
        endpoint = raw.get('webSocketDebuggerUrl', '')
        parsed = urlparse(endpoint)
        if parsed.scheme != 'ws' or parsed.hostname not in ('localhost', '127.0.0.1') or parsed.port != port:
            raise AuthBlocked('Unexpected CDP endpoint')
        self.ws = websocket.create_connection(endpoint, timeout=15, suppress_origin=True)
        self.counter = 0

    def call(self, method, params=None, session=None):
        self.counter += 1
        request = {'id': self.counter, 'method': method, 'params': params or {}}
        if session:
            request['sessionId'] = session
        self.ws.send(json.dumps(request))
        while True:
            result = json.loads(self.ws.recv())
            if result.get('id') == self.counter:
                if 'error' in result:
                    raise AuthBlocked('Browser operation failed')
                return result['result']

    def evaluate(self, session, expression):
        result = self.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True}, session)
        if 'exceptionDetails' in result or 'value' not in result.get('result', {}):
            raise AuthBlocked('Browser state unreadable')
        return result['result']['value']

    def pages(self):
        return [p for p in self.call('Target.getTargets')['targetInfos'] if p['type'] == 'page']

    def attach(self, target):
        return self.call('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']

    def click(self, session, point):
        self.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': point['x'], 'y': point['y']}, session)
        for kind in ('mousePressed', 'mouseReleased'):
            self.call('Input.dispatchMouseEvent', {'type': kind, 'x': point['x'], 'y': point['y'],
                      'button': 'left', 'clickCount': 1}, session)

READ_DOM = r"""(()=>{
function deep(root,out){for(const e of root.querySelectorAll('*')){out.push(e);if(e.shadowRoot)deep(e.shadowRoot,out)}return out}
function visible(e){const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'}
const all=deep(document,[]);
const body=document.body.innerText||'';
return {origin:location.origin,path:location.pathname,title:document.title,
headings:all.filter(e=>['H1','H2'].includes(e.tagName)&&visible(e)).map(e=>(e.innerText||'').slice(0,100)),
signals:{verify:body.includes("Verify it's you"),authenticator:/Authenticator|authentication code/i.test(body),wrong_password:/Wrong password|incorrect password/i.test(body),wrong_code:/Wrong code|incorrect code/i.test(body),phone:/phone number|your phone/i.test(body),passkey:/passkey|security key/i.test(body),rate_limit:/too many|try again later/i.test(body)},
inputs:all.filter(e=>e.tagName==='INPUT'&&visible(e)).map(e=>({type:e.type,name:e.name,id:e.id,aria:e.getAttribute('aria-label'),autocomplete:e.autocomplete,disabled:!!e.disabled})),
buttons:all.filter(e=>e.tagName==='BUTTON'&&visible(e)).map(e=>({id:e.id,aria:e.getAttribute('aria-label'),text:(e.innerText||'').trim().slice(0,100),disabled:!!e.disabled,aria_disabled:e.getAttribute('aria-disabled')}))}
})()"""

# Native button observed in auth_dom_baseline.json at 2026-09-14T07:47:17Z.
NEXT_POINT = r"""(()=>{
const host=document.querySelector('ytcp-auth-confirmation-dialog');
const app=document.querySelector('ytcp-app');
const dialog=host&&host.querySelector('tp-yt-paper-dialog');
if(!host||!dialog||dialog.opened!==true||!host.innerText.includes("Verify it's you"))return null;
const all=Array.from(host.querySelectorAll('#confirm-button button[aria-label="Next"]'));
const buttons=all.filter(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'&&!e.disabled&&e.getAttribute('aria-disabled')!=='true'});
if(buttons.length!==1)return null;
const r=buttons[0].getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2,origin:location.origin,path:location.pathname,app_uc:app&&app.channel&&app.channel.channelId}
})()"""

def configured_credentials(profile):
    from google_auth_secrets import load_credentials
    path = BASE / 'private' / 'google_accounts.json'
    st = path.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise AuthBlocked('Credentials file must be private to the current user')
    document = json.loads(path.read_text())
    bindings = [b for b in document.get('bindings', [])
                if b.get('profile') == profile and b.get('channel_uc') == BINDINGS[profile][1]]
    if len(bindings) != 1:
        raise AuthBlocked('Exact credential binding missing or ambiguous')
    return load_credentials(path, bindings[0]['login'])

def account_choice_expression(login):
    # Live source: accountchooser_dom.json, 2026-09-14T07:59:03Z.
    return r"""(()=>{
const expected=EXPECTED;
if(location.origin!=='https://accounts.google.com'||location.pathname!=='/v3/signin/accountchooser')return null;
const matches=Array.from(document.querySelectorAll('div[role="link"][data-identifier]')).filter(e=>{
 const r=e.getBoundingClientRect(),s=getComputedStyle(e);
 return e.getAttribute('data-identifier')===expected&&r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&e.getAttribute('aria-disabled')!=='true';
});
if(matches.length!==1)return null;
const r=matches[0].getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2,origin:location.origin,path:location.pathname,account_matches:true};
})()""".replace('EXPECTED', json.dumps(login))

def snapshot(cdp):
    result = []
    for page in cdp.pages():
        parsed = urlparse(page.get('url', ''))
        entry = {'target_id': page['targetId'], 'opener_id': page.get('openerId'),
                 'origin': parsed.scheme + '://' + parsed.netloc, 'path': parsed.path}
        if parsed.hostname in ('studio.youtube.com', 'accounts.google.com'):
            sid = cdp.attach(page['targetId'])
            try:
                entry['dom'] = cdp.evaluate(sid, READ_DOM)
            finally:
                cdp.call('Target.detachFromTarget', {'sessionId': sid})
        result.append(entry)
    return result

def require_original(cdp, original, uc):
    sid = cdp.attach(original)
    try:
        state = cdp.evaluate(sid, """(()=>{const app=document.querySelector('ytcp-app');return {origin:location.origin,path:location.pathname,uc:app&&app.channel&&app.channel.channelId};})()""")
        if (state.get('origin') != 'https://studio.youtube.com'
                or not state.get('path', '').startswith('/channel/' + uc + '/')
                or state.get('uc') != uc):
            raise AuthBlocked('Original exact Studio channel changed')
    finally:
        cdp.call('Target.detachFromTarget', {'sessionId': sid})

def password_expression(login):
    # Live password_dom.json at 2026-09-14T08:09:04Z. Values never returned.
    return r"""(()=>{
const expected=EXPECTED;
if(location.origin!=='https://accounts.google.com'||location.pathname!=='/v3/signin/challenge/pwd')return null;
function visible(e){const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'}
function ready(e){return visible(e)&&!e.disabled&&e.getAttribute('aria-disabled')!=='true'}
const accounts=Array.from(document.querySelectorAll('div[data-profile-identifier]')).filter(visible);
if(accounts.length!==1||accounts[0].textContent.trim()!==expected)return null;
const inputs=Array.from(document.querySelectorAll('input[type="password"][name="Passwd"]')).filter(ready);
const next=Array.from(document.querySelectorAll('button')).filter(e=>ready(e)&&e.innerText.trim()==='Next');
if(inputs.length!==1||next.length!==1)return null;
function point(e){const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}}
return {input:point(inputs[0]),next:point(next[0]),empty:inputs[0].value.length===0,focused:document.activeElement===inputs[0],account_matches:true};
})()""".replace('EXPECTED', json.dumps(login))

def submit_password(cdp, profile, port, identity, lease, before, out):
    _, uc, original = BINDINGS[profile]
    credentials = configured_credentials(profile)
    pages = [p for p in before if p['opener_id'] == original
             and p['origin'] == 'https://accounts.google.com'
             and p['path'] == '/v3/signin/challenge/pwd']
    if len(pages) != 1:
        raise AuthBlocked('One original-opener Google password challenge required')
    target = pages[0]['target_id']
    sid = cdp.attach(target)
    try:
        lease.assert_held()
        if activity(profile)[0] != port or browser_identity(profile, port) != identity:
            raise AuthBlocked('Profile browser identity changed')
        require_original(cdp, original, uc)
        page = next((p for p in cdp.pages() if p['targetId'] == target), None)
        if not page or page.get('openerId') != original:
            raise AuthBlocked('Authentication opener changed')
        cdp.call('Target.activateTarget', {'targetId': target})
        expression = password_expression(credentials.login)
        state = cdp.evaluate(sid, expression)
        if not state or not state['empty']:
            raise AuthBlocked('Exact account with empty enabled password input required')
        save_new(BASE / 'evidence' / f'auth_password_{target}.intent.json',
                 {'at_utc': now(), 'profile': profile, 'target': target, 'channel_uc': uc})
        if cdp.evaluate(sid, expression) != state:
            raise AuthBlocked('Password form changed after durable intent')
        cdp.click(sid, state['input'])
        focused = cdp.evaluate(sid, expression)
        if not focused or not focused['focused'] or not focused['empty']:
            raise AuthBlocked('Password input did not receive native focus')
        lease.assert_held()
        require_original(cdp, original, uc)
        if cdp.evaluate(sid, expression) != focused:
            raise AuthBlocked('Password focus or account changed before input')
        cdp.call('Input.insertText', {'text': credentials.password}, sid)
        ready = cdp.evaluate(sid, expression)
        if not ready or ready['empty'] or not ready['focused']:
            raise AuthBlocked('Password field input not acknowledged')
        lease.assert_held()
        require_original(cdp, original, uc)
        if cdp.evaluate(sid, expression) != ready:
            raise AuthBlocked('Password form changed before submit')
        cdp.click(sid, ready['next'])
        save_new(out / 'password_submit_ack.json', {'at_utc': now(), 'ack': True})
        time.sleep(5)
    finally:
        cdp.call('Target.detachFromTarget', {'sessionId': sid})

def totp_expression(login):
    # totp_dom.json, 2026-09-14T08:14:03Z: same account marker/Next topology.
    return password_expression(login).replace(
        '/v3/signin/challenge/pwd', '/v3/signin/challenge/totp'
    ).replace('input[type="password"][name="Passwd"]',
              'input[type="tel"][name="totpPin"][id="totpPin"]')

def submit_totp(cdp, profile, port, identity, lease, before, out):
    from google_auth_secrets import totp_code
    _, uc, original = BINDINGS[profile]
    credentials = configured_credentials(profile)
    synced = subprocess.run(['timedatectl', 'show', '-p', 'NTPSynchronized', '--value'],
                            capture_output=True, text=True, timeout=5)
    if synced.returncode or synced.stdout.strip() != 'yes':
        raise AuthBlocked('Synchronized system clock required for TOTP')
    pages = [p for p in before if p['opener_id'] == original
             and p['origin'] == 'https://accounts.google.com'
             and p['path'] == '/v3/signin/challenge/totp']
    if len(pages) != 1:
        raise AuthBlocked('One original-opener Google Authenticator challenge required')
    target = pages[0]['target_id']
    sid = cdp.attach(target)
    try:
        lease.assert_held()
        if activity(profile)[0] != port or browser_identity(profile, port) != identity:
            raise AuthBlocked('Profile browser identity changed')
        require_original(cdp, original, uc)
        page = next((p for p in cdp.pages() if p['targetId'] == target), None)
        if not page or page.get('openerId') != original:
            raise AuthBlocked('Authentication opener changed')
        cdp.call('Target.activateTarget', {'targetId': target})
        expression = totp_expression(credentials.login)
        state = cdp.evaluate(sid, expression)
        if not state or not state['empty']:
            raise AuthBlocked('Exact account with empty enabled Authenticator input required')
        save_new(BASE / 'evidence' / f'auth_totp_{target}.intent.json',
                 {'at_utc': now(), 'profile': profile, 'target': target, 'channel_uc': uc})
        if cdp.evaluate(sid, expression) != state:
            raise AuthBlocked('Authenticator form changed after durable intent')
        cdp.click(sid, state['input'])
        focused = cdp.evaluate(sid, expression)
        if not focused or not focused['focused'] or not focused['empty']:
            raise AuthBlocked('Authenticator input did not receive native focus')
        remaining = 30 - time.time() % 30
        if remaining < 12:
            time.sleep(remaining + 0.2)
        lease.assert_held()
        require_original(cdp, original, uc)
        if cdp.evaluate(sid, expression) != focused:
            raise AuthBlocked('Authenticator focus or account changed before input')
        issued = time.time()
        code, remaining = totp_code(credentials.totp, timestamp=issued)
        if remaining < 10:
            raise AuthBlocked('Insufficient TOTP lifetime; nothing entered')
        cdp.call('Input.insertText', {'text': code}, sid)
        code = None
        ready = cdp.evaluate(sid, expression)
        if not ready or ready['empty'] or not ready['focused']:
            raise AuthBlocked('Authenticator input not acknowledged')
        lease.assert_held()
        require_original(cdp, original, uc)
        if cdp.evaluate(sid, expression) != ready:
            raise AuthBlocked('Authenticator form changed before submit')
        current = time.time()
        if int(current // 30) != int(issued // 30) or 30 - current % 30 < 5:
            raise AuthBlocked('TOTP window changed before submit; no automatic retry')
        cdp.click(sid, ready['next'])
        save_new(out / 'totp_submit_ack.json', {'at_utc': now(), 'ack': True})
        time.sleep(5)
    finally:
        try:
            cdp.call('Target.detachFromTarget', {'sessionId': sid})
        except AuthBlocked:
            # Google normally closes this popup after successful reauthentication.
            if any(p['targetId'] == target for p in cdp.pages()):
                raise

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile', choices=tuple(BINDINGS), required=True)
    parser.add_argument('--action', choices=('inspect', 'click-next', 'choose-account', 'password', 'totp'), required=True)
    args = parser.parse_args()
    if socket.gethostname() != 'vova-System-Product-Name' or os.name != 'posix':
        raise AuthBlocked('Minsk is required')
    night = load('stress_night', NIGHT / 'night_upload.py')
    coordination = load('stress_coordination', SOURCE / 'src/upload_coordination.py')
    profile = args.profile
    uid, uc, original = BINDINGS[profile]
    with night.exclusive_lock(NIGHT / '.night_upload.lock'):
        with night.exclusive_lock(NIGHT / '.locks' / f'profile_{profile}.lock'):
            with coordination.profile_lease(profile, night.profile_lease_context()) as lease:
                port, hosts = activity(profile)
                identity = browser_identity(profile, port)
                cdp = CDP(port)
                try:
                    before = snapshot(cdp)
                    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                    out = BASE / 'evidence' / ('auth_' + profile + '_' + stamp)
                    save_new(out / 'before.json', {'at_utc': now(), 'hosts': hosts, 'browser': identity, 'pages': before})
                    if args.action == 'click-next':
                        page = next((p for p in before if p['target_id'] == original), None)
                        if not page or page['origin'] != 'https://studio.youtube.com' or not page['path'].startswith('/channel/' + uc + '/'):
                            raise AuthBlocked('Original exact channel target unavailable')
                        if any(p['origin'] == 'https://accounts.google.com' for p in before):
                            raise AuthBlocked('An authentication page already exists; inspect it first')
                        sid = cdp.attach(original)
                        try:
                            lease.assert_held()
                            if activity(profile)[0] != port:
                                raise AuthBlocked('Profile port changed')
                            if browser_identity(profile, port) != identity:
                                raise AuthBlocked('Browser process identity changed')
                            pages = cdp.pages()
                            if {p['targetId'] for p in pages} != {p['target_id'] for p in before}:
                                raise AuthBlocked('Page target set changed before Next')
                            cdp.call('Target.activateTarget', {'targetId': original})
                            point = cdp.evaluate(sid, NEXT_POINT)
                            if (not point or point['origin'] != 'https://studio.youtube.com'
                                    or not point['path'].startswith('/channel/' + uc + '/')
                                    or point['app_uc'] != uc):
                                raise AuthBlocked('Fresh exact channel and enabled native Next required')
                            marker = BASE / 'evidence' / f'auth_next_{original}.intent.json'
                            save_new(marker, {'at_utc': now(), 'profile': profile, 'target': original, 'channel_uc': uc})
                            current = cdp.evaluate(sid, NEXT_POINT)
                            if current != point:
                                raise AuthBlocked('Next state changed after durable intent; no input sent')
                            cdp.click(sid, current)
                            save_new(out / 'next_ack.json', {'at_utc': now(), 'ack': True})
                            time.sleep(5)
                        finally:
                            cdp.call('Target.detachFromTarget', {'sessionId': sid})
                    elif args.action == 'choose-account':
                        credentials = configured_credentials(profile)
                        pages = [p for p in before if p['opener_id'] == original
                                 and p['origin'] == 'https://accounts.google.com'
                                 and p['path'] == '/v3/signin/accountchooser']
                        if len(pages) != 1:
                            raise AuthBlocked('One original-opener Google account chooser required')
                        target = pages[0]['target_id']
                        sid = cdp.attach(target)
                        try:
                            expression = account_choice_expression(credentials.login)
                            lease.assert_held()
                            if activity(profile)[0] != port or browser_identity(profile, port) != identity:
                                raise AuthBlocked('Profile browser identity changed')
                            current_pages = cdp.pages()
                            current_page = next((p for p in current_pages if p['targetId'] == target), None)
                            if not current_page or current_page.get('openerId') != original:
                                raise AuthBlocked('Authentication opener changed')
                            cdp.call('Target.activateTarget', {'targetId': target})
                            point = cdp.evaluate(sid, expression)
                            if not point:
                                raise AuthBlocked('One exact configured Google account required')
                            save_new(BASE / 'evidence' / f'auth_account_{target}.intent.json',
                                     {'at_utc': now(), 'profile': profile, 'target': target, 'channel_uc': uc})
                            current = cdp.evaluate(sid, expression)
                            if current != point:
                                raise AuthBlocked('Account chooser changed after intent; no input sent')
                            cdp.click(sid, current)
                            save_new(out / 'account_ack.json', {'at_utc': now(), 'ack': True})
                            time.sleep(5)
                        finally:
                            cdp.call('Target.detachFromTarget', {'sessionId': sid})
                    elif args.action == 'password':
                        submit_password(cdp, profile, port, identity, lease, before, out)
                    elif args.action == 'totp':
                        submit_totp(cdp, profile, port, identity, lease, before, out)
                    after = snapshot(cdp)
                    save_new(out / 'after.json', {'at_utc': now(), 'pages': after})
                    print(json.dumps({'evidence': str(out), 'pages': after}, ensure_ascii=False), flush=True)
                finally:
                    cdp.ws.close()

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Coordinator/subprocess exceptions can embed secret arguments: no traceback.
        print(json.dumps({'blocked': True, 'error_type': type(exc).__name__,
                          'message': str(exc) if isinstance(exc, AuthBlocked) else 'Operation stopped; inspect sanitized evidence'}))
        raise SystemExit(2)
