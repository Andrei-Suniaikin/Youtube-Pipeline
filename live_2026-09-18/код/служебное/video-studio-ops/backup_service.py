#!/usr/bin/env python3
"""Independent component snapshots, verified offsite roundtrip, bounded owned retention."""
import argparse
from contextlib import closing
import fcntl
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import uuid
from urllib.parse import quote

RUN_RE=re.compile(r'^run-\d{8}T\d{6}-[0-9a-f]{8}$')
NAME_RE=re.compile(r'^[a-zA-Z0-9_-]+$')

def sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.backup-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:json.dump(value,f,sort_keys=True);f.flush();os.fsync(f.fileno())
        os.replace(name,path)
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if os.path.exists(name):os.unlink(name)

def sqlite_check(path):
    with closing(sqlite3.connect('file:'+quote(str(Path(path).resolve()))+'?mode=ro',uri=True,timeout=15)) as db:
        if db.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('SQLite integrity check failed')
        tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {t:db.execute('SELECT COUNT(*) FROM "'+t.replace('"','""')+'"').fetchone()[0] for t in tables}

def snapshot_db(source,dest,timeout=300):
    if not Path(source).is_file():raise FileNotFoundError('database missing')
    deadline=time.monotonic()+timeout
    def progress(*_):
        if time.monotonic()>deadline:raise TimeoutError('SQLite snapshot deadline')
    with closing(sqlite3.connect('file:'+quote(str(Path(source).resolve()))+'?mode=ro',uri=True,timeout=15)) as src:
        with closing(sqlite3.connect(dest)) as dst:src.backup(dst,pages=256,progress=progress,sleep=.05)
    return sqlite_check(dest)

def build_component(name,component,directory):
    if not NAME_RE.fullmatch(name):raise ValueError('unsafe component name')
    with tempfile.TemporaryDirectory(prefix='.snapshot-',dir=directory) as tmp:
        stage=Path(tmp);manifest={'component':name,'created':time.time(),'entries':[],'optional_absent':[]}
        for index,path in enumerate(component.get('databases',[])):
            target=stage/('database-'+str(index)+'.db')
            counts=snapshot_db(path,target)
            manifest['entries'].append({'name':target.name,'source':str(path),'kind':'sqlite','sha256':sha(target),'size':target.stat().st_size,'tables':counts})
        paths=[(Path(p),False) for p in component.get('files',[])]
        # Paid-operation identities are small and indispensable after host loss.
        # Binary/result caches remain separately retained; restored queues are held.
        for pattern in component.get('recovery_globs', []):
            paths.extend((Path(p), False) for p in glob.glob(pattern, recursive=True) if Path(p).is_file())
        for root in component.get('directories',[]):
            root=Path(root)
            if not root.is_dir():raise FileNotFoundError('required configuration directory missing')
            paths.extend((p,False) for p in root.rglob('*') if p.is_file() or p.is_symlink())
        for pattern in component.get('optional_globs',[]):
            paths.extend((Path(p),False) for p in glob.glob(pattern,recursive=True))
        for root in component.get('auth_roots',[]):
            root=Path(root)
            if not root.exists():manifest['optional_absent'].append(str(root));continue
            for p in root.rglob('auth.json'):paths.append((p,False))
            for p in root.rglob('.applied.sha256'):paths.append((p,False))
        for p in component.get('optional_files',[]):
            p=Path(p)
            if p.exists():paths.append((p,False))
            else:manifest['optional_absent'].append(str(p))
        seen=set()
        for path,_ in paths:
            if path.is_symlink() or not path.is_file():raise ValueError('secret/config path is not a regular file')
            if str(path.resolve()) in seen:continue
            seen.add(str(path.resolve()))
            target=stage/('file-'+str(len(seen)))
            shutil.copyfile(path,target)
            if '.remote_jobs' in path.parts and path.suffix == '.json':
                json.loads(target.read_bytes())  # damaged paid IDs must not pass backup
            manifest['entries'].append({'name':target.name,'source':str(path),'kind':'file','sha256':sha(target),'size':target.stat().st_size})
        if not manifest['entries']:raise ValueError('component has no files')
        atomic_json(stage/'manifest.json',manifest)
        archive=directory/(name+'.tar.gz')
        with tarfile.open(archive,'w:gz',compresslevel=6) as tar:
            for filename in ['manifest.json']+[entry['name'] for entry in manifest['entries']]:
                tar.add(stage/filename,arcname=filename,recursive=False)
        verify_archive(archive)
        return {'archive':archive.name,'sha256':sha(archive),'bytes':archive.stat().st_size,'entries':len(manifest['entries']),
                'database_count':sum(e['kind']=='sqlite' for e in manifest['entries']),'optional_absent':len(manifest['optional_absent'])}

def verify_archive(archive):
    # Real restore to fresh files. Never extract caller-controlled tar paths.
    with tempfile.TemporaryDirectory(prefix='backup-restore-') as tmp:
        with tarfile.open(archive,'r:gz') as tar:
            members=tar.getmembers()
            if any(not m.isfile() or Path(m.name).name!=m.name for m in members):raise ValueError('unsafe backup member')
            manifest=json.load(tar.extractfile('manifest.json'))
            expected={'manifest.json'}|{e['name'] for e in manifest['entries']}
            if len(members)!=len(expected) or {m.name for m in members}!=expected:raise ValueError('archive manifest mismatch')
            for e in manifest['entries']:
                target=Path(tmp)/e['name']
                with tar.extractfile(e['name']) as source,open(target,'wb') as out:shutil.copyfileobj(source,out,1024*1024)
                if target.stat().st_size!=e['size'] or sha(target)!=e['sha256']:raise ValueError('restored checksum mismatch')
                if e['kind']=='sqlite' and sqlite_check(target)!=e['tables']:raise ValueError('restored database rows differ')
            return {'component':manifest['component'],'restored_entries':len(manifest['entries'])}

class Offsite:
    def __init__(self,config):
        self.config=config;self.local=Path(config['local_directory']).resolve() if config.get('local_directory') else None
        self.root=config.get('directory','/root/video-studio-backups')
        self.target=config.get('target')
        if not self.local and (not self.target or not re.fullmatch(r'[a-zA-Z0-9_.@:-]+',self.target)):raise ValueError('bad SSH target')
    def remote(self,script,args=(),stdin=None):
        command='python3 -c '+shlex.quote(script)+' '+' '.join(shlex.quote(str(a)) for a in args)
        return subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',self.target,command],
                              stdin=stdin,capture_output=True,check=True,timeout=600)
    def put(self,source,run,name):
        if not RUN_RE.fullmatch(run) or Path(name).name!=name:raise ValueError('unsafe offsite path')
        if self.local:
            directory=self.local/run;directory.mkdir(parents=True,exist_ok=True);tmp=directory/(name+'.tmp');shutil.copyfile(source,tmp);os.replace(tmp,directory/name)
            return sha(directory/name)
        script="import os,sys,pathlib,hashlib,shutil;os.umask(0o077);p=pathlib.Path(sys.argv[1])/sys.argv[2];p.mkdir(parents=True,exist_ok=True);assert shutil.disk_usage(p).free>=int(sys.argv[4])+134217728,'offsite free-space reserve';t=p/(sys.argv[3]+'.tmp');f=open(t,'wb');h=hashlib.sha256();\nwhile True:\n b=sys.stdin.buffer.read(1048576)\n if not b:break\n f.write(b);h.update(b)\nf.flush();os.fsync(f.fileno());f.close();os.replace(t,p/sys.argv[3]);print(h.hexdigest())"
        with open(source,'rb') as src:return self.remote(script,[self.root,run,name,Path(source).stat().st_size],src).stdout.decode().strip()
    def get(self,run,name,dest):
        if self.local:shutil.copyfile(self.local/run/name,dest);return
        script="import sys,pathlib,shutil;p=pathlib.Path(sys.argv[1])/sys.argv[2]/sys.argv[3];shutil.copyfileobj(open(p,'rb'),sys.stdout.buffer,1048576)"
        command='python3 -c '+shlex.quote(script)+' '+' '.join(shlex.quote(str(x)) for x in [self.root,run,name])
        with open(dest,'wb') as out:
            subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',self.target,command],stdout=out,stderr=subprocess.PIPE,check=True,timeout=600)
    def prune(self,keep):
        # Only our successful runs, only the exact files from each manifest.
        script=PRUNE_SCRIPT
        if self.local:return prune_owned(self.local,keep)
        return json.loads(self.remote(script,[self.root,keep]).stdout)

def prune_owned(root,keep):
    root=Path(root);candidates={};deleted=[]
    for path in root.glob('run-*'):
        if not path.is_dir() or path.is_symlink() or not RUN_RE.fullmatch(path.name):continue
        try:manifest=json.loads((path/'run.json').read_text())
        except (OSError,ValueError):continue
        for name,entry in manifest.get('components',{}).items():
            archive=entry.get('archive','')
            if not entry.get('ok') or not entry.get('restored_from_offsite') or Path(archive).name!=archive:continue
            target=path/archive
            if target.is_file() and not target.is_symlink():
                candidates.setdefault(name,[]).append((manifest['generated_at'],target,entry))
    # A broken service never prevents verified snapshots of its neighbors from
    # rotating. Each component retains at least two independent successful runs.
    for entries in candidates.values():
        for _,target,entry in sorted(entries,reverse=True)[max(2,int(keep)):]:
            if sha(target)!=entry['sha256']:continue
            retired=target.parent/'retention.json'
            previous=json.loads(retired.read_text()) if retired.exists() else {}
            previous[target.name]={'retired_at':time.time(),'sha256':entry['sha256']}
            atomic_json(retired,previous)
            target.unlink();deleted.append(str(target.relative_to(root)))
    return deleted

# Keep the remote retention implementation identical to the locally tested one.
import inspect
PRUNE_SCRIPT='import pathlib,json,re,hashlib,os,tempfile,time\nPath=pathlib.Path\nRUN_RE=re.compile('+repr(RUN_RE.pattern)+')\n'+inspect.getsource(sha)+inspect.getsource(atomic_json)+inspect.getsource(prune_owned)+'\nimport sys\nprint(json.dumps(prune_owned(sys.argv[1],int(sys.argv[2]))))'

def run(config):
    os.umask(0o077)
    local=Path(config['local_directory']);local.mkdir(parents=True,exist_ok=True)
    state=Path(config.get('report','/var/lib/video-studio-ops/backup.json'))
    lock=open(local/'backup.lock','a+b')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise RuntimeError('backup already running')
    run_id=time.strftime('run-%Y%m%dT%H%M%S',time.gmtime())+'-'+uuid.uuid4().hex[:8]
    directory=local/run_id;directory.mkdir()
    offsite=Offsite(config['offsite']);report={'generated_at':time.time(),'ok':False,'run_id':run_id,'components':{}}
    atomic_json(directory/'progress.json',report)
    for name,component in config['components'].items():
        started=time.time()
        try:
            if shutil.disk_usage(local).free<int(config.get('min_local_free_bytes',2*1024**3)):
                raise OSError('local backup free-space reserve')
            entry=build_component(name,component,directory)
            if offsite.put(directory/entry['archive'],run_id,entry['archive'])!=entry['sha256']:raise ValueError('offsite upload checksum mismatch')
            with tempfile.TemporaryDirectory(prefix='backup-roundtrip-') as tmp:
                restored=Path(tmp)/entry['archive'];offsite.get(run_id,entry['archive'],restored)
                if sha(restored)!=entry['sha256']:raise ValueError('offsite download checksum mismatch')
                verify_archive(restored)
            report['components'][name]={**entry,'ok':True,'restored_from_offsite':True,'seconds':time.time()-started}
        except Exception as exc:
            report['components'][name]={'ok':False,'error_type':type(exc).__name__,'seconds':time.time()-started}
        report['generated_at']=time.time();atomic_json(directory/'progress.json',report)
        print(json.dumps({'component':name,**report['components'][name]}),flush=True)
    report['ok']=bool(report['components']) and all(c['ok'] for c in report['components'].values())
    atomic_json(directory/'run.json',report)
    try:
        checksum=offsite.put(directory/'run.json',run_id,'run.json')
        if checksum!=sha(directory/'run.json'):raise ValueError('run manifest checksum mismatch')
        report['offsite_pruned']=offsite.prune(config.get('keep',2));report['local_pruned']=prune_owned(local,config.get('keep',2))
    except Exception as exc:report['ok']=False;report['offsite_manifest_error']=type(exc).__name__
    report['generated_at']=time.time();atomic_json(state,report)
    return report

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    report=run(json.loads(Path(args.config).read_text()));print(json.dumps(report));raise SystemExit(0 if report['ok'] else 1)
