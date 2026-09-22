"""Probe 2: title mode, provider always reports FAILED after accepting submit.
Count POSTs per cover (attempt loop) and check override_provider on resume.
Isolated: temp DBs, fake keys, localhost stub; no real provider calls."""
import io, json, base64, threading, time, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from fastapi.testclient import TestClient
posts=[]; llm=[0]
class Vendor(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def send(self,data,status=200):
        self.send_response(status); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(json.dumps(data).encode())
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path.endswith('/chat/completions'):
            llm[0]+=1
            content=json.dumps({'scene_description':'a scene','text':'TEXT','adapted_text':'TEXT','overall_pass':True,'issues':'','characters':[],'mood':'bright'})
            self.send({'id':'c','object':'chat.completion','created':1,'model':'t','choices':[{'index':0,'message':{'role':'assistant','content':content},'finish_reason':'stop'}]}); return
        posts.append(1); self.send({'id':'gen-%d'%len(posts)})
    def do_GET(self):
        if self.path.endswith('/usage'): self.send({'max_threads':8}); return
        self.send({'status':'failed','error':'content policy violation'})
vendor=ThreadingHTTPServer(('127.0.0.1',0),Vendor); threading.Thread(target=vendor.serve_forever,daemon=True).start()
base='http://127.0.0.1:%d'%vendor.server_port
os.environ['FAST_GEN_API_BASE']=base; os.environ['OPENAI_BASE_URL']=base+'/v1'
import cover_server as app
client=TestClient(app.app)
r=client.post('/auth/setup',json={'username':'probe2','password':'probe-local-123'}); h={'X-Session-Id':r.json()['session_id']}
assert client.post('/settings/vendor-keys',headers=h,json={'mode':'own','keys':{'fastgen':['probe-dummy-fg-1'],'openai':['probe-dummy-oa-1']}}).status_code==200
body={'mode':'title','languages':['Русский'],'titles_per_lang':{'Русский':'Заголовок'},'preset_data':{'name':'default','generation_provider':'flow'}}
r=client.post('/jobs/cover',headers=h,json=body); assert r.status_code==200, r.text; jid=r.json()['job_id']
app.worker.start()
def terminal():
    lim=time.monotonic()+30
    while time.monotonic()<lim:
        j=app.queue.get_job(jid)
        if j.status not in ('pending','running'): return j
        time.sleep(.05)
    raise AssertionError('not terminal')
j=terminal()
out={'retries_setting':app._server_max_retries(),'run1':dict(status=j.status,results=j.results_count,error=j.error[:140],posts=len(posts))}
r=client.post('/jobs/'+jid+'/resume',headers=h,json={'override_provider':'flow','override_model':'NARWHAL'})
out['override_resume']=(r.status_code, r.json().get('detail','')[:120])
app.worker.stop(); vendor.shutdown()
print(json.dumps(out,ensure_ascii=False,indent=1))
