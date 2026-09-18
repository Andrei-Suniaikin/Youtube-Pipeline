"""Real Cover worker failure and lease release; fake only provider HTTP."""
import os
import subprocess
import sys
from pathlib import Path


def test_expired_key_does_not_hold_lease_or_block_good_key(tmp_path):
    project=Path(__file__).parents[1]
    script=r'''
import os,json,time,threading,io,base64
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from PIL import Image
buf=io.BytesIO();Image.new('RGB',(64,36)).save(buf,format='PNG')
uri='data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode()
calls=[]
class Vendor(BaseHTTPRequestHandler):
    def log_message(self,*a):pass
    def send(self,data,status=200):
        self.send_response(status);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(data).encode())
    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        key=self.headers['X-API-Key'];calls.append(key)
        if key=='own-expired-key':self.send({'error':'invalid key'},401)
        else:self.send({'id':'one-remote-id'})
    def do_GET(self):
        if self.path.endswith('/usage'):self.send({'max_threads':8})
        else:self.send({'status':'succeeded','results':[{'data':uri}]})
vendor=ThreadingHTTPServer(('127.0.0.1',0),Vendor);threading.Thread(target=vendor.serve_forever,daemon=True).start()
os.environ['FAST_GEN_API_BASE']='http://127.0.0.1:'+str(vendor.server_port)
import cover_server as app
from cover_credentials import save_user_credentials
from api_cover_helpers import acquire_server_keys_for_job
from job_queue_cover import Job
save_user_credentials('failure-owner',{'mode':'own','keys':{'fastgen':['own-expired-key','own-working-key']}})
ids=[]
for i in range(2):
    j=Job(user_id='failure-owner',credential_mode='own',mode='seed',generation_provider='flow',languages=json.dumps(['Русский']),seeds_per_lang=json.dumps({'Русский':'seed '+str(i)}),n_variants=1,preset_data=json.dumps({'name':'default','generation_provider':'flow'}))
    app.queue.add_job(j);ids.append(j.id)
app.worker.start();deadline=time.monotonic()+45
while time.monotonic()<deadline:
    jobs=[app.queue.get_job(i) for i in ids]
    if all(j.status in ('completed','completed_partial','failed') for j in jobs):break
    time.sleep(.1)
app.worker.stop()
assert sorted(j.status for j in jobs)==['completed','completed_partial'],[(j.status,j.error) for j in jobs]
assert set(calls)=={'own-expired-key','own-working-key'},calls
assert all('server-' not in key for key in calls)
assert all('own-expired-key' not in j.log for j in jobs)
import cover_core
cover_core.log_event('error','echoed credential own-expired-key')
assert 'own-expired-key' not in open(os.environ['COVER_EVENTS_LOG']).read()
save_user_credentials('failure-owner',{'keys':{'fastgen':['own-expired-key']}})
with acquire_server_keys_for_job(jobs[0]) as lease:assert lease.generation_key=='own-expired-key'
print(json.dumps({'expired_key_terminal':'completed_partial','neighbor_key_completed':True,'expired_lease_released':True,'calls':len(calls)}))
vendor.shutdown()
'''
    env=dict(os.environ,PYTHONPATH=str(project),COVER_SERVER_DIR=str(tmp_path),COVER_DB_PATH=str(tmp_path/'jobs.db'),COVER_SESSION_DB=str(tmp_path/'sessions.db'),COVER_USER_KEYS_DB=str(tmp_path/'own.db'),COVER_EVENTS_LOG=str(tmp_path/'events.log'),COVER_MAX_THREADS_PER_JOB='1',COVER_MAX_GENERATION_RETRIES='1',COVER_JOB_SLOTS='2',FAST_GEN_BUDGET_OVERRIDE='8')
    result=subprocess.run([sys.executable,'-c',script],cwd=project,env=env,text=True,capture_output=True,timeout=60)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
    print(result.stdout.strip())


def test_shared_physical_key_waits_pending_across_owners(tmp_path):
    project=Path(__file__).parents[1]
    script=r'''
import os,json,time,threading,io,base64
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from PIL import Image
buf=io.BytesIO();Image.new('RGB',(64,36)).save(buf,format='PNG')
uri='data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode()
started=threading.Event();release=threading.Event();calls=0;active=0;peak=0;lock=threading.Lock()
class Vendor(BaseHTTPRequestHandler):
    def log_message(self,*a):pass
    def send(self,data):
        self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(data).encode())
    def do_POST(self):
        global calls,active,peak
        self.rfile.read(int(self.headers['Content-Length']))
        with lock:calls+=1;active+=1;peak=max(peak,active)
        started.set();self.send({'id':str(calls)})
    def do_GET(self):
        global active
        if self.path.endswith('/usage'):self.send({'max_threads':8});return
        assert release.wait(10)
        with lock:active-=1
        self.send({'status':'succeeded','results':[{'data':uri}]})
vendor=ThreadingHTTPServer(('127.0.0.1',0),Vendor);threading.Thread(target=vendor.serve_forever,daemon=True).start()
os.environ['FAST_GEN_API_BASE']='http://127.0.0.1:'+str(vendor.server_port)
import cover_server as app
from cover_credentials import save_user_credentials
from job_queue_cover import Job
ids=[]
for i,mode in enumerate(('own','own','server')):
    uid='owner-'+str(i)
    if mode=='own':save_user_credentials(uid,{'mode':'own','keys':{'fastgen':['same-physical-key']}})
    j=Job(user_id=uid,credential_mode=mode,mode='seed',generation_provider='flow',languages=json.dumps(['Русский']),seeds_per_lang=json.dumps({'Русский':'seed '+str(i)}),n_variants=1,preset_data=json.dumps({'name':'default','generation_provider':'flow'}))
    app.queue.add_job(j);ids.append(j.id)
app.worker.start();assert started.wait(10);time.sleep(.4)
statuses=[app.queue.get_job(i).status for i in ids]
assert sorted(statuses)==['pending','pending','running'],statuses
release.set();deadline=time.monotonic()+30
while time.monotonic()<deadline:
    jobs=[app.queue.get_job(i) for i in ids]
    if all(j.status=='completed' for j in jobs):break
    time.sleep(.1)
app.worker.stop()
assert all(j.status=='completed' for j in jobs),[(j.status,j.error) for j in jobs]
assert calls==3 and peak==1,(calls,peak)
print(json.dumps({'same_key_owners':2,'same_key_shared_neighbors':1,'initial_states':statuses,'completed':3,'physical_peak':peak,'no_busy_failure':True}))
vendor.shutdown()
'''
    env=dict(os.environ,PYTHONPATH=str(project),COVER_SERVER_DIR=str(tmp_path),COVER_DB_PATH=str(tmp_path/'jobs.db'),COVER_SESSION_DB=str(tmp_path/'sessions.db'),COVER_USER_KEYS_DB=str(tmp_path/'own.db'),COVER_MAX_THREADS_PER_JOB='1',COVER_JOB_SLOTS='4',COVER_FASTGEN_JOB_SLOTS='4',COVER_FAST_GEN_KEY='same-physical-key',FAST_GEN_BUDGET_OVERRIDE='8')
    result=subprocess.run([sys.executable,'-c',script],cwd=project,env=env,text=True,capture_output=True,timeout=45)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
    print(result.stdout.strip())
