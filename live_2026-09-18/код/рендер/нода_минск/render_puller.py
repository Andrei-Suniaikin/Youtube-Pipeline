#!/usr/bin/env python3
"""
render_puller — агент рендера на GPU-боксе.

Цикл: claim задание с ген-сервера → скачать ассеты (tar.gz) → запустить render_worker
с конфигом задания → слать прогресс → отдать готовый mp4 (или ошибку) → повторить.

Только ИСХОДЯЩИЕ HTTP к ген-серверу (за NAT работает, портов открывать не надо).
Запускать на боксе (там же render_worker.py + ffmpeg + whisper). Env:
  VSGEN_SERVER   базовый URL ген-сервера (https://gen.13.140.129.66.sslip.io)
  PULLER_TOKEN   общий секрет (совпадает с серверным)
  RENDER_WORKER  путь к render_worker.py (default /workspace/render_worker.py)
  PULLER_WORK    рабочая папка (default /workspace/render_jobs)
  PULLER_POLL    интервал опроса сек (default 8)
"""
import os, sys, io, time, json, tarfile, subprocess, shutil, re, signal, threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
from render_delivery import DeliveryStore
import requests

SERVER = os.environ.get("VSGEN_SERVER", "https://gen.13.140.129.66.sslip.io").rstrip("/")
TOKEN = os.environ.get("PULLER_TOKEN", "")
RENDER_WORKER = os.environ.get("RENDER_WORKER", "/workspace/render_worker.py")
WORK = os.environ.get("PULLER_WORK", "/workspace/render_jobs")
POLL = int(os.environ.get("PULLER_POLL", "8"))
# Параллельных рендеров на ноде. x264-энкод CPU-bound (20 потоков делятся), но whisper-фаза
# (GPU, ~105с/джоб) и скачивание/заливка (сеть) у параллельных джобов перекрываются →
# на батчах ~1.5-2x throughput при N=3 (i5-14600KF/31GB тянет 3 слайдшоу-рендера).
CONCURRENCY = max(1, min(3, int(os.environ.get("PULLER_CONCURRENCY", "1"))))
H = {"X-Puller-Token": TOKEN}

HEARTBEAT_SEC = float(os.environ.get("PULLER_HEARTBEAT_SEC", "5"))
DOWNLOAD_TIMEOUT = float(os.environ.get("PULLER_DOWNLOAD_TIMEOUT", "1800"))
UPLOAD_TIMEOUT = float(os.environ.get("PULLER_UPLOAD_TIMEOUT", "1800"))
RENDER_TIMEOUT = float(os.environ.get("PULLER_RENDER_TIMEOUT", "10800"))
WHISPER_TIMEOUT = float(os.environ.get("PULLER_WHISPER_TIMEOUT", "1200"))
NO_PROGRESS_TIMEOUT = float(os.environ.get("PULLER_NO_PROGRESS_TIMEOUT", "900"))
JOB_TIMEOUT = float(os.environ.get("PULLER_JOB_TIMEOUT", "14400"))
MIN_FREE_BYTES = int(os.environ.get("PULLER_MIN_FREE_BYTES", str(32 * 1024 ** 3)))
MAX_DELIVERY_ATTEMPTS = max(1, min(50, int(os.environ.get("PULLER_MAX_DELIVERY_ATTEMPTS", "20"))))
MAX_OUTBOX_BYTES = int(os.environ.get("PULLER_MAX_OUTBOX_BYTES", str(64 * 1024 ** 3)))
_STORE = None
_STORE_LOCK = threading.Lock()


def delivery_store():
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = DeliveryStore(WORK, job_timeout=JOB_TIMEOUT, max_delivery_attempts=MAX_DELIVERY_ATTEMPTS)
        return _STORE


class RenderCancelled(Exception):
    pass


def _job_headers(claim_token):
    return {**H, "X-Render-Claim": claim_token}


def _log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def claim():
    r = requests.post(f"{SERVER}/api/render/claim", headers=H, timeout=30, verify=True)
    r.raise_for_status()
    return r.json().get("job")


class _CheckedReader(io.RawIOBase):
    def __init__(self, source, monitor):
        self.source, self.monitor = source, monitor
    def readable(self):
        return True
    def readinto(self, buffer):
        self.monitor.check()
        data = self.source.read(len(buffer))
        self.monitor.check()
        if data:
            self.monitor.activity()
            buffer[:len(data)] = data
        return len(data)


def download_assets(jid, projdir, claim_token, monitor):
    with requests.get(f"{SERVER}/api/render/{jid}/assets", headers=_job_headers(claim_token),
                      stream=True, timeout=(15, 30)) as response:
        if response.status_code in (403, 409):
            raise RenderCancelled("asset claim rejected")
        response.raise_for_status()
        response.raw.decode_content = True
        root = Path(projdir).resolve()
        reader = _CheckedReader(response.raw, monitor)
        with tarfile.open(fileobj=io.BufferedReader(reader, buffer_size=1 << 20), mode="r|gz") as archive:
            for member in archive:
                monitor.check()
                target = (root / member.name).resolve()
                if root not in target.parents or member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                    raise ValueError("unsafe asset archive member")
                archive.extract(member, path=projdir)
                monitor.activity()


def post_progress(jid, claim_token, pct, stage):
    try:
        r = requests.post(f"{SERVER}/api/render/{jid}/progress", headers=_job_headers(claim_token),
                          data={"pct": int(pct), "stage": stage[:120]}, timeout=15)
        if r.status_code in (403, 409):
            return False
        r.raise_for_status()
    except Exception:
        return True   # сеть/5xx не должны убивать локальный рендер
    return True


def post_transcript(jid, claim_token, projdir):
    """Отдаёт chapters/transcript.json (сегменты whisper от рендера) — до mp4.

    Порядок важен: mp4 переводит задачу в done, и сервер там же решает, ставить ли
    главы. Прилетевший позже транскрипт этого решения уже не изменит.
    Не критично: нет файла или сеть отвалилась → рендер всё равно засчитывается.
    """
    path = os.path.join(projdir, "chapters", "transcript.json")
    if not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as f:
            r = requests.post(f"{SERVER}/api/render/{jid}/transcript",
                              headers={**_job_headers(claim_token), "Content-Type": "application/json"},
                              data=f, timeout=180)
        if r.status_code == 409:
            raise RenderCancelled()
        r.raise_for_status()
        _log(f"транскрипт отправлен: {os.path.getsize(path) // 1024} КБ")
        return True
    except RenderCancelled:
        raise
    except Exception as e:
        _log(f"транскрипт не отправлен ({e}) — главы посчитать будет не из чего")
        return False


class _UploadFile:
    def __init__(self, handle, monitor):
        self.handle, self.monitor = handle, monitor
        self.len = os.fstat(handle.fileno()).st_size
    def read(self, size=-1):
        self.monitor.check()
        data = self.handle.read(size)
        if data:
            self.monitor.activity()
        return data
    def tell(self):
        return self.handle.tell()


def get_receipt(record):
    response = requests.get(f"{SERVER}/api/render/{record['job_id']}/receipt",
                            headers=_job_headers(record['claim_token']), timeout=(10, 20))
    if response.status_code in (403, 409):
        raise RenderCancelled("receipt claim rejected")
    response.raise_for_status()
    return response.json()


def post_done(record, monitor):
    # Transcript is sent before result so the server can enqueue chapters once.
    if record['config'].get('auto_chapters'):
        if not post_transcript(record['job_id'], record['claim_token'], record['project_dir']):
            raise RuntimeError("required transcript delivery failed")
    else:
        post_transcript(record['job_id'], record['claim_token'], record['project_dir'])
    with open(record['output'], "rb") as handle:
        response = requests.post(f"{SERVER}/api/render/{record['job_id']}/result?status=done",
            headers={**_job_headers(record['claim_token']), "Content-Type": "video/mp4",
                     "X-Render-SHA256": record['sha256'], "Content-Length": str(record['size'])},
            data=_UploadFile(handle, monitor), timeout=(15, 30))
    if response.status_code in (403, 409):
        raise RenderCancelled("result claim rejected")
    response.raise_for_status()
    return response.json()


def post_error(record):
    error = record['error'][:480]
    response = requests.post(f"{SERVER}/api/render/{record['job_id']}/result?status=error",
        headers={**_job_headers(record['claim_token']), "X-Render-Error-SHA256": hashlib.sha256(error.encode()).hexdigest()},
        params={"error": error}, timeout=(10, 20))
    if response.status_code in (403, 409):
        raise RenderCancelled("error claim rejected")
    response.raise_for_status()
    return response.json()


def terminal_reconcile(record, reason):
    """One bounded final receipt check, then release the lease and retain media."""
    store = delivery_store()
    outcome = 'server unreachable; lease will expire and files require reconciliation'
    try:
        receipt = get_receipt(record)
        if receipt.get('accepted') and receipt.get('status') == 'done' and record.get('sha256'):
            store.acknowledge(record, receipt)
            return True
        if not receipt.get('accepted'):
            failure = reason[:480]
            receipt = post_error({**record, 'error': failure})
            expected = hashlib.sha256(failure.encode()).hexdigest()
            if not (receipt.get('accepted') is True and receipt.get('job_id') == record['job_id']
                    and receipt.get('status') == 'error' and receipt.get('error_sha256') == expected):
                raise ValueError('terminal reconcile receipt mismatch')
        outcome = 'server terminal receipt observed; local files retained'
    except RenderCancelled:
        outcome = 'claim stale; local files retained'
    except Exception as exc:
        outcome += ': ' + type(exc).__name__
    record['reconcile_outcome'] = outcome
    store.quarantine(record, reason)
    _log(f"job {record['job_id']}: {reason}; {outcome}")
    return False


def deliver(record, monitor):
    store = delivery_store()
    reason = store.exhausted(record)
    if reason:
        return terminal_reconcile(record, reason)
    try:
        required_transcript = Path(record['project_dir']) / 'chapters' / 'transcript.json'
        if record['phase'] == 'ready' and record['config'].get('auto_chapters') and not required_transcript.is_file():
            record['fatal_delivery_error'] = 'required chapters transcript is missing; rendered MP4 retained for repair'
            store.save(record)
        receipt = get_receipt(record)
        if record.get('fatal_delivery_error'):
            failure = record['fatal_delivery_error'][:480]
            if not receipt.get('accepted'):
                receipt = post_error({**record, 'error': failure})
            if not (receipt.get('accepted') is True and receipt.get('job_id') == record['job_id']
                    and receipt.get('status') == 'error'
                    and receipt.get('error_sha256') == hashlib.sha256(failure.encode()).hexdigest()):
                raise ValueError('permanent delivery failure receipt mismatch')
            store.quarantine(record, failure)
            return False
        if not receipt.get('accepted'):
            monitor.phase('upload', 99, "Доставка результата" if record['phase'] == 'ready' else "Доставка ошибки")
            receipt = post_done(record, monitor) if record['phase'] == 'ready' else post_error(record)
        if record['phase'] == 'ready':
            store.acknowledge(record, receipt)
        else:
            store.acknowledge_error(record, receipt)
        _log(f"job {record['job_id']}: delivery ACK verified")
        return True
    except RenderCancelled as exc:
        # A heartbeat may observe done between commit and the HTTP response.
        try:
            receipt = get_receipt(record)
            if record['phase'] == 'ready':
                store.acknowledge(record, receipt)
            else:
                store.acknowledge_error(record, receipt)
            return True
        except RenderCancelled:
            store.quarantine(record, str(exc))
            _log(f"job {record['job_id']}: stale claim; artifact quarantined")
        except Exception as retry_error:
            store.defer(record, retry_error)
        return False
    except Exception as exc:
        if isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code in (400, 413, 422):
            record['fatal_delivery_error'] = f"render result rejected (HTTP {exc.response.status_code}); MP4 retained for repair"
        store.defer(record, exc)
        _log(f"job {record['job_id']}: delivery deferred, files retained: {exc}")
        return False


def _render_step(line):
    # print_progress is emitted in production; descriptive headers are debug-only.
    match = re.search(r"этапов рендера:\s*([1-4])/4\b", line)
    if not match:
        match = re.search(r"ШАГ\s+([1-4])/4:\s*(?:Аудио|Субтитры|Видеоряд|Финальная сборка)", line)
    return int(match.group(1)) if match else None


def _parse_pct(line, step=0):
    # A worker renders many short clips before the final FFmpeg invocation.
    # Their local 100% must never become overall 99%.
    if step == 4:
        match = re.search(r"·\s*(\d+)%", line)
        if match:
            return 60 + int(min(100, int(match.group(1))) * 0.39)
    if step == 3:
        match = re.search(r"(?:пачек|задач|батчей картинок):\s*(\d+)/(\d+)", line)
        # Nested Ken Burns clips emit 1/1. Only the outer multi-item counter
        # is informative; a single-item render advances at the next header.
        if match and int(match.group(2)) > 1:
            return 20 + int(min(1, int(match.group(1)) / int(match.group(2))) * 40)
        match = re.search(r"картинок:\s*(\d+)/(\d+)", line)
        if match and int(match.group(2)) > 1:
            return 15 + int(min(1, int(match.group(1)) / int(match.group(2))) * 5)
    return None


def _stage_of(line, step=0, previous="Рендер"):
    labels = {1: "Подготовка аудио", 2: "Подготовка субтитров",
              3: "Подготовка кадров", 4: "Финальная сборка"}
    if _render_step(line) is not None:
        return labels.get(step, previous)
    if step == 3:
        match = re.search(r"(?:пачек|задач|батчей картинок):\s*(\d+)/(\d+)", line)
        if match:
            return "Кодирование"
        if "картинок:" in line:
            return "Подготовка кадров"
    return previous


def _stop_process_group(proc):
    """Остановить render_worker вместе со всеми порождёнными ffmpeg."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    except ProcessLookupError:
        pass


def _process_work_signal(proc, project_dir):
    """CPU counters and file growth count as work even when stdout is quiet."""
    cpu = 0
    if proc is not None and os.path.isdir('/proc'):
        pending, seen = [proc.pid], set()
        while pending:
            pid = pending.pop()
            if pid in seen:
                continue
            seen.add(pid)
            try:
                stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
                cpu += int(stat[11]) + int(stat[12])
                pending.extend(int(p) for p in Path(f'/proc/{pid}/task/{pid}/children').read_text().split())
            except (OSError, ValueError, IndexError):
                pass
    size, newest = 0, 0
    for root, directories, files in os.walk(project_dir):
        for filename in files:
            try:
                st = os.stat(os.path.join(root, filename))
                size += st.st_size
                newest = max(newest, st.st_mtime_ns)
            except OSError:
                pass
    return cpu, size, newest


class JobMonitor:
    def __init__(self, record):
        self.record = record
        self.proc = None
        self.stop_event = threading.Event()
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        now = time.monotonic()
        self.started = self.phase_started = self.last_activity = now
        self.phase_name, self.pct, self.stage = 'download', 1, "Скачиваю материалы"
        self.whisper_started = None
        self.render_step = 0
        self.reason = ''
        self.last_work = None
        self.thread = threading.Thread(target=self._run, daemon=True)
    def start(self):
        self.thread.start()
    def close(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=20)
    def phase(self, name, pct, stage):
        with self.lock:
            self.phase_name, self.pct, self.stage = name, pct, stage
            self.phase_started = self.last_activity = time.monotonic()
    def activity(self):
        with self.lock:
            self.last_activity = time.monotonic()
    def line(self, line):
        with self.lock:
            self.last_activity = time.monotonic()
            if '__RENDER_PHASE__|whisper_start' in line:
                self.whisper_started = time.monotonic()
            if '__RENDER_PHASE__|whisper_end' in line:
                self.whisper_started = None
            step = _render_step(line)
            if step is not None:
                self.render_step = max(self.render_step, step)
                self.pct = max(self.pct, {1: 5, 2: 10, 3: 15, 4: 60}[self.render_step])
            if self.whisper_started is not None:
                self.stage = "Синхронизация по озвучке"
            elif '__RENDER_PHASE__|whisper_end' in line:
                self.stage = _stage_of("ШАГ %s/4: Видеоряд" % self.render_step, self.render_step, self.stage)
            else:
                self.stage = _stage_of(line, self.render_step, self.stage)
            pct = _parse_pct(line, self.render_step)
            if pct is not None:
                self.pct = max(self.pct, pct)
    def check(self):
        if self.cancelled.is_set():
            if self.reason == 'claim rejected':
                raise RenderCancelled(self.reason)
            raise TimeoutError(self.reason)
        now = time.monotonic()
        with self.lock:
            timeout = {'download': DOWNLOAD_TIMEOUT, 'render': RENDER_TIMEOUT, 'upload': UPLOAD_TIMEOUT}.get(self.phase_name, JOB_TIMEOUT)
            reason = ''
            if time.time() >= float(self.record.get('deadline_at', self.record.get('created_at', time.time()) + JOB_TIMEOUT)) or now - self.started > JOB_TIMEOUT:
                reason = 'overall render job deadline exceeded'
            elif now - self.phase_started > timeout:
                reason = self.phase_name + ' wall-time deadline exceeded'
            elif self.whisper_started is not None and now - self.whisper_started > WHISPER_TIMEOUT:
                reason = 'Whisper stage deadline exceeded'
            elif now - self.last_activity > NO_PROGRESS_TIMEOUT:
                reason = 'render job made no observable progress'
        if reason:
            self.reason = reason
            self.cancelled.set()
            raise TimeoutError(reason)
    def _run(self):
        while not self.stop_event.is_set():
            try:
                if self.phase_name == 'render':
                    signal_value = _process_work_signal(self.proc, self.record['project_dir'])
                    if signal_value != self.last_work:
                        self.last_work = signal_value
                        self.activity()
                self.check()
                with self.lock:
                    pct, stage = self.pct, self.stage
                if not post_progress(self.record['job_id'], self.record['claim_token'], pct, stage):
                    self.reason = 'claim rejected'
                    self.cancelled.set()
                    break
            except (TimeoutError, RenderCancelled):
                break
            except Exception as exc:
                _log(f"job {self.record['job_id']}: monitor error {type(exc).__name__}")
            if self.stop_event.wait(HEARTBEAT_SEC):
                break
        if self.cancelled.is_set() and self.proc is not None:
            _stop_process_group(self.proc)


def run_job(job, widx=0):
    store = delivery_store()
    identity = store.identity(job)
    if not store.reserve(identity):
        return
    try:
        record = store.begin(job)
    except Exception:
        store.release(identity)
        raise
    monitor = JobMonitor(record)
    proc = None
    tail = deque(maxlen=50)
    monitor.start()
    try:
        if record['phase'] in {'ready', 'error'}:
            deliver(record, monitor)
            return
        if record['phase'] in {'quarantined', 'acked'}:
            return
        project = record['project_dir']
        monitor.check()
        download_assets(record['job_id'], project, record['claim_token'], monitor)
        monitor.phase('render', 5, "Рендер")
        record['phase'] = 'rendering'
        store.save(record)
        proc = subprocess.Popen([sys.executable, '-u', RENDER_WORKER, '--project_dir', project,
                                 '--config', json.dumps(record['config'])], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
        monitor.proc = proc
        output = None
        for line in proc.stdout:
            monitor.line(line)
            clean = line.rstrip()
            if clean:
                tail.append(clean)
            match = re.search(r"__RENDER_SUCCESS__\|(.+)", line)
            if match:
                output = match.group(1).strip()
        proc.wait()
        monitor.check()
        if proc.returncode != 0 or not output:
            raise RuntimeError(f"render rc={proc.returncode}: " + "\n".join(tail)[-400:])
        store.ready(record, output)
        deliver(record, monitor)
    except RenderCancelled as exc:
        store.quarantine(record, str(exc))
        _log(f"job {record['job_id']}: cancelled/stale, local state retained")
    except Exception as exc:
        # If render already produced a durable completion marker, recover the
        # video before classifying a crash as failure. Never discard its MP4.
        try:
            recovered = store.recover(record) if record['phase'] == 'rendering' else record
        except Exception:
            recovered = record
        if recovered['phase'] != 'ready':
            record.update(phase='error', error=str(exc)[:480], next_attempt=0)
            store.save(record)
        _log(f"job {record['job_id']}: {exc}; delivery state retained")
    finally:
        monitor.close()
        if proc is not None and proc.poll() is None:
            _stop_process_group(proc)
        store.release(identity)


def replay_outbox_once():
    store = delivery_store()
    for record in store.entries():
        if record['phase'] in {'invalid', 'quarantined'} or (record.get('next_attempt', 0) > time.time() and not store.exhausted(record)):
            continue
        identity = record['identity']
        if not store.reserve(identity):
            continue
        monitor = None
        try:
            # A live uploader may have ACKed after the directory scan. Re-read
            # under the cross-process lock; never resurrect its deleted record.
            record = store.load(identity)
            if record['phase'] == 'acked':
                store.cleanup(record)
                continue
            record = store.recover(record)
            monitor = JobMonitor(record)
            monitor.phase('upload', 99, "Повтор доставки результата")
            monitor.start()
            deliver(record, monitor)
        except FileNotFoundError:
            pass
        except Exception as exc:
            store.defer(record, exc)
        finally:
            if monitor:
                monitor.close()
            store.release(identity)


def heartbeat_outbox_once():
    # Delivery retries can outlive one worker slot. Every durable live claim
    # keeps its lease even while another upload is slow or waiting for retry.
    store = delivery_store()
    records = [r for r in store.entries() if r['phase'] in {'ready', 'error'} and not store.exhausted(r)]
    def beat(record):
        post_progress(record['job_id'], record['claim_token'], 99, "Ожидает подтверждения доставки")
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(beat, records))


PULLER_RUNTIME_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def node_health_once():
    store = delivery_store()
    records = list(store.entries())
    now = time.time()
    payload = {
        "free_bytes": shutil.disk_usage(WORK).free,
        "outbox_retained_bytes": sum(int(r.get("retained_bytes") or r.get("size") or 0) for r in records if r["phase"] != "acked"),
        "outbox_attention": sum(r["phase"] in {"invalid", "quarantined"} for r in records),
        "outbox_stuck": sum(r["phase"] not in {"invalid", "quarantined", "acked"} and float(r.get("deadline_at") or now+1) < now for r in records),
        "active_jobs": sum(r["phase"] in {"preparing", "rendering"} for r in records),
        "puller_sha256": PULLER_RUNTIME_SHA256,
        "worker_sha256": hashlib.sha256(Path(RENDER_WORKER).read_bytes()).hexdigest(),
    }
    response = requests.post(f"{SERVER}/api/render/node-health", headers=H, json=payload, timeout=(3, 5))
    response.raise_for_status()
    return payload


def node_health_loop(stop_event=None):
    stop_event = stop_event or threading.Event()
    while not stop_event.is_set():
        try:
            node_health_once()
        except Exception as exc:
            _log("node health error:", type(exc).__name__)
        stop_event.wait(30)


def outbox_heartbeat_loop():
    while True:
        try:
            heartbeat_outbox_once()
        except Exception as exc:
            _log("outbox heartbeat error:", type(exc).__name__)
        time.sleep(max(1, HEARTBEAT_SEC))


_HEALTH = {}
_HEALTH_LOCK = threading.Lock()


def _loop_health(name, error=''):
    with _HEALTH_LOCK:
        previous = _HEALTH.get(name, {})
        _HEALTH[name] = {'last_tick': time.time(), 'last_error': error,
                         'failures': previous.get('failures', 0) + bool(error)}


def loop_health():
    with _HEALTH_LOCK:
        return {name: dict(value) for name, value in _HEALTH.items()}


def outbox_loop(stop_event=None):
    stop_event = stop_event or threading.Event()
    while not stop_event.is_set():
        try:
            replay_outbox_once()
            _loop_health('outbox')
        except Exception as exc:
            _loop_health('outbox', str(exc)[:300])
            _log('outbox replay loop recovered from:', type(exc).__name__, str(exc)[:200])
        stop_event.wait(min(POLL, 5))


_claim_lock = threading.Lock()


def worker_loop(widx, stop_event=None):
    """Один слот рендера; лок сериализует claim, сами рендеры идут параллельно."""
    stop_event = stop_event or threading.Event()
    while not stop_event.is_set():
        job = None
        try:
            with _claim_lock:
                if delivery_store().admission_allowed(MIN_FREE_BYTES, MAX_OUTBOX_BYTES):
                    job = claim()
                else:
                    _log("render admission paused: outbox limit, invalid manifest or low disk")
        except Exception as e:
            _loop_health(f'worker-{widx}', str(e)[:300])
            _log(f"[w{widx}] claim/storage recovered error:", type(e).__name__, str(e)[:200])
        if not job:
            stop_event.wait(POLL)
            continue
        try:
            _log(f"[w{widx}] взял job {job['job_id']} '{job.get('name')}'")
            run_job(job, widx)
            _loop_health(f'worker-{widx}')
        except Exception as exc:
            _loop_health(f'worker-{widx}', str(exc)[:300])
            _log(f"[w{widx}] execution/storage recovered error:", type(exc).__name__, str(exc)[:200])
            try:
                post_error({**job, 'error': 'render node storage failure: ' + type(exc).__name__})
            except Exception as report_error:
                _log(f"[w{widx}] storage error report unavailable:", type(report_error).__name__)
            stop_event.wait(POLL)


def main():
    if not TOKEN:
        _log("FATAL: PULLER_TOKEN не задан"); sys.exit(1)
    os.makedirs(WORK, exist_ok=True)
    _log(f"puller старт: server={SERVER} worker={RENDER_WORKER} poll={POLL}s concurrency={CONCURRENCY}")
    # Recover durable deliveries before opening new render slots.
    threading.Thread(target=outbox_heartbeat_loop, daemon=True).start()
    threading.Thread(target=node_health_loop, daemon=True).start()
    try:
        replay_outbox_once()
    except Exception as exc:
        _loop_health('outbox', str(exc)[:300])
        _log('startup outbox replay will retry:', type(exc).__name__)
    threading.Thread(target=outbox_loop, daemon=True).start()
    threads = [threading.Thread(target=worker_loop, args=(i,), daemon=True) for i in range(CONCURRENCY)]
    for t in threads:
        t.start()
    while True:
        time.sleep(30)
        _log('puller loop health:', json.dumps(loop_health(), ensure_ascii=False))


if __name__ == "__main__":
    main()
