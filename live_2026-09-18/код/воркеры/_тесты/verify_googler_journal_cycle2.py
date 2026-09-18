"""Real subprocesses and HTTP; no replacement of worker/journal functions under test."""
from pathlib import Path
import base64, collections, json, os, signal, socket, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT=Path(__file__).parent;C=ROOT/'worker_candidate2';TMP=Path(tempfile.mkdtemp(prefix='googler-journal-'))
counts=collections.Counter();fault='';post_event=threading.Event();get_event=threading.Event()
DATA=base64.b64encode(b'image-bytes-for-recovery').decode()
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def send(self,code,data):
  raw=json.dumps(data).encode();self.send_response(code);self.send_header('Content-Length',str(len(raw)));self.end_headers()
  try:self.wfile.write(raw)
  except (BrokenPipeError,ConnectionResetError):pass
 def do_POST(self):
  counts['POST']+=1;self.rfile.read(int(self.headers['Content-Length']));post_event.set()
  if fault=='hold_post':time.sleep(2)
  if fault=='500':self.send(500,{'error':'ambiguous'});return
  if fault=='401':self.send(401,{'error':'invalid_api_key'});return
  if self.path.endswith('/sync'):self.send(200,{'data':[{'b64_json':DATA}]});return
  self.send(200,{'id':'retained-task'})
 def do_GET(self):
  counts['GET']+=1;get_event.set()
  if fault=='hold_get':time.sleep(2)
  if fault=='download500':self.send(500,{'error':'CDN temporary'});return
  self.send(200,{'status':'succeeded','results':[{'data':DATA}]})
server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
url=f'http://127.0.0.1:{server.server_port}'
runner=TMP/'call.py';runner.write_text('''import os,sys,json
sys.path.insert(0,sys.argv[1]);sys.path.insert(1,sys.argv[2])
import googler_worker as g
g.GOOGLER_API_KEY=os.environ.get("TEST_KEY","synthetic-A");g.GOOGLER_BASE_URL_V6=sys.argv[3];g.RETRY_DELAY=0;g.VIDEO_POLL_INTERVAL=.02
if sys.argv[5]=="sync":
 r=g._journal_sync_post("openai",g.GOOGLER_API_KEY,"sync1",sys.argv[3]+"/sync",{}, {"prompt":"p"},1)
 print("RESULT",r.status_code)
else:
 r=g.googler_v6_generate("openai-image","same prompt",index=1,timeout=float(sys.argv[4]))
 print("RESULT",bool(r))
''')
R={'fixture':str(TMP),'checks':{}}
def run(jid,timeout=2,kind='async',key='synthetic-A',wait=True):
 env={**os.environ,'VS_REMOTE_JOURNAL_DIR':str(TMP/str(jid)),'VS_QUEUE_JOB_ID':str(jid),'TEST_KEY':key,'VS_CREDENTIAL_SOURCE':'byok'}
 p=subprocess.Popen([sys.executable,str(runner),str(C),str(ROOT.parents[1]/'src'),url,str(timeout),kind],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
 if not wait:return p
 stdout,stderr=p.communicate(timeout=12);assert p.returncode==0,stderr
 return stdout
def total():return counts['POST']
n=total();a=run(1,0);assert 'RESULT False' in a and total()==n+1
b=run(1);assert 'RESULT True' in b and total()==n+1
c=run(1);assert 'RESULT True' in c and total()==n+1
R['checks']['poll_timeout_restart_same_id_and_completed_cache']=True
fault='500';n=total();assert 'RESULT False' in run(2);fault='';assert 'RESULT False' in run(2);assert total()==n+1
R['checks']['ambiguous500_no_transport_or_outer_post_retry']=True
fault='401';n=total();run(3);fault='';assert 'RESULT True' in run(3,key='synthetic-B');assert total()==n+2
R['checks']['explicit_rejection_releases_pin_for_key_rotation']=True
n=total();assert 'RESULT True' in run(1,key='synthetic-B');assert total()==n
assert 'RESULT False' in run(2,key='synthetic-B');assert total()==n
R['checks']['completed_cache_allows_new_key_but_uncertain_work_does_not']=True
fault='hold_get';get_event.clear();n=total();p=run(4,wait=False);assert get_event.wait(5);p.kill();p.communicate();fault='';assert 'RESULT True' in run(4);assert total()==n+1
R['checks']['killed_after_accepted_id_resumes_same_remote_task']=True
fault='hold_post';post_event.clear();n=total();p=run(5,wait=False);assert post_event.wait(5);p.kill();p.communicate();fault='';assert 'RESULT False' in run(5);assert total()==n+1
R['checks']['killed_during_post_uncertain_intent_blocks_duplicate']=True
n=total();a=run(6,kind='sync');b=run(6,kind='sync');assert 'RESULT 200' in a+b and total()==n+1
R['checks']['synchronous_response_retained_before_output_use']=True
fault='500';n=total();run(7,kind='sync');fault='';run(7,kind='sync');assert total()==n+1
R['checks']['sync_ambiguous_response_not_charged_twice']=True
n=total();processes=[run(8,wait=False) for _ in range(4)]
for p in processes:
 out,err=p.communicate(timeout=15);assert p.returncode==0 and 'RESULT True' in out,err
assert total()==n+1
R['checks']['four_processes_one_logical_scene_one_post']=True
server.shutdown();server.server_close();R['http_counts']=dict(counts)
(ROOT/'verify_googler_journal_cycle2_result.json').write_text(json.dumps(R,indent=2));print(json.dumps(R,indent=2))
