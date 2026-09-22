#!/usr/bin/env python3
"""Read-only PUBLIC ledger evidence transport; never upload or infer publication.

Authentication is provided by the existing node token at the HTTP boundary.
Only accepted exact ledger proofs qualify; historical incomplete proofs fail closed.
"""
import argparse
import base64
import datetime as dt
import fcntl
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
import unicodedata
import urllib.request
from backup_service import atomic_json, sha

MAX_BODY = 2 * 1024**2
HASH = re.compile(r'^[a-f0-9]{64}$')
UPLOAD_KEYS = ('project_id','channel_uc','profile_id','manifest_sha256','title_sha256',
               'video_sha256','cover_sha256','phase','video_id','status','attempt_id')
ATTEMPT_KEYS = ('attempt_id','channel_uc','project_id','started_at','phase','video_id','status','result_json')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def timestamp(value):
    parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.timestamp() > time.time():
        raise ValueError('invalid evidence timestamp')
    return parsed.timestamp()


def file_inside(value, root):
    path, root = Path(value), Path(root).resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
        raise ValueError('evidence path outside source project')
    return path.resolve()


def _validate(envelope):
    if len(canonical(envelope)) > MAX_BODY or envelope.get('version') != 1 or envelope.get('kind') != 'night_public_v1':
        raise ValueError('unsupported delivery evidence')
    if envelope.get('producer') not in ('minsk', 'mac'):
        raise ValueError('unsupported evidence producer')
    record, attempt, proof = (envelope[k] for k in ('upload','attempt','proof'))
    if not re.fullmatch(r'p_[a-f0-9]{10}', record.get('project_id','')) or not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', record.get('channel_uc','')):
        raise ValueError('invalid publication identity')
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', record.get('video_id','')):
        raise ValueError('invalid remote video identity')
    if record.get('status') != 'confirmed_public' or attempt.get('status') != 'needs_readback':
        raise ValueError('publication is not accepted')
    if record.get('phase') != 'video_id_known' or attempt.get('phase') != 'video_id_known':
        raise ValueError('unfinished upload attempt')
    for key in ('attempt_id','project_id','channel_uc','video_id'):
        if not record.get(key) or record[key] != attempt.get(key) or record[key] != proof.get(key):
            raise ValueError('current publication attempt mismatch')
    for key in ('video_sha256','manifest_sha256','cover_sha256','title_sha256'):
        if not HASH.fullmatch(record.get(key,'')):
            raise ValueError('missing exact asset binding')
    if any(proof.get(k) != record[k] for k in ('video_sha256','manifest_sha256')) or proof.get('thumbnail_sha256') != record['cover_sha256']:
        raise ValueError('publication asset binding mismatch')
    if proof.get('visibility') != 'PUBLIC' or proof.get('verified') is not True or proof.get('thumbnail_verified') is not True:
        raise ValueError('publication was not verified public')
    result = json.loads(attempt['result_json'])
    if result.get('phase') != 'video_id_known' or result.get('video_id') != record['video_id'] or result.get('do_not_reupload') is not True or type(result.get('success')) is not bool:
        raise ValueError('terminal worker result missing')
    started, observed, accepted = (timestamp(v) for v in (attempt['started_at'],proof['verified_at'],proof['accepted_at']))
    if not started <= observed <= accepted:
        raise ValueError('publication predates current attempt')
    manifest_bytes = base64.b64decode(envelope['manifest_b64'], validate=True)
    evidence_bytes = base64.b64decode(envelope['evidence_b64'], validate=True)
    if not evidence_bytes or hashlib.sha256(evidence_bytes).hexdigest() != proof.get('evidence_sha256'):
        raise ValueError('accepted evidence digest mismatch')
    if hashlib.sha256(manifest_bytes).hexdigest() != record['manifest_sha256']:
        raise ValueError('original manifest changed')
    manifest = json.loads(manifest_bytes)
    rows = manifest if isinstance(manifest,list) else manifest.get('candidates',[])
    rows = [row for row in rows if row.get('project_id') == record['project_id']]
    if len(rows) != 1 or rows[0].get('channel_uc') != record['channel_uc'] or str(rows[0].get('profile_id')) != str(record['profile_id']):
        raise ValueError('manifest project or channel mismatch')
    title = re.sub(r'\s+', ' ', unicodedata.normalize('NFC', str(rows[0].get('publication_title','')))).strip().casefold()
    if hashlib.sha256(title.encode()).hexdigest() != record['title_sha256'] or proof.get('publication_title') != rows[0].get('publication_title'):
        raise ValueError('manifest title identity mismatch')
    return {'project_id':record['project_id'],'video_sha256':record['video_sha256'],
            'manifest_sha256':record['manifest_sha256'],'delivered_at':accepted,
            'evidence_sha256':hashlib.sha256(canonical(envelope)).hexdigest()}


def validate(envelope):
    try:return _validate(envelope)
    except (KeyError,TypeError,AttributeError,UnicodeError) as exc:
        raise ValueError('malformed publication evidence') from exc


def accept_envelope(config, envelope):
    """Call only after authenticating the existing puller token. Durable ACK."""
    if not isinstance(envelope,dict):raise ValueError('evidence must be an object')
    root = Path(config['delivery_evidence']); root.mkdir(parents=True, exist_ok=True)
    if envelope.get('kind') == 'collector_status_v1':
        if set(envelope) != {'kind','producer','generated_at','ok','confirmed','pending','source_errors','transport_errors'} or envelope.get('producer') not in ('minsk','mac') or type(envelope.get('ok')) is not bool:
            raise ValueError('invalid collector status')
        if any(type(envelope[k]) is not int or envelope[k] < 0 for k in ('confirmed','pending','source_errors','transport_errors')):
            raise ValueError('invalid collector counts')
        if envelope['ok'] and (envelope['source_errors'] or envelope['transport_errors']):
            raise ValueError('collector health contradicts errors')
        if not time.time()-900 <= float(envelope['generated_at']) <= time.time()+30:
            raise ValueError('stale collector status')
        atomic_json(root/'health'/(envelope['producer']+'.json'),envelope)
        return {'accepted':True,'evidence_sha256':hashlib.sha256(canonical(envelope)).hexdigest()}
    binding = validate(envelope)
    with (root/'.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        target = root/(binding['evidence_sha256']+'.json')
        if target.exists():
            if canonical(json.loads(target.read_bytes())) != canonical(envelope):
                raise ValueError('evidence identity conflict')
        else:
            size = len(canonical(envelope))
            if sum(p.stat().st_size for p in root.glob('*.json')) + size > int(config.get('delivery_evidence_max_bytes',4*1024**3)) or shutil.disk_usage(root).free < size+256*1024**2:
                raise OSError('delivery evidence storage reserve')
            atomic_json(target,envelope)
    return {'accepted':True,'evidence_sha256':binding['evidence_sha256']}


def bind_envelope(config, envelope):
    from storage_service import get_project, regular_files
    binding = validate(envelope)
    project = get_project(config,binding['project_id'])
    destination = Path(config['deliveries'])/(hashlib.sha256((binding['project_id']+binding['video_sha256']).encode()).hexdigest()+'.json')
    if destination.exists():
        existing=json.loads(destination.read_bytes())
        if existing.get('evidence_sha256') == binding['evidence_sha256']:
            verify_bound_record(config,existing)
            return existing
        raise ValueError('delivery binding already exists with other evidence')
    candidates = [p for p in regular_files(project['dir_path']) if p.suffix.lower()=='.mp4' and sha(p)==binding['video_sha256']]
    if len(candidates) != 1:
        raise ValueError('exact local video missing or ambiguous')
    record = dict(binding,version=1,source='trusted_night_public',video_path=str(candidates[0].resolve()))
    atomic_json(destination,record)
    return record


def verify_bound_record(config, record):
    from storage_service import get_project, within
    digest = record.get('evidence_sha256','')
    if not HASH.fullmatch(digest):
        raise ValueError('invalid evidence identity')
    path = within(Path(config['delivery_evidence'])/(digest+'.json'), config['delivery_evidence'])
    binding = validate(json.loads(path.read_bytes()))
    if any(record.get(k) != v for k,v in binding.items()):
        raise ValueError('bound publication evidence changed')
    project = get_project(config,record['project_id'])
    video = within(record['video_path'],project['dir_path'])
    return project,video


def ingest_pending(config):
    bound,errors = 0,{}
    for path in sorted(Path(config['delivery_evidence']).glob('*.json')):
        try:
            envelope=json.loads(path.read_bytes())
            if validate(envelope)['evidence_sha256']+'.json' != path.name:
                raise ValueError('evidence filename digest mismatch')
            bind_envelope(config,envelope);bound+=1
        except Exception as exc:
            code=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
            errors[code]=errors.get(code,0)+1
    producers={}
    for producer in config.get('delivery_evidence_producers',[]):
        try:
            health=json.loads((Path(config['delivery_evidence'])/'health'/(producer+'.json')).read_bytes())
            producers[producer]={'ok':health['ok'] and time.time()-health['generated_at']<=900,
                                 'generated_at':health['generated_at'],'source_errors':health['source_errors'],'transport_errors':health['transport_errors']}
        except Exception:producers[producer]={'ok':False,'reason':'collector heartbeat missing'}
    return {'ok':not errors and all(v['ok'] for v in producers.values()),'bound':bound,'errors':errors,'producers':producers}


def collect(base, producer, acked=()):
    """Read committed ledger snapshot; no importing or invoking upload code."""
    base=Path(base).resolve();manifest=(base/'candidates.json').read_bytes()
    db=sqlite3.connect('file:'+str(base/'upload_ledger.sqlite3')+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    try:
        db.execute('BEGIN')
        records=[]
        for row in db.execute("SELECT * FROM uploads WHERE status='confirmed_public'"):
            attempt=db.execute('SELECT * FROM attempts WHERE attempt_id=?',(row['attempt_id'],)).fetchone()
            records.append((dict(row),dict(attempt) if attempt else None))
        db.rollback()
    finally:db.close()
    envelopes=[];errors={};skipped=0
    for record,attempt in records:
        try:
            if not attempt:raise ValueError('current finished attempt missing')
            proof=json.loads(record.get('confirmed_proof') or '{}')
            project=base/record['project_id']
            evidence=file_inside(proof.get('evidence_path',''),project).read_bytes()
            envelope={'version':1,'kind':'night_public_v1','producer':producer,
                      'upload':{k:record.get(k) for k in UPLOAD_KEYS},
                      'attempt':{k:attempt.get(k) for k in ATTEMPT_KEYS},'proof':proof,
                      'manifest_b64':base64.b64encode(manifest).decode(),
                      'evidence_b64':base64.b64encode(evidence).decode()}
            binding=validate(envelope)
            if binding['evidence_sha256'] in acked:skipped+=1;continue
            media=json.loads((project/'media_verified.json').read_bytes())
            if media.get('verified') is not True or media.get('project_id')!=record['project_id'] or media.get('channel_uc')!=record['channel_uc'] or media.get('sha256')!=binding['video_sha256']:
                raise ValueError('source media identity changed')
            video=file_inside(media['path'],project)
            if video.suffix.lower()!='.mp4' or video.stat().st_size!=media.get('bytes') or sha(video)!=binding['video_sha256']:
                raise ValueError('source video changed')
            envelopes.append(envelope)
        except Exception as exc:
            code=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
            errors[code]=errors.get(code,0)+1
    return envelopes,{'confirmed':len(records),'ready':len(envelopes),'already_acknowledged':skipped,'errors':errors}


def push(config):
    state=Path(config['state']);state.mkdir(parents=True,exist_ok=True)
    with (state/'.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        ack_file=state/'acknowledged.json'
        acked=json.loads(ack_file.read_bytes()) if ack_file.exists() else []
        sent_file=state/'last_sent.json'
        last_sent=json.loads(sent_file.read_bytes()) if sent_file.exists() else {}
        outbox=state/'envelopes';outbox.mkdir(exist_ok=True)
        reports=[];sent=0;transport_errors={};attempted=0;deadline=time.monotonic()+240
        token=os.environ.get('PULLER_TOKEN','')
        if not token and config.get('puller_unit'):
            # Reuse this user's running node credential, never copy it to a
            # new file or command line. Stopped/inaccessible node fails closed.
            unit=config['puller_unit']
            if not re.fullmatch(r'[A-Za-z0-9_.-]+',unit):raise ValueError('invalid puller unit')
            pid=int(subprocess.check_output(['systemctl','show',unit,'--property=MainPID','--value'],timeout=8).strip())
            if pid<=1:raise ValueError('puller is not running')
            proc=Path('/proc')/str(pid)
            if proc.stat().st_uid!=os.getuid():raise ValueError('puller user differs from collector')
            values=dict(item.split(b'=',1) for item in (proc/'environ').read_bytes().split(b'\0') if b'=' in item)
            token=values.get(b'PULLER_TOKEN',b'').decode()
        if not token:raise ValueError('PULLER_TOKEN missing')
        bases=set(config.get('bases',[]))
        for pattern in config.get('base_globs',[]):bases.update(glob.glob(pattern))
        for base in sorted(bases):
            if not (Path(base)/'upload_ledger.sqlite3').is_file():continue
            try:envelopes,report=collect(base,config['producer'],acked)
            except Exception as exc:
                reports.append({'confirmed':0,'ready':0,'already_acknowledged':0,'errors':{type(exc).__name__:1}});continue
            reports.append(report)
            for envelope in envelopes:
                digest=validate(envelope)['evidence_sha256']
                target=outbox/(digest+'.json')
                if not target.exists():
                    if sum(p.stat().st_size for p in outbox.glob('*.json'))+len(canonical(envelope))>int(config.get('outbox_max_bytes',4*1024**3)):
                        transport_errors['producer_outbox_cap']=transport_errors.get('producer_outbox_cap',0)+1;continue
                    atomic_json(target,envelope)
        # Keep the exact proof independently of source MP4 retention. Daily
        # replay also repairs a restored VPS that lost ACKed post-snapshot data.
        pending=0
        for path in sorted(outbox.glob('*.json'),key=lambda p:float(last_sent.get(p.stem,0))):
            if path.stem in acked and time.time()-float(last_sent.get(path.stem,0))<86400:continue
            pending+=1
            if attempted>=20 or time.monotonic()>deadline or sum(transport_errors.values())>=3:continue
            attempted+=1
            try:
                envelope=json.loads(path.read_bytes());digest=validate(envelope)['evidence_sha256']
                if path.stem!=digest:raise ValueError('producer outbox digest changed')
                try:
                    request=urllib.request.Request(config['server'].rstrip('/')+'/api/render/delivery-evidence',data=canonical(envelope),
                        headers={'X-Puller-Token':token,'Content-Type':'application/json'},method='POST')
                    with urllib.request.urlopen(request,timeout=15) as response:
                        receipt=json.loads(response.read(65536))
                    if receipt.get('accepted') is not True or receipt.get('evidence_sha256')!=digest:
                        raise ValueError('delivery ACK mismatch')
                    if digest not in acked:acked.append(digest)
                    atomic_json(ack_file,acked);last_sent[digest]=time.time();atomic_json(sent_file,last_sent);sent+=1
                except Exception as exc:
                    code=type(exc).__name__;transport_errors[code]=transport_errors.get(code,0)+1
            except Exception as exc:
                code=type(exc).__name__;transport_errors[code]=transport_errors.get(code,0)+1
        if not reports:transport_errors['no_configured_ledger']=1
        result={'generated_at':time.time(),'ok':not transport_errors and all(not r['errors'] for r in reports),'sent':sent,'sources':reports,'transport_errors':transport_errors}
        health={'kind':'collector_status_v1','producer':config['producer'],'generated_at':time.time(),'ok':result['ok'],
                'confirmed':sum(r['confirmed'] for r in reports),'pending':pending-sent,
                'source_errors':sum(sum(r['errors'].values()) for r in reports),'transport_errors':sum(transport_errors.values())}
        try:
            request=urllib.request.Request(config['server'].rstrip('/')+'/api/render/delivery-evidence',data=canonical(health),
                headers={'X-Puller-Token':token,'Content-Type':'application/json'},method='POST')
            with urllib.request.urlopen(request,timeout=15) as response:receipt=json.loads(response.read(65536))
            if receipt.get('accepted') is not True or receipt.get('evidence_sha256')!=hashlib.sha256(canonical(health)).hexdigest():raise ValueError('health ACK mismatch')
        except Exception:
            result['ok']=False;result['transport_errors']['health_delivery_failed']=1
        atomic_json(state/'report.json',result);return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True)
    args=parser.parse_args();print(json.dumps(push(json.loads(Path(args.config).read_bytes())),sort_keys=True))
