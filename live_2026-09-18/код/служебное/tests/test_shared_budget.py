import os,sys,json,time,subprocess,threading,tempfile,hashlib
from pathlib import Path
import concurrent.futures
import pytest
HERE=Path(__file__).parent.resolve(); ROOT=HERE.parent
sys.path.insert(0,str(HERE))
import credential_budget as budget

@pytest.fixture(autouse=True)
def isolate(tmp_path,monkeypatch):
 monkeypatch.setenv('VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR',str(tmp_path/'budget'))
 monkeypatch.setenv('VIDEO_STUDIO_SHARED_DIR',str(HERE))

def test_atomic_weighted_acquire_and_double_release():
 key='synthetic-weighted'; leases=[budget.try_acquire('fastgen',key,6) for i in range(3)]
 assert all(leases) and budget.try_acquire('fastgen',key,1) is None
 with concurrent.futures.ThreadPoolExecutor(12) as ex:list(ex.map(lambda _:leases[0].release(),range(50)))
 assert budget.try_acquire('fastgen',key,7) is None
 six=budget.try_acquire('fastgen',key,6);assert six
 for lease in leases+[six]:lease.release()
 allslots=budget.try_acquire('fastgen',key,18);assert allslots;allslots.release()

def test_crash_releases_and_store_has_no_secrets(tmp_path):
 key='synthetic-crash-key-not-a-real-secret'
 script='import credential_budget as b,time; lease=b.try_acquire("fastgen",'+repr(key)+',18); assert lease; print("ready",flush=True); time.sleep(60)'
 env={**os.environ,'PYTHONPATH':str(HERE)}
 proc=subprocess.Popen([sys.executable,'-c',script],env=env,stdout=subprocess.PIPE,text=True)
 try:
  assert proc.stdout.readline().strip()=='ready'
  assert budget.try_acquire('fastgen',key,1) is None
  files=list((tmp_path/'budget').iterdir());assert files and all(key not in str(p) and p.read_bytes()==b'' for p in files)
 finally:proc.kill();proc.wait(timeout=5)
 lease=budget.try_acquire('fastgen',key,18);assert lease;lease.release()

def test_codex_account_identity_not_auth_bytes(tmp_path):
 doc={'tokens':{'account_id':'synthetic-account','access_token':'first','refresh_token':'r1'}}
 home=tmp_path/'home';home.mkdir();(home/'auth.json').write_text(json.dumps(doc))
 a=budget.try_acquire('codex',json.dumps(doc));assert a
 doc['tokens']['access_token']='rotated';doc['tokens']['refresh_token']='r2'
 (home/'auth.json').write_text(json.dumps(doc))
 assert budget.try_acquire('codex',str(home)) is None
 a.release();b=budget.try_acquire('codex',str(home));assert b;b.release()
 with pytest.raises(budget.BudgetConfigurationError):budget.try_acquire('codex',json.dumps({'tokens':{'access_token':'no-account'}}))
 assert budget.credential_identity('openai','same-bytes')!=budget.credential_identity('fastgen','same-bytes')

def test_busy_cancel_and_corrupt_store_fail_closed(tmp_path,monkeypatch):
 held=budget.try_acquire('fastgen','cancel-key',18)
 begin=time.monotonic()
 with pytest.raises(budget.BudgetBusy):budget.acquire('fastgen','cancel-key',timeout=.1)
 assert time.monotonic()-begin<.5
 with pytest.raises(budget.BudgetBusy):budget.acquire('fastgen','cancel-key',cancel_check=lambda:True)
 held.release()
 bad=tmp_path/'bad';bad.symlink_to(tmp_path/'budget',target_is_directory=True)
 monkeypatch.setenv('VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR',str(bad))
 with pytest.raises(budget.BudgetConfigurationError):budget.try_acquire('fastgen','key',1)

# Each subprocess imports unmodified candidate allocator and actual queue classes.
COMMON='''
import os,sys,json,time,tempfile,contextlib,io,importlib.util
from pathlib import Path
ROOT=Path(os.environ['TEST_ROOT']); TMP=Path(tempfile.mkdtemp(prefix='shared-adapter-'))
os.environ.update(VS_SERVER_DB=str(TMP/'vs.db'),VS_SERVER_KEYS_PATH=str(TMP/'keys.json'),VS_SERVER_DISABLE_BACKGROUND='1',COVER_USER_KEYS_DB=str(TMP/'coverkeys.db'),COVER_SERVER_DIR=str(TMP/'cover'),SESSION_DB=str(TMP/'rewrite.db'),REWRITE_SERVER_DIR=str(TMP/'rewrite'))
(TMP/'keys.json').write_text(json.dumps({'googler':'synthetic-unrelated-server-fastgen','_concurrency':{'recrafter':{'global':4,'per_tenant':4,'threads':6,'key_budget':18,'byok_global':4}}}))
import credential_budget as budget
'''
VSLOAD='''
sys.path.insert(0,str(ROOT/'worker_candidate2'))
sys.path.insert(0,str(ROOT/'vsgen_candidate2'))
sys.path.insert(0,str(ROOT/'vsgen_candidate3'))
os.environ['VS_STORAGE_MIN_FREE_GIB']='0.001'
os.environ['VS_STORAGE_JOB_GIB']='0.001'
spec=importlib.util.spec_from_file_location('actualvs',ROOT/'shared_budget_c3/vsgen_main.after.py');m=importlib.util.module_from_spec(spec)
with contextlib.redirect_stdout(io.StringIO()):spec.loader.exec_module(m)
m.TENANTS_DIR=str(TMP/'tenants')
'''
COVERLOAD='''
sys.path.insert(0,str(ROOT/'cover_candidate3'))
from api_cover_helpers import acquire_server_keys_for_job,physical_job_can_start,ServerKeyBusy
from cover_credentials import save_user_credentials
from job_queue_cover import Job
'''
REWRITELOAD='''
sys.path.insert(0,str(ROOT/'rewrite_candidate3'))
import rewrite_core as core
'''

def run_script(script):
 env={**os.environ,'PYTHONPATH':str(HERE),'TEST_ROOT':str(ROOT)}
 run=subprocess.run([sys.executable,'-c',COMMON+script],env=env,capture_output=True,text=True,timeout=45)
 assert run.returncode==0,run.stdout+'\n'+run.stderr
 return run.stdout

def test_actual_vsgen_scheduler_waits_on_cover_and_claims_after_release():
 output=run_script(COVERLOAD+'''
os.environ['COVER_FAST_GEN_KEY']='synthetic-common-fastgen'
cover=acquire_server_keys_for_job(Job(user_id='cover-neighbor',mode='seed',credential_mode='server',languages='["en"]'))
'''+VSLOAD+'''
c=m.db();c.execute("INSERT INTO users(id,name,token,role,keys,key_opts,tenant_id) VALUES(101,'fixture','token','user',?,?,'probe')",(json.dumps({'googler':['synthetic-common-fastgen']}),json.dumps({'recrafter':{'parallel':4,'threads':6},'_credentials':{'mode':'own_only'}})))
for n in range(3):
 c.execute("INSERT INTO gen_jobs(id,user_id,type,status,ts) VALUES(?,101,'images','queued','2026-09-14')",(100+n,))
 c.execute("INSERT INTO queue_tasks(job_id,tenant_id,project_id,kind,params,uid,status,created,provider) VALUES(?,'probe',?,'images',?,101,'queued',?,'recrafter')",(100+n,'p'+str(n),json.dumps({'model':'OPENAI'}),time.time()+n))
c.commit();m._refresh_byok_pool_hint();tasks=m._claim_fair_tasks(c)
assert len(tasks)==2,(len(tasks),[dict(r) for r in c.execute("select status,error from queue_tasks")],m._provider_lane_budget("recrafter","byok"),m._own_user_job_limit(101,"recrafter"),m._available_memory_mb())
assert c.execute("select status from queue_tasks where job_id=102").fetchone()[0]=='queued'
assert m._claim_fair_tasks(c)==[]
cover.release();third=m._claim_fair_tasks(c);assert len(third)==1,len(third)
for t in tasks+third:t['_credential_lease'].release()
c.close();print('cover1 + vsgen2x6; third6 queued then claimed')
''')
 assert 'queued then claimed' in output

def test_actual_cover_admission_and_no_host_lease_leak():
 run_script(COVERLOAD+'''
key='cover-shared-key';save_user_credentials('owner',{'mode':'own','keys':{'fastgen':[key],'openai':['cover-openai-key']}})
j=Job(user_id='owner',mode='title',credential_mode='own',languages='["en","de"]',max_threads=2,generation_provider='flow')
held=budget.try_acquire('fastgen',key,17)
assert not physical_job_can_start(j,[])
try:acquire_server_keys_for_job(j)
except ServerKeyBusy:pass
else:raise AssertionError('ordinary contention must not call provider')
# OpenAI family was probed before FastGen: partial reservation must be released.
oai=budget.try_acquire('openai','cover-openai-key',12);assert oai;oai.release()
held.release();lease=acquire_server_keys_for_job(j);assert lease
assert budget.try_acquire('fastgen',key,17) is None
lease.release();allslots=budget.try_acquire('fastgen',key,18);assert allslots;allslots.release()
''')

def test_actual_rewrite_pool_contends_cross_service_and_releases():
 run_script(REWRITELOAD+'''
pool=core.ServerCredentialPool('openai',['same-openai'],per_credential_limit=8)
held=budget.try_acquire('openai','same-openai',12)
started=time.monotonic()
try:pool.acquire(timeout=5)
except core.CredentialPoolExhausted as exc:assert str(exc).startswith(core.HOST_BUDGET_WAIT_PREFIX)
else:raise AssertionError('shared host budget bypassed')
assert time.monotonic()-started<3 and pool.stats()['in_use']==0
held.release();lease=pool.acquire(timeout=.1);assert lease
assert budget.try_acquire('openai','same-openai',12) is None
lease.release();allslots=budget.try_acquire('openai','same-openai',12);assert allslots;allslots.release()
# Service names differ but both map to the same OAuth account.
held=budget.try_acquire('claude_oauth','same-oauth',1)
oauth=core.ServerCredentialPool('claude',['same-oauth'],3)
try:oauth.acquire(timeout=3)
except core.CredentialPoolExhausted:pass
else:raise AssertionError('OAuth host cap bypassed')
held.release()
''')

def test_vsgen_nested_call_reservation_and_subscription_threads():
 run_script(VSLOAD+'''
u={'id':202,'role':'admin','keys':json.dumps({'googler':['fg'],'openai':['oai']}),'key_opts':json.dumps({'recrafter':{'threads':25,'parallel':4}})}
units,cap=m._own_task_budget(u,'recrafter','images',{});assert units==cap==18
for channel in ('claude','codex'):
 assert m._prompt_threads_setting(channel)==1
 for shared in (True,False):
  env=m._key_env(u,shared=shared,prompt_provider=channel)
  assert env['VS_SCENE_THREADS']=='1' and env['VS_CLAUDE_MAX_PARALLEL']=='1' and env['VS_CODEX_MAX_PARALLEL']=='1'
params={'model':'GPT_IMAGE2','image_mode':'ai'}
assert m._shared_task_units('openai','images',params,1)>=m._server_image_threads(params)
''')

def test_fork_cannot_unlock_parent_or_keep_it_alive():
 if not hasattr(os,'fork'):pytest.skip('requires POSIX fork')
 lease=budget.try_acquire('fastgen','fork-key',18);assert lease
 readfd,writefd=os.pipe();pid=os.fork()
 if pid==0:
  try:
   os.close(readfd);lease.release();os.write(writefd,b'R');time.sleep(2)
  finally:os._exit(0)
 os.close(writefd)
 try:
  assert os.read(readfd,1)==b'R'
  assert budget.try_acquire('fastgen','fork-key',1) is None
  lease.release();free=budget.try_acquire('fastgen','fork-key',18);assert free;free.release()
 finally:os.close(readfd);os.waitpid(pid,0);lease.release()

def test_raw_worker_holds_budget_and_releases_after_real_child():
 run_script(VSLOAD+'''
m.CODEX_HOME=str(TMP/'absent-codex')
fake=TMP/'workers';fake.mkdir();m.SRC_DIR=str(fake)
code="import os,sys,json;from pathlib import Path;sys.path.insert(0,"+repr(str(ROOT/'shared_budget_c3'))+");import credential_budget as b;os.environ['VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR']="+repr(os.environ['VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR'])+";assert b.try_acquire('fastgen',os.environ['GOOGLER_API_KEY'],1) is None;assert os.environ['VS_SCENE_THREADS']=='1';a=sys.argv;assert a[a.index('--image_threads')+1]=='12';p=Path(a[a.index('--project_dir')+1]);p.mkdir(parents=True,exist_ok=True);(p/'done.txt').write_text('done')"
(fake/'googler_worker.py').write_text(code)
c=m.db();c.execute("INSERT INTO users(id,name,token,role,keys,key_opts,tenant_id) VALUES(201,'raw','raw-token','admin','{}','{}','raw')")
c.execute("INSERT INTO gen_jobs(id,user_id,type,status,ts) VALUES(201,201,'raw','queued','2026-09-14')");c.commit();c.close()
m._run_worker_job(201,201,'googler_worker.py',['images','--project_dir','__OUT__','--image_threads','999'],{})
c=m.db();row=c.execute('select status,error from gen_jobs where id=201').fetchone();assert row['status']=='done',dict(row);c.close()
free=budget.try_acquire('fastgen','synthetic-unrelated-server-fastgen',18);assert free;free.release()
''')

def test_restore_hold_refuses_both_queue_retry_paths():
 run_script(COVERLOAD+'''
from api_cover_helpers import consume_recovery_attempt
from job_queue_cover import JobQueue
from fastapi import HTTPException
q=JobQueue(str(TMP/'cover-queue.db'));j=Job(user_id='hold',restore_hold=True);q.add_job(j);loaded=q.get_job(j.id);assert loaded.restore_hold
try:consume_recovery_attempt(loaded)
except HTTPException as exc:assert exc.status_code==409
else:raise AssertionError('restored cover job silently retried')
''')
 run_script(REWRITELOAD+'''
from job_queue import Job,JobQueue
q=JobQueue(str(TMP/'rewrite-queue.db'));j=Job(user_id='hold',restore_hold=True);q.add_job(j)
with q._lock:
 q._conn.execute("update jobs set status='failed' where id=?",(j.id,));q._conn.commit()
assert q.get_job(j.id).restore_hold
assert q.begin_retry(j.id) is None and q.begin_retry(j.id,full=True) is None
''')

def test_legacy_image_and_video_wait_before_provider_submit():
 run_script(VSLOAD+'''
m.GEN_DIR=str(TMP/'legacy-results');Path(m.GEN_DIR).mkdir()
(TMP/'keys.json').write_text(json.dumps({'googler':'legacy-fastgen','veo':'legacy-veo'}))
# Fresh config cache, not a replacement of allocator/provider code.
m._keys_cache = None if hasattr(m,'_keys_cache') else None
c=m.db()
for jid in (401,402):c.execute("INSERT INTO gen_jobs(id,user_id,type,status,ts) VALUES(?,1,'image','queued','2026-09-14')",(jid,))
c.commit();c.close()
fg=budget.try_acquire('fastgen',m._first_credential(m.keys(),'googler'),18)
m._run_gen_job(401,'test','test','OPENAI','landscape')
assert m.JOB_Q.qsize()==1
fg.release()
veo=budget.try_acquire('veo',m._first_credential(m.keys(),'veo'),24)
m._run_video_job(402,'test','landscape');assert m.JOB_Q.qsize()==2;veo.release()
c=m.db();rows=c.execute('select status,error from gen_jobs where id in (401,402)').fetchall();assert all(r['status']=='queued' and 'Ожидание' in r['error'] for r in rows),[dict(r) for r in rows]
''')

def test_cover_http_restore_hold_caps_and_busy_queue():
 run_script(COVERLOAD+'''
os.environ['COVER_DB_PATH']=str(TMP/'cover-http.db');os.environ['COVER_SESSION_DB']=str(TMP/'cover-sessions.db');os.environ['COVER_MAX_THREADS_PER_JOB']='1'
from fastapi.testclient import TestClient
import cover_server as app
client=TestClient(app.app)
s=client.post('/auth/setup',json={'username':'hold-owner','password':'Synthetic-Only-12345!'}).json()
h={'X-Session-ID':s['session_id']};uid=s['user_id']
assert client.post('/settings/vendor-keys',headers=h,json={'mode':'own','keys':{'fastgen':['hold-fastgen-key']}}).status_code==200
app.ApiAuthState.configure(app.DB_PATH);app.ApiAuthState.init_db()
ah={'X-API-Key':client.post('/api-keys',headers=h,json={'name':'hold-test'}).json()['plain_key']}
j=Job(user_id=uid,credential_mode='own',mode='seed',languages='["Русский"]',seeds_per_lang='{"Русский":"Test seed"}',n_variants=1,preset_data='{"generation_provider":"flow","name":"default"}')
app.queue.add_job(j);claimed=app.queue.claim_next_for_user(uid)
held=budget.try_acquire('fastgen','hold-fastgen-key',18)
app.worker._execute_job(claimed)
state=app.queue.get_job(j.id);assert state.status=='pending' and not state.error,(state.status,state.error)
assert state.recovery_attempts==0
held.release();app.queue.cancel_job(j.id);state=app.queue.get_job(j.id);state.restore_hold=True
app.queue.update_job(state,expected_status='cancelled');before=app.queue.get_job(j.id)
for url,headers in [('/api/v1/jobs/'+j.id+'/retry?full=true',ah),('/jobs/'+j.id+'/resume',h)]:
 r=client.post(url,headers=headers,json={});assert r.status_code==409,(r.status_code,r.text)
 after=app.queue.get_job(j.id);assert after.restore_hold and after.recovery_attempts==before.recovery_attempts and after.output_files==before.output_files
r=client.post('/admin/gen-settings',headers=h,json={'max_threads_per_job':25});assert r.status_code==422,(r.status_code,r.text)
os.environ['COVER_MAX_THREADS_PER_JOB']='25';assert app._server_max_threads()==12
''')

def test_explicit_own_copy_of_server_key_stays_own_and_shares_budget():
 run_script(VSLOAD+'''
key=m._first_credential(m.keys(),'googler')
u={'id':900,'role':'user','keys':json.dumps({'googler':[key]}),'key_opts':json.dumps({'_credentials':{'mode':'own_only'},'recrafter':{'threads':6}})}
assert m._own_pool_values(u,'googler')==[key] and m._user_own_credentials(u)['googler']==key
server=m._reserve_task_credential('recrafter','images',{})
assert server and server.units==6
own=[m._reserve_own_task_credential(u,'recrafter','images',{}) for _ in range(3)]
assert sum(x is not None for x in own)==2 and all(x.source=='byok' for x in own if x)
server.release()
for x in own:
 if x:x.release()
''')


def test_actual_legacy_disk_wait_and_tts_no_provider():
 output=run_script(VSLOAD+'''
key='synthetic-legacy-disk-key'
c=m.db()
for jid,kind in ((970,'image'),(971,'tts')):
 c.execute("INSERT INTO gen_jobs(id,user_id,type,status,settings,ts) VALUES(?,1,?,'queued','{}','now')",(jid,kind))
c.commit();c.close()
os.environ['VS_STORAGE_MIN_FREE_GIB']='99999999'
assert m._legacy_wait_budget(970,m._run_gen_job,(970,'p','s','m','a'),{'fastgen':key}) is None
lease=budget.try_acquire('fastgen',key,18);assert lease;lease.release()
m._run_tts_job(971,'synthetic-text','voice','edge')
c=m.db();assert [r[0] for r in c.execute('SELECT status FROM gen_jobs WHERE id IN (970,971) ORDER BY id')]==['queued','queued'];c.close()
os.environ['VS_STORAGE_MIN_FREE_GIB']='0.001'
leases=m._legacy_wait_budget(970,m._run_gen_job,(970,'p','s','m','a'),{'fastgen':key})
assert leases
c=m.db();assert c.execute('SELECT status FROM gen_jobs WHERE id=970').fetchone()[0]=='running';c.close()
for lease in leases:lease.release()
assert m._legacy_wait_budget(970,m._run_gen_job,(970,'p','s','m','a'),{'fastgen':key}) is None
lease=budget.try_acquire('fastgen',key,18);assert lease;lease.release()
print('disk busy image/TTS queued before provider; physical lease released; successful CAS claims once')
''')
 assert 'claims once' in output


def test_codex_apikey_auth_shares_openai_physical_capacity(tmp_path):
 key='synthetic-codex-openai-physical-key'
 doc={'auth_mode':'apikey','OPENAI_API_KEY':key}
 home=tmp_path/'api-home';home.mkdir();(home/'auth.json').write_text(json.dumps(doc))
 assert budget.credential_identity('codex',json.dumps(doc))==budget.credential_identity('openai',key)
 held=budget.try_acquire('openai',key,11);assert held
 codex=budget.try_acquire('codex',str(home),1);assert codex
 assert budget.try_acquire('openai',key,1) is None
 assert budget.try_acquire('codex',json.dumps(doc),1) is None
 codex.release();held.release()
 allslots=budget.try_acquire('openai',key,12);assert allslots;allslots.release()


def test_actual_legacy_raw_codex_apikey_alias_does_not_self_wait():
 output=run_script(VSLOAD+'''
key='synthetic-shared-raw-codex-api'
doc=json.dumps({'auth_mode':'apikey','OPENAI_API_KEY':key})
for values in ({'openai':key,'codex':doc},{'codex':doc,'openai':key}):
 leases=m._legacy_budget_reserve(values,True)
 assert leases is not None and len(leases)==1
 assert budget.try_acquire('openai',key,1) is None
 for lease in leases:lease.release()
 allslots=budget.try_acquire('openai',key,12);assert allslots;allslots.release()
print('actual raw allocator physical aliases deduped in either order, cap12 held and fully released')
''')
 assert 'deduped in either order' in output
