#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пуллер сбора часов монетизации на Минске (13.10.2026).

Студия ставит задание «собрать профиль N» (кнопка «Обновить часы»). Этот процесс раз
в POLL_SEC спрашивает у студии задание, запускает сборщик Володи
(`~/adspower_audit_20261004/collector.py`) на один профиль и отправляет в студию только
`reader_result.json`. Числа студия принимает тем же разбором, что и ручную загрузку.

Почему это безопасно рядом с заливкой:
  * сборщик берёт ту же бронь профиля у координатора на VPS, что и заливщики, и сам
    пропускает профиль, открытый где угодно (`skipped_busy`);
  * опция смены страны (`set_unset_country`) сюда не передаётся — только чтение;
  * одно задание за раз: профили открываются по одному.

Занятый профиль — не сбой: пуллер ждёт и пробует снова (BUSY_RETRY_SEC), пока не
выйдет BUSY_MAX_SEC, и всё это время шлёт пульс, чтобы студия не отдала задание заново.

Окружение — те же файлы, что у пуллеров заливки (`~/.vsupload_env`,
`~/.vsupload_env_studio`): VSGEN_SERVER и UPLOAD_TOKEN.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

SERVER = os.environ.get("VSGEN_SERVER", "").rstrip("/")
TOKEN = os.environ.get("UPLOAD_TOKEN", "")
HOST = os.environ.get("EARN_HOST", "minsk")
COLLECTOR_DIR = Path(os.environ.get("EARN_COLLECTOR_DIR",
                                    os.path.expanduser("~/adspower_audit_20261004")))
PYTHON = os.environ.get("EARN_PYTHON", "/home/vova/VideoStudio/.venv-upload/bin/python")
RUNS = Path(os.environ.get("EARN_RUNS_DIR", os.path.expanduser("~/earn_runs")))
LOCAL_API = os.environ.get("ADSPOWER_API", "http://localhost:50325")
PEER_API = os.environ.get("EARN_PEER_API", "http://100.100.244.89:50325")
POLL_SEC = int(os.environ.get("EARN_POLL_SEC", "30"))
BEAT_SEC = 60
RUN_MAX_SEC = int(os.environ.get("EARN_RUN_MAX_SEC", str(40 * 60)))
BUSY_RETRY_SEC = int(os.environ.get("EARN_BUSY_RETRY_SEC", "300"))
BUSY_MAX_SEC = int(os.environ.get("EARN_BUSY_MAX_SEC", str(3 * 3600)))
KEEP_RUNS = 30


def log(msg: str) -> None:
    print(f"[earn-puller] {msg}", flush=True)


def call(path: str, payload=None, claim: str = "", data: bytes = None, ctype: str = ""):
    url = f"{SERVER}/studio/api/earn{path}"
    headers = {"X-Upload-Token": TOKEN}
    if claim:
        headers["X-Earn-Claim"] = claim
    if data is None:
        data = json.dumps(payload or {}).encode("utf-8")
        ctype = "application/json"
    headers["Content-Type"] = ctype
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def multipart(name: str, payload: bytes) -> tuple:
    boundary = "earn" + uuid.uuid4().hex
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{name}\"\r\nContent-Type: application/zip\r\n\r\n").encode()
    return head + payload + f"\r\n--{boundary}--\r\n".encode(), \
        f"multipart/form-data; boundary={boundary}"


class Beat:
    """Пульс студии раз в минуту, пока идёт работа по заданию."""

    def __init__(self, job):
        self.job, self.note = job, "взял задание"
        self._stop = threading.Event()
        self.lost = False
        self._t = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(timeout=5)

    def _loop(self):
        while not self._stop.wait(BEAT_SEC):
            try:
                call(f"/{self.job['id']}/beat", {"note": self.note}, self.job["token"])
            except urllib.error.HTTPError as exc:
                if exc.code == 409:              # задание отдали другому — останавливаемся
                    self.lost = True
                    return
            except Exception as exc:             # noqa: BLE001 — сеть мигнула, пробуем дальше
                log(f"пульс не дошёл: {exc}")


def run_collector(profile: str, out: Path, beat: Beat) -> int:
    cmd = [PYTHON, str(COLLECTOR_DIR / "collector.py"), "--api", LOCAL_API,
           "--peer-api", PEER_API, "--lease-context", str(COLLECTOR_DIR / "lease_context.json"),
           "--out", str(out), "--profiles", profile]
    env = dict(os.environ)
    env.setdefault("ADSPOWER_RATE_LIMIT_DIR",
                   os.path.expanduser("~/.local/state/video-studio/adspower-rate"))
    with open(out.parent / f"{out.name}.log", "ab") as logf:
        proc = subprocess.Popen(cmd, cwd=str(COLLECTOR_DIR), env=env,
                                stdout=logf, stderr=subprocess.STDOUT)
        started = time.time()
        while proc.poll() is None:
            if beat.lost or time.time() - started > RUN_MAX_SEC:
                # SIGTERM: сборщик сам закрывает свой профиль и отпускает бронь.
                proc.terminate()
                try:
                    proc.wait(timeout=180)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            time.sleep(5)
    return proc.returncode if proc.returncode is not None else -1


def outcome(out: Path, profile: str) -> tuple:
    """(файлы результата, профиль был занят, последняя причина остановки)."""
    results = sorted(out.rglob("reader_result.json"))
    busy, why = False, ""
    ev = out / "events.jsonl"
    if ev.exists():
        for line in ev.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("stage") == "skipped_busy" or e.get("event") == "skipped_busy":
                busy = True
            if e.get("event") in ("run_stopped", "profile_error") or e.get("error_type"):
                why = str(e.get("reason") or e.get("error") or e.get("error_type") or "")[:300]
            reason = str(e.get("reason") or "").lower()
            # Бронь профиля держит заливщик: «Could not hold required profile lease»,
            # «profile lease acquire returned invalid status».
            if "lease" in reason and ("hold" in reason or "acquire" in reason):
                busy = True
    return results, busy, why


def handle(job) -> None:
    profile = str(job["profile"])
    log(f"задание #{job['id']}: профиль {profile}")
    RUNS.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with Beat(job) as beat:
        attempt = 0
        while True:
            attempt += 1
            out = RUNS / f"{job['id']}-{profile}-{time.strftime('%Y%m%dT%H%M%S')}-{attempt}"
            beat.note = f"сборщик читает профиль {profile}"
            rc = run_collector(profile, out, beat)
            if beat.lost:
                log(f"#{job['id']}: задание отдали другому — результат не шлю")
                return
            results, busy, why = outcome(out, profile)
            if results:
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    for f in results:
                        z.write(f, str(f.relative_to(out)))
                body, ctype = multipart("earn.zip", buf.getvalue())
                res = call(f"/{job['id']}/result", claim=job["token"], data=body, ctype=ctype)
                log(f"#{job['id']}: отправлено {len(results)}, записано {res.get('written')}")
                return
            if busy and time.time() - started < BUSY_MAX_SEC:
                beat.note = f"профиль {profile} занят (заливка или открыт руками) — жду"
                log(f"#{job['id']}: профиль {profile} занят, повтор через {BUSY_RETRY_SEC} с")
                if beat._stop.wait(BUSY_RETRY_SEC) or beat.lost:
                    return
                continue
            note = ("профиль так и не освободился за "
                    f"{BUSY_MAX_SEC // 3600} ч") if busy else \
                (f"сборщик не дал результата (код {rc})" + (f": {why}" if why else ""))
            call(f"/{job['id']}/fail", {"note": note}, job["token"])
            log(f"#{job['id']}: {note}")
            return


def prune() -> None:
    try:
        runs = sorted((p for p in RUNS.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for p in runs[:-KEEP_RUNS]:
        subprocess.run(["rm", "-rf", "--", str(p)], check=False)
        log_file = p.parent / f"{p.name}.log"
        if log_file.exists():
            log_file.unlink()


def main() -> int:
    if not SERVER or not TOKEN:
        log("нет VSGEN_SERVER или UPLOAD_TOKEN — проверьте EnvironmentFile")
        return 2
    if not (COLLECTOR_DIR / "collector.py").exists():
        log(f"нет сборщика: {COLLECTOR_DIR}/collector.py")
        return 2
    log(f"старт: {SERVER}, сборщик {COLLECTOR_DIR}")
    while True:
        try:
            job = call("/claim", {"host": HOST}).get("job")
        except Exception as exc:                 # noqa: BLE001
            log(f"студия не ответила: {exc}")
            job = None
        if job:
            try:
                handle(job)
            except Exception as exc:             # noqa: BLE001
                log(f"#{job['id']}: сбой пуллера {type(exc).__name__}: {exc}")
                try:
                    call(f"/{job['id']}/fail", {"note": f"сбой пуллера: {exc}"[:300]}, job["token"])
                except Exception:                # noqa: BLE001
                    pass
            prune()
            continue
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    sys.exit(main())
