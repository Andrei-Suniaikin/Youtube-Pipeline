"""Real SQLite/tar/restore/retention tests. Offsite is a second actual directory."""
from pathlib import Path
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('backup_service',HERE/'ops_candidate/backup_service.py')
backup=importlib.util.module_from_spec(spec);sys.modules[spec.name]=backup;spec.loader.exec_module(backup)
TMP=Path(tempfile.mkdtemp(prefix='backup-c3-'));source=TMP/'source';source.mkdir();local=TMP/'local';offsite=TMP/'offsite'
for name in ['vsgen','rewrite','cover']:
    with sqlite3.connect(source/(name+'.db')) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY, state TEXT)')
        db.executemany('INSERT INTO jobs VALUES (?,?)',[(i,'completed') for i in range(100)])
(source/'keys.json').write_text('{"synthetic":"not-a-real-key"}')
auth=source/'auth-home';auth.mkdir();(auth/'auth.json').write_text('{"synthetic_refresh":"test"}')
config={'local_directory':str(local),'offsite':{'local_directory':str(offsite)},'report':str(TMP/'backup.json'),'keep':2,
        'components':{name:{'databases':[str(source/(name+'.db'))]} for name in ['vsgen','rewrite','cover']}}
config['components']['credentials']={'files':[str(source/'keys.json')],'auth_roots':[str(auth)]}
results={}
first=backup.run(config);assert first['ok'] and len(first['components'])==4
assert all(x['restored_from_offsite'] for x in first['components'].values())
assert all((local/first['run_id']/c['archive']).stat().st_mode & 0o077 == 0 for c in first['components'].values())
results['three_database_services_keys_auth_actual_roundtrip']=True
# One service unavailable: healthy component backups still rotate independently.
bad=source/'vsgen.db';bad.rename(source/'vsgen.saved')
for _ in range(3):
    run=backup.run(config);assert not run['ok'] and not run['components']['vsgen']['ok']
    assert all(run['components'][n]['ok'] for n in ['rewrite','cover','credentials'])
assert len(list(offsite.glob('run-*/vsgen.tar.gz')))==1
assert len(list(offsite.glob('run-*/rewrite.tar.gz')))==2
assert len(list(offsite.glob('run-*/credentials.tar.gz')))==2
assert len(list(local.glob('run-*/cover.tar.gz')))==2
results['failure_independent_retention_keeps_two_per_healthy_component']=True
(source/'vsgen.saved').rename(bad)
valid=next(offsite.glob('run-*/rewrite.tar.gz'))
broken=TMP/'broken.tar.gz';data=valid.read_bytes();broken.write_bytes(data[:len(data)//2])
try:backup.verify_archive(broken)
except Exception:pass
else:raise AssertionError('corrupt archive accepted')
results['corrupt_restore_rejected']=True
# Unowned folders and known archives changed after manifest are retained.
unowned=offsite/'not-a-backup';unowned.mkdir();(unowned/'keep').write_text('keep')
backup.prune_owned(offsite,2);assert (unowned/'keep').read_text()=='keep'
results['retention_excludes_unowned_files']=True
# Remote retention source executes unchanged in a fresh process.
run=subprocess.run([sys.executable,'-c',backup.PRUNE_SCRIPT,str(offsite),'2'],capture_output=True,text=True,check=True)
assert isinstance(json.loads(run.stdout),list)
results['identical_remote_retention_python']=True
spec2=importlib.util.spec_from_file_location('backup_legacy_reclaim',HERE/'ops_candidate/backup_legacy_reclaim.py')
legacy=importlib.util.module_from_spec(spec2);spec2.loader.exec_module(legacy)
old=TMP/'legacy';old.mkdir();db=old/'old.db';shutil.copyfile(source/'vsgen.db',db)
def remote(mode,path,expected=None):
    args=[sys.executable,'-c',legacy.REMOTE,mode,str(old),str(path)]
    if expected is not None:args.append(json.dumps(expected))
    return subprocess.run(args,capture_output=True,text=True)
proof=json.loads(remote('inspect',db).stdout)
db.write_bytes(db.read_bytes()+b'changed')
assert remote('unlink',db,proof).returncode!=0 and db.exists()
proof=json.loads(remote('inspect',db).stdout)
assert remote('unlink',db,proof).returncode==0 and not db.exists()
other=TMP/'outside.db';other.write_bytes(b'protected')
assert remote('inspect',other).returncode!=0 and other.exists()
results['legacy_cas_exact_unlink_and_outside_refusal']=True
# Recovery metadata is in the archive, and restore never starts raw pending work.
recovery=source/'project' / '.remote_jobs' / '7'; recovery.mkdir(parents=True)
(recovery/'credential_pin.json').write_text(json.dumps({'family':'googler','sha256':'a'*64,'state':'active'}))
(recovery/'operation.json').write_text(json.dumps({'state':'submitted','remote_id':'paid-existing-id'}))
(recovery/'large.result').write_bytes(b'not included in metadata backup')
config['components']['vsgen']['recovery_globs']=[str(source/'project'/'.remote_jobs'/'**'/'*.json')]
with sqlite3.connect(source/'vsgen.db') as db:
    db.execute('CREATE TABLE projects(id TEXT,dir_path TEXT)')
    db.execute('INSERT INTO projects VALUES (?,?)',('p',str(source/'project')))
    db.execute('CREATE TABLE admin_state(k TEXT PRIMARY KEY,v TEXT,updated REAL)')
    db.execute('CREATE TABLE queue_tasks(job_id INTEGER,project_id TEXT,status TEXT,error TEXT,claim_token TEXT)')
    db.execute('INSERT INTO queue_tasks VALUES (7,"p","running","","old-token")')
    db.execute('CREATE TABLE gen_jobs(id INTEGER,status TEXT,error TEXT)')
    db.execute('INSERT INTO gen_jobs VALUES (7,"running","")')
for service in ['rewrite','cover']:
    with sqlite3.connect(source/(service+'.db')) as db:
        db.execute('ALTER TABLE jobs ADD COLUMN data TEXT')
        db.execute('ALTER TABLE jobs ADD COLUMN status TEXT')
        db.execute('ALTER TABLE jobs ADD COLUMN run_token TEXT')
        db.execute('ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER')
        db.execute('UPDATE jobs SET data=?,status="running",run_token="old",cancel_requested=0 WHERE id=1',(json.dumps({'status':'running','run_token':'old'}),))
        db.execute('UPDATE jobs SET status="completed",data=? WHERE id<>1',(json.dumps({'status':'completed'}),))
recovery_run=backup.run(config);assert recovery_run['ok']
spec3=importlib.util.spec_from_file_location('backup_restore',HERE/'ops_candidate/backup_restore.py')
restore=importlib.util.module_from_spec(spec3);spec3.loader.exec_module(restore)
destination=TMP/'restored-offline'
proof=restore.restore(offsite/recovery_run['run_id'],destination)
assert proof['ok'] and proof['held_jobs']==3 and proof['media_restored'] is False
vs=restore.mapped(destination,source/'vsgen.db')
with sqlite3.connect(vs) as db:
    assert db.execute('SELECT status,error,claim_token FROM queue_tasks').fetchone()==('error',restore.MESSAGE,'')
    assert json.loads(db.execute('SELECT v FROM admin_state WHERE k="queue_pause"').fetchone()[0])['all'] is True
for service in ['rewrite','cover']:
    with sqlite3.connect(restore.mapped(destination,source/(service+'.db'))) as db:
        status,data,token=db.execute('SELECT status,data,run_token FROM jobs WHERE id=1').fetchone()
        assert status=='failed' and json.loads(data)['restore_hold'] is True and token==''
        assert db.execute('SELECT COUNT(*) FROM jobs WHERE status="completed"').fetchone()[0]==99
restored_job=restore.mapped(destination,recovery)
assert json.loads((restored_job/'operation.json').read_text())['remote_id']=='paid-existing-id'
assert json.loads((restored_job/'credential_pin.json').read_text())['family']=='restore_hold'
assert json.loads((restored_job/'.restore_original'/'credential_pin.json').read_text())['family']=='googler'
assert not (restored_job/'large.result').exists()
results['metadata_and_safe_offline_restore_three_services_no_pending']=True
try:restore.restore(offsite/recovery_run['run_id'],destination)
except FileExistsError:pass
else:raise AssertionError('existing restore root overwritten')
results['restore_refuses_existing_root']=True

payload={'passed':len(results),'checks':results,'temporary_evidence':str(TMP),'initial_manifest':first}
(HERE/'backup_verify_cycle3_result.json').write_text(json.dumps(payload,indent=2));print(json.dumps(payload,indent=2))
