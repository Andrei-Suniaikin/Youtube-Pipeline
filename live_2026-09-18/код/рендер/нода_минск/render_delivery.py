"""Private, durable delivery receipts for render_puller. No network or worker policy."""
import hashlib
import fcntl
import json
import os
import shutil
import threading
import time
from pathlib import Path


def _fsync_dir(path):
    if hasattr(os, 'O_DIRECTORY'):
        fd = os.open(str(path), os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp-' + str(threading.get_ident()))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _fsync_dir(path.parent)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def directory_bytes(path):
    total = 0
    for root, dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
        os.fsync(handle.fileno())
    return digest.hexdigest()


class DeliveryStore:
    def __init__(self, root, job_timeout=14400, max_delivery_attempts=20):
        self.job_timeout = max(1, float(job_timeout))
        self.max_delivery_attempts = max(1, int(max_delivery_attempts))
        self.root = Path(root).resolve()
        self.manifests = self.root / 'outbox'
        self.manifests.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._busy = {}

    @staticmethod
    def identity(job):
        return str(int(job['job_id'])) + '-' + hashlib.sha256(job['claim_token'].encode()).hexdigest()[:16]

    def project_dir(self, job):
        return self.root / ('job-' + self.identity(job))

    def path(self, identity):
        if '/' in identity or '\\' in identity or identity.startswith('.'):
            raise ValueError('invalid delivery identity')
        return self.manifests / (identity + '.json')

    def reserve(self, identity):
        with self._lock:
            if identity in self._busy:
                return False
            lock_path = self.path(identity).with_suffix('.lock')
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return False
            self._busy[identity] = fd
            return True

    def release(self, identity):
        with self._lock:
            fd = self._busy.pop(identity, None)
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def save(self, record):
        record['updated_at'] = time.time()
        atomic_json(self.path(record['identity']), record)
        return record

    def begin(self, job):
        identity = self.identity(job)
        path = self.path(identity)
        if path.exists():
            return self.load(identity)
        project = self.project_dir(job)
        project.mkdir(parents=True, exist_ok=True)
        return self.save({'identity': identity, 'job_id': int(job['job_id']),
                          'claim_token': job['claim_token'], 'config': job['config'],
                          'project_dir': str(project), 'phase': 'preparing',
                          'created_at': time.time(), 'deadline_at': time.time() + self.job_timeout, 'attempts': 0, 'next_attempt': 0})

    def load(self, identity):
        with open(self.path(identity), encoding='utf-8') as handle:
            record = json.load(handle)
        project = Path(record['project_dir']).resolve()
        if project.parent != self.root or project.name != 'job-' + identity or record.get('identity') != identity:
            raise ValueError('delivery manifest path mismatch')
        if self.identity(record) != identity:
            raise ValueError('delivery manifest identity mismatch')
        record.setdefault('deadline_at', float(record.get('created_at', 0)) + self.job_timeout)
        return record

    def entries(self):
        for path in sorted(self.manifests.glob('*.json')):
            try:
                yield self.load(path.stem)
            except (OSError, ValueError, KeyError, TypeError):
                # Corruption cannot authorize cleanup or new work admission.
                yield {'identity': path.stem, 'phase': 'invalid', 'size': 0}

    def ready(self, record, output):
        project = Path(record['project_dir']).resolve()
        output = Path(output).resolve()
        if project not in output.parents or not output.is_file() or output.stat().st_size <= 1024:
            raise ValueError('render output is missing, empty or outside claim directory')
        record.update(phase='ready', output=str(output), size=output.stat().st_size,
                      sha256=file_sha256(output), retained_bytes=directory_bytes(project), error='', next_attempt=0)
        return self.save(record)

    def recover(self, record):
        if record['phase'] not in {'preparing', 'rendering'}:
            return record
        marker = Path(record['project_dir']) / '_render_completion.json'
        reason = 'render node restarted before a durable completed output'
        try:
            if marker.exists():
                with marker.open(encoding='utf-8') as handle:
                    completion = json.load(handle)
                output = Path(completion['output'])
                if output.is_file() and output.stat().st_size == completion['size']:
                    return self.ready(record, output)
                reason = 'render completion marker references missing or changed media'
        except (OSError, ValueError, KeyError, TypeError):
            reason = 'render completion marker is invalid'
        # Crash before marker commit leaves an uncertain MP4/partial. Report
        # failure but retain these files for reconciliation even after error ACK.
        record.update(phase='error', error=reason, next_attempt=0, retain_on_error=True,
                      retained_bytes=directory_bytes(record['project_dir']))
        return self.save(record)

    def exhausted(self, record):
        deadline = float(record.get('deadline_at', float(record.get('created_at', 0)) + self.job_timeout))
        if time.time() >= deadline:
            return 'persisted render/delivery deadline exceeded'
        if int(record.get('attempts', 0)) >= self.max_delivery_attempts:
            return 'delivery retry budget exhausted'
        return ''

    def defer(self, record, error):
        attempts = record.get('attempts', 0) + 1
        record.update(attempts=attempts, last_error=str(error)[:500],
                      next_attempt=time.time() + min(300, 2 ** min(attempts, 8)))
        return self.save(record)

    def quarantine(self, record, error):
        record.update(phase='quarantined', last_error=str(error)[:500], next_attempt=0)
        return self.save(record)

    def acknowledge(self, record, receipt):
        if not (receipt.get('accepted') is True and receipt.get('job_id') == record['job_id']
                and receipt.get('status') == 'done' and receipt.get('sha256') == record.get('sha256')
                and receipt.get('size') == record.get('size')):
            raise ValueError('server receipt does not match durable result')
        record.update(phase='acked', receipt={k: receipt[k] for k in ('job_id','status','sha256','size')})
        self.save(record)
        self.cleanup(record)

    def acknowledge_error(self, record, receipt):
        expected = hashlib.sha256(record['error'][:480].encode()).hexdigest()
        if not (receipt.get('accepted') is True and receipt.get('job_id') == record['job_id']
                and receipt.get('status') == 'error' and receipt.get('error_sha256') == expected):
            raise ValueError('server error receipt does not match durable failure')
        record.update(phase='quarantined' if record.get('retain_on_error') else 'acked', receipt={k: receipt[k] for k in ('job_id', 'status', 'error_sha256')})
        self.save(record)
        if record['phase'] == 'acked':
            self.cleanup(record)

    def cleanup(self, record):
        if record['phase'] != 'acked':
            raise ValueError('cleanup requires a durable ACK')
        # Validate again before deletion; never remove another claim's directory.
        record = self.load(record['identity'])
        if record['phase'] != 'acked':
            raise ValueError('cleanup lost ACK state')
        shutil.rmtree(record['project_dir'], ignore_errors=False) if Path(record['project_dir']).exists() else None
        self.path(record['identity']).unlink(missing_ok=True)
        _fsync_dir(self.manifests)

    def admission_allowed(self, min_free_bytes, max_outbox_bytes):
        records = list(self.entries())
        if any(r['phase'] == 'invalid' for r in records):
            return False
        pending_size = sum(int(r.get('retained_bytes') or r.get('size') or 0) for r in records if r['phase'] != 'acked')
        return pending_size < max_outbox_bytes and shutil.disk_usage(self.root).free >= min_free_bytes
