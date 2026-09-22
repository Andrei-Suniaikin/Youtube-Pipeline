"""Durable VoiceGenCore request identity. No raw key or source text is persisted."""
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path


class JournalError(RuntimeError):
    pass


class CredentialRequired(JournalError):
    def __init__(self, fingerprint):
        self.fingerprint = fingerprint
        super().__init__('VoiceGenCore: требуется исходный ключ задачи (' + fingerprint[:12] + '); автоматической замены ключа нет')


def fingerprint(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def root_for_project(project):
    project = Path(project).resolve()
    configured = os.environ.get('VS_REMOTE_JOURNAL_DIR')
    job_id = os.environ.get('VS_QUEUE_JOB_ID', '')
    if configured or job_id:
        if not configured or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', job_id):
            raise JournalError('VoiceGenCore: неверная конфигурация VS_REMOTE_JOURNAL_DIR / VS_QUEUE_JOB_ID')
        expected = project / '.remote_jobs' / job_id
        if Path(configured).resolve() != expected.resolve():
            raise JournalError('VoiceGenCore: журнал задачи находится вне её проекта')
        return expected
    return project / '.remote_jobs' / 'voicegencore-standalone'


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    fd = os.open(str(temporary), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fd = os.open(str(path.parent), os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise JournalError('VoiceGenCore: этот выходной файл уже обрабатывается другим процессом') from exc
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read_json(path):
    try:
        with open(path, encoding='utf-8') as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError('object required')
        return value
    except (OSError, ValueError) as exc:
        raise JournalError('VoiceGenCore: журнал недоступен или повреждён; новый POST остановлен') from exc


def validate_pin(project, token):
    root = root_for_project(project)
    path = root / 'credential_pin.json'
    if path.exists():
        pin = read_json(path)
        if pin.get('family') != 'voicegencore' or pin.get('state') not in {'active', 'uncertain', 'released'}:
            raise JournalError('VoiceGenCore: неверная запись credential_pin')
        if pin.get('state') != 'released' and (pin.get('sha256') != fingerprint(token) or pin.get('source', 'byok') != os.environ.get('VS_CREDENTIAL_SOURCE', 'byok')):
            raise CredentialRequired(str(pin.get('sha256') or 'unknown'))
    return root


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


class VGCJournal:
    TERMINAL = {'completed', 'failed', 'rejected', 'cancelled'}

    @classmethod
    def from_saved(cls, project, path, token):
        """Open an accepted request for cancel/reconciliation without source text."""
        root = validate_pin(project, token)
        saved = read_json(path)
        identity = saved.get('identity')
        if not isinstance(identity, dict):
            raise JournalError('VoiceGenCore: идентичность сохранённого запроса повреждена')
        request_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        if Path(path).resolve() != (root / ('request-' + request_id + '.json')).resolve():
            raise JournalError('VoiceGenCore: неверный путь сохранённого запроса')
        obj = cls.__new__(cls)
        obj.project = Path(project).resolve()
        obj.root = root
        obj.output = (obj.project / str(identity.get('output', ''))).resolve()
        if obj.output.parent != obj.project / '_FINAL_AUDIO_VGC':
            raise JournalError('VoiceGenCore: сохранённый output находится вне audio-каталога проекта')
        obj.output_dir = obj.output.parent
        obj.identity, obj.request_id, obj.path = identity, request_id, Path(path)
        obj.key_hash = fingerprint(token)
        obj.source = os.environ.get('VS_CREDENTIAL_SOURCE', 'byok')
        obj.record = None
        return obj
    def __init__(self, output_dir, filename, token, text, voice_id, voice_engine, stability, speed):
        self.output_dir = Path(output_dir).resolve()
        if Path(filename).name != filename or not filename or filename in {'.', '..'}:
            raise JournalError('VoiceGenCore: неверное имя выходного файла')
        self.output = self.output_dir / filename
        self.project = self.output_dir.parent
        self.root = validate_pin(self.project, token)
        self.key_hash = fingerprint(token)
        self.source = os.environ.get('VS_CREDENTIAL_SOURCE', 'byok')
        if self.source not in {'byok', 'server'}:
            raise JournalError('VoiceGenCore: неверный источник credentials')
        self.identity = {'text_sha256': hashlib.sha256(text.encode()).hexdigest(), 'voice_id': str(voice_id),
                         'voice_engine': str(voice_engine), 'stability': float(stability), 'speed': float(speed),
                         'output': str(self.output.relative_to(self.project))}
        if (not math.isfinite(self.identity['speed']) or self.identity['speed'] <= 0
                or not math.isfinite(self.identity['stability'])):
            raise JournalError('VoiceGenCore: неверные скорость или стабильность; запрос не отправлен')
        self.request_id = hashlib.sha256(json.dumps(self.identity, sort_keys=True).encode()).hexdigest()
        self.path = self.root / ('request-' + self.request_id + '.json')
        self.record = None

    def lock(self):
        output_identity = hashlib.sha256(str(self.output).encode()).hexdigest()
        return file_lock(self.project / '.remote_jobs' / 'output_locks' / (output_identity + '.lock'))

    def load(self):
        for path in self.root.glob('request-*.json'):
            other = read_json(path)
            if (path != self.path and other.get('identity', {}).get('output') == self.identity['output']
                    and other.get('state') != 'rejected'):
                raise JournalError('VoiceGenCore: текст или параметры этой задачи изменились; исходный remote ID сохранён, новая генерация остановлена')
        if self.path.exists():
            self.record = read_json(self.path)
            if self.record.get('identity') != self.identity or self.record.get('request_id') != self.request_id:
                raise JournalError('VoiceGenCore: идентичность запроса в журнале не совпала')
            state = self.record.get('state')
            if (state not in self.TERMINAL | {'intent', 'uncertain', 'active', 'pending', 'ready', 'cancel_requested'}
                    or not re.fullmatch(r'[0-9a-f]{64}', str(self.record.get('credential_sha256', '')))
                    or (state not in {'intent', 'uncertain', 'rejected'} and not self.record.get('task_id'))):
                raise JournalError('VoiceGenCore: запись remote task повреждена; новый POST остановлен')
            late_cancel_completed = state == 'cancelled' and self.record.get('remote_status') in {'done', 'completed'}
            independent = ((state in {'rejected', 'failed', 'cancelled'} and not late_cancel_completed) or self.completed_matches())
            if (state == 'completed' and not independent) or late_cancel_completed:
                self.restore_completed_pin()
            if self.record.get('credential_sha256') != self.key_hash and not independent:
                raise CredentialRequired(str(self.record.get('credential_sha256') or 'unknown'))
        return self.record

    def restore_completed_pin(self):
        """A lost local result needs its original key again, before scheduler retry."""
        required = self.record['credential_sha256']
        source = self.record.get('credential_source', 'byok')
        with file_lock(self.root / 'pin.lock'):
            pinpath = self.root / 'credential_pin.json'
            pin = read_json(pinpath) if pinpath.exists() else {}
            records = [read_json(path) for path in self.root.glob('request-*.json')]
            conflict = any(r.get('state') not in self.TERMINAL and
                           (r.get('credential_sha256') != required or r.get('credential_source', 'byok') != source)
                           for r in records)
            if (conflict or (pin.get('state') not in {None, 'released'} and
                    (pin.get('sha256') != required or pin.get('source', 'byok') != source))):
                raise JournalError('VoiceGenCore: для потерянного аудио нужен прежний ключ, но другая remote task ещё активна; требуется сверка, новый POST остановлен')
            # Publish the credential before marking the request pending. A
            # crash between these files still schedules the required old key.
            atomic_json(pinpath, {'family': 'voicegencore', 'sha256': required, 'state': 'active', 'source': source})
            self.record.update(state='pending', updated_at=time.time())
            atomic_json(self.path, self.record)

    def save(self):
        self.record['updated_at'] = time.time()
        atomic_json(self.path, self.record)
        self.refresh_pin()

    def refresh_pin(self):
        with file_lock(self.root / 'pin.lock'):
            records = [read_json(path) for path in self.root.glob('request-*.json')]
            if any(r.get('credential_sha256') != self.key_hash and r.get('state') not in self.TERMINAL for r in records):
                required = next(r['credential_sha256'] for r in records if r.get('credential_sha256') != self.key_hash and r.get('state') not in self.TERMINAL)
                raise CredentialRequired(required)
            uncertain = any(r.get('state') in {'intent', 'uncertain'} for r in records)
            released = bool(records) and all(r.get('state') in self.TERMINAL for r in records)
            state = 'uncertain' if uncertain else 'released' if released else 'active'
            atomic_json(self.root / 'credential_pin.json', {'family': 'voicegencore', 'sha256': self.key_hash, 'state': state, 'source': self.source})

    def intent(self, max_attempts):
        with file_lock(self.root / 'pin.lock'):
            pinpath = self.root / 'credential_pin.json'
            if pinpath.exists():
                pin = read_json(pinpath)
                if pin.get('state') != 'released' and (pin.get('sha256') != self.key_hash or pin.get('source', 'byok') != self.source):
                    raise CredentialRequired(str(pin.get('sha256') or 'unknown'))
                if pin.get('state') == 'uncertain':
                    raise JournalError('VoiceGenCore: исход предыдущего POST неизвестен; повторное создание остановлено для сверки у провайдера')
            old = self.record or {}
            attempts = int(old.get('create_attempts', 0))
            if attempts >= max_attempts:
                raise JournalError('VoiceGenCore: исчерпан сохранённый лимит создания задач')
            history = list(old.get('history') or [])
            if old.get('task_id'):
                history.append({'task_id': old['task_id'], 'state': old.get('state')})
            # Pin is durable before intent and before the billable POST. A crash
            # in this window stays conservatively uncertain; never switches key.
            atomic_json(pinpath, {'family': 'voicegencore', 'sha256': self.key_hash, 'state': 'uncertain', 'source': self.source})
            self.record = {'request_id': self.request_id, 'identity': self.identity, 'credential_sha256': self.key_hash, 'credential_source': self.source,
                           'state': 'intent', 'created_at': time.time(), 'create_attempts': attempts + 1,
                           'history': history, 'task_id': None, 'queue_start_attempted': False}
            atomic_json(self.path, self.record)

    def accepted(self, task_id):
        self.record.update(state='active', task_id=str(task_id), accepted_at=time.time())
        self.save()

    def state(self, state, **fields):
        self.record.update(state=state, **fields)
        self.save()

    def completed_matches(self):
        r = self.record or {}
        return (r.get('state') == 'completed' and self.output.is_file()
                and self.output.stat().st_size == r.get('size') and file_hash(self.output) == r.get('sha256'))

    def complete(self):
        self.state('completed', size=self.output.stat().st_size, sha256=file_hash(self.output))
