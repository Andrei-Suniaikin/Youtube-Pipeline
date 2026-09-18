"""Verifier probe: submit fails WITHOUT a generation ID (HTTP 502 / 429 on FastGen v6,
or HTTP 400 from direct xAI). Does the slot become permanently unrecoverable?
Isolated stand: temp DBs, dummy keys, localhost stub. No real provider calls.
SCEN = post502 | post429 | xai400
"""
import io, json, base64, threading, time, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from PIL import Image
from fastapi.testclient import TestClient

SCEN = os.environ.get("SCEN", "post502")
b = io.BytesIO(); Image.new('RGB', (64, 36), 'navy').save(b, format='PNG')
uri = 'data:image/png;base64,' + base64.b64encode(b.getvalue()).decode()
posts = []
fail_mode = {'on': True}


class Vendor(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, data, status=200):
        self.send_response(status); self.send_header('Content-Type', 'application/json'); self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        posts.append(self.path)
        if '/xai/' in self.path:
            if fail_mode['on']:
                self.send({'error': 'moderation: request rejected'}, 400); return
            self.send({'data': [{'b64_json': uri.split(',', 1)[1]}]}); return
        if fail_mode['on']:
            self.send({'error': 'x'}, 502 if SCEN == 'post502' else 429); return
        self.send({'id': 'gen-%d' % len(posts)})

    def do_GET(self):
        if self.path.endswith('/usage'):
            self.send({'max_threads': 8}); return
        self.send({'status': 'succeeded', 'results': [{'data': uri}]})


vendor = ThreadingHTTPServer(('127.0.0.1', 0), Vendor)
threading.Thread(target=vendor.serve_forever, daemon=True).start()
base = 'http://127.0.0.1:%d' % vendor.server_port
os.environ['FAST_GEN_API_BASE'] = base
import cover_core
cover_core.XAI_BASE_URL = base + '/xai'
cover_core.FAST_GEN_API_BASE = base
import cover_server as app
client = TestClient(app.app)
r = client.post('/auth/setup', json={'username': 'verifier', 'password': 'verifier-local-123'})
h = {'X-Session-Id': r.json()['session_id']}
assert client.post('/settings/vendor-keys', headers=h, json={'mode': 'own', 'keys': {
    'fastgen': ['dummy-fastgen-key-1'], 'xai': ['dummy-xai-key-1']}}).status_code == 200
provider = 'grok_api' if SCEN == 'xai400' else 'flow'
body = {'mode': 'seed', 'languages': ['Русский'], 'seeds_per_lang': {'Русский': 'item'}, 'n_variants': 1,
        'preset_data': {'name': 'default', 'generation_provider': provider}}
r = client.post('/jobs/cover', headers=h, json=body); assert r.status_code == 200, r.text
jid = r.json()['job_id']
app.worker.start()


def terminal():
    lim = time.monotonic() + 40
    while time.monotonic() < lim:
        j = app.queue.get_job(jid)
        if j.status not in ('pending', 'running'):
            return j
        time.sleep(.05)
    raise AssertionError('not terminal')


out = {'scen': SCEN, 'provider': provider, 'retries_setting': app._server_max_retries()}
j = terminal(); out['run1'] = dict(status=j.status, results=j.results_count, error=j.error[:170], posts=len(posts))
out['manifest1'] = [(x['item'], x['state'], bool(x['remote_id'])) for x in client.get('/jobs/' + jid + '/recovery', headers=h).json()['items']]
# provider is healthy again from here on
fail_mode['on'] = False
r = client.post('/jobs/' + jid + '/resume', headers=h, json={})
j = terminal(); out['resume_plain'] = dict(http=r.status_code, status=j.status, results=j.results_count, posts=len(posts), error=j.error[:170])
alt = 'flow' if provider == 'grok_api' else 'grok'
r = client.post('/jobs/' + jid + '/resume', headers=h, json={'override_provider': alt, 'override_model': ''})
if r.status_code == 200:
    j = terminal(); out['resume_override'] = dict(http=200, status=j.status, results=j.results_count, posts=len(posts), error=j.error[:170])
else:
    out['resume_override'] = dict(http=r.status_code, detail=r.json().get('detail', '')[:170])
r = client.post('/jobs/' + jid + '/retry', headers=h)
out['retry_http'] = (r.status_code, (r.json().get('detail') or r.json().get('status') or '')[:120])
app.worker.stop(); vendor.shutdown()
print(json.dumps(out, ensure_ascii=False, indent=1))
