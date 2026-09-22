#!/usr/bin/env python3
"""Receipt-bound 14-day retention, verified archive, and finite disk admission.

An MP4 arriving from a render node is NOT a publication/delivery receipt.
Only a trusted upload receipt bound to an exact source manifest admits cleanup.
"""
import argparse
from contextlib import contextmanager, nullcontext
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
import uuid
from backup_service import atomic_json, sha

GIB = 1024**3
ACTIVE = ('queued','pending','running','waiting','resetting')
SHA_RE = re.compile(r'^[a-f0-9]{64}$')


def regular_files(root):
    root = Path(root)
    if root.is_symlink():
        raise ValueError('symlink root refused')
    if not root.is_dir():
        return
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(directory)/d).is_symlink()]
        for name in files:
            path = Path(directory)/name
            if path.is_symlink():
                continue
            if path.is_file():
                yield path


def within(path, root):
    path, root = Path(path), Path(root).resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('path outside project')
    return path.resolve()


@contextmanager
def connect(config):
    db = sqlite3.connect(config['database'], timeout=20)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def projects(config):
    with connect(config) as db:
        return [dict(r) for r in db.execute('SELECT id,dir_path FROM projects')]


def get_project(config, project_id):
    with connect(config) as db:
        value = db.execute('SELECT id,dir_path FROM projects WHERE id=?', (project_id,)).fetchone()
    if not value:
        raise ValueError('project missing')
    project = dict(value)
    project['dir_path'] = str(within(project['dir_path'], config['projects_root']))
    return project


def bind_delivery(config, project_id, manifest_path, receipt_path):
    """Explicit root-side ingest from the existing trusted receipt inbox/archive."""
    project = get_project(config, project_id)
    receipt_path = within(receipt_path, config['upload_receipts'])
    scope = receipt_path.relative_to(Path(config['upload_receipts']).resolve()).parts[:-1]
    if scope not in [('inbox',),('archive','expired')]:
        raise ValueError('receipt is not in accepted inbox/archive')
    if not receipt_path.is_file():
        raise ValueError('trusted upload receipt missing')
    spec = importlib.util.spec_from_file_location('storage_receipt_protocol', config['receipt_io'])
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    receipt = module.validate_receipt_structure(json.loads(receipt_path.read_bytes()))
    # Never infer source identity from a title, basename, duration, or render ACK.
    manifest_path = Path(manifest_path)
    if sha(manifest_path) != receipt['manifest_sha256'] or manifest_path.name != receipt['manifest_name']:
        raise ValueError('upload manifest identity mismatch')
    manifest = json.loads(manifest_path.read_bytes())
    if manifest.get('project_id') != project_id or not SHA_RE.fullmatch(manifest.get('video_sha256','')):
        raise ValueError('manifest has no exact project/video binding')
    video = within(manifest['video_path'], project['dir_path'])
    if video.suffix.lower() != '.mp4' or sha(video) != manifest['video_sha256']:
        raise ValueError('delivered video differs from local video')
    verified = dt.datetime.fromisoformat(receipt['verified_at'].replace('Z','+00:00')).timestamp()
    delivered = max(verified, float(receipt['scheduled_epoch']))
    if delivered > time.time():
        raise ValueError('publication time has not arrived')
    record = {'version':1,'project_id':project_id,'video_path':str(video),
              'video_sha256':manifest['video_sha256'],'delivered_at':delivered,
              'manifest_sha256':receipt['manifest_sha256'],'receipt_sha256':receipt['receipt_sha256'],
              'receipt_path':str(receipt_path),'source':'trusted_upload_receipt'}
    proof_root=Path(config['deliveries'])/'proofs';proof_root.mkdir(parents=True,exist_ok=True)
    for source,name in [(receipt_path,receipt['receipt_sha256']+'.receipt'),(manifest_path,receipt['manifest_sha256']+'.manifest')]:
        target=proof_root/name
        if target.exists():
            if target.read_bytes()!=source.read_bytes():raise ValueError('delivery proof conflict')
        else:
            with target.open('xb') as out:out.write(source.read_bytes());out.flush();os.fsync(out.fileno())
    destination = Path(config['deliveries'])/(hashlib.sha256((project_id+record['video_sha256']).encode()).hexdigest()+'.json')
    if destination.exists():
        if json.loads(destination.read_bytes()) != record:
            raise ValueError('delivery binding conflict')
    else:
        atomic_json(destination, record)
    return record


def verified_delivery(config, record):
    if record.get('source') == 'trusted_night_public':
        from delivery_evidence import verify_bound_record
        return verify_bound_record(config,record)
    if record.get('source') != 'trusted_upload_receipt':
        raise ValueError('untrusted delivery')
    # Registration stores only trusted validated receipt identities. Recheck the
    # original canonical receipt, without any age cutoff used by daily monitors.
    proof_root=Path(config['deliveries'])/'proofs'
    if not SHA_RE.fullmatch(record.get('receipt_sha256','')) or not SHA_RE.fullmatch(record.get('manifest_sha256','')):
        raise ValueError('invalid retained proof identity')
    receipt_path=within(proof_root/(record['receipt_sha256']+'.receipt'),proof_root)
    spec = importlib.util.spec_from_file_location('storage_receipt_check', config['receipt_io'])
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    receipt = module.validate_receipt_structure(json.loads(receipt_path.read_bytes()))
    if receipt['receipt_sha256'] != record['receipt_sha256'] or receipt['manifest_sha256'] != record['manifest_sha256']:
        raise ValueError('trusted receipt changed')
    delivered=max(dt.datetime.fromisoformat(receipt['verified_at'].replace('Z','+00:00')).timestamp(),float(receipt['scheduled_epoch']))
    if record['delivered_at']!=delivered:raise ValueError('delivery timestamp changed')
    manifest_path=within(proof_root/(record['manifest_sha256']+'.manifest'),proof_root)
    if sha(manifest_path)!=record['manifest_sha256']:raise ValueError('retained manifest changed')
    manifest=json.loads(manifest_path.read_bytes())
    if any(manifest.get(key)!=record[key] for key in ('project_id','video_sha256')) or str(Path(manifest['video_path']).resolve())!=record['video_path']:
        raise ValueError('retained delivery binding changed')
    project = get_project(config, record['project_id'])
    video = within(record['video_path'], project['dir_path'])
    if not SHA_RE.fullmatch(record['video_sha256']):
        raise ValueError('invalid video hash')
    return project, video


def active_jobs(db, project_id):
    placeholders = ','.join('?' for _ in ACTIVE)
    return db.execute('SELECT COUNT(*) FROM queue_tasks WHERE project_id=? AND status IN ('+placeholders+')', (project_id,*ACTIVE)).fetchone()[0]


def retire_project_cache(config, project, apply=False, _leased=False):
    """Remove only redundant base64, preserving IDs and explicit retired refs."""
    root=Path(project['dir_path'])
    journal=root/'.remote_jobs'
    if not journal.is_dir():return {'state':'no_cache','saved_bytes':0}
    with connect(config) as db:
        rows=[dict(r) for r in db.execute('SELECT * FROM queue_tasks WHERE project_id=?',(project['id'],))]
    if not rows or any(r['status'] not in ('done','error','cancelled','canceled') for r in rows):
        return {'state':'retained_nonterminal','saved_bytes':0}
    renders=[r for r in rows if r.get('kind')=='render' and r['status']=='done' and r.get('render_sha256')]
    if not renders:return {'state':'retained_no_verified_render','saved_bytes':0}
    latest=max(renders,key=lambda r:r.get('finished') or 0)
    # Completed chapters are text-only follow-up metadata. Later input mutations
    # still retain cache, including work started earlier but finished after render.
    if any(r.get('kind')!='chapters' and max(r.get('created') or 0,r.get('finished') or 0)>(latest.get('finished') or 0) for r in rows):
        return {'state':'retained_newer_work','saved_bytes':0}
    final=within(root/(latest.get('output_rel') or ''),root)
    if not final.is_file() or final.stat().st_size!=latest.get('render_size') or sha(final)!=latest['render_sha256']:
        return {'state':'retained_final_changed','saved_bytes':0}
    records=[]
    for path in journal.rglob('*.json'):
        if path.is_symlink():raise ValueError('journal symlink')
        record=json.loads(path.read_bytes())
        if path.name=='credential_pin.json' and record.get('state')!='released':
            return {'state':'retained_pinned_remote','saved_bytes':0}
        if record.get('state') in ('intent','uncertain','submitted','pending','cancel_requested'):
            return {'state':'retained_unsettled_remote','saved_bytes':0}
        if record.get('state')=='done' and record.get('result_sha256'):
            records.append(path)
    if not records:return {'state':'no_result_cache','saved_bytes':0}
    if apply and not _leased:
        with project_lease(config,project['id']):
            return retire_project_cache(config,project,apply=True,_leased=True)
    outputs={}
    for path in regular_files(root):
        if '.remote_jobs' not in path.relative_to(root).parts and path.suffix.lower() in ('.png','.jpg','.jpeg','.webp'):
            outputs[sha(path)]=path
    sys.path.insert(0,config['worker_module_directory'])
    from remote_cache_storage import compact_result
    total=0;compacted=0
    with nullcontext():
        # All admission paths use this same lease; repeat final identity under it.
        if sha(final)!=latest['render_sha256']:raise ValueError('render changed before cache retirement')
        for path in records:
            result=compact_result(path,outputs,apply,retire_to_project=root)
            total+=result['saved_bytes'];compacted+=result['state'] in ('eligible','compacted')
    return {'state':'retired' if apply else 'eligible','saved_bytes':total,'results':compacted}


@contextmanager
def project_lease(config, project_id):
    owner = uuid.uuid4().hex; key = 'storage_cleanup:'+project_id
    with connect(config) as db:
        db.execute('BEGIN IMMEDIATE')
        prior = db.execute('SELECT v FROM admin_state WHERE k=?', (key,)).fetchone()
        if prior and float(json.loads(prior[0])['expires_at']) > time.time():
            raise ValueError('project already in storage maintenance')
        if active_jobs(db, project_id):
            raise ValueError('project has active jobs')
        expires = time.time()+3600
        db.execute('INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES(?,?,?)', (key,json.dumps({'owner':owner,'expires_at':expires}),time.time()))
        db.commit()
    try:
        yield key, owner
    finally:
        with connect(config) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT v FROM admin_state WHERE k=?', (key,)).fetchone()
            if row and json.loads(row[0]).get('owner') == owner:
                db.execute('DELETE FROM admin_state WHERE k=?', (key,))


def target_files(config, project, record):
    root = Path(project['dir_path'])
    video = within(record['video_path'], root)
    if not video.is_file() or sha(video) != record['video_sha256']:
        raise ValueError('current MP4 not the delivered MP4')
    with connect(config) as db:
        if active_jobs(db, project['id']):
            raise ValueError('project active')
        latest = db.execute('SELECT MAX(MAX(COALESCE(created,0),COALESCE(finished,0))) FROM queue_tasks WHERE project_id=?',(project['id'],)).fetchone()[0]
        if latest and latest > record['delivered_at']:
            raise ValueError('project has newer generation work')
    candidates = [video]
    for directory in ('_RENDER_MEDIA','.remote_jobs'):
        paths=list(regular_files(root/directory))
        # New background edits are retained as a whole. They do not invalidate an
        # exact already-delivered old MP4, and cache metadata has its own lifetime.
        if directory=='_RENDER_MEDIA' and any(p.stat().st_mtime>record['delivered_at'] for p in paths):
            continue
        for path in paths:
            if path.suffix == '.lock' or path.name.endswith('.lock'):
                continue
            if directory == '.remote_jobs' and path.suffix == '.json':
                value = json.loads(path.read_bytes())
                if path.name == 'credential_pin.json':
                    if value.get('state') != 'released':raise ValueError('retained remote credential')
                elif value.get('state') and value['state'] not in ('done','completed','rejected','failed','cancelled'):
                    raise ValueError('unsettled remote operation')
                if value.get('state') == 'failed':
                    raise ValueError('failed remote operation retained')
            if directory=='.remote_jobs':
                # Paid identities and explicit retired references outlive media.
                # Removing them would make same-job retry appear never submitted.
                if path.suffix!='.result' and '.result-blobs' not in path.relative_to(root/'.remote_jobs').parts:
                    continue
                if path.stat().st_mtime>record['delivered_at']:
                    continue
            candidates.append(path)
    return candidates


def verify_media_archive(path, entries):
    with tarfile.open(path, 'r:gz') as tar:
        members = tar.getmembers()
        if len(members) != len(entries) or {m.name for m in members} != {e['relative'] for e in entries}:
            raise ValueError('media archive contents differ')
        for entry in entries:
            member = tar.getmember(entry['relative'])
            if not member.isfile() or member.size != entry['bytes']:
                raise ValueError('media archive entry type/size differs')
            h = hashlib.sha256()
            with tar.extractfile(member) as source:
                for chunk in iter(lambda:source.read(1024*1024),b''):h.update(chunk)
            if h.hexdigest() != entry['sha256']:
                raise ValueError('media archive checksum differs')


def retire(config, record, apply=False):
    project, video = verified_delivery(config, record)
    if time.time()-record['delivered_at'] < int(config.get('retention_days',14))*86400:
        return {'state':'retained_younger_than_14_days','bytes':0}
    archive_id=hashlib.sha256((project['id']+record['video_sha256']).encode()).hexdigest()
    existing=Path(config['archives'])/(archive_id+'.json')
    if existing.exists():
        prior=json.loads(existing.read_bytes())
        if prior.get('state')=='committed' and prior.get('delivery')==record:
            return {'state':'already_retired','bytes':0}
    candidates = target_files(config, project, record)
    total = sum(p.stat().st_size for p in candidates)
    if not apply:
        return {'state':'eligible','bytes':total,'files':len(candidates)}
    archive_root = Path(config['archives']);archive_root.mkdir(parents=True,exist_ok=True)
    retained = sum(p.stat().st_size for p in regular_files(archive_root))
    if retained+total > int(config.get('max_archive_bytes',64*GIB)):
        raise ValueError('media archive cap reached; retain sources')
    if shutil.disk_usage(archive_root).free-total < int(config.get('min_free_bytes',64*GIB)):
        raise ValueError('media archive reserve; retain sources')
    with project_lease(config, project['id']) as (key, owner):
        candidates = target_files(config, project, record)
        entries = []
        for path in candidates:
            info = path.stat()
            entries.append({'relative':str(path.relative_to(project['dir_path'])),'sha256':sha(path),'bytes':info.st_size,'inode':info.st_ino,'mtime_ns':info.st_mtime_ns})
        archive_id = hashlib.sha256((project['id']+record['video_sha256']).encode()).hexdigest()
        archive = archive_root/(archive_id+'.tar.gz'); manifest_path = archive_root/(archive_id+'.json')
        if archive.exists() or manifest_path.exists():
            raise ValueError('existing media archive requires reconciliation; no overwrite')
        temporary = archive.with_suffix('.tmp')
        with tarfile.open(temporary, 'x:gz', compresslevel=1) as tar:
            for entry in entries:tar.add(Path(project['dir_path'])/entry['relative'],arcname=entry['relative'],recursive=False)
        with temporary.open('rb') as fd:os.fsync(fd.fileno())
        verify_media_archive(temporary, entries)
        os.replace(temporary, archive)
        manifest = {'version':1,'project_id':project['id'],'delivery':record,'entries':entries,'archive_sha256':sha(archive),'verified_at':time.time(),'state':'verified','removed':[]}
        atomic_json(manifest_path, manifest)
        # Hashing/copying is outside the DB transaction. The deletion transaction
        # is bounded to exact regular files and checks lease + current identities.
        with connect(config) as db:
            db.execute('BEGIN IMMEDIATE')
            lease = db.execute('SELECT v FROM admin_state WHERE k=?',(key,)).fetchone()
            lease = json.loads(lease[0]) if lease else {}
            if lease.get('owner') != owner or lease.get('expires_at',0) <= time.time() or active_jobs(db,project['id']):
                raise ValueError('storage fence expired or project restarted')
            for entry in entries:
                path = within(Path(project['dir_path'])/entry['relative'],project['dir_path']);info=path.stat()
                if (info.st_ino,info.st_size,info.st_mtime_ns) != (entry['inode'],entry['bytes'],entry['mtime_ns']):
                    raise ValueError('source changed after verified archive')
            # All source identities checked before first unlink. Data remains in
            # verified archive if a later filesystem failure interrupts cleanup.
            for entry in entries:
                (Path(project['dir_path'])/entry['relative']).unlink()
                manifest['removed'].append(entry['relative'])
                atomic_json(manifest_path,manifest)
        manifest.update(state='committed',committed_at=time.time())
        atomic_json(manifest_path,manifest)
        return {'state':'retired','bytes':total,'files':len(entries),'archive_sha256':manifest['archive_sha256']}


def prune_archives(config):
    """Additional rollback copy lasts one day AFTER the authorized 14-day period."""
    root = Path(config['archives']);removed=0
    for path in root.glob('*.json'):
        if path.is_symlink() or not SHA_RE.fullmatch(path.stem):continue
        manifest=json.loads(path.read_bytes());archive=path.with_suffix('.tar.gz')
        if manifest.get('state')!='committed' or time.time()-manifest['committed_at'] < int(config.get('archive_days',1))*86400:continue
        verified_delivery(config,manifest['delivery'])
        if time.time()-manifest['delivery']['delivered_at'] < int(config.get('retention_days',14))*86400:continue
        if not archive.exists():continue
        if archive.is_symlink() or sha(archive)!=manifest['archive_sha256']:raise ValueError('archive changed; retention blocked')
        manifest['archive_retired_at']=time.time();atomic_json(path,manifest)
        archive.unlink();removed+=1
    return removed


def run(config, apply=False):
    os.umask(0o077)
    output=Path(config['report']);output.parent.mkdir(parents=True,exist_ok=True)
    with open(output.with_suffix('.lock'),'a+b') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        categories={'mp4_bytes':0,'render_media_bytes':0,'remote_journal_bytes':0,'other_bytes':0}
        vanished_files=0
        all_projects=projects(config)
        for project in all_projects:
            root=within(project['dir_path'],config['projects_root'])
            for path in regular_files(root):
                rel=path.relative_to(root)
                try:size=path.stat().st_size
                except FileNotFoundError:
                    vanished_files+=1;continue
                if rel.parts[0]=='_RENDER_MEDIA':categories['render_media_bytes']+=size
                elif rel.parts[0]=='.remote_jobs':categories['remote_journal_bytes']+=size
                elif path.suffix.lower()=='.mp4':categories['mp4_bytes']+=size
                else:categories['other_bytes']+=size
        evidence={'ok':True,'bound':0,'errors':{}}
        if config.get('delivery_evidence'):
            from delivery_evidence import ingest_pending
            evidence=ingest_pending(config)
        retained=0;eligible=0;errors={};retired=0;bindings=0
        cache_saved=0;cache_results=0;cache_errors={}
        if config.get('cache_retirement_enabled'):
            module=Path(config['worker_module_directory'])/'remote_generation_journal.py'
            if apply and sha(module)!=config.get('cache_loader_sha256'):
                raise ValueError('compatible journal loader must be deployed before cache retirement')
            for project in all_projects:
                try:
                    result=retire_project_cache(config,project,apply)
                    cache_saved+=result['saved_bytes'];cache_results+=result.get('results',0)
                except Exception as exc:
                    code=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
                    cache_errors[code]=cache_errors.get(code,0)+1
        if apply:prune_archives(config)
        for path in Path(config['deliveries']).glob('*.json'):
            bindings+=1
            try:
                record=json.loads(path.read_bytes());result=retire(config,record,apply)
                if result['state']=='eligible':eligible+=result['bytes']
                elif result['state']=='retired':retired+=result['bytes']
                else:retained+=1
            except Exception as exc:
                # Reasons describe correctness gates; never print file contents.
                code=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
                errors[code]=errors.get(code,0)+1
        free=shutil.disk_usage(config['projects_root']).free
        reserve=int(config.get('min_free_bytes',64*GIB));warning=int(config.get('warning_free_bytes',80*GIB))
        report={'generated_at':time.time(),'ok':free>=warning and not errors and not cache_errors and evidence['ok'],
                'components':{'disk':{'ok':free>=warning,'free_bytes':free,'admission_allowed':free>=reserve,'min_free_bytes':reserve,'warning_free_bytes':warning},
                'retention':{'ok':not errors,'days':14,'bindings':bindings,'eligible_bytes':eligible,'retired_bytes':retired,'retained_bindings':retained,'errors':errors,'archive_bytes':sum(p.stat().st_size for p in regular_files(config['archives']))},
                'cache':{'ok':not cache_errors,'saved_bytes':cache_saved,'results':cache_results,'errors':cache_errors,'mode':'apply' if apply else 'dryrun'},
                'delivery_evidence':evidence,
                'inventory':{'ok':True,'projects':len(all_projects),'vanished_files':vanished_files,**categories}},'apply':apply}
        atomic_json(output,report);return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True)
    sub=parser.add_subparsers(dest='mode',required=True)
    scan=sub.add_parser('scan');scan.add_argument('--apply',action='store_true')
    bind=sub.add_parser('bind-delivery');bind.add_argument('--project-id',required=True);bind.add_argument('--manifest',required=True);bind.add_argument('--receipt',required=True)
    args=parser.parse_args();config=json.loads(Path(args.config).read_bytes())
    result=run(config,args.apply) if args.mode=='scan' else bind_delivery(config,args.project_id,args.manifest,args.receipt)
    print(json.dumps(result,sort_keys=True))
