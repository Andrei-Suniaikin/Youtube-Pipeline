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

14.10.2026 — ежедневный сбор и Мак Володи:
  * Сборщик не открывает ни одного профиля, пока не ответит AdsPower второй машины
    (Мак Володи, `--peer-api`): «не ответила» он не считает «там ничего не открыто».
    Задание №1 14.10 упало именно так — Мак был выключен. Опции «без Мака» у сборщика
    нет, и обходить её подставным адресом мы не будем: открытый руками на Маке профиль
    видно только оттуда. Поэтому пуллер сам спрашивает Мак ДО задания: пока тот молчит,
    берёт только задания по кнопке и сразу закрывает их словами, а ночные оставляет в
    очереди ждать Мака.
  * Перед запуском сборщика пуллер смотрит бронь профиля у того же координатора на VPS,
    что и заливщики (`inspect` — только чтение, разрешено ключом Минска). Профиль под
    бронью ночной обход пропускает до следующих суток, а не ждёт часами; задание по
    кнопке по-прежнему ждёт. Открытый прямо сейчас профиль сборщик ловит сам
    (`skipped_busy`) — у пуллера своего опроса AdsPower нет и не нужно: лишние запросы
    к нему мешали бы заливке.

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
# Бронь профилей: те же настройки координатора, с которыми сборщик берёт бронь сам.
LEASE_CONTEXT = Path(os.environ.get("EARN_LEASE_CONTEXT",
                                    str(COLLECTOR_DIR / "lease_context.json")))
# Мак: как у сборщика — 3 попытки, пауза 10 с (collect_channel_emails.HOST_POLL_*),
# чтобы не объявить его мёртвым из-за секундного обрыва туннеля. Ответ помним минуту.
PEER_TRIES = int(os.environ.get("EARN_PEER_TRIES", "3"))
PEER_PAUSE_SEC = float(os.environ.get("EARN_PEER_PAUSE_SEC", "10"))
PEER_CACHE_SEC = float(os.environ.get("EARN_PEER_CACHE_SEC", "60"))
# 16.10.2026: Мак выключен, профили на нём закрыты (Володя) — сбор идёт без него.
PEER_OFFLINE_OK = os.environ.get("EARN_PEER_OFFLINE_OK", "1") == "1"
PEER_DOWN_NOTE = ("Мак Володи не отвечает — без него сбор не запускается, "
                  "чтобы не открыть профиль на двух машинах сразу")


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


def _peer_get(url: str, timeout: float) -> dict:
    """GET к AdsPower Мака через общий темп запросов сборщика и заливщиков
    (ADSPOWER_RATE_LIMIT_DIR): AdsPower режет частые запросы, и наш опрос не должен
    отнимать слот у заливки. Нет модуля — обычный urllib."""
    if "get" not in _PACER:
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "adspower_rate_limit", COLLECTOR_DIR / "src" / "adspower_rate_limit.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _PACER["get"] = mod.get
        except Exception:                        # noqa: BLE001
            _PACER["get"] = None
    if _PACER["get"] is not None:
        return _PACER["get"](url, timeout=timeout).json()
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


_PACER: dict = {}
_PEER = {"at": 0.0, "ok": None}


def peer_alive(force: bool = False) -> bool:
    """Отвечает ли AdsPower Мака списком открытых профилей — то же, что требует сборщик.

    Молчащий Мак переспрашиваем раз в 5 минут, а не каждую: каждая проверка мёртвого
    адреса — почти минута таймаутов, и держать её подряд незачем."""
    ttl = PEER_CACHE_SEC if _PEER["ok"] else max(PEER_CACHE_SEC, 300)
    if not force and _PEER["ok"] is not None and time.time() - _PEER["at"] < ttl:
        return _PEER["ok"]
    ok = False
    for attempt in range(max(1, PEER_TRIES)):
        if attempt:
            time.sleep(PEER_PAUSE_SEC)
        try:
            d = _peer_get(f"{PEER_API}/api/v1/browser/local-active", 12)
            if d.get("code") == 0 and isinstance((d.get("data") or {}).get("list"), list):
                ok = True
                break
        except Exception:                        # noqa: BLE001 — молчит = не ответил
            continue
    if not ok and PEER_OFFLINE_OK and COLLECTOR_DIR.joinpath("collector_studio.py").exists():
        # 16.10.2026: Мак выключен, Володя подтвердил — профили на нём закрыты. Наша копия
        # сборщика с --peer-offline-ok считает молчащий Мак пустым; отвечающий проверяет.
        if _PEER["ok"] is not True:
            log("Мак Володи не отвечает — собираем без него (EARN_PEER_OFFLINE_OK=1)")
        _PEER.update(at=time.time(), ok=True)
        return True
    if ok is not _PEER["ok"]:
        log("Мак Володи отвечает" if ok else "Мак Володи не отвечает — ночные задания ждут")
    _PEER.update(at=time.time(), ok=ok)
    return ok


def lease_holder(profile: str):
    """Чья бронь на профиле у координатора: '' — свободен, имя держателя — занят,
    None — узнать не вышло (тогда решает сам сборщик: он берёт бронь и не идёт дальше
    без неё)."""
    try:
        ctx = json.loads(LEASE_CONTEXT.read_text(encoding="utf-8"))
        cmd = [str(x) for x in ctx["coordinator_command"]] + ["inspect", "--profile", str(profile)]
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=float(ctx.get("command_timeout_seconds") or 40),
                              check=False)
        lines = [x for x in (proc.stdout or "").splitlines() if x.strip()]
        res = json.loads(lines[-1]) if lines else {}
    except Exception as exc:                     # noqa: BLE001
        log(f"бронь профиля {profile} не проверил: {type(exc).__name__}")
        return None
    if proc.returncode != 0 or not isinstance(res.get("active"), bool):
        log(f"бронь профиля {profile} не проверил: {str(res.get('error') or '')[:80]}")
        return None
    if not res["active"]:
        return ""
    return str((res.get("lease") or {}).get("holder") or "неизвестно")[:40]


def holder_note(holder: str) -> str:
    return {"minsk": "профиль занят заливкой на Минске",
            "mac": "профиль занят на Маке Володи"}.get(
        holder, f"профиль под бронью «{holder}»")


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


def collector_script() -> Path:
    """Наша копия сборщика (`make_collector_studio.py`), если она собрана, иначе Володин.

    15.10.2026: копия открывает напрямую каналы студии, которых нет в списках почт
    профиля (доступ менеджера), и читает «Studio за 28 дней» с дашборда.
    """
    ours = COLLECTOR_DIR / "collector_studio.py"
    return ours if ours.exists() else COLLECTOR_DIR / "collector.py"


def run_collector(profile: str, out: Path, beat: Beat, ucs=None) -> int:
    script = collector_script()
    cmd = [PYTHON, str(script), "--api", LOCAL_API,
           "--peer-api", PEER_API, "--lease-context", str(COLLECTOR_DIR / "lease_context.json"),
           "--out", str(out), "--profiles", profile]
    if ucs and script.name == "collector_studio.py":
        cmd += ["--expect-ucs", ",".join(ucs)]
    # 16.10.2026: macbook-pro-4 (второй хост AdsPower) выключен, Володя подтвердил, что
    # профили на нём закрыты. Недоступный второй хост не останавливает сбор; доступный
    # проверяется как раньше. Выключить: EARN_PEER_OFFLINE_OK=0.
    if script.name == "collector_studio.py" and PEER_OFFLINE_OK:
        cmd += ["--peer-offline-ok"]
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


def busy_reason(out: Path) -> str:
    """Чем занят профиль — по записи сборщика `skipped_busy` (его `gate`)."""
    for rec in sorted(out.glob("profiles/*/attempt_*/record.json")):
        try:
            r = json.loads(rec.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if r.get("status") != "skipped_busy":
            continue
        gate = r.get("gate") if isinstance(r.get("gate"), dict) else {}
        on = gate.get("active_on") or []
        if "peer" in on:
            return "профиль открыт на Маке Володи"
        if "local" in on:
            return "профиль открыт на Минске"
        if gate.get("uploaders"):
            return "на Минске идёт заливка"
    return ""


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


def skip(job, why: str) -> None:
    """Ночной обход: занятый профиль не ждём — до следующих суток. Не сбой."""
    note = f"пропущено: {why} — соберётся в следующий проход"
    call(f"/{job['id']}/fail", {"note": note, "skipped": True}, job["token"])
    log(f"#{job['id']}: {note}")


def handle(job) -> None:
    profile = str(job["profile"])
    auto = bool(job.get("auto"))
    log(f"задание #{job['id']}: профиль {profile}" + (" (ежедневный сбор)" if auto else ""))
    # Сюда задание по кнопке приходит и при молчащем Маке — переспросим его сейчас и,
    # если молчит, ответим словами, а не голым исключением сборщика через минуту.
    if not _PEER["ok"] and not peer_alive(force=True):
        call(f"/{job['id']}/fail", {"note": PEER_DOWN_NOTE}, job["token"])
        log(f"#{job['id']}: {PEER_DOWN_NOTE}")
        return
    RUNS.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with Beat(job) as beat:
        attempt = 0
        while True:
            attempt += 1
            holder = lease_holder(profile)
            if holder:
                results, busy, why, rc, bwhy = [], True, "", 0, holder_note(holder)
            else:
                out = RUNS / f"{job['id']}-{profile}-{time.strftime('%Y%m%dT%H%M%S')}-{attempt}"
                beat.note = f"сборщик читает профиль {profile}"
                rc = run_collector(profile, out, beat, job.get("ucs") or [])
                if beat.lost:
                    log(f"#{job['id']}: задание отдали другому — результат не шлю")
                    return
                results, busy, why = outcome(out, profile)
                bwhy = busy_reason(out) or "профиль занят заливкой"
            if not results and "peer active-list unavailable" in why:
                # Мак замолчал между нашей проверкой и запуском сборщика.
                _PEER.update(at=time.time(), ok=False)
                call(f"/{job['id']}/fail", {"note": PEER_DOWN_NOTE}, job["token"])
                log(f"#{job['id']}: {PEER_DOWN_NOTE}")
                return
            if busy and auto and not results:
                skip(job, bwhy)
                return
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
                beat.note = f"{bwhy} — жду"
                log(f"#{job['id']}: {bwhy} ({profile}), повтор через {BUSY_RETRY_SEC} с")
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
        if not once():
            time.sleep(POLL_SEC)


def once() -> bool:
    """Один заход: спросить задание и выполнить. False — заданий не было."""
    try:
        # Мак молчит — ночные задания не берём: им ждать его в очереди, а не падать.
        ok = peer_alive()
        job = call("/claim", {"host": HOST, "peer_ok": ok,
                              "manual_only": not ok}).get("job")
    except Exception as exc:                     # noqa: BLE001
        log(f"студия не ответила: {exc}")
        job = None
    if not job:
        return False
    try:
        handle(job)
    except Exception as exc:                     # noqa: BLE001
        log(f"#{job['id']}: сбой пуллера {type(exc).__name__}: {exc}")
        try:
            call(f"/{job['id']}/fail", {"note": f"сбой пуллера: {exc}"[:300]}, job["token"])
        except Exception:                        # noqa: BLE001
            pass
    prune()
    return True


if __name__ == "__main__":
    sys.exit(main())
