"""Durable create intent: same owner/key/body returns the same job forever."""
import hashlib
import json
from fastapi import HTTPException

class IdempotencyError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status

def identity(request, payload, scope):
    key = request.headers.get('Idempotency-Key', '')
    if not key:
        return None  # Existing external clients remain compatible.
    if len(key) > 160 or not key.isascii() or not all(33 <= ord(c) <= 126 for c in key):
        raise HTTPException(422, 'Idempotency-Key must contain 1..160 visible ASCII characters')
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return (scope, hashlib.sha256(key.encode()).hexdigest(), hashlib.sha256(encoded).hexdigest())

def bind(job, intent):
    if intent:
        job.create_scope, job.create_key_hash, job.create_payload_hash = intent
    return job

def lookup(queue, uid, intent):
    if not intent:
        return None
    try:
        return queue.lookup_create(uid, *intent)
    except IdempotencyError as exc:
        raise HTTPException(exc.status, str(exc)) from exc

def file_hash(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def snapshot_inputs(root, names):
    """Copy once into an immutable owned content directory before enqueue."""
    import os
    import tempfile
    from pathlib import Path
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    outputs = []
    for name in names:
        source = Path(name)
        source = (source if source.is_absolute() else root/source).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            raise HTTPException(422, 'Исходный файл не найден внутри каталога владельца')
        stage_root = root/'.job_inputs'
        stage_root.mkdir(exist_ok=True)
        if stage_root.is_symlink():
            raise HTTPException(409, 'Небезопасный каталог снимков исходников')
        fd, temporary = tempfile.mkstemp(prefix='.new-', dir=stage_root)
        try:
            h = hashlib.sha256()
            with os.fdopen(fd, 'wb') as out, source.open('rb') as inp:
                before = os.fstat(inp.fileno())
                for block in iter(lambda: inp.read(1024*1024), b''):
                    h.update(block); out.write(block)
                out.flush(); os.fsync(out.fileno())
                after = os.fstat(inp.fileno())
            if (before.st_ino,before.st_size,before.st_mtime_ns) != (after.st_ino,after.st_size,after.st_mtime_ns):
                raise HTTPException(409, 'Исходник изменился во время запуска; повторите запрос')
            directory = stage_root/h.hexdigest()
            directory.mkdir(exist_ok=True)
            if directory.is_symlink():
                raise HTTPException(409, 'Небезопасный снимок исходника')
            target = directory/source.name
            try:
                os.link(temporary, target)  # Atomic creation; never overwrite accepted input.
            except FileExistsError:
                if target.is_symlink() or file_hash(target) != h.hexdigest():
                    raise HTTPException(409, 'Снимок исходника изменён; запуск остановлен')
            dir_fd = os.open(directory, os.O_RDONLY)
            try: os.fsync(dir_fd)
            finally: os.close(dir_fd)
            outputs.append(str(target.relative_to(root)))
        finally:
            Path(temporary).unlink(missing_ok=True)
    return outputs
