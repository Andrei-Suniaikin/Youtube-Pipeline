"""Real puller HTTP/filesystem/process orchestration against a fault-injecting local server."""
from pathlib import Path
import os,sys,json,time,threading,tempfile,subprocess,tarfile,io,hashlib,socket,shutil,importlib.util
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
P=Path(__file__).parent;C=P/'render_candidate';TMP=Path(tempfile.mkdtemp(prefix='render-hardening-'))
sys.path.insert(0,str(C))
assets=TMP/'assets';assets.mkdir()
subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=3','-c:a','libmp3lame',str(assets/'audio.mp3')],check=True)
for i,color in enumerate(['blue','yellow','red']):
 subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',f'color=c={color}:s=256x144:d=0.1','-frames:v','1',str(assets/f'img{i}.png')],check=True)
buf=io.BytesIO()
with tarfile.open(fileobj=buf,mode='w:gz') as archive:
 for f in assets.iterdir():archive.add(f,arcname=f.name)
ASSETS=buf.getvalue();lock=threading.Lock();states={};received={};heartbeats={};faults={};posts={};claim_jobs=[]
class Handler(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*args):pass
 def send(self,status,payload):
  body=json.dumps(payload).encode();self.send_response(status);self.send_header('Content-Length',str(len(body)));self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body)
 def job(self):
  path=urlparse(self.path).path;parts=path.split('/');jid=int(parts[3]);token=self.headers.get('X-Render-Claim')
  if states[jid]['token']!=token:self.send(409,{'detail':'stale'});return None
  return jid
 def do_GET(self):
  jid=self.job()
  if jid is None:return
  if self.path.endswith('/assets'):
   self.send_response(200);self.send_header('Content-Length',str(len(ASSETS)));self.end_headers()
   if faults.get(jid)=='slow_assets':
    self.wfile.write(ASSETS[:100]);self.wfile.flush();time.sleep(.3);self.wfile.write(ASSETS[100:])
   else:self.wfile.write(ASSETS)
  elif self.path.endswith('/receipt'):self.send(200,received.get(jid,{'accepted':False,'job_id':jid,'status':'running'}))
  else:self.send(404,{})
 def do_POST(self):
  if self.path=='/api/render/claim':
   with lock:j=claim_jobs.pop(0) if claim_jobs else None
   if j and faults.get(j['job_id'])=='storage_lock':
    puller.delivery_store().path(puller.delivery_store().identity(j)).with_suffix('.lock').mkdir()
   if j and faults.get(j['job_id'])=='restore_storage':
    puller.delivery_store().path(puller.delivery_store().identity({'job_id':19,'claim_token':states[19]['token']})).with_suffix('.lock').rmdir()
   self.send(200,{'job':j});return
  jid=self.job()
  if jid is None:return
  length=int(self.headers.get('Content-Length','0'))
  if '/progress' in self.path:
   self.rfile.read(length);heartbeats[jid]=heartbeats.get(jid,0)+1;self.send(200,{'ok':True});return
  if '/transcript' in self.path:self.rfile.read(length);self.send(200,{'ok':True});return
  posts[jid]=posts.get(jid,0)+1
  q=parse_qs(urlparse(self.path).query)
  if q.get('status')==['error']:
   error=q['error'][0];digest=hashlib.sha256(error.encode()).hexdigest();assert self.headers.get('X-Render-Error-SHA256')==digest
   result={'ok':True,'accepted':True,'job_id':jid,'status':'error','error_sha256':digest}
  else:
   if faults.get(jid)=='drop_mid_upload':
    self.rfile.read(min(length,1024));self.close_connection=True;self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
   data=self.rfile.read(length)
   if faults.get(jid)=='http500':self.send(500,{'detail':'temporary'});return
   (TMP/f'received_{jid}.mp4').write_bytes(data)
   digest=hashlib.sha256(data).hexdigest();assert digest==self.headers.get('X-Render-SHA256');result={'ok':True,'accepted':True,'job_id':jid,'status':'done','sha256':digest,'size':len(data)}
  if faults.get(jid)=='wrong_receipt':result={**result,'sha256':'incorrect'}
  received[jid]=result
  if faults.get(jid)=='lost_response':
   faults[jid]='';self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
  self.send(200,result)
server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
os.environ.update({'VSGEN_SERVER':f'http://127.0.0.1:{server.server_port}','PULLER_TOKEN':'synthetic-puller','PULLER_WORK':str(TMP/'work'),'RENDER_WORKER':str(C/'render_worker.py'),'PULLER_HEARTBEAT_SEC':'.08','PULLER_MIN_FREE_BYTES':'0','PULLER_MAX_OUTBOX_BYTES':str(64*1024**3),'PULLER_CONCURRENCY':'3'})
import render_puller as puller
from render_delivery import DeliveryStore
R={'fixture_root':str(TMP),'transport':'real requests to local fault-injecting HTTP server','checks':{}}
config={'quality':'144p','add_subtitles':False,'sync_audio':False,'enable_fade':False,'ken_burns':False,'render_settings':{'force_encoder':'libx264','preset':'ultrafast','max_pool_workers':1,'ffmpeg_threads':1,'image_convert_workers':1},'target_fps':5,'material_order':'images_first'}
def job(jid):
 states[jid]={'token':f'synthetic-claim-{jid}'}
 return {'job_id':jid,'claim_token':states[jid]['token'],'config':config}
def record(jid):return puller.delivery_store().load(puller.delivery_store().identity({'job_id':jid,'claim_token':states[jid]['token']}))
# Actual renderer, not a fake success marker: produce MP4 and durable marker, upload and ACK.
j=job(1);faults[1]='slow_assets';puller.run_job(j);assert received[1]['status']=='done';assert posts[1]==1 and heartbeats[1]>=2
assert not puller.delivery_store().path(puller.delivery_store().identity(j)).exists();assert not puller.delivery_store().project_dir(j).exists()
probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration:stream=codec_type,width,height','-of','json',str(TMP/'received_1.mp4')],text=True))
assert 2.5<=float(probe['format']['duration'])<=5 and {x['codec_type'] for x in probe['streams']}=={'audio','video'}
R['checks']['real_tiny_render_download_heartbeat_and_verified_ack_cleanup']={'size':received[1]['size'],'heartbeats':heartbeats[1],'ffprobe':probe}
# Build durable records from genuine MP4 media; no monkeypatch of tested store/delivery code.
media=TMP/'sample.mp4';subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=s=256x144:r=5:d=1','-c:v','libx264',str(media)],check=True)
def ready(jid):
 j=job(jid);r=puller.delivery_store().begin(j);target=Path(r['project_dir'])/'result.mp4';shutil.copy2(media,target);return puller.delivery_store().ready(r,target)
def attempt(r):
 m=puller.JobMonitor(r);m.phase('upload',99,'upload');m.start()
 try:return puller.deliver(r,m)
 finally:m.close()
r=ready(2);faults[2]='lost_response';assert not attempt(r);assert Path(r['output']).exists() and received[2]['status']=='done';r=record(2);assert attempt(r);assert posts[2]==1
R['checks']['lost_response_receipt_recovery_without_duplicate_upload']=True
r=ready(3);faults[3]='drop_mid_upload';assert not attempt(r);assert Path(r['output']).exists() and 3 not in received;faults[3]='';assert attempt(record(3));assert posts[3]==2
R['checks']['half_upload_retained_and_retried']=True
r=ready(4);faults[4]='http500';assert not attempt(r);assert Path(r['output']).exists();faults[4]='';fresh=DeliveryStore(TMP/'work');assert attempt(fresh.load(r['identity']));R['checks']['server500_then_new_store_replay']=True
r=ready(5);states[5]['token']='newer-claim';assert not attempt(r);assert Path(r['output']).exists();assert puller.delivery_store().load(r['identity'])['phase']=='quarantined';R['checks']['stale_claim_quarantines_without_deleting_mp4']=True
r=ready(6)
try:puller.delivery_store().acknowledge(r,{'accepted':True,'job_id':6,'status':'done','sha256':'wrong','size':r['size']})
except ValueError:pass
else:raise AssertionError('bad digest authorized cleanup')
assert Path(r['output']).exists();assert attempt(r);R['checks']['wrong_ack_digest_cannot_cleanup']=True
# Crash after renderer marker but before ready manifest recovers the real file.
j=job(7);r=puller.delivery_store().begin(j);r['phase']='rendering';puller.delivery_store().save(r);target=Path(r['project_dir'])/'result.mp4';shutil.copy2(media,target)
from render_delivery import atomic_json
atomic_json(Path(r['project_dir'])/'_render_completion.json',{'output':str(target),'size':target.stat().st_size})
fresh=DeliveryStore(TMP/'work');restored=fresh.recover(fresh.load(r['identity']));assert restored['phase']=='ready';assert attempt(restored);R['checks']['restart_after_completion_marker_recovers']=True
# Persistent execution failure also survives lost ACK.
j=job(8);r=puller.delivery_store().begin(j);r.update(phase='error',error='synthetic bounded failure');puller.delivery_store().save(r);faults[8]='lost_response';assert not attempt(r);assert attempt(record(8));assert posts[8]==1;R['checks']['error_receipt_idempotent_replay']=True
# Limits act on genuine filesystem state without stubbing disk_usage.
r=ready(9);assert not puller.delivery_store().admission_allowed(0,1);assert not puller.delivery_store().admission_allowed(shutil.disk_usage(TMP).free+1,10**15)
R['checks']['outbox_and_free_disk_admission']=True
# Actual process-group watchdog: hung child is killed, including its child.
fixture=TMP/'hung.py';fixture.write_text('import subprocess,sys,time\nchild=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"])\nprint(child.pid,flush=True)\ntime.sleep(60)\n')
j=job(10);r=puller.delivery_store().begin(j);proc=subprocess.Popen([sys.executable,str(fixture)],stdout=subprocess.PIPE,text=True,start_new_session=True);childpid=int(proc.stdout.readline())
m=puller.JobMonitor(r);m.proc=proc;m.phase('render',5,'render');original=puller.RENDER_TIMEOUT;puller.RENDER_TIMEOUT=.15;m.start();proc.wait(timeout=5);m.close();puller.RENDER_TIMEOUT=original;assert m.cancelled.is_set() and 'deadline' in m.reason
R['checks']['wall_deadline_kills_real_process_group']=True
j=job(11);r=puller.delivery_store().begin(j);m=puller.JobMonitor(r);m.phase('render',5,'render');m.whisper_started=time.monotonic()-puller.WHISPER_TIMEOUT-1
try:m.check()
except TimeoutError:pass
else:raise AssertionError('whisper deadline absent')
assert 'Whisper' in m.reason;R['checks']['whisper_deadline']=True
j=job(12);r=puller.delivery_store().begin(j);m=puller.JobMonitor(r);m.phase('render',5,'render');m.last_activity=time.monotonic()-puller.NO_PROGRESS_TIMEOUT-1
try:m.check()
except TimeoutError:pass
else:raise AssertionError('no progress deadline absent')
assert 'progress' in m.reason;R['checks']['no_progress_deadline']=True
# Restarted unfinished render becomes a replayable error, not false done.
j=job(13);r=puller.delivery_store().begin(j);fresh=DeliveryStore(TMP/'work');restored=fresh.recover(fresh.load(r['identity']));assert restored['phase']=='error';assert attempt(restored)
R['checks']['restart_incomplete_render_reports_error']=True
# OS flock covers independent processes replaying the same durable manifest.
r=ready(14);store=puller.delivery_store();assert store.reserve(r['identity'])
script="import sys;sys.path.insert(0,sys.argv[1]);from render_delivery import DeliveryStore;s=DeliveryStore(sys.argv[2]);print(int(s.reserve(sys.argv[3])))"
cmd=[sys.executable,'-c',script,str(C),str(TMP/'work'),r['identity']]
assert subprocess.check_output(cmd,text=True).strip()=='0';store.release(r['identity']);assert subprocess.check_output(cmd,text=True).strip()=='1'
R['checks']['cross_process_outbox_lock']=True
# No-progress and Whisper expiry kill real quiet children, not only check predicates.
for jid,kind in [(15,'no_progress'),(16,'whisper')]:
 j=job(jid);r=store.begin(j);proc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],stdout=subprocess.PIPE,text=True,start_new_session=True)
 m=puller.JobMonitor(r);m.proc=proc;m.phase('render',5,'render')
 name='NO_PROGRESS_TIMEOUT' if kind=='no_progress' else 'WHISPER_TIMEOUT';previous=getattr(puller,name);setattr(puller,name,.15)
 if kind=='whisper':m.line('__RENDER_PHASE__|whisper_start')
 m.start();proc.wait(timeout=5);m.close();setattr(puller,name,previous);assert m.cancelled.is_set()
 R['checks'][kind+'_kills_real_child']=True
# Every queued durable delivery gets a heartbeat even when its retry is delayed.
before=heartbeats.get(9,0);puller.heartbeat_outbox_once();assert heartbeats.get(9,0)>before
R['checks']['backoff_outbox_keeps_lease']=True
r=ready(17);r['config']={**r['config'],'auto_chapters':True};puller.delivery_store().save(r);assert not attempt(r)
assert received[17]['status']=='error' and Path(r['output']).exists() and record(17)['phase']=='quarantined'
R['checks']['required_transcript_missing_is_terminal_error_mp4_retained']=True
j=job(18);r=store.begin(j);uncertain=Path(r['project_dir'])/'uncertain.mp4';shutil.copy2(media,uncertain)
r=store.recover(r);assert r['phase']=='error' and r['retain_on_error'];assert attempt(r);assert uncertain.exists() and record(18)['phase']=='quarantined'
R['checks']['crash_before_marker_preserves_uncertain_mp4_after_error_ack']=True
# Persisted deadlines/retry counts survive a fresh store and do not renew on replay.
r=ready(22);r['deadline_at']=time.time()-1;store.save(r);fresh=DeliveryStore(TMP/'work');r=fresh.load(r['identity']);assert not attempt(r)
assert record(22)['phase']=='quarantined' and Path(r['output']).exists() and received[22]['status']=='error'
before=heartbeats.get(22,0);puller.heartbeat_outbox_once();assert heartbeats.get(22,0)==before
R['checks']['persisted_expired_deadline_terminal_reconcile_releases_lease']=True
r=ready(23);r['attempts']=puller.MAX_DELIVERY_ATTEMPTS;store.save(r);assert not attempt(DeliveryStore(TMP/'work').load(r['identity']));assert record(23)['phase']=='quarantined' and Path(r['output']).exists()
R['checks']['persisted_retry_budget_finite']=True
r=ready(24);faults[24]='lost_response';assert not attempt(r);r=record(24);r['deadline_at']=time.time()-1;store.save(r);assert attempt(r);assert posts[24]==1
R['checks']['expired_deadline_still_accepts_exact_committed_receipt']=True
r=ready(25);faults[25]='wrong_receipt'
for i in range(puller.MAX_DELIVERY_ATTEMPTS+1):
 assert not attempt(DeliveryStore(TMP/'work').load(r['identity']))
assert record(25)['phase']=='quarantined' and Path(r['output']).exists()
R['checks']['permanent_wrong_receipt_has_finite_retries']=True
# Real worker loop sees a lock-path filesystem error, reports it and renders next job.
previous_poll=puller.POLL;puller.POLL=.05
j=job(19);faults[19]='storage_lock';j2=job(20);faults[20]='restore_storage';claim_jobs.extend([j,j2]);stop=threading.Event();thread=threading.Thread(target=puller.worker_loop,args=(7,stop));thread.start();deadline=time.monotonic()+10
while time.monotonic()<deadline and 20 not in received:time.sleep(.03)
stop.set();thread.join(timeout=3);assert received[19]['status']=='error' and received[20]['status']=='done' and not thread.is_alive()
assert puller.loop_health()['worker-7']['failures']>=1
R['checks']['worker_loop_filesystem_failure_then_next_real_render']=True
# Replay thread also survives real lock-path IO failure and recovers after filesystem repair.
r=ready(21);badlock=store.path(r['identity']).with_suffix('.lock');badlock.mkdir();stop=threading.Event();thread=threading.Thread(target=puller.outbox_loop,args=(stop,));thread.start();deadline=time.monotonic()+10
while time.monotonic()<deadline and not puller.loop_health().get('outbox',{}).get('failures'):time.sleep(.03)
assert thread.is_alive() and puller.loop_health()['outbox']['failures']>=1;badlock.rmdir();deadline=time.monotonic()+10
while time.monotonic()<deadline and 21 not in received:time.sleep(.03)
stop.set();thread.join(timeout=3);puller.POLL=previous_poll;assert received[21]['status']=='done' and not thread.is_alive()
R['checks']['outbox_loop_filesystem_failure_then_recovery']=True
server.shutdown();server.server_close()
path=P/'render_verify_cycle2_result.json'
if path.exists():shutil.copy2(path,str(path)+'.previous.bak')
path.write_text(json.dumps(R,indent=2));print(json.dumps(R,indent=2))
