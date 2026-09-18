"""Conservative disk reservations. Called only inside the queue claim transaction."""
import json
import math
import os
from pathlib import Path
import shutil

GIB=1024**3


def _positive(value, default):
    try:
        value=float(value)
        return value if math.isfinite(value) and value>0 else default
    except (ValueError,TypeError):
        return default


def estimated_bytes(params):
    params=params if isinstance(params,dict) else {}
    scenes=max(_positive(params.get(name),500) for name in ('images_count','scene_count','images_expected'))
    seconds=max(_positive(params.get(name),5400) for name in ('duration_seconds','audio_duration','video_duration_seconds'))
    minutes=_positive(params.get('duration_minutes'),90)
    scale=max(1,scenes/500,seconds/5400,minutes/90)
    return int(_positive(os.environ.get('VS_STORAGE_JOB_GIB'),6)*GIB*scale)


def admission(connection, directory, params):
    """No mutation: caller's BEGIN IMMEDIATE serializes running+batch reservations.

    Count all running rows conservatively, including other kinds/projects. A claim
    made earlier in this transaction is visible here and cannot evade the budget.
    """
    if not connection.in_transaction:
        raise RuntimeError('storage admission requires the claim transaction')
    directory=Path(directory)
    while not directory.exists() and directory!=directory.parent:
        directory=directory.parent
    try:
        free=shutil.disk_usage(directory).free
    except OSError:
        return {'allowed':False,'reason':'disk_unavailable'}
    reserved=0
    for row in connection.execute("SELECT params FROM queue_tasks WHERE status='running'"):
        try:running=json.loads(row[0] or '{}')
        except (ValueError,TypeError):running={}
        reserved+=estimated_bytes(running)
    # Legacy API/raw-worker jobs have no queue row; never count canonical jobs twice.
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gen_jobs'").fetchone():
        for row in connection.execute("SELECT settings FROM gen_jobs g WHERE status='running' AND NOT EXISTS (SELECT 1 FROM queue_tasks q WHERE q.job_id=g.id)"):
            try:running=json.loads(row[0] or '{}')
            except (ValueError,TypeError):running={}
            reserved+=estimated_bytes(running)
    required=estimated_bytes(params)
    floor=int(_positive(os.environ.get('VS_STORAGE_MIN_FREE_GIB'),64)*GIB)
    return {'allowed':free-reserved-required>=floor,'free_bytes':free,
            'reserved_bytes':reserved,'claim_bytes':required,'minimum_free_bytes':floor,
            'reason':'disk_reserve'}


def reserve_legacy(connection, directory, job_id, params):
    """Owns a short transaction; caller retains/cleans up the connection."""
    if connection.in_transaction:
        raise RuntimeError('legacy storage reservation owns its transaction')
    try:
        connection.execute('BEGIN IMMEDIATE')
        row=connection.execute('SELECT status FROM gen_jobs WHERE id=?',(job_id,)).fetchone()
        if not row or row[0]!='queued':
            connection.commit()
            return {'allowed':False,'reason':'job_not_queued'}
        decision=admission(connection,directory,params)
        if decision['allowed']:
            updated=connection.execute("UPDATE gen_jobs SET status='running',error=NULL WHERE id=? AND status='queued'",(job_id,))
            decision['allowed']=updated.rowcount==1
        connection.commit()
        return decision
    except BaseException:
        connection.rollback()
        raise
