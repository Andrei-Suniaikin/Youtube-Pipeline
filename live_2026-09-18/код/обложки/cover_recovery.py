"""Durable generation identities and fenced image publication; no vendor secrets."""
import hashlib,json,os,sqlite3,threading,time,uuid,io,base64
from contextlib import contextmanager
from pathlib import Path
from cover_credentials import redact_credentials

_local=threading.local()

class RecoveryRequired(RuntimeError):
    pass


def current_call():
    return getattr(_local,'call',None)


def current_journal():
    return getattr(_local,'journal',None)


@contextmanager
def item_context(journal,item):
    previous=(getattr(_local,'journal',None),getattr(_local,'item',None),getattr(_local,'ordinal',0),getattr(_local,'call',None))
    _local.journal=journal;_local.item=item;_local.ordinal=0;_local.call=None
    try:yield
    finally:_local.journal,_local.item,_local.ordinal,_local.call=previous


class GenerationJournal:
    def __init__(self,db_path,job_id,run_token,output_dir):
        self.db_path=str(db_path);self.job_id=job_id;self.run_token=run_token;self.output_dir=Path(output_dir)
        with self.connect() as con:
            con.execute('CREATE TABLE IF NOT EXISTS cover_remote (job_id TEXT,item TEXT,ordinal INTEGER,family TEXT,credential_hash TEXT,remote_id TEXT DEFAULT "",state TEXT,cache TEXT DEFAULT "",error TEXT DEFAULT "",updated_at REAL,PRIMARY KEY(job_id,item,ordinal))')
            columns={r[1] for r in con.execute('PRAGMA table_info(cover_remote)')}
            for name in ('request_hash','request_seed','request_json','source_hash'):
                if name not in columns:con.execute('ALTER TABLE cover_remote ADD COLUMN '+name+' TEXT NOT NULL DEFAULT ""')
            con.execute('CREATE TABLE IF NOT EXISTS cover_stages(job_id TEXT,item TEXT,stage TEXT,source_hash TEXT,value_json TEXT,PRIMARY KEY(job_id,item,stage))')
            con.execute('CREATE TABLE IF NOT EXISTS cover_artifacts (job_id TEXT,path TEXT,item TEXT,sha256 TEXT,fallback INTEGER,PRIMARY KEY(job_id,path))')

    @contextmanager
    def connect(self):
        con=sqlite3.connect(self.db_path,timeout=30)
        try:
            with con:yield con
        finally:con.close()

    def check_owner(self,con):
        row=con.execute('SELECT status,run_token,cancel_requested FROM jobs WHERE id=?',(self.job_id,)).fetchone()
        if row is None or row[0]!='running' or row[1]!=self.run_token or row[2]:
            raise RecoveryRequired('Задача отменена или её выполнение уже передано другому worker')

    def source_hash(self,con):
        row=con.execute('SELECT data FROM jobs WHERE id=?',(self.job_id,)).fetchone()
        data=json.loads(row[0]) if row else {}
        fields=('urls','preset_data','mode','titles_per_lang','extra_prompt','seeds_per_lang','n_variants','languages','refs_per_language','generation_provider')
        source={k:data.get(k) for k in fields}
        return hashlib.sha256(json.dumps(source,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

    def checkpoint(self,item,stage,calculate):
        with self.connect() as con:
            self.check_owner(con);source=self.source_hash(con)
            row=con.execute('SELECT source_hash,value_json FROM cover_stages WHERE job_id=? AND item=? AND stage=?',(self.job_id,item,stage)).fetchone()
            if row:
                if row[0]!=source:raise RecoveryRequired('Исходные параметры задачи изменились; сохранённый этап требует сверки')
                return _decode_snapshot(json.loads(row[1]))
        value=calculate()
        if stage=='source_analysis' and (value[0] is None or value[1] is None):return value
        encoded=json.dumps(_encode_snapshot(value),ensure_ascii=False)
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE');self.check_owner(con)
            if source!=self.source_hash(con):raise RecoveryRequired('Исходные параметры изменились во время этапа')
            con.execute('INSERT INTO cover_stages VALUES(?,?,?,?,?)',(self.job_id,item,stage,source,encoded))
        return value

    def begin_call(self,family,api_key,request_hash="",request_payload=None):
        _local.ordinal+=1;item=str(_local.item);ordinal=_local.ordinal
        kid=hashlib.sha256(api_key.encode()).hexdigest()
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE');self.check_owner(con)
            con.row_factory=sqlite3.Row
            row=con.execute('SELECT * FROM cover_remote WHERE job_id=? AND item=? AND ordinal=?',(self.job_id,item,ordinal)).fetchone()
            if row:
                result=dict(row)
                if result.get('source_hash') and result['source_hash']!=self.source_hash(con):
                    raise RecoveryRequired('Исходные параметры задачи изменились; сохранённая генерация требует сверки')
                if result.get('request_hash') and result['request_hash'] != request_hash:
                    raise RecoveryRequired('Исходный запрос генерации изменился; сохранённый ID/результат требует сверки')
                if result['state']=='ready' and result['cache']:
                    _local.call=result;return result
                if result['state'] in ('submitting','uncertain','failed') and not result['remote_id']:
                    raise RecoveryRequired('Генерация требует сверки с провайдером; повторный платный запрос запрещён ('+result['state']+')')
                if result['family']!=family or result['credential_hash']!=kid:
                    raise RecoveryRequired('Для продолжения generation ID нужен исходный личный ключ; другой ключ не используется')
            else:
                con.execute('INSERT INTO cover_remote(job_id,item,ordinal,family,credential_hash,state,updated_at,request_hash,request_json,source_hash) VALUES(?,?,?,?,?,?,?,?,?,?)',(self.job_id,item,ordinal,family,kid,'submitting',time.time(),request_hash,json.dumps(request_payload or {},ensure_ascii=False),self.source_hash(con)))
                result={'job_id':self.job_id,'item':item,'ordinal':ordinal,'family':family,'credential_hash':kid,'remote_id':'','state':'submitting','cache':'','request_hash':request_hash,'request_seed':''}
        _local.call=result
        return result

    def update_call(self,**changes):
        call=current_call()
        if not call:return
        allowed={'remote_id','state','cache','error','request_seed'}
        assert set(changes)<=allowed
        if 'error' in changes:changes['error']=redact_credentials(changes['error'])[:500]
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row=con.execute('SELECT remote_id,state,cache FROM cover_remote WHERE job_id=? AND item=? AND ordinal=?',(self.job_id,call['item'],call['ordinal'])).fetchone()
            if not row:raise RecoveryRequired('Сохранённая генерация отсутствует')
            known_id,state,cache=row
            if known_id and changes.get('remote_id') and changes['remote_id']!=known_id:
                raise RecoveryRequired('Запрещена замена сохранённого generation ID')
            try:self.check_owner(con);owner=True
            except RecoveryRequired:owner=False
            # A stale submit response may add its previously unknown ID. It may
            # never replace another ID, overwrite diagnostics, or downgrade ready.
            if not owner:
                changes={'remote_id':changes['remote_id']} if changes.get('remote_id') and not known_id else {}
                if changes and state!='ready':changes['state']='submitted'
            if state=='ready':
                changes={k:v for k,v in changes.items() if k=='remote_id' and not known_id}
            if changes:
                con.execute('UPDATE cover_remote SET '+','.join(k+'=?' for k in changes)+',updated_at=? WHERE job_id=? AND item=? AND ordinal=?',(*changes.values(),time.time(),self.job_id,call['item'],call['ordinal']))
            call.update(remote_id=changes.get('remote_id',known_id),state=changes.get('state',state),cache=cache)
            call.update(changes)

    def cache_image(self,image):
        call=current_call();folder=self.output_dir/'.recovery';folder.mkdir(parents=True,exist_ok=True)
        rel='.recovery/'+hashlib.sha256((call['item']+':'+str(call['ordinal'])).encode()).hexdigest()+'.png'
        target=self.output_dir/rel;tmp=target.with_name(target.name+'.'+uuid.uuid4().hex+'.tmp')
        image.save(tmp,format='PNG')
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE');self.check_owner(con);os.replace(tmp,target)
            con.execute('UPDATE cover_remote SET state="ready",cache=?,updated_at=? WHERE job_id=? AND item=? AND ordinal=?',(rel,time.time(),self.job_id,call['item'],call['ordinal']))
        call.update(state='ready',cache=rel)

    def publish(self,image,target,fallback=False,item=None):
        target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
        assert target.resolve().is_relative_to(self.output_dir.resolve())
        tmp=target.with_name(target.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            image.save(tmp,format='JPEG',quality=92)
            digest=hashlib.sha256(tmp.read_bytes()).hexdigest()
            with self.connect() as con:
                con.execute('BEGIN IMMEDIATE');self.check_owner(con);os.replace(tmp,target)
                rel=str(target.relative_to(self.output_dir))
                con.execute('INSERT OR REPLACE INTO cover_artifacts VALUES(?,?,?,?,?)',(self.job_id,rel,str(item if item is not None else getattr(_local,'item','')),digest,int(fallback)))
        finally:
            if tmp.exists():tmp.unlink()

    def credential_pins(self):
        with self.connect() as con:
            return dict(con.execute('SELECT DISTINCT family,credential_hash FROM cover_remote WHERE job_id=? AND remote_id!="" AND state!="ready"',(self.job_id,)))

    def archive_items(self,paths):
        """Explicit fallback regeneration preserves IDs under an archive item name."""
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            for path in paths:
                row=con.execute('SELECT item FROM cover_artifacts WHERE job_id=? AND path=?',(self.job_id,path)).fetchone()
                if not row or not row[0]:continue
                item=row[0]
                if con.execute('SELECT 1 FROM cover_remote WHERE job_id=? AND item=? AND state!="ready"',(self.job_id,item)).fetchone():
                    raise RecoveryRequired('Сначала сверить незавершённую генерацию; fallback сохранён')
                archived='archived:'+uuid.uuid4().hex+':'+item
                con.execute('UPDATE cover_remote SET item=? WHERE job_id=? AND item=?',(archived,self.job_id,item))
                con.execute('UPDATE cover_stages SET item=? WHERE job_id=? AND item=?',(archived,self.job_id,item))

    def manifest(self):
        with self.connect() as con:
            rows=con.execute('SELECT item,ordinal,family,remote_id,state,error FROM cover_remote WHERE job_id=? ORDER BY item,ordinal',(self.job_id,)).fetchall()
            artifacts=con.execute('SELECT path,sha256,fallback FROM cover_artifacts WHERE job_id=?',(self.job_id,)).fetchall()
        return {'items':[dict(zip(('item','call','provider','remote_id','state','error'),r)) for r in rows], 'artifacts':[dict(zip(('path','sha256','fallback'),r)) for r in artifacts]}


def valid_image(path):
    try:
        from PIL import Image
        with Image.open(path) as image:image.verify()
        return True
    except (OSError,ValueError):return False


def save_image(image,path,journal=None,fallback=False,item=None):
    if journal:journal.publish(image,path,fallback,item=item)
    else:
        path=Path(path);tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
        try:image.save(tmp,format='JPEG',quality=92);os.replace(tmp,path)
        finally:
            if tmp.exists():tmp.unlink()


def recovery_item(journal,key_fn):
    def decorate(fn):
        def wrapped(args):
            with item_context(journal,key_fn(args)):
                return fn(args)
        return wrapped
    return decorate


def _encode_snapshot(value):
    from PIL import Image
    if isinstance(value,Image.Image):
        buf=io.BytesIO();value.save(buf,format='PNG')
        return {'__cover_image_png__':base64.b64encode(buf.getvalue()).decode()}
    if isinstance(value,tuple):return {'__cover_tuple__':[_encode_snapshot(x) for x in value]}
    if isinstance(value,list):return [_encode_snapshot(x) for x in value]
    if isinstance(value,dict):return {k:_encode_snapshot(v) for k,v in value.items()}
    return value


def _decode_snapshot(value):
    if isinstance(value,dict):
        if '__cover_image_png__' in value:
            from PIL import Image
            with Image.open(io.BytesIO(base64.b64decode(value['__cover_image_png__']))) as im:return im.convert('RGB')
        if '__cover_tuple__' in value:return tuple(_decode_snapshot(x) for x in value['__cover_tuple__'])
        return {k:_decode_snapshot(v) for k,v in value.items()}
    if isinstance(value,list):return [_decode_snapshot(x) for x in value]
    return value


def checkpoint(stage,calculate,journal=None,item=None):
    journal=journal or current_journal()
    if journal is None:return calculate()
    return journal.checkpoint(str(item if item is not None else getattr(_local,'item','global')),stage,calculate)
