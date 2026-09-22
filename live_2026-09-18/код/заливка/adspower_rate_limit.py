"""Opt-in, cross-process pacing for AdsPower control requests only.

All participating processes must use the same ADSPOWER_RATE_LIMIT_DIR. Requests
remain unmodified when it is unset. The mutex covers a control request, never
the Studio/CDP connection or the media transfer. No automatic request retries.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid

ENV = 'ADSPOWER_RATE_LIMIT_DIR'
INTERVAL_NS = 1_050_000_000
LOCK_TIMEOUT = 180.0
_DEADLINE = ContextVar('adspower_control_deadline', default=None)


class RateLimitError(RuntimeError):
    pass


@contextmanager
def deadline_scope(deadline, *, check=None):
    """Opt-in monotonic deadline for this thread/context, including caller guards."""
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise RateLimitError('Invalid AdsPower control deadline')
    previous = _DEADLINE.get()
    if previous is not None:
        deadline = min(deadline, previous[0])
    token = _DEADLINE.set((deadline, check))
    try:
        _remaining()
        yield
    finally:
        _DEADLINE.reset(token)


def _remaining():
    budget = _DEADLINE.get()
    if budget is None:
        return None
    if budget[1] is not None:
        budget[1]()
    left = budget[0] - time.monotonic()
    if left <= 0:
        raise RateLimitError('AdsPower control deadline exceeded')
    return left


def _timeout(value):
    left = _remaining()
    if left is None:
        return value
    if isinstance(value, tuple):
        return tuple(left if part is None else min(part, left) for part in value)
    return left if value is None else min(value, left)


def _pause(seconds):
    left = _remaining()
    time.sleep(seconds if left is None else min(seconds, left, 0.05))
    _remaining()


def origin_key(url):
    try:
        value = urlsplit(url)
        if value.scheme not in ('http', 'https') or not value.hostname or value.username or value.password:
            raise ValueError
        host = value.hostname.lower()
        if host in ('localhost', '127.0.0.1', '::1'):
            host = 'loopback'
        port = value.port or (443 if value.scheme == 'https' else 80)
    except (TypeError, ValueError) as exc:
        raise RateLimitError('Invalid AdsPower API origin') from exc
    return f'{value.scheme}://{host}:{port}'


def boot_id():
    if sys.platform.startswith('linux'):
        value = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if not re.fullmatch(r'[0-9a-f-]{36}', value):
            raise RateLimitError('Cannot establish monotonic clock boot identity')
        return value
    if sys.platform == 'darwin':
        result = subprocess.run(['sysctl', '-n', 'kern.boottime'], capture_output=True, text=True, timeout=_timeout(5))
        match = re.search(r'sec\s*=\s*(\d+)', result.stdout)
        if result.returncode == 0 and match:
            return 'darwin-' + match.group(1)
    raise RateLimitError('Cannot establish monotonic clock boot identity')


@contextmanager
def timed_lock(path, timeout=LOCK_TIMEOUT):
    deadline = time.monotonic() + timeout
    with Path(path).open('a') as handle:
        while True:
            _remaining()
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RateLimitError('AdsPower control API queue deadline exceeded')
                _pause(0.05)
        try:
            _remaining()
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _write_state(path, value):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temp.open('x') as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _completion_ns(path, state, epoch, current):
    """Trust only a receipt for this exact dispatch; legacy writers have none.

    Keep the dispatch file's v1 shape so already running older callers can
    continue sharing this mutex. A crash or an older caller changes started_ns
    without replacing the completion receipt, forcing a full recovery interval.
    """
    if state['boot_id'] != epoch or not path.exists():
        return None
    try:
        receipt = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RateLimitError('AdsPower completion state is unreadable') from exc
    if (not isinstance(receipt, dict)
            or set(receipt) != {'schema_version', 'origin', 'boot_id', 'started_ns', 'completed_ns'}
            or type(receipt['schema_version']) is not int or receipt['schema_version'] != 1
            or receipt['origin'] != state['origin'] or not isinstance(receipt['boot_id'], str)
            or not receipt['boot_id'] or type(receipt['started_ns']) is not int or receipt['started_ns'] < 0
            or type(receipt['completed_ns']) is not int or receipt['completed_ns'] < receipt['started_ns']):
        raise RateLimitError('AdsPower completion state is invalid')
    if receipt['boot_id'] != epoch:
        return None
    if receipt['completed_ns'] > current:
        raise RateLimitError('AdsPower monotonic clock moved backwards')
    if receipt['started_ns'] != state['started_ns']:
        return None
    return receipt['completed_ns']


@contextmanager
def control_slot(url):
    _remaining()
    directory = os.environ.get(ENV)
    if not directory:
        yield
        return
    root = Path(directory)
    if not root.is_absolute():
        raise RateLimitError('ADSPOWER_RATE_LIMIT_DIR must be absolute')
    origin, epoch = origin_key(url), boot_id()
    root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(origin.encode()).hexdigest()
    state_path = root / (key + '.json')
    completed_path = root / (key + '.completed.json')
    with timed_lock(root / (key + '.lock')):
        current = time.monotonic_ns()
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text())
            except (OSError, ValueError) as exc:
                raise RateLimitError('AdsPower pacing state is unreadable') from exc
            if (not isinstance(state, dict) or set(state) != {'schema_version', 'origin', 'boot_id', 'started_ns'}
                    or type(state['schema_version']) is not int or state['schema_version'] != 1
                    or state['origin'] != origin or not isinstance(state['boot_id'], str)
                    or not state['boot_id'] or type(state['started_ns']) is not int or state['started_ns'] < 0):
                raise RateLimitError('AdsPower pacing state is invalid')
            if state['boot_id'] == epoch and state['started_ns'] > current:
                raise RateLimitError('AdsPower monotonic clock moved backwards')
            completed_ns = _completion_ns(completed_path, state, epoch, current)
            # A verified completion already spent part/all of the interval idle.
            # Legacy or interrupted requests have no matching completion, so
            # wait the full interval after acquiring their released mutex.
            deadline_ns = (current if completed_ns is None else completed_ns) + INTERVAL_NS
            while time.monotonic_ns() < deadline_ns:
                _pause(max(0, deadline_ns - time.monotonic_ns()) / 1_000_000_000)
        state = {'schema_version': 1, 'origin': origin, 'boot_id': epoch,
                 'started_ns': time.monotonic_ns()}
        _write_state(state_path, state)
        # Holding the mutex through this short control request also prevents
        # a paused process from dispatching after a later reserved request.
        try:
            _remaining()
            yield
        finally:
            # Still under the same mutex, including transport exceptions and
            # deadline cancellation. A hard crash leaves an unmatched receipt.
            _write_state(completed_path, dict(state, completed_ns=time.monotonic_ns()))


def get(url, **kwargs):
    import requests
    with control_slot(url):
        if _DEADLINE.get() is not None:
            kwargs['timeout'] = _timeout(kwargs.get('timeout'))
        result = requests.get(url, **kwargs)
        _remaining()
        return result
