"""Durable remote operation identity. Stores hashes/IDs/results, never credentials."""
import contextlib
import contextvars
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time

_CONTEXT = contextvars.ContextVar('remote_generation_context', default=None)


@contextlib.contextmanager
def journal_context(root, source='server'):
    token = _CONTEXT.set({'root': str(root), 'source': source})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


class RecoveryRequired(RuntimeError):
    pass


def sha(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def atomic_bytes(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.journal-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def atomic_json(path, value):
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, sort_keys=True).encode())


class Journal:
    def __init__(self, family, credential, slot, body):
        context = _CONTEXT.get() or {}
        root = context.get('root') or os.environ.get('VS_REMOTE_JOURNAL_DIR')
        if not root:
            raise RecoveryRequired('remote journal directory is required for paid generation')
        self.root = Path(root)
        self.directory = self.root / family
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / (sha(str(slot)) + '.json')
        self.family = family
        self.source = context.get('source') or os.environ.get('VS_CREDENTIAL_SOURCE', 'byok')
        self.credential_hash = sha(credential or '')
        self.request_hash = sha(json.dumps(body, sort_keys=True, ensure_ascii=False))
        self.lock = None
        self.record = None

    def __enter__(self):
        self.lock = open(str(self.path) + '.lock', 'a+b')
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        try:
            self.record = json.loads(self.path.read_text()) if self.path.exists() else None
            if self.record:
                if self.record['state'] not in ('rejected', 'failed'):
                    if self.record['request_hash'] != self.request_hash:
                        raise RecoveryRequired('request changed while a paid operation is retained')
                    if self.record['state'] != 'done' and self.record['credential_hash'] != self.credential_hash:
                        raise RecoveryRequired('original credential required to resume paid operation')
                if self.record['state'] in ('intent', 'uncertain'):
                    raise RecoveryRequired('submit outcome is unknown; reconcile before resubmitting')
            return self
        except BaseException as exc:
            self.__exit__(None, None, None)
            if isinstance(exc, (OSError, ValueError, KeyError)) and not isinstance(exc, RecoveryRequired):
                raise RecoveryRequired('retained journal is damaged or unavailable; restore it before retry') from exc
            raise

    def __exit__(self, *args):
        if self.lock:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()
            self.lock = None

    def save(self):
        self.record['updated'] = time.time()
        atomic_json(self.path, self.record)

    def intent(self):
        attempts = int((self.record or {}).get('attempts', 0)) + 1
        if attempts > 8:
            raise RecoveryRequired('maximum confirmed generation attempts reached')
        # Serialize the shared pin across scenes; never rewrite another in-flight key.
        with open(self.root / 'credential_pin.lock', 'a+b') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            pin_path = self.root / 'credential_pin.json'
            pin = json.loads(pin_path.read_text()) if pin_path.exists() else {}
            if pin.get('state') in ('active', 'uncertain') and (pin.get('family'), pin.get('sha256')) != (self.family, self.credential_hash):
                raise RecoveryRequired('another credential has retained remote work for this queue job')
            atomic_json(pin_path, {'family': self.family, 'sha256': self.credential_hash, 'state': 'active',
                                   'source': self.source})
            self.record = {'version': 1, 'state': 'intent', 'family': self.family, 'credential_hash': self.credential_hash,
                           'request_hash': self.request_hash, 'attempts': attempts, 'created': time.time()}
            self.save()

    def rejected(self):
        self.record['state'] = 'rejected'
        self.save()
        self.release_if_settled()

    def release_if_settled(self):
        # Completed cached assets no longer require their old credential. Only
        # unresolved accepted/uncertain work pins subsequent queue attempts.
        with open(self.root / 'credential_pin.lock', 'a+b') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            records = [json.loads(p.read_text()) for p in self.directory.glob('*.json')]
            if all(r['state'] in ('rejected', 'failed', 'done') for r in records):
                atomic_json(self.root / 'credential_pin.json', {'family': self.family, 'sha256': self.credential_hash, 'state': 'released', 'source': self.source})

    def accepted(self, remote_id):
        self.record.update(state='submitted', remote_id=str(remote_id))
        self.save()

    def failed(self):
        self.record['state'] = 'failed'
        self.save()
        self.release_if_settled()

    def result(self, value=None):
        path = self.path.with_suffix('.result')
        if value is not None:
            raw = value.encode()
            atomic_bytes(path, raw)
            self.record.update(state='done', result_sha256=sha(raw))
            self.save()
            self.release_if_settled()
            return value
        try:
            from remote_cache_storage import read_result
            raw = read_result(path, self.record['result_sha256'])
        except (OSError, ValueError, KeyError) as exc:
            raise RecoveryRequired('retained generation result is missing; restore it before retry') from exc
        if sha(raw) != self.record['result_sha256']:
            raise RecoveryRequired('retained generation result checksum mismatch')
        return raw.decode()
