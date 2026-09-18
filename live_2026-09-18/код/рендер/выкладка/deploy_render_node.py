"""Run as node service user after VPS render admission is paused and idle."""
from pathlib import Path
import hashlib,json,os,py_compile,shutil,signal,subprocess,time
import psutil
stage=Path('/tmp/launch-render-c5-progress-20260914');target=Path('/opt/video_agent');unit='vsrender-puller.service'
manifest=json.loads((stage/'manifest.json').read_text());sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
pid=int(subprocess.check_output(['systemctl','show',unit,'-p','MainPID','--value'],text=True).strip());assert pid>0
assert psutil.Process(pid).uids().real==os.getuid()
assert subprocess.check_output(['systemctl','show',unit,'-p','Restart','--value'],text=True).strip()=='always'
children=[]
for p in psutil.Process(pid).children(recursive=True):
 try:
  if any(Path(a).name=='render_worker.py' for a in p.cmdline()):children.append(p.pid)
 except psutil.NoSuchProcess:pass
from collections import Counter
counts=Counter()
for f in (target/'render_jobs/outbox').glob('*.json'):
 counts[json.loads(f.read_text()).get('phase','invalid')]+=1
assert not any(counts[x] for x in ('preparing','rendering','ready','error','invalid')),dict(counts)
if children:print(json.dumps({'deployed':False,'active_render_pids':children}));raise SystemExit(3)
for name,info in manifest.items():
 assert sha(target/name)==info['baseline_sha256'],'production changed:'+name
 assert sha(stage/'payload'/name)==info['candidate_sha256'],'candidate changed:'+name
 py_compile.compile(str(stage/'payload'/name),doraise=True)
backup=target/'backups'/('launch_c5_progress_'+time.strftime('%Y%m%d_%H%M%S'));backup.mkdir(parents=True)
for name in manifest:
 if (target/name).exists():shutil.copy2(target/name,backup/name)
changed=[]
try:
 for name,info in manifest.items():
  assert sha(target/name)==info['baseline_sha256']
  tmp=target/(name+'.launch-new');shutil.copy2(target/name,tmp)
  with tmp.open('wb') as handle:
   handle.write((stage/'payload'/name).read_bytes());handle.flush();os.fsync(handle.fileno())
  os.replace(tmp,target/name);changed.append(name)
except Exception:
 for name in changed:
  assert sha(target/name)==manifest[name]['candidate_sha256'],'concurrent edit during recovery'
  if (backup/name).exists():
   tmp=target/(name+'.launch-rollback');shutil.copy2(backup/name,tmp);os.replace(tmp,target/name)
  else:os.replace(target/name,backup/(name+'.unactivated'))
 raise
# Same service user may signal its own idle daemon; Restart=always was verified.
os.kill(pid,signal.SIGTERM);deadline=time.monotonic()+65
while time.monotonic()<deadline:
 value=subprocess.check_output(['systemctl','show',unit,'-p','MainPID','--value'],text=True).strip();newpid=int(value or 0)
 active=subprocess.run(['systemctl','is-active','--quiet',unit]).returncode==0
 if active and newpid>0 and newpid!=pid:break
 time.sleep(2)
else:raise RuntimeError('new service process not observed; retain render admission pause')
receipt={'deployed':True,'backup':str(backup),'old_pid':pid,'new_pid':newpid,'unit_active':active,'sha256':{n:sha(target/n) for n in manifest},'time':time.time()}
(stage/'deployment.json').write_text(json.dumps(receipt,indent=2));print(json.dumps(receipt,indent=2))
