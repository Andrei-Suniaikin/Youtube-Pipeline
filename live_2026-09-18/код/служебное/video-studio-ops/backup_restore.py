#!/usr/bin/env python3
"""Restore into a NEW offline root, then quarantine unfinished work before startup.

Metadata restore is not media restore. Never point running services at a raw DB
extracted manually from these archives; use this command and reconcile the holds.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import time
from backup_service import atomic_json, sha, verify_archive

TERMINAL = {'done', 'completed', 'error', 'failed', 'cancelled', 'canceled'}
MESSAGE = 'RESTORE_HOLD: reconcile retained remote IDs and missing media before retry'


def mapped(root, source):
    source = Path(source)
    if not source.is_absolute() or '..' in source.parts:
        raise ValueError('invalid archive destination')
    destination = root / str(source).lstrip('/')
    if not destination.resolve().is_relative_to(root.resolve()):
        raise ValueError('archive destination escaped restore root')
    return destination


def quarantine_database(path, restore_root):
    counts = {'held_jobs': 0, 'held_projects': 0}
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'queue_tasks' in tables:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute("SELECT v FROM admin_state WHERE k='queue_pause'").fetchone()
            pause = json.loads(existing[0]) if existing else {}
            pause.update(all=True, restore_hold=True)
            db.execute('INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES(?,?,?)', ('queue_pause', json.dumps(pause), time.time()))
            rows = db.execute('SELECT job_id,project_id,status FROM queue_tasks').fetchall()
            for row in rows:
                if row['status'] in TERMINAL:
                    continue
                db.execute("UPDATE queue_tasks SET status='error',error=?,claim_token='' WHERE job_id=?", (MESSAGE, row['job_id']))
                db.execute("UPDATE gen_jobs SET status='error',error=? WHERE id=?", (MESSAGE, row['job_id']))
                project = db.execute('SELECT dir_path FROM projects WHERE id=?', (row['project_id'],)).fetchone()
                if project:
                    journal = mapped(restore_root, project[0]) / '.remote_jobs' / str(row['job_id'])
                    journal.mkdir(parents=True, exist_ok=True)
                    pin = journal / 'credential_pin.json'
                    if pin.exists():
                        backup = journal / '.restore_original' / 'credential_pin.json'
                        backup.parent.mkdir(exist_ok=True)
                        if backup.exists():
                            raise ValueError('preexisting restore identity')
                        shutil.copyfile(pin, backup)
                    atomic_json(pin, {'family': 'restore_hold', 'state': 'uncertain', 'sha256': '0'*64, 'source': 'byok', 'reason': MESSAGE})
                    atomic_json(journal / 'restore_hold.json', {'queue_job_id': row['job_id'], 'reason': MESSAGE})
                    counts['held_projects'] += 1
                counts['held_jobs'] += 1
            # Legacy jobs have no queue row, but must not be auto-resumed either.
            for row in db.execute('SELECT id,status FROM gen_jobs').fetchall():
                if row['status'] not in TERMINAL:
                    db.execute("UPDATE gen_jobs SET status='error',error=? WHERE id=?", (MESSAGE, row['id']))
            db.commit()
        elif 'jobs' in tables:
            columns = {r[1] for r in db.execute('PRAGMA table_info(jobs)')}
            if not {'id','data','status'}.issubset(columns):
                return counts
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT id,data,status FROM jobs').fetchall():
                if row['status'] in TERMINAL:
                    continue
                data = json.loads(row['data'])
                data.update(status='failed', error=MESSAGE, restore_hold=True, run_token='', cancel_requested=False)
                assignments = "status='failed',data=?"
                if 'run_token' in columns: assignments += ",run_token=''"
                if 'cancel_requested' in columns: assignments += ',cancel_requested=0'
                db.execute('UPDATE jobs SET '+assignments+' WHERE id=?', (json.dumps(data, ensure_ascii=False), row['id']))
                counts['held_jobs'] += 1
            db.commit()
    return counts


def restore(run_directory, destination):
    os.umask(0o077)
    run_directory = Path(run_directory)
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    report = json.loads((run_directory/'run.json').read_text())
    if not report.get('ok') or not report.get('components'):
        raise ValueError('complete verified backup run required')
    databases = []
    restored = []
    for component, value in report['components'].items():
        if not value.get('ok') or not value.get('restored_from_offsite') or Path(value['archive']).name != value['archive']:
            raise ValueError('component not verified')
        archive = run_directory / value['archive']
        if sha(archive) != value['sha256']:
            raise ValueError('archive checksum mismatch')
        verify_archive(archive)
        with tarfile.open(archive, 'r:gz') as tar:
            manifest = json.load(tar.extractfile('manifest.json'))
            for entry in manifest['entries']:
                target = mapped(destination, entry['source'])
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as out, tar.extractfile(entry['name']) as source:
                    shutil.copyfileobj(source, out, 1024*1024)
                    out.flush(); os.fsync(out.fileno())
                if sha(target) != entry['sha256']:
                    raise ValueError('restored entry checksum mismatch')
                if entry['kind'] == 'sqlite': databases.append(target)
                restored.append({'component': component, 'source': entry['source'], 'sha256': entry['sha256']})
    holds = [quarantine_database(path, destination) for path in databases]
    result = {'generated_at': time.time(), 'ok': True, 'restore_mode': 'offline_hold',
              'media_restored': False, 'entries': len(restored), 'databases': len(databases),
              'held_jobs': sum(h['held_jobs'] for h in holds), 'source_run': report['run_id'],
              'instructions': 'Do not clear holds until original IDs, credentials and media are reconciled. No automatic new provider requests are authorized by restore.'}
    atomic_json(destination/'RESTORE_REPORT.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-directory', required=True)
    parser.add_argument('--destination', required=True, help='new offline directory; never a live filesystem root')
    args = parser.parse_args()
    print(json.dumps(restore(args.run_directory, args.destination)))
