"""Network-boundary recovery tests; queue, journal and allocator remain real."""
import hashlib,json,threading,time,io,base64
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from PIL import Image
from job_queue_cover import Job,JobQueue,IdempotencyConflict
from cover_recovery import GenerationJournal,item_context,RecoveryRequired


def claimed(tmp_path):
    queue=JobQueue(str(tmp_path/'jobs.db'))
    job=Job(user_id='owner',mode='seed',generation_provider='flow')
    queue.add_job(job)
    result=queue.claim_next_for_user('owner')
    assert result
    output=tmp_path/'out';output.mkdir()
    return queue,result,GenerationJournal(queue.db_path,result.id,result.run_token,output)


def test_create_idempotency_100_concurrent_requests(tmp_path):
    queue=JobQueue(str(tmp_path/'jobs.db'))
    def create(_):return queue.add_job(Job(user_id='owner'),idempotency_key='one-request-key',request_hash='same-body')
    with ThreadPoolExecutor(max_workers=16) as pool:ids=list(pool.map(create,range(100)))
    assert len(set(ids))==1 and len(queue.get_all_jobs())==1
    with pytest.raises(IdempotencyConflict):queue.add_job(Job(user_id='owner'),idempotency_key='one-request-key',request_hash='changed')
    assert queue.add_job(Job(user_id='other'),idempotency_key='one-request-key',request_hash='same-body')!=ids[0]
    queue.delete_job(ids[0])
    with pytest.raises(IdempotencyConflict):create(1)


def test_known_remote_id_survives_restart_and_uses_same_key(tmp_path):
    import cover_core
    queue,job,journal=claimed(tmp_path)
    calls={'post':0,'get':0};key='own-test-recovery-key'
    b=io.BytesIO();Image.new('RGB',(48,24),'green').save(b,format='PNG');uri='data:image/png;base64,'+base64.b64encode(b.getvalue()).decode()
    class Vendor(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            calls['post']+=1;self.send_response(500);self.end_headers()
        def do_GET(self):
            assert self.headers['X-API-Key']==key
            calls['get']+=1;self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps({'status':'succeeded','results':[{'data':uri}]}).encode())
    server=ThreadingHTTPServer(('127.0.0.1',0),Vendor);threading.Thread(target=server.serve_forever,daemon=True).start()
    with item_context(journal,'seed:ru:1'):
        journal.begin_call('fastgen',key)
        journal.update_call(remote_id='persisted-id',state='submitted')
    # Actual queue recovery changes the run token; a new journal reads existing ID.
    queue.recover_interrupted_jobs();job=queue.claim_next_for_user('owner')
    restarted=GenerationJournal(queue.db_path,job.id,job.run_token,journal.output_dir)
    assert restarted.credential_pins()=={'fastgen':hashlib.sha256(key.encode()).hexdigest()}
    with item_context(restarted,'seed:ru:1'):
        with pytest.raises(RecoveryRequired):restarted.begin_call('fastgen','another-physical-key')
    with item_context(restarted,'seed:ru:1'):
        restarted.begin_call('fastgen',key)
        image=cover_core.generate_image_v6('same-task',key,'image_generate',base_url='http://127.0.0.1:'+str(server.server_port),poll_interval=.01,poll_max_wait=2)
        assert image.size==(48,24);restarted.cache_image(image)
    assert calls=={'post':0,'get':1}
    # Terminal cache survives another process/journal instance and needs no POST.
    with item_context(restarted,'seed:ru:1'):
        image=cover_core.generate_image_via_fast_gen('same-task',key,'flow')
        assert image.size==(48,24)
    assert calls['post']==0
    server.shutdown()


def test_uncertain_submit_never_resubmits_after_retry(tmp_path):
    import cover_core
    queue,job,journal=claimed(tmp_path);posts=[]
    class Vendor(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            posts.append(1);self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(503);self.end_headers();self.wfile.write(b'upstream outcome unknown')
    server=ThreadingHTTPServer(('127.0.0.1',0),Vendor);threading.Thread(target=server.serve_forever,daemon=True).start()
    with item_context(journal,'seed:ru:1'):
        journal.begin_call('fastgen','own-recovery-key')
        with pytest.raises(RecoveryRequired):cover_core.generate_image_v6('p','own-recovery-key','image_generate',base_url='http://127.0.0.1:'+str(server.server_port),max_retries=20)
    for _ in range(3):
        with item_context(journal,'seed:ru:1'):
            with pytest.raises(RecoveryRequired):journal.begin_call('fastgen','own-recovery-key')
    assert len(posts)==1
    assert journal.manifest()['items'][0]['state']=='uncertain'
    server.shutdown()


def test_stale_owner_cannot_publish_and_atomic_image_verified(tmp_path):
    queue,job,journal=claimed(tmp_path);image=Image.new('RGB',(48,24),'red');dest=journal.output_dir/'a.jpg'
    journal.publish(image,dest)
    digest=hashlib.sha256(dest.read_bytes()).hexdigest()
    queue.recover_interrupted_jobs();new=queue.claim_next_for_user('owner')
    with pytest.raises(RecoveryRequired):journal.publish(Image.new('RGB',(48,24),'blue'),dest)
    assert hashlib.sha256(dest.read_bytes()).hexdigest()==digest
    assert new.run_token!=job.run_token


@pytest.mark.parametrize("mode",["seed","title","url","title_qc"])
def test_process_kill_after_real_submit_resumes_without_paid_duplicate(tmp_path,mode):
    import os,subprocess,sys,sqlite3
    from cover_credentials import save_user_credentials
    project=Path(__file__).parents[1]
    stats={'post':0,'get':0,'keys':[],'llm':0};first=threading.Event()
    b=io.BytesIO();Image.new('RGB',(64,36),'navy').save(b,format='PNG');uri='data:image/png;base64,'+base64.b64encode(b.getvalue()).decode()
    thumb_bytes=io.BytesIO();Image.new('RGB',(320,180),'orange').save(thumb_bytes,format='PNG',compress_level=0)
    class Vendor(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,data):
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(data).encode())
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path.endswith('/chat/completions'):
                stats['llm']+=1
                content=json.dumps({'scene_description':'different scene '+str(stats['llm']),'text':'different text '+str(stats['llm']),'overall_pass':not(mode=='title_qc' and stats['llm']==2),'issues':'','characters':[],'mood':'bright'})
                self.send({'id':'chat-test','object':'chat.completion','created':1,'model':'test','choices':[{'index':0,'message':{'role':'assistant','content':content},'finish_reason':'stop'}]});return
            stats['post']+=1;stats['keys'].append(self.headers['X-API-Key'])
            if stats['post']==(2 if mode=='title_qc' else 1):first.set()
            self.send({'id':'real-submitted-unique-id-'+str(stats['post'])})
        def do_GET(self):
            if '/fixture-youtube/' in self.path:
                if 'oembed' in self.path:self.send({'title':'Original source title'});return
                self.send_response(200);self.send_header('Content-Type','image/png');self.end_headers();self.wfile.write(thumb_bytes.getvalue());return
            if self.path.endswith('/usage'):self.send({'max_threads':8});return
            stats['get']+=1;stats['keys'].append(self.headers['X-API-Key']);self.send({'status':'succeeded','results':[{'data':uri}]})
    server=ThreadingHTTPServer(('127.0.0.1',0),Vendor);threading.Thread(target=server.serve_forever,daemon=True).start()
    env=dict(os.environ,PYTHONPATH=str(project),COVER_SERVER_DIR=str(tmp_path),COVER_DB_PATH=str(tmp_path/'jobs.db'),COVER_SESSION_DB=str(tmp_path/'sessions.db'),COVER_USER_KEYS_DB=str(tmp_path/'own.db'),FAST_GEN_API_BASE='http://127.0.0.1:'+str(server.server_port),COVER_JOB_SLOTS='1',COVER_MAX_THREADS_PER_JOB='1',FAST_GEN_BUDGET_OVERRIDE='8',COVER_TEST_MODE=('title' if mode=='title_qc' else mode),OPENAI_BASE_URL='http://127.0.0.1:'+str(server.server_port)+'/v1')
    initialize=r'''
import cover_server as app,json,os
from cover_credentials import save_user_credentials
from job_queue_cover import Job
save_user_credentials('kill-owner',{'mode':'own','keys':{'fastgen':['original-recovery-key','different-recovery-key'],'openai':['original-openai-test-key']}})
j=Job(user_id='kill-owner',credential_mode='own',mode=os.environ['COVER_TEST_MODE'],generation_provider='flow',languages=json.dumps(['Русский']),seeds_per_lang=json.dumps({'Русский':'persisted seed'}),titles_per_lang=json.dumps({'Русский':'Original title'}),urls=json.dumps(['https://www.youtube.com/watch?v=dQw4w9WgXcQ']),n_variants=1,preset_data=json.dumps({'name':'default','generation_provider':'flow','use_auto_style':False,'simple_mode':False}))
print(app.queue.add_job(j))
'''
    run=r'''
import os,requests,urllib.parse
original_request=requests.sessions.Session.request
def local_transport(self,method,url,*args,**kwargs):
    parsed=urllib.parse.urlparse(url)
    if parsed.hostname in ('www.youtube.com','youtube.com','img.youtube.com','i.ytimg.com'):
        url=os.environ['FAST_GEN_API_BASE']+'/fixture-youtube'+parsed.path+('?' + parsed.query if parsed.query else '')
    return original_request(self,method,url,*args,**kwargs)
requests.sessions.Session.request=local_transport
import cover_server as app,time,json
app.queue.recover_interrupted_jobs();app.worker.start()
limit=time.monotonic()+25
while time.monotonic()<limit:
    jobs=app.queue.get_all_jobs()
    if all(j.status in ('completed','completed_partial','failed') for j in jobs):break
    time.sleep(.05)
app.worker.stop()
print(json.dumps([{'status':j.status,'count':j.results_count,'files':json.loads(j.output_files),'error':j.error} for j in app.queue.get_all_jobs()]))
'''
    result=subprocess.run([sys.executable,'-c',initialize],cwd=project,env=env,text=True,capture_output=True,check=True)
    child=subprocess.Popen([sys.executable,'-c',run],cwd=project,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        if not first.wait(10):
            child.kill();out,err=child.communicate(timeout=5)
            raise AssertionError(out+err)
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            with sqlite3.connect(tmp_path/'jobs.db') as con:
                rows=con.execute('SELECT remote_id,state FROM cover_remote ORDER BY ordinal').fetchall()
            if len(rows)==(2 if mode=='title_qc' else 1) and rows[-1][0]:break
            time.sleep(.01)
        assert rows[-1][0]=='real-submitted-unique-id-'+str(2 if mode=='title_qc' else 1)
        llm_before_restart=stats['llm']
        child.kill();child.wait(timeout=5)
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=5)
    # Deliberately change pool order: successful polling MUST stay on old credential.
    reorder=r'''from cover_credentials import save_user_credentials;save_user_credentials('kill-owner',{'keys':{'fastgen':['different-recovery-key','original-recovery-key']}})'''
    subprocess.run([sys.executable,'-c',reorder],cwd=project,env=env,check=True)
    result=subprocess.run([sys.executable,'-c',run],cwd=project,env=env,text=True,capture_output=True,timeout=35)
    assert result.returncode==0,result.stdout+result.stderr
    receipt=json.loads(result.stdout.strip().splitlines()[-1]);assert receipt[0]['status']=='completed',receipt
    assert receipt[0]['count']==1 and len(receipt[0]['files'])==1
    assert stats['post']==(2 if mode=='title_qc' else 1) and stats['get']==stats['post'],stats
    with sqlite3.connect(tmp_path/'jobs.db') as con:
        assert con.execute("SELECT COUNT(*) FROM cover_remote WHERE state!='ready'").fetchone()[0]==0
    assert set(stats['keys'])=={'original-recovery-key'},stats
    assert stats['llm']-llm_before_restart == (0 if mode=='seed' else 1),stats
    print(json.dumps({'mode':mode,'llm_before_restart':llm_before_restart,'llm_total':stats['llm'],'killed_after_durable_submit':True,'submits':stats['post'],'polls':stats['get'],'same_original_credential':True,'completed':1}))
    server.shutdown()


def test_authenticated_create_idempotency_under_full_queue(tmp_path):
    import os,subprocess,sys
    project=Path(__file__).parents[1]
    script=r'''
from fastapi.testclient import TestClient
from concurrent.futures import ThreadPoolExecutor
import cover_server as app,json
client=TestClient(app.app)
r=client.post('/auth/setup',json={'username':'idem-owner','password':'test-local-123'});assert r.status_code==200
h={'X-Session-Id':r.json()['session_id'],'Idempotency-Key':'cover-http-request-001'}
r=client.post('/settings/vendor-keys',headers=h,json={'mode':'own','keys':{'fastgen':['idem-dummy-key']}});assert r.status_code==200
body={'mode':'seed','languages':['Русский'],'seeds_per_lang':{'Русский':'A valid deterministic seed'},'n_variants':1,'preset_data':{'name':'default','generation_provider':'flow'}}
def submit(_):
    result=client.post('/jobs/cover',headers=h,json=body)
    assert result.status_code==200,result.text
    return result.json()['job_id']
with ThreadPoolExecutor(max_workers=16) as pool:ids=list(pool.map(submit,range(100)))
assert len(set(ids))==1 and len(app.queue.get_all_jobs())==1
changed={**body,'seeds_per_lang':{'Русский':'different seed'}}
assert client.post('/jobs/cover',headers=h,json=changed).status_code==409
assert client.post('/jobs/cover',headers={**h,'Idempotency-Key':'different-request-002'},json=body).status_code==429
assert client.get('/jobs/'+ids[0]+'/recovery').status_code==401
print(json.dumps({'http_requests':100,'jobs_created':1,'changed_body':409,'new_job_when_full':429}))
'''
    env=dict(os.environ,PYTHONPATH=str(project),COVER_SERVER_DIR=str(tmp_path),COVER_DB_PATH=str(tmp_path/'jobs.db'),COVER_SESSION_DB=str(tmp_path/'sessions.db'),COVER_USER_KEYS_DB=str(tmp_path/'own.db'),COVER_MAX_PENDING='1')
    result=subprocess.run([sys.executable,'-c',script],cwd=project,env=env,text=True,capture_output=True,timeout=40)
    assert result.returncode==0,result.stdout+result.stderr
    print(result.stdout.strip())


def test_partial_resume_keeps_successful_item_and_quarantines_unknown(tmp_path):
    import os,subprocess,sys
    project=Path(__file__).parents[1]
    script=r'''
import json,time,threading,io,base64,hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from PIL import Image
from fastapi.testclient import TestClient
b=io.BytesIO();Image.new('RGB',(64,36),'green').save(b,format='PNG');uri='data:image/png;base64,'+base64.b64encode(b.getvalue()).decode()
posts=[]
class Vendor(BaseHTTPRequestHandler):
    def log_message(self,*a):pass
    def send(self,data,status=200):
        self.send_response(status);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(data).encode())
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));posts.append(body['prompt'])
        if 'uncertain' in body['prompt']:self.send({'error':'upstream outcome unknown'},503)
        else:self.send({'id':'accepted-id'})
    def do_GET(self):
        if self.path.endswith('/usage'):self.send({'max_threads':8})
        else:self.send({'status':'succeeded','results':[{'data':uri}]})
vendor=ThreadingHTTPServer(('127.0.0.1',0),Vendor);threading.Thread(target=vendor.serve_forever,daemon=True).start()
import os
os.environ['FAST_GEN_API_BASE']='http://127.0.0.1:'+str(vendor.server_port)
import cover_server as app
client=TestClient(app.app)
r=client.post('/auth/setup',json={'username':'partial_owner','password':'test-local-123'});h={'X-Session-Id':r.json()['session_id']}
assert client.post('/settings/vendor-keys',headers=h,json={'mode':'own','keys':{'fastgen':['partial-dummy-key']}}).status_code==200
body={'mode':'seed','languages':['Русский','Английский'],'seeds_per_lang':{'Русский':'good-item','Английский':'uncertain-item'},'n_variants':1,'preset_data':{'name':'default','generation_provider':'flow'}}
r=client.post('/jobs/cover',headers=h,json=body);assert r.status_code==200,r.text;jid=r.json()['job_id']
app.worker.start()
def terminal():
    limit=time.monotonic()+15
    while time.monotonic()<limit:
        j=app.queue.get_job(jid)
        if j.status not in ('pending','running'):return j
        time.sleep(.05)
    raise AssertionError('not terminal')
j=terminal();assert j.status=='completed_partial' and j.results_count==1,(j.status,j.error)
files=json.loads(j.output_files);assert len(files)==1
path=Path(j.output_dir)/files[0];checksum=hashlib.sha256(path.read_bytes()).hexdigest()
manifest=client.get('/jobs/'+jid+'/recovery',headers=h).json()
assert sorted(x['state'] for x in manifest['items'])==['ready','uncertain'],manifest
for _ in range(3):
    r=client.post('/jobs/'+jid+'/resume',headers=h,json={});assert r.status_code==200,r.text
    j=terminal();assert j.status=='completed_partial' and j.results_count==1,(j.status,j.error)
assert client.post('/jobs/'+jid+'/resume',headers=h,json={}).status_code==409
assert len(posts)==2,posts
assert hashlib.sha256(path.read_bytes()).hexdigest()==checksum
app.worker.stop();vendor.shutdown()
print(json.dumps({'partial_items':'1/2','resume_attempts':3,'quarantine_http':409,'submits_total':2,'successful_item_unchanged':True}))
'''
    env=dict(os.environ,PYTHONPATH=str(project),COVER_SERVER_DIR=str(tmp_path),COVER_DB_PATH=str(tmp_path/'jobs.db'),COVER_SESSION_DB=str(tmp_path/'sessions.db'),COVER_USER_KEYS_DB=str(tmp_path/'own.db'),COVER_JOB_SLOTS='1',COVER_MAX_THREADS_PER_JOB='1',FAST_GEN_BUDGET_OVERRIDE='8')
    result=subprocess.run([sys.executable,'-c',script],cwd=project,env=env,text=True,capture_output=True,timeout=45)
    assert result.returncode==0,result.stdout+result.stderr
    print(result.stdout.strip())


def test_cancel_during_poll_keeps_remote_id_and_blocks_publication(tmp_path):
    import cover_core
    queue,job,journal=claimed(tmp_path);submitted=threading.Event();counts={'post':0,'get':0}
    class Vendor(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']));counts['post']+=1
            self.send_response(200);self.end_headers();self.wfile.write(b'{"id":"cancel-remote-id"}');submitted.set()
        def do_GET(self):
            counts['get']+=1;self.send_response(200);self.end_headers();self.wfile.write(b'{"status":"running"}')
    server=ThreadingHTTPServer(('127.0.0.1',0),Vendor);threading.Thread(target=server.serve_forever,daemon=True).start()
    def generate():
        with item_context(journal,'seed:ru:1'):
            journal.begin_call('fastgen','own-cancel-key')
            return cover_core.generate_image_v6('p','own-cancel-key','generate',base_url='http://127.0.0.1:'+str(server.server_port),poll_interval=.1,poll_max_wait=5,cancel_check=lambda:queue.should_stop_run(job.id,job.run_token))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(generate);assert submitted.wait(5)
        assert queue.cancel_job(job.id)
        assert future.result(timeout=3) is None
    assert queue.get_job(job.id).cancel_requested
    queue.recover_interrupted_jobs()
    assert queue.get_job(job.id).status=='cancelled'
    assert journal.manifest()['items'][0]['remote_id']=='cancel-remote-id'
    with pytest.raises(RecoveryRequired):journal.publish(Image.new('RGB',(16,16)),journal.output_dir/'cancelled.jpg')
    assert not (journal.output_dir/'cancelled.jpg').exists() and counts['post']==1
    server.shutdown()


def test_default_retention_and_fallback_archive_preserve_results(tmp_path):
    import os,subprocess,sys,textwrap
    project=Path(__file__).resolve().parents[1]
    script=r'''
import os,time,json
from pathlib import Path
from PIL import Image
from fastapi.testclient import TestClient
import cover_server as app
from cover_recovery import GenerationJournal,item_context
from job_queue_cover import Job
client=TestClient(app.app)
session=client.post('/auth/setup',json={'username':'recovery-test','password':'Recovery-Only-12345!'}).json()
h={'X-Session-ID':session['session_id']};uid=session['user_id']
r=client.post('/settings/vendor-keys',headers=h,json={'mode':'own','keys':{'fastgen':['archive-dummy-key']}});assert r.status_code==200,r.text
job=Job(user_id=uid,credential_mode='own',mode='seed',generation_provider='flow',preset_data='{"generation_provider":"flow"}')
app.queue.add_job(job);job=app.queue.claim_next_for_user(uid)
root=app._get_user_output_dir(uid)/job.artifact_namespace;root.mkdir()
job.output_dir=str(root);app.queue.update_job(job,expected_status='running',expected_run_token=job.run_token)
journal=GenerationJournal(app.DB_PATH,job.id,job.run_token,root)
with item_context(journal,'seed:ru:1'):
    journal.begin_call('fastgen','archive-dummy-key');journal.update_call(remote_id='archive-paid-id',state='ready')
    journal.publish(Image.new('RGB',(24,16),'yellow'),root/'fallback.jpg',True)
old=(root/'fallback.jpg').read_bytes()
job.output_files='["fallback.jpg"]';job.fallback_files='["fallback.jpg"]';job.status='completed_partial';job.finished_at=time.time()-40*86400
app.queue.update_job(job,expected_status='running',expected_run_token=job.run_token)
app._cleanup_old_results();assert app.queue.get_job(job.id) and (root/'fallback.jpg').read_bytes()==old
r=client.post('/jobs/'+job.id+'/resume',headers=h,json={'regenerate_fallbacks':True});assert r.status_code==200,r.text
assert not (root/'fallback.jpg').exists()
assert [p.read_bytes() for p in root.glob('.fallback-archive/*/fallback.jpg')]==[old]
manifest=client.get('/jobs/'+job.id+'/recovery',headers=h).json();assert manifest['items'][0]['remote_id']=='archive-paid-id'
assert manifest['items'][0]['item'].startswith('archived:')
assert app.queue.get_job(job.id).status=='pending'
assert client.get('/jobs/missing/recovery',headers=h).status_code==404
print(json.dumps({'retained_40_days':True,'archived_bytes_unchanged':True,'remote_id_retained':True}))
'''
    env=dict(os.environ,PYTHONPATH=str(project),COVER_SERVER_DIR=str(tmp_path),COVER_DB_PATH=str(tmp_path/'jobs.db'),COVER_SESSION_DB=str(tmp_path/'sessions.db'),COVER_USER_KEYS_DB=str(tmp_path/'own.db'))
    env.pop('COVER_RESULTS_RETENTION_ENABLED',None)
    result=subprocess.run([sys.executable,'-c',script],cwd=project,env=env,text=True,capture_output=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    print(result.stdout.strip())


def test_cached_generation_request_fingerprint_rejects_changed_inputs(tmp_path):
    import cover_core
    queue,job,journal=claimed(tmp_path)
    args={'prompt':'original prompt','provider':'flow','model':'GEM_PIX_2','aspect_ratio':'16:9','reference_images':[]}
    fingerprint=hashlib.sha256(json.dumps(args,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    with item_context(journal,'seed:ru:1'):
        journal.begin_call('fastgen','original-key',fingerprint)
        journal.update_call(remote_id='same-paid-id',state='submitted',request_seed='12345')
        journal.cache_image(Image.new('RGB',(24,16),'green'))
    with item_context(journal,'seed:ru:1'):
        assert cover_core.generate_image_via_fast_gen('original prompt','original-key','flow').size==(24,16)
    for change in ({'prompt':'changed prompt'},{'model':'another-model'},{'reference_images':['data:image/png;base64,changed']}):
        with item_context(journal,'seed:ru:1'):
            with pytest.raises(RecoveryRequired):
                cover_core.generate_image_via_fast_gen(change.get('prompt','original prompt'),'original-key','flow',model=change.get('model','GEM_PIX_2'),reference_images=change.get('reference_images'))
    with journal.connect() as conn:
        row=conn.execute('SELECT request_hash,request_seed,remote_id FROM cover_remote').fetchone()
    assert row==(fingerprint,'12345','same-paid-id')


def test_api_retry_budget_full_alias_preserves_partial_metadata(tmp_path):
    import os,subprocess,sys
    project=Path(__file__).resolve().parents[1]
    script=r'''
import json
from fastapi.testclient import TestClient
import cover_server as app
from job_queue_cover import Job
client=TestClient(app.app)
r=client.post('/auth/setup',json={'username':'api-retry-owner','password':'Local-Test-12345!'})
s=r.json();h={'X-Session-ID':s['session_id']};uid=s['user_id']
assert client.post('/settings/vendor-keys',headers=h,json={'mode':'own','keys':{'fastgen':['retry-budget-dummy']}}).status_code==200
app.ApiAuthState.configure(app.DB_PATH);app.ApiAuthState.init_db()
r=client.post('/api-keys',headers=h,json={'name':'retry-test'});ah={'X-API-Key':r.json()['plain_key']}
j=Job(user_id=uid,mode='seed',credential_mode='own',generation_provider='flow',preset_data='{"generation_provider":"flow"}')
app.queue.add_job(j);app.queue.cancel_job(j.id);j=app.queue.get_job(j.id)
j.output_files='["ready.jpg"]';j.fallback_files='["fallback.jpg"]';j.results_count=1;j.status='completed_partial';j.recovery_attempts=2
app.queue.update_job(j,expected_status='cancelled')
r=client.post('/api/v1/jobs/'+j.id+'/retry?full=true',headers=ah);assert r.status_code==200,r.text
stored=app.queue.get_job(j.id);assert stored.recovery_attempts==3
assert (stored.output_files,stored.fallback_files,stored.results_count)==(j.output_files,j.fallback_files,j.results_count)
app.queue.cancel_job(j.id);before=app.queue.get_job(j.id)
for suffix in ('?full=true','?full=false'):
    r=client.post('/api/v1/jobs/'+j.id+'/retry'+suffix,headers=ah);assert r.status_code==409,r.text
    after=app.queue.get_job(j.id)
    assert (after.status,after.output_files,after.fallback_files,after.results_count,after.recovery_attempts)==(before.status,before.output_files,before.fallback_files,before.results_count,3)
print(json.dumps({'api_recovery_attempts':3,'api_budget_exhausted':409,'full_preserves_partial_metadata':True}))
'''
    env=dict(os.environ,PYTHONPATH=str(project),COVER_SERVER_DIR=str(tmp_path),COVER_DB_PATH=str(tmp_path/'jobs.db'),COVER_SESSION_DB=str(tmp_path/'sessions.db'),COVER_USER_KEYS_DB=str(tmp_path/'own.db'))
    result=subprocess.run([sys.executable,'-c',script],cwd=project,env=env,text=True,capture_output=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    print(result.stdout.strip())


def test_stale_update_cannot_downgrade_ready_or_replace_remote_id(tmp_path):
    queue,job,old=claimed(tmp_path)
    submitted=threading.Event();continue_old=threading.Event()
    def old_worker():
        with item_context(old,'seed:ru:1'):
            old.begin_call('fastgen','same-key');old.update_call(remote_id='accepted-id',state='submitted')
            submitted.set();assert continue_old.wait(10)
            old.update_call(state='submitted',error='late timeout')
            with pytest.raises(RecoveryRequired):old.update_call(remote_id='other-id',state='submitted')
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(old_worker);assert submitted.wait(10)
        queue.recover_interrupted_jobs();new_job=queue.claim_next_for_user('owner')
        new=GenerationJournal(queue.db_path,job.id,new_job.run_token,old.output_dir)
        with item_context(new,'seed:ru:1'):
            new.begin_call('fastgen','same-key');new.cache_image(Image.new('RGB',(24,16),'blue'))
        continue_old.set();future.result(timeout=10)
    row=new.manifest()['items'][0]
    assert row['state']=='ready' and row['remote_id']=='accepted-id' and row['error']==''
    assert new.credential_pins()=={}


def test_stage_snapshot_refuses_changed_source_job(tmp_path):
    queue,job,journal=claimed(tmp_path)
    assert journal.checkpoint('title-source','scene',lambda:{'text':'original'})=={'text':'original'}
    restarted=GenerationJournal(queue.db_path,job.id,job.run_token,journal.output_dir)
    assert restarted.checkpoint('title-source','scene',lambda:{'text':'changed LLM answer'})=={'text':'original'}
    job.extra_prompt='changed external source'
    assert queue.update_job(job,expected_status='running',expected_run_token=job.run_token)
    with pytest.raises(RecoveryRequired):restarted.checkpoint('title-source','scene',lambda:{'text':'new'})
