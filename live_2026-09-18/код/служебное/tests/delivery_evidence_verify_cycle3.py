"""Actual SQLite/HTTP/fs evidence transport tests, no patched validation gates."""
import base64, concurrent.futures, copy, datetime as dt, hashlib, json, os
from pathlib import Path
import sqlite3, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
ROOT=Path(__file__).parent
sys.path.insert(0,str(ROOT/'ops_candidate'))
import delivery_evidence as evidence
import storage_service as storage
checks=[]
def rejected(fn):
    try:fn()
    except (ValueError,OSError):return
    raise AssertionError('unsafe evidence accepted')
def iso(seconds):return dt.datetime.fromtimestamp(seconds,dt.timezone.utc).isoformat()

with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);base=root/'source';base.mkdir();tenants=root/'tenants';tenants.mkdir()
    config={'database':str(root/'server.db'),'projects_root':str(tenants),'delivery_evidence':str(root/'inbox'),
            'deliveries':str(root/'bindings'),'delivery_evidence_producers':['minsk']}
    rows=[]
    for i in range(3):
        rows.append({'project_id':'p_'+f'{i:010x}','channel_uc':'UC'+str(i)*22,'profile_id':str(i+1),'publication_title':'Title '+str(i)})
    raw=json.dumps(rows).encode();(base/'candidates.json').write_bytes(raw);manifest_sha=hashlib.sha256(raw).hexdigest()
    db=sqlite3.connect(base/'upload_ledger.sqlite3')
    db.execute('CREATE TABLE uploads('+','.join(k+' TEXT' for k in evidence.UPLOAD_KEYS)+',confirmed_proof TEXT)')
    db.execute('CREATE TABLE attempts('+','.join(k+' TEXT' for k in evidence.ATTEMPT_KEYS)+')')
    serverdb=sqlite3.connect(config['database']);serverdb.execute('CREATE TABLE projects(id TEXT,dir_path TEXT)')
    envelopes=[]
    for i,row in enumerate(rows):
        project=base/row['project_id'];project.mkdir();serverproject=tenants/row['project_id'];serverproject.mkdir()
        video=project/'final.mp4';video.write_bytes(b'exact accepted uploaded bytes'+bytes([i]))
        (serverproject/'final.mp4').write_bytes(video.read_bytes());digest=evidence.sha(video)
        serverdb.execute('INSERT INTO projects VALUES (?,?)',(row['project_id'],str(serverproject)))
        media={'verified':True,'project_id':row['project_id'],'channel_uc':row['channel_uc'],'path':str(video),'bytes':video.stat().st_size,'sha256':digest}
        (project/'media_verified.json').write_text(json.dumps(media))
        proof_file=project/'live-proof.json';proof_file.write_text(json.dumps({'exact_id':str(i)*11,'visibility':'PUBLIC'}))
        record={**row,'manifest_sha256':manifest_sha,'title_sha256':hashlib.sha256(row['publication_title'].casefold().encode()).hexdigest(),
                'video_sha256':digest,'cover_sha256':'a'*64,'phase':'video_id_known','video_id':str(i)*11,'status':'confirmed_public','attempt_id':f'attempt-{i}'}
        proof={k:record[k] for k in ('project_id','channel_uc','video_id','manifest_sha256','video_sha256','attempt_id','publication_title')}
        proof.update(visibility='PUBLIC',verified=True,thumbnail_verified=True,thumbnail_sha256=record['cover_sha256'],
                     verified_at=iso(time.time()-100),accepted_at=iso(time.time()-90),evidence_path=str(proof_file),evidence_sha256=evidence.sha(proof_file))
        attempt={k:record[k] for k in ('attempt_id','channel_uc','project_id','phase','video_id')}
        attempt.update(started_at=iso(time.time()-200),status='needs_readback',result_json=json.dumps({'phase':'video_id_known','video_id':record['video_id'],'do_not_reupload':True,'success':True}))
        if i==2:proof.pop('video_sha256') # Historical confirmed but incomplete, fail closed.
        db.execute('INSERT INTO uploads VALUES ('+','.join('?' for _ in range(len(evidence.UPLOAD_KEYS)+1))+')',tuple(record[k] for k in evidence.UPLOAD_KEYS)+(json.dumps(proof),))
        db.execute('INSERT INTO attempts VALUES ('+','.join('?' for _ in evidence.ATTEMPT_KEYS)+')',tuple(attempt[k] for k in evidence.ATTEMPT_KEYS))
    db.commit();db.close();serverdb.commit();serverdb.close()
    envelopes,summary=evidence.collect(base,'minsk');assert len(envelopes)==2 and sum(summary['errors'].values())==1
    checks.append('actual_readonly_ledger_accepts_exact_rejects_legacy_incomplete')
    original=envelopes[0]
    for mutate in [lambda x:x['proof'].update(video_sha256='b'*64),lambda x:x['attempt'].update(attempt_id='other'),
                   lambda x:x['proof'].update(visibility='PRIVATE'),lambda x:x['upload'].update(status='needs_readback'),
                   lambda x:x['proof'].update(accepted_at=iso(time.time()+100)),lambda x:x.update(manifest_b64=base64.b64encode(b'[]').decode()),
                   lambda x:x.update(evidence_b64=base64.b64encode(b'changed').decode())]:
        changed=copy.deepcopy(original);mutate(changed);rejected(lambda:evidence.accept_envelope(config,changed))
    for malformed in [{}, {'kind':'night_public_v1','version':1}, dict(original,proof=None)]:rejected(lambda:evidence.validate(malformed))
    checks.append('seven_identity_timestamp_payload_attacks_and_malformed_rejected')
    rejected(lambda:evidence.accept_envelope(dict(config,delivery_evidence_max_bytes=1),original))
    assert not list((root/'inbox').glob('*.json'));checks.append('inbox_cap_refuses_without_source_loss')
    class Handler(BaseHTTPRequestHandler):
        requests=0;lose_once=True;bad_ack=False
        def log_message(self,*a):pass
        def do_POST(self):
            assert self.headers['X-Puller-Token']=='test-token'
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            ack=evidence.accept_envelope(config,payload)
            Handler.requests+=1
            if Handler.lose_once and payload['kind']=='night_public_v1':
                Handler.lose_once=False;self.close_connection=True;return
            if Handler.bad_ack and payload['kind']=='night_public_v1':ack['evidence_sha256']='0'*64
            data=json.dumps(ack).encode();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    os.environ['PULLER_TOKEN']='test-token'
    source={'state':str(root/'sender'),'bases':[str(base)],'producer':'minsk','server':f'http://127.0.0.1:{server.server_port}'}
    capped=evidence.push(dict(source,state=str(root/'capped'),outbox_max_bytes=1))
    assert capped['sent']==0 and capped['transport_errors'].get('producer_outbox_cap')==2 and len(list(base.glob('*/final.mp4')))==3
    assert not (root/'capped'/'acknowledged.json').exists() and not list((root/'capped'/'envelopes').glob('*.json'))
    checks.append('producer_cap_preserves_unacknowledged_sources_and_reports_pending')
    first=evidence.push(source);assert first['sent']==1 and first['transport_errors'];assert len(list((root/'inbox').glob('*.json')))==2
    second=evidence.push(source);assert second['sent']==1 and not second['transport_errors'];assert len(list((root/'inbox').glob('*.json')))==2
    third=evidence.push(source);assert third['sent']==0 and third['sources'][0]['already_acknowledged']==2
    checks.append('real_http_lost_ack_replay_is_durable_idempotent_then_skips_media_hash')
    assert evidence.ingest_pending(config)['bound']==2
    bound_before={p.name:p.read_bytes() for p in (root/'bindings').glob('*.json')}
    for p in (root/'inbox').glob('*.json'):p.unlink() # Simulate restoring a pre-ACK VPS snapshot.
    for p in (root/'bindings').glob('*.json'):p.unlink()
    evidence.atomic_json(root/'sender'/'last_sent.json',{p.stem:0 for p in (root/'sender'/'envelopes').glob('*.json')})
    for p in base.glob('*/final.mp4'):p.unlink() # Originals may already have expired.
    source_config=root/'source-config.json';source_config.write_text(json.dumps(source))
    child=subprocess.run([sys.executable,str(ROOT/'ops_candidate'/'delivery_evidence.py'),'--config',str(source_config)],capture_output=True,text=True,timeout=30,check=True)
    recovery=json.loads(child.stdout);assert recovery['sent']==2 and len(list((root/'inbox').glob('*.json')))==2
    assert evidence.ingest_pending(config)['bound']==2
    assert bound_before=={p.name:p.read_bytes() for p in (root/'bindings').glob('*.json')}
    checks.append('receiver_snapshot_loss_recovers_acked_payload_without_original_mp4')
    def submit(_):
        req=evidence.urllib.request.Request(source['server']+'/api/render/delivery-evidence',data=evidence.canonical(original),headers={'X-Puller-Token':'test-token'})
        with evidence.urllib.request.urlopen(req,timeout=15) as reply:return json.loads(reply.read())['accepted']
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:assert all(pool.map(submit,range(100)))
    assert len(list((root/'inbox').glob('*.json')))==2;checks.append('100_real_concurrent_http_duplicate_deliveries_no_overwrite')
    report=evidence.ingest_pending(config);assert report['bound']==2 and not report['ok'] # Incomplete historical source remains visible.
    record=json.loads(next((root/'bindings').glob('*.json')).read_bytes());storage.verified_delivery(config,record)
    changed=dict(record,delivered_at=record['delivered_at']-86400);rejected(lambda:storage.verified_delivery(config,changed))
    checks.append('actual_vps_binding_exact_media_sha_and_immutable_retention_date')
    (Path(record['video_path'])).unlink();evidence.ingest_pending(config) # Proof survives authorized later local expiry.
    storage.verified_delivery(config,record);checks.append('registered_proof_survives_source_media_expiry')
    health=root/'inbox'/'health'/'minsk.json';value=json.loads(health.read_bytes());value['generated_at']-=3600;evidence.atomic_json(health,value)
    assert not evidence.ingest_pending(config)['producers']['minsk']['ok'];checks.append('collector_stale_and_source_error_visible_in_storage_health')
    Handler.bad_ack=True
    import shutil
    shutil.copytree(root/'sender'/'envelopes',root/'bad-ack'/'envelopes')
    saved_before={p.name:p.read_bytes() for p in (root/'bad-ack'/'envelopes').glob('*.json')}
    fourth=evidence.push(dict(source,state=str(root/'bad-ack')));assert fourth['sent']==0 and fourth['transport_errors'] and not (root/'bad-ack'/'acknowledged.json').exists()
    assert not (root/'bad-ack'/'last_sent.json').exists() and saved_before=={p.name:p.read_bytes() for p in (root/'bad-ack'/'envelopes').glob('*.json')}
    checks.append('wrong_ack_never_marks_local_delivery')
    server.shutdown();server.server_close()
result={'ok':True,'count':len(checks),'passed':checks,'sha256':{n:hashlib.sha256((ROOT/'ops_candidate'/n).read_bytes()).hexdigest() for n in ('delivery_evidence.py','storage_service.py')}}
print(json.dumps(result,sort_keys=True))
