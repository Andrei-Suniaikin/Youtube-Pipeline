"""Host-wide provider capacity. Secret material is never written to disk.

All services use the same directory/inodes; never unlink its lock files while
services are alive. flock is per open file description, including between two
threads in one process. Each acquisition opens its own descriptors. There is no
TTL: live reservations cannot expire. Crashes close descriptors in the kernel.
"""
import hashlib
import json
import os
import stat
import threading
import time
from pathlib import Path
import fcntl

# Operational safety policy, not claims about every vendor subscription tier.
# Shared by every caller; a service cannot silently choose a larger denominator.
CAPACITIES = {"fastgen": 18, "openai": 12, "claude_oauth": 1,
              "codex": 1, "veo": 24}
_fds = set()
_fd_lock = threading.Lock()


class BudgetConfigurationError(RuntimeError):
    pass


class BudgetBusy(RuntimeError):
    pass


def _after_fork():
    global _fds, _fd_lock
    # Close inherited copies without LOCK_UN, which would unlock the parent's
    # open file descriptions too. Child workers acquire their own reservations.
    for fd in _fds:
        try:
            os.close(fd)
        except OSError:
            pass
    _fds = set()
    _fd_lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _canonical_credential(family, value):
    if family not in CAPACITIES:
        raise BudgetConfigurationError("Unknown credential budget family")
    value = str(value or "").strip()
    if not value:
        raise BudgetConfigurationError("Empty credential cannot reserve capacity")
    if family == "codex":
        try:
            doc = json.loads(value) if value.startswith("{") else json.loads((Path(value)/"auth.json").read_text())
            api_key = doc.get("OPENAI_API_KEY")
            if isinstance(api_key, str) and api_key.strip():
                # Codex API-key auth and ordinary OpenAI calls spend the same
                # physical credential. They must share capacity and lock inodes.
                return "openai", api_key.strip()
            account = (doc.get("tokens") or {}).get("account_id") or doc.get("account_id")
            if not isinstance(account, str) or not account.strip():
                raise ValueError()
            value = "account:" + account.strip()
        except (OSError, ValueError, TypeError, AttributeError):
            raise BudgetConfigurationError("Codex auth.json requires OPENAI_API_KEY or stable account_id for shared capacity") from None
    return family, value


def credential_identity(family, value):
    family, value = _canonical_credential(family, value)
    return hashlib.sha256((family + "\0" + value).encode()).hexdigest()


def _root():
    path = Path(os.environ.get("VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR", "/run/video-studio-credential-budget"))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = path.lstat()
    if not stat.S_ISDIR(mode.st_mode) or mode.st_uid != os.geteuid() or mode.st_mode & 0o077:
        raise BudgetConfigurationError("Shared credential budget directory must be private and owned by the service user")
    return path


def _open(path):
    try:
        with _fd_lock:
            fd = os.open(str(path), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                os.close(fd)
                raise BudgetConfigurationError("Unsafe shared credential budget lock file")
            _fds.add(fd)
            return fd
    except OSError:
        raise BudgetConfigurationError("Cannot open shared credential budget lock") from None


def _close(fd):
    with _fd_lock:
        if fd in _fds:
            _fds.remove(fd)
            os.close(fd)


class Lease:
    def __init__(self, fds=(), family="", identity=""):
        self._fds = list(fds)
        self._pid = os.getpid()
        self._lock = threading.Lock()
        self.family, self.identity = family, identity

    def release(self):
        if self._pid != os.getpid():
            return
        with self._lock:
            fds, self._fds = self._fds, []
            if self._pid == os.getpid():
                for fd in fds:
                    _close(fd)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.release()

    def __del__(self):
        try:
            self.release()
        except Exception:
            pass

    def __repr__(self):
        return "<SharedCredentialBudgetLease>"


def try_acquire(family, value, units=1):
    """Atomic all-or-none, nonblocking; None means ordinary capacity contention."""
    family, value = _canonical_credential(family, value)
    identity = hashlib.sha256((family + "\0" + value).encode()).hexdigest()
    units = int(units)
    if not 1 <= units <= CAPACITIES[family]:
        raise BudgetConfigurationError("Job concurrency exceeds shared credential budget")
    root = _root()
    gate = _open(root / (identity + ".gate"))
    acquired = []
    try:
        try:
            fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        for index in range(CAPACITIES[family]):
            fd = _open(root / (identity + "." + str(index)))
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                _close(fd)
                continue
            acquired.append(fd)
            if len(acquired) == units:
                lease = Lease(acquired, family, identity)
                acquired = []
                return lease
        return None
    finally:
        for fd in acquired:
            _close(fd)
        _close(gate)


def can_acquire(family, value, units=1):
    lease = try_acquire(family, value, units)
    if lease is None:
        return False
    lease.release()
    return True


def acquire(family, value, units=1, timeout=2.0, cancel_check=None):
    deadline = time.monotonic() + max(0, float(timeout))
    while True:
        if cancel_check and cancel_check():
            raise BudgetBusy("Shared credential budget wait cancelled before provider submit")
        lease = try_acquire(family, value, units)
        if lease is not None:
            return lease
        if time.monotonic() >= deadline:
            raise BudgetBusy("Shared credential budget busy; wait in queue before provider submit")
        time.sleep(min(0.05, max(0, deadline-time.monotonic())))
