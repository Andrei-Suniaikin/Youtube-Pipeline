#!/usr/bin/env python3
"""Preserve full historical raw DB bytes as verified gzip before exact CAS unlink."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shlex
import select
import stat
import subprocess
import sys
import time
from backup_service import atomic_json, sha

REMOTE = r'''
import os,sys,json,pathlib,hashlib,stat
mode,root,path=sys.argv[1:4];root=pathlib.Path(root).resolve();p=pathlib.Path(path)
if p.parent.resolve()!=root or p.suffix!='.db':raise SystemExit('path outside allowed legacy directory')
s=p.lstat()
if not stat.S_ISREG(s.st_mode):raise SystemExit('not a regular legacy file')
h=hashlib.sha256()
with open(p,'rb') as f:
 while True:
  b=f.read(1048576)
  if not b:break
  h.update(b)
after=p.lstat()
if (s.st_ino,s.st_mtime_ns,s.st_size)!=(after.st_ino,after.st_mtime_ns,after.st_size):raise SystemExit('legacy changed during read')
record={'sha256':h.hexdigest(),'size':s.st_size,'inode':s.st_ino,'mtime_ns':s.st_mtime_ns}
if mode=='inspect':print(json.dumps(record))
elif mode=='unlink':
 expected=json.loads(sys.argv[4])
 if any(record[k]!=expected[k] for k in ('sha256','size','inode','mtime_ns')):raise SystemExit('legacy CAS mismatch; retained')
 p.unlink();print(json.dumps({'unlinked':str(p),'bytes':s.st_size}))
else:raise SystemExit('invalid mode')
'''

def command(target,script,args):
    return ['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',target,
            'python3 -c '+shlex.quote(script)+' '+' '.join(shlex.quote(str(x)) for x in args)]

def raw_digest(path):
    h=hashlib.sha256();size=0
    with gzip.open(path,'rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b);size+=len(b)
    return h.hexdigest(),size

def snapshot(args):
    os.umask(0o077);destination=Path(args.destination).resolve();destination.mkdir(parents=True,exist_ok=False)
    query="import pathlib,json;print(json.dumps([str(p) for p in sorted(pathlib.Path(__import__('sys').argv[1]).glob('*.db'))]))"
    paths=json.loads(subprocess.check_output(command(args.target,query,[args.remote_directory]),text=True,timeout=30))
    manifest={'generated_at':time.time(),'target':args.target,'remote_directory':args.remote_directory,'copies':[],'deletion_authorized':False}
    for index,path in enumerate(paths):
        before=json.loads(subprocess.check_output(command(args.target,REMOTE,['inspect',args.remote_directory,path]),text=True,timeout=300))
        out=destination/(str(index)+'-'+Path(path).name+'.gz')
        reader="import sys,shutil;shutil.copyfileobj(open(sys.argv[1],'rb'),sys.stdout.buffer,1048576)"
        process=subprocess.Popen(command(args.target,reader,[path]),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        deadline=time.monotonic()+600
        try:
            with gzip.open(out,'wb',compresslevel=6) as f:
                while True:
                    if time.monotonic()>deadline:raise TimeoutError('legacy stream deadline')
                    if not select.select([process.stdout],[],[],1)[0]:continue
                    chunk=os.read(process.stdout.fileno(),1024*1024)
                    if not chunk:break
                    f.write(chunk)
            process.wait(timeout=300)
            if process.returncode:raise RuntimeError('legacy SSH stream failed')
        finally:
            if process.poll() is None:process.kill();process.wait()
        with open(out,'rb') as f:os.fsync(f.fileno())
        digest,size=raw_digest(out)
        after=json.loads(subprocess.check_output(command(args.target,REMOTE,['inspect',args.remote_directory,path]),text=True,timeout=300))
        if before!=after or (digest,size)!=(before['sha256'],before['size']):raise RuntimeError('legacy copy verification failed; no deletion')
        manifest['copies'].append({'source':path,'snapshot':str(out),'gzip_sha256':sha(out),'source_identity':before,'verified':True})
        atomic_json(destination/'manifest.json',manifest)
        print(json.dumps({'copied':Path(path).name,'raw_bytes':size,'gzip_bytes':out.stat().st_size}),flush=True)
    return manifest

def unlink_verified(args):
    manifest=json.loads(Path(args.manifest).read_text());results=[]
    receipt=Path(args.manifest).with_name('unlink_receipt.json')
    manifest_sha=sha(args.manifest)
    if receipt.exists():
        previous=json.loads(receipt.read_text())
        if previous['manifest_sha256'] != manifest_sha:raise RuntimeError('unlink receipt identity conflict')
        results=previous['results']
    already={result['unlinked'] for result in results}
    for entry in manifest['copies']:
        snapshot=Path(entry['snapshot'])
        if not entry.get('verified') or sha(snapshot)!=entry['gzip_sha256']:raise RuntimeError('snapshot changed; no deletion')
        if raw_digest(snapshot)!=(entry['source_identity']['sha256'],entry['source_identity']['size']):raise RuntimeError('snapshot not restorable; no deletion')
        if entry['source'] in already:continue
        result=json.loads(subprocess.check_output(command(manifest['target'],REMOTE,['unlink',manifest['remote_directory'],entry['source'],json.dumps(entry['source_identity'])]),text=True,timeout=300))
        results.append(result)
        atomic_json(receipt,{'generated_at':time.time(),'results':results,'manifest_sha256':manifest_sha})
        print(json.dumps(result),flush=True)
    return results

if __name__=='__main__':
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='mode',required=True)
    s=sub.add_parser('snapshot');s.add_argument('--target',required=True);s.add_argument('--remote-directory',default='/root/offsite_backups/vsgen');s.add_argument('--destination',required=True)
    s=sub.add_parser('unlink-verified');s.add_argument('--manifest',required=True)
    args=p.parse_args();print(json.dumps(snapshot(args) if args.mode=='snapshot' else unlink_verified(args),indent=2))
