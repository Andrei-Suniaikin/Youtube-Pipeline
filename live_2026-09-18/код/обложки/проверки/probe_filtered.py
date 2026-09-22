"""Probe: provider accepts submit, then reports generation FAILED (content filter).
Does resume regenerate the item, or re-poll the same failed remote ID forever?
Isolated: temp DBs, fake keys, localhost vendor stub. No real provider calls."""
import io, json, base64, threading, time, os, sys, sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from PIL import Image
from fastapi.testclient import TestClient
b=io.BytesIO(); Image.new('RGB',(64,36),'navy').save(b,format='PNG'); uri='data:image/png;base64,'+base64.b64encode(b.getvalue()).decode()
posts=[]; gets={}
ids={}
class Vendor(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def send(self,data,status=200):
        self.send_response(status); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(json.dumps(data).encode())
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        n=len(posts)+1; posts.append(body['prompt'][:40])
        gid='gen-%d'%n; ids[gid]=('blocked' in body['prompt'])
        self.send({'id':gid})
    def do_GET(self):
        if self.path.endswith('/usage'): self.send({'max_threads':8}); return
        gid=self.path.rsplit('/',1)[-1]; gets[gid]=gets.get(gid,0)+1
        if ids.get(gid): self.send({'status':'failed','error':'content policy violation'})
        else: self.send({'status':'succeeded','results':[{'data':uri}]})
vendor=ThreadingHTTPServer(('127.0.0.1',0),Vendor); threading.Thread(target=vendor.serve_forever,daemon=True).start()
os.environ['FAST_GEN_API_BASE']='http://127.0.0.1:%d'%vendor.server_port
import cover_server as app
client=TestClient(app.app)
r=client.post('/auth/setup',json={'username':'probe_owner','password':'probe-local-123'}); h={'X-Session-Id':r.json()['session_id']}
assert client.post('/settings/vendor-keys',headers=h,json={'mode':'own','keys':{'fastgen':['probe-dummy-key-1']}}).status_code==200
body={'mode':'seed','languages':['Русский','Английский'],'seeds_per_lang':{'Русский':'good-item','Английский':'blocked-item'},'n_variants':1,'preset_data':{'name':'default','generation_provider':'flow'}}
r=client.post('/jobs/cover',headers={**h,'Idempotency-Key':'probe-key-0001'},json=body); assert r.status_code==200, r.text; jid=r.json()['job_id']
app.worker.start()
def terminal():
    lim=time.monotonic()+20
    while time.monotonic()<lim:
        j=app.queue.get_job(jid)
        if j.status not in ('pending','running'): return j
        time.sleep(.05)
    raise AssertionError('not terminal')
out={}
j=terminal(); out['run1']=dict(status=j.status,results=j.results_count,error=j.error[:160],posts=len(posts))
out['manifest1']=[(x['item'],x['state'],bool(x['remote_id'])) for x in client.get('/jobs/'+jid+'/recovery',headers=h).json()['items']]
for i in range(3):
    r=client.post('/jobs/'+jid+'/resume',headers=h,json={}); 
    if r.status_code!=200: out['resume%d'%(i+1)]=r.status_code; break
    j=terminal(); out['resume%d'%(i+1)]=dict(status=j.status,results=j.results_count,posts=len(posts),gets=dict(gets),error=j.error[:120])
r=client.post('/jobs/'+jid+'/resume',headers=h,json={}); out['resume4_http']=r.status_code; out['resume4_detail']=r.json().get('detail','')[:100]
r=client.post('/jobs/'+jid+'/resume',headers=h,json={'override_provider':'Nano Banana 2'}); out['override_http']=r.status_code
out['idem_replay']=client.post('/jobs/cover',headers={**h,'Idempotency-Key':'probe-key-0001'},json=body).json().get('job_id')==jid
client.delete('/jobs/'+jid,headers=h)
con=sqlite3.connect(os.environ['COVER_DB_PATH'])
out['after_delete_rows']={t:con.execute('select count(*) from %s where job_id=?'%t,(jid,)).fetchone()[0] for t in ('cover_remote','cover_stages','cover_artifacts','job_requests')}
out['idem_after_delete']=client.post('/jobs/cover',headers={**h,'Idempotency-Key':'probe-key-0001'},json=body).status_code
app.worker.stop(); vendor.shutdown()
print(json.dumps(out,ensure_ascii=False,indent=1))
