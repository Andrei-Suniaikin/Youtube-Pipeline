"""Real HTTP auth, persistent store, dispatcher and provider adapter.

Only the external FastGen boundary is a local HTTP server; no live credentials.
"""
import os
import subprocess
import sys
from pathlib import Path


def test_own_lease_cancel_atomic_families_and_release(tmp_path, monkeypatch):
    import threading
    import time
    import json
    from concurrent.futures import ThreadPoolExecutor
    from cover_credentials import save_user_credentials, estimate_job_memory_mb
    from api_cover_helpers import acquire_server_keys_for_job, own_job_can_start
    from job_queue_cover import Job
    monkeypatch.setenv('COVER_USER_KEYS_DB',str(tmp_path/'private.db'))
    uid='cancel-owner-'+tmp_path.name
    save_user_credentials(uid,{'mode':'own','keys':{'fastgen':['test-own-key-001','test-own-key-002','test-own-key-003'],'openai':['test-openai-key-001']}})
    def job(provider='flow'):
        return Job(user_id=uid,credential_mode='own',mode='seed',generation_provider=provider,preset_data=json.dumps({'generation_provider':provider}))
    leases=[acquire_server_keys_for_job(job()) for _ in range(3)]
    assert len({lease.generation_key for lease in leases})==3
    assert not own_job_can_start(job('flower'),[job(),job(),job()])
    cancel=threading.Event()
    timer=threading.Timer(.1,cancel.set);timer.start()
    started=time.monotonic()
    try:
        acquire_server_keys_for_job(job(),cancel.is_set)
    except RuntimeError as exc:
        assert 'отменена' in str(exc)
    else:
        raise AssertionError('cancel did not interrupt the lease wait')
    assert time.monotonic()-started<1
    timer.join()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _:leases[0].release(),range(30)))
    again=acquire_server_keys_for_job(job())
    assert again.generation_key==leases[0].generation_key
    again.release()
    for lease in leases:lease.release()
    assert estimate_job_memory_mb(job())==512
    busy=job();busy.max_threads=8;busy.languages='["ru","en","de","fr","pl","cs","it","es"]'
    assert estimate_job_memory_mb(busy)==1408



def test_same_physical_key_two_owners_and_server(tmp_path, monkeypatch):
    import threading
    import time
    from api_cover_helpers import acquire_server_keys_for_job
    from cover_credentials import save_user_credentials
    from job_queue_cover import Job
    key = 'same-physical-' + tmp_path.name
    monkeypatch.setenv('COVER_USER_KEYS_DB', str(tmp_path/'private.db'))
    monkeypatch.setenv('COVER_FAST_GEN_KEY', key)
    monkeypatch.delenv('COVER_FAST_GEN_KEYS', raising=False)
    for uid in ('physical-owner-a', 'physical-owner-b'):
        save_user_credentials(uid, {'mode':'own','keys':{'fastgen':[key]}})
    def job(uid, mode='own'):
        return Job(user_id=uid, credential_mode=mode, mode='seed', generation_provider='flow')
    def blocked(candidate):
        stop=threading.Event();timer=threading.Timer(.1,stop.set);timer.start()
        try:
            acquire_server_keys_for_job(candidate, stop.is_set)
        except RuntimeError as exc:
            assert 'отменена' in str(exc)
        else:
            raise AssertionError('one physical credential acquired twice')
        finally:timer.join()
    # Shared goes FIRST after keys were persisted, before any own lease exists.
    shared=acquire_server_keys_for_job(job('neighbor','server'))
    blocked(job('physical-owner-a'))
    shared.release()
    first=acquire_server_keys_for_job(job('physical-owner-a'))
    blocked(job('physical-owner-b'))
    blocked(job('neighbor','server'))
    first.release()
    with acquire_server_keys_for_job(job('physical-owner-b')) as lease:
        assert lease.generation_key == key
    with acquire_server_keys_for_job(job('neighbor','server')) as lease:
        assert lease.generation_key == key

def test_own_pool_100_jobs_real_dispatch_and_provider_boundary(tmp_path):
    project = Path(__file__).parents[1]
    script = r'''
import os,json,threading,time,base64,io,sqlite3
from collections import Counter
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from PIL import Image
from fastapi.testclient import TestClient

keys=['own-fastgen-alpha','own-fastgen-bravo','own-fastgen-charlie']
lock=threading.Lock(); calls=Counter(); inflight=Counter(); peaks=Counter(); remote={}; server_peak=0
buf=io.BytesIO();Image.new('RGB',(128,72),'navy').save(buf,format='PNG')
uri='data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode()
class Vendor(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def send(self,data):
        raw=json.dumps(data).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(raw)
    def do_POST(self):
        global server_peak
        assert self.path=='/api/v6/generations'
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        key=self.headers['X-API-Key']; assert key in keys+['server-fastgen-neighbor']
        with lock:
            rid=str(len(remote)+1);remote[rid]=key;calls[key]+=1;inflight[key]+=1;peaks[key]=max(peaks[key],inflight[key]);server_peak=max(server_peak,sum(inflight.values()))
        self.send({'id':rid})
    def do_GET(self):
        if self.path=='/api/v5/usage':self.send({'max_threads':8});return
        rid=self.path.rsplit('/',1)[-1]
        with lock:
            key=remote[rid];assert self.headers['X-API-Key']==key;inflight[key]-=1
        self.send({'status':'succeeded','results':[{'data':uri}]})
vendor=ThreadingHTTPServer(('127.0.0.1',0),Vendor)
threading.Thread(target=vendor.serve_forever,daemon=True).start()
os.environ['FAST_GEN_API_BASE']='http://127.0.0.1:'+str(vendor.server_port)
import cover_server as app
from api_cover_helpers import acquire_server_keys_for_job
from cover_credentials import read_user_credentials,redact_credentials
client=TestClient(app.app)
r=client.post('/auth/setup',json={'username':'pool_owner','password':'safe-test-password'});assert r.status_code==200,r.text
h={'X-Session-Id':r.json()['session_id']};uid=app.license_mgr.get_user_id(r.json()['session_id'])
assert client.get('/settings/vendor-keys').status_code==401
assert client.post('/settings/vendor-keys',headers=h,json={'user_id':'other','mode':'own'}).status_code==422
r=client.post('/settings/vendor-keys',headers=h,json={'mode':'own','keys':{'fastgen':keys+[keys[0]],'openai':['own-openai-alpha'],'veo':['own-veo-alpha'],'xai':['own-xai-alpha']}})
assert r.status_code==200,r.text
assert r.json()['counts']=={'fastgen':3,'openai':1,'veo':1,'xai':1}
assert not any(k in r.text for k in keys)
assert (os.stat(os.environ['COVER_USER_KEYS_DB']).st_mode & 0o777)==0o600
assert read_user_credentials(uid)['keys']['fastgen']==keys
assert '[REDACTED]' in redact_credentials('provider echoes '+keys[0])
# Separate account cannot read or override owner's settings; legacy server route survives.
r=client.post('/auth/accounts',headers=h,json={'username':'pool_neighbor','password':'safe-test-password','user_name':'Neighbor'});assert r.status_code==200,r.text
r=client.post('/auth/login-password',json={'username':'pool_neighbor','password':'safe-test-password'});assert r.status_code==200,r.text
nh={'X-Session-Id':r.json()['session_id']}
r=client.get('/settings/vendor-keys',headers=nh);assert r.json()['mode']=='server' and not any(r.json()['counts'].values())
app.ApiAuthState.configure(app.DB_PATH);app.ApiAuthState.init_db()
r=client.post('/api-keys',headers=h,json={'name':'own-test'});assert r.status_code==200,r.text
api_h={'X-API-Key':r.json()['plain_key']}
assert client.get('/api/v1/settings/vendor-keys',headers=api_h).json()['counts']['fastgen']==3

def submit(i,headers=h):
    b={'mode':'seed','languages':['Русский'],'seeds_per_lang':{'Русский':'Unique seed '+str(i)},'n_variants':1,'preset_name':'default','preset_data':{'name':'default','generation_provider':'flow','generation_model':'GEM_PIX_2'}}
    r=client.post('/jobs/cover',headers=headers,json=b);assert r.status_code==200,r.text
    return r.json()['job_id']
ids=[submit(i) for i in range(100)]
neighbor_ids=[submit('neighbor-'+str(i),nh) for i in range(2)]
assert all(app.queue.get_job(i).credential_mode=='own' for i in ids)
assert all(app.queue.get_job(i).credential_mode=='server' for i in neighbor_ids)
app.worker.start()
deadline=time.monotonic()+180
while time.monotonic()<deadline:
    jobs=[app.queue.get_job(i) for i in ids+neighbor_ids]
    if all(j.status in ('completed','failed','cancelled','completed_partial') for j in jobs):break
    time.sleep(.2)
app.worker.stop()
assert all(j.status=='completed' for j in jobs),[(j.id,j.status,j.error) for j in jobs if j.status!='completed']
assert sum(calls[k] for k in keys)==100,calls
assert all(calls[k]>=30 for k in keys),calls
assert all(peaks[k]==1 for k in keys),peaks
assert calls['server-fastgen-neighbor']==2 and peaks['server-fastgen-neighbor']==1
assert 3<=server_peak<=int(os.environ['COVER_JOB_SLOTS']),server_peak
assert all(len(json.loads(j.output_files))==1 for j in jobs)
raw=open(app.DB_PATH,'rb').read()
assert not any(k.encode() in raw for k in keys)
assert not any(k in json.dumps(client.get('/jobs',headers=h).json()) for k in keys)
# A queued own job remains own-only even if the account later switches to server.
r=client.post('/settings/vendor-keys',headers=h,json={'mode':'server'});assert r.status_code==200
j=app.queue.get_job(ids[0])
with acquire_server_keys_for_job(j) as lease:assert lease.generation_key in keys
r=client.post('/settings/vendor-keys',headers=h,json={'keys':{'fastgen':[]}});assert r.status_code==200
try:acquire_server_keys_for_job(j)
except RuntimeError as e:assert 'серверные ключи не используются' in str(e)
else:raise AssertionError('own job fell back to server')
print(json.dumps({'jobs_completed':len(jobs),'own_jobs':100,'own_keys_used':len(keys),'calls_per_own_key':[calls[k] for k in keys],'per_key_peak':[peaks[k] for k in keys],'machine_peak':server_peak,'neighbor_server_jobs':calls['server-fastgen-neighbor'],'tenant_isolation':True,'own_mode_pinned':True,'secrets_not_in_jobs_or_responses':True}))
vendor.shutdown()
'''
    env = dict(os.environ, PYTHONPATH=str(project), COVER_SERVER_DIR=str(tmp_path), COVER_DB_PATH=str(tmp_path/'jobs.db'), COVER_SESSION_DB=str(tmp_path/'sessions.db'), COVER_USER_KEYS_DB=str(tmp_path/'own.db'), COVER_JOB_SLOTS='4', COVER_FASTGEN_JOB_SLOTS='1', COVER_MAX_THREADS_PER_JOB='1', COVER_FAST_GEN_KEY='server-fastgen-neighbor', COVER_OPENAI_KEY='server-openai-neighbor', FAST_GEN_BUDGET_OVERRIDE='8')
    result = subprocess.run([sys.executable,'-c',script],cwd=project,env=env,capture_output=True,text=True,timeout=200)
    assert result.returncode==0, result.stdout+'\n'+result.stderr
    print(result.stdout.strip())
