"""Bounded 7-day source cleanup under the same transactional admission fence.

Keeps final video, paid journals/refs, scripts, plans and user render media.
Only exact-source files belonging to the latest verified render are eligible.
"""
from contextlib import contextmanager
import hashlib,json,math,os
from pathlib import Path
import sqlite3,stat,tempfile,time,uuid

TERMINAL=('done','error','cancelled','canceled')
MEDIA={'.jpg','.jpeg','.png','.webp','.bmp','.gif','.mp3','.wav','.m4a','.aac','.ogg','.flac','.mp4','.mov','.webm'}
IMAGE={'.jpg','.jpeg','.png','.webp','.bmp','.gif'}

class Retained(ValueError):pass

@contextmanager
def connection(database):
    db=sqlite3.connect(database,timeout=5);db.row_factory=sqlite3.Row
    try:
        with db:yield db
    finally:db.close()

def held(value,now=None):
    try:
        value=json.loads(value) if isinstance(value,str) else value
        expiry=float(value['expires_at'])
        return not value.get('owner') or not math.isfinite(expiry) or expiry>(time.time() if now is None else now)
    except (ValueError,TypeError,KeyError,AttributeError):return True

def sha(path):
    digest=hashlib.sha256()
    with open(path,'rb') as stream:
        for block in iter(lambda:stream.read(1048576),b''):digest.update(block)
    return digest.hexdigest()

def inside(path,root):
    path=Path(path);root=Path(root).resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(root):raise Retained('source path changed')
    return path.resolve()

def atomic_json(path,value):
    path=Path(path);fd,name=tempfile.mkstemp(prefix='.source-retention-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:json.dump(value,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if os.path.exists(name):os.unlink(name)

def legacy_receipt(root):
    """Старый рендер записан без расписки — выводим её из единственного mp4 на диске.

    Расписка нужна ради двух вещей: найти готовый ролик и убедиться, что он не меняется,
    пока мы удаляем исходники. Обе выполняются и без записи в базе, если в каталоге
    render/ лежит РОВНО ОДИН mp4: двусмысленности нет. Несколько файлов или ни одного —
    отказ как раньше. Угадывать нельзя: движок публикаций берёт «самый свежий mp4», и
    ошибка уехала бы на канал.

    Контрольную сумму здесь НЕ считаем: у проекта может не оказаться исходников для
    удаления, и тогда чтение 165 МБ было бы впустую (ровно этого избегает вызывающий).
    Для выведенной расписки защитой служит сверка устройства, inode, размера и времени
    файла до и после — она в purge_project уже есть.
    """
    if root is None:return None
    d=root/'render'
    if d.is_symlink() or not d.is_dir():return None
    files=[p for p in d.iterdir() if p.suffix.lower()=='.mp4' and not p.is_symlink() and p.is_file()]
    if len(files)!=1:return None
    info=files[0].stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size<=0:return None
    return {'output_rel':str(files[0].relative_to(root)),'render_size':info.st_size,
            'render_sha256':None,'legacy_derived':True}


def latest_render(db,project_id,cutoff,root=None):
    rows=[dict(r) for r in db.execute('SELECT * FROM queue_tasks WHERE project_id=?',(project_id,))]
    if any(r.get('status') not in TERMINAL for r in rows):raise Retained('project has active work')
    renders=[r for r in rows if r.get('kind')=='render' and r.get('status')=='done']
    if not renders:raise Retained('no completed render')
    latest=max(renders,key=lambda r:float(r.get('finished') or r.get('created') or 0))
    stamp=float(latest.get('finished') or latest.get('created') or 0)
    if stamp<=0 or stamp>=cutoff:raise Retained('latest render younger than retention')
    # Chapters read existing media and write text metadata only. Every other
    # later stage, including an unknown future kind, conservatively retains input.
    if any(r.get('kind')!='chapters' and r['job_id']!=latest['job_id'] and
           max(float(r.get('created') or 0),float(r.get('finished') or 0))>stamp for r in rows):
        raise Retained('new generation after latest render')
    if not latest.get('render_sha256') or not latest.get('render_size') or not latest.get('output_rel'):
        derived=legacy_receipt(root)
        if not derived:raise Retained('legacy render lacks exact output receipt')
        latest.update(derived)
    latest['source_cutoff']=stamp
    return latest

def remote_settled(root):
    for path in (root/'.remote_jobs').rglob('*.json'):
        if path.is_symlink():raise Retained('remote journal path changed')
        try:value=json.loads(path.read_bytes())
        except (OSError,ValueError):raise Retained('remote journal unavailable')
        if not isinstance(value,dict):raise Retained('remote journal invalid')
        state=value.get('state')
        if path.name=='credential_pin.json':
            if state!='released':raise Retained('remote credential unresolved')
        elif state and state not in ('done','completed','rejected','failed','cancelled','canceled'):
            raise Retained('remote operation unresolved')

def source_files(root,stamp):
    candidates=[]
    googler=root/'googler'
    if googler.is_symlink():raise Retained('source directory changed')
    if googler.is_dir():
        candidates.extend(p for p in googler.iterdir() if p.suffix.lower() in IMAGE)
    for directory in root.iterdir():
        if directory.name not in ('Images','.thumbs','googler video') and not directory.name.startswith('_FINAL_AUDIO_'):continue
        if directory.is_symlink():raise Retained('source directory changed')
        if directory.is_dir():
            for walk,dirs,files in os.walk(directory,followlinks=False):
                if any((Path(walk)/d).is_symlink() for d in dirs):raise Retained('source directory changed')
                candidates.extend(Path(walk)/name for name in files if Path(name).suffix.lower() in MEDIA)
    result=[]
    for path in candidates:
        path=inside(path,root);info=path.stat()
        if not stat.S_ISREG(info.st_mode):raise Retained('source is not regular')
        if info.st_mtime>stamp:raise Retained('source edited after latest render')
        result.append((path,info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns))
    return result

def _unlink_exact(root,entry):
    path,device,inode,size,mtime=entry
    relative=path.relative_to(root);fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for component in relative.parts[:-1]:
            nxt=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=nxt
        info=os.stat(relative.name,dir_fd=fd,follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)!=(device,inode,size,mtime):
            raise Retained('source changed during retention')
        os.unlink(relative.name,dir_fd=fd)
    finally:os.close(fd)

def purge_project(database,projects_root,project_id,cutoff,deadline,lease_seconds=120,batch_size=64):
    key='storage_cleanup:'+str(project_id);owner=uuid.uuid4().hex;acquired=False;freed=0;removed=0
    try:
        with connection(database) as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT v FROM admin_state WHERE k=?',(key,)).fetchone()
            if old and held(old['v']):raise Retained('project has maintenance owner')
            row=db.execute('SELECT dir_path FROM projects WHERE id=?',(project_id,)).fetchone()
            if not row:raise Retained('project missing')
            root=inside(row['dir_path'],projects_root)
            latest=latest_render(db,project_id,cutoff,root)
            db.execute('INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES(?,?,?)',
                       (key,json.dumps({'owner':owner,'expires_at':time.time()+lease_seconds}),time.time()))
        acquired=True
        # An empty source set needs no final hash: nothing can be deleted.
        # This avoids re-reading old MP4s on every maintenance pass.
        entries=source_files(root,latest['source_cutoff'])
        if not entries:return {'state':'no_sources','freed_bytes':0,'removed':0}
        remote_settled(root)
        final=inside(root/latest['output_rel'],root)
        final_info=final.stat()
        if not stat.S_ISREG(final_info.st_mode) or final_info.st_size!=latest['render_size']:
            raise Retained('final video differs from render receipt')
        # У записанной расписки сверяем ещё и сумму. У выведенной её нет по построению:
        # сравнивать файл с суммой, взятой из него же, бессмысленно — там работает
        # сверка dev/inode/size/mtime ниже.
        if not latest.get('legacy_derived') and sha(final)!=latest['render_sha256']:
            raise Retained('final video differs from render receipt')
        after_hash=final.stat()
        if (after_hash.st_dev,after_hash.st_ino,after_hash.st_size,after_hash.st_mtime_ns)!=(final_info.st_dev,final_info.st_ino,final_info.st_size,final_info.st_mtime_ns):
            raise Retained('final video changed during verification')
        for start in range(0,len(entries),batch_size):
            if time.monotonic()>=deadline:raise Retained('retention time budget exhausted')
            with connection(database) as db:
                db.execute('BEGIN IMMEDIATE')
                lease=db.execute('SELECT v FROM admin_state WHERE k=?',(key,)).fetchone()
                try:lease=json.loads(lease['v']) if lease else {}
                except (TypeError,ValueError):lease={}
                expiry=float(lease.get('expires_at',0))
                if lease.get('owner')!=owner or not math.isfinite(expiry) or expiry<=time.time():raise Retained('retention lease expired')
                current=latest_render(db,project_id,cutoff,root)
                if current['job_id']!=latest['job_id']:raise Retained('render receipt changed')
                info=final.stat()
                if (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)!=(final_info.st_dev,final_info.st_ino,final_info.st_size,final_info.st_mtime_ns):
                    raise Retained('final video changed during retention')
                remote_settled(root)
                for entry in entries[start:start+batch_size]:
                    if time.monotonic()>=deadline:raise Retained('retention time budget exhausted')
                    _unlink_exact(root,entry);freed+=entry[3];removed+=1
        # Metadata is a durable audit marker, never the eligibility decision for
        # future renders; a new render can safely produce and later retire input.
        atomic_json(root/'_sources_purged.json',{'purged_at':time.time(),'freed_bytes':freed,
                    'render_job_id':latest['job_id'],'render_sha256':latest['render_sha256'],'removed':removed})
        return {'state':'purged','freed_bytes':freed,'removed':removed}
    except Retained as exc:
        exc.freed_bytes=freed;exc.removed=removed
        raise
    finally:
        if acquired:
            with connection(database) as db:
                db.execute('BEGIN IMMEDIATE')
                old=db.execute('SELECT v FROM admin_state WHERE k=?',(key,)).fetchone()
                try:same=old and json.loads(old['v']).get('owner')==owner
                except (ValueError,TypeError):same=False
                if same:db.execute('DELETE FROM admin_state WHERE k=?',(key,))

def sweep(database,projects_root,retention_days=7,budget_seconds=20):
    started=time.monotonic();deadline=started+max(0,float(budget_seconds));cutoff=time.time()-float(retention_days)*86400
    with connection(database) as db:
        ids=[r[0] for r in db.execute("SELECT project_id FROM queue_tasks WHERE kind='render' AND status='done' GROUP BY project_id HAVING MAX(COALESCE(finished,created))<? ORDER BY project_id",(cutoff,))]
        row=db.execute("SELECT v FROM admin_state WHERE k='source_retention_cursor'").fetchone()
        try:cursor=json.loads(row['v']).get('last_project_id','') if row else ''
        except (ValueError,TypeError,AttributeError):cursor=''
        if not isinstance(cursor,str):cursor=''
    # A cursor changes only visit order; all per-project safety gates still run.
    ids=[pid for pid in ids if pid>cursor]+[pid for pid in ids if pid<=cursor]
    report={'projects':0,'freed_bytes':0,'retained':{}}
    for project_id in ids:
        if time.monotonic()>=deadline:break
        try:
            result=purge_project(database,projects_root,project_id,cutoff,deadline)
            report['projects']+=result['state']=='purged';report['freed_bytes']+=result['freed_bytes']
        except (Retained,OSError,sqlite3.Error,ValueError) as exc:
            report['freed_bytes']+=getattr(exc,'freed_bytes',0)
            reason=str(exc) if isinstance(exc,Retained) else type(exc).__name__
            report['retained'][reason]=report['retained'].get(reason,0)+1
        finally:
            # Advance even after ineligible/failed work so the same expensive
            # first project cannot starve later candidates after a restart.
            with connection(database) as db:
                db.execute("INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES('source_retention_cursor',?,?)",
                           (json.dumps({'last_project_id':project_id}),time.time()))
    report['elapsed_seconds']=time.monotonic()-started
    return report
