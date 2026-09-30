"""Bounded live release check, using the real durable queue and real providers.

The web service worker must already be running. This script enqueues one
3-frame project, reports progress, validates all images and decodes the video.
It never writes/copies API keys and never silently falls back to fixtures.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

from generation_receipts import atomic_json, valid_image
from web_queue import QueueStore


SOURCE = (
    "Илья впервые пришёл на работу в небольшую мастерскую. Он поставил сумку рядом со стулом "
    "и открыл деревянную коробку на столе. Внутри лежали старые часы. Илья осторожно поднял "
    "часы и заметил, что стрелки неподвижны. Он положил их на мягкую ткань и открыл крышку. "
    "Из механизма выпал маленький винт. Илья нашёл для него место и закрутил отвёрткой. "
    "Часы тихо затикали. Он улыбнулся и поставил часы на полку у окна."
)


def validate_health(payload, expected_root):
    """Reject an HTTP-only success or a worker serving a different queue."""
    if not isinstance(payload, dict) or payload.get("status") != "ok" or payload.get("worker") != "running":
        raise ValueError("Сервер ещё не готов: очередь должна иметь работающий worker")
    queue_root = payload.get("queue_root")
    if not isinstance(queue_root, str) or Path(queue_root).resolve() != Path(expected_root).resolve():
        raise ValueError("Корень очереди /health не совпадает с корнем smoke; проверка не создана")
    return payload


def prepare_request(root, token, frames):
    """Persist the logical request before any job becomes visible to the worker."""
    directory = Path(root) / "smoke_requests"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "request.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current_path = directory / "current"
        has_current = current_path.exists()
        current = json.loads(current_path.read_text())["token"] if has_current else None
        token = token if token is not None else (current if has_current else "release-smoke")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", token) or token in (".", ".."):
            raise ValueError("Токен smoke: 1–80 букв, цифр, точек, дефисов или подчёркиваний")
        request = {"token": token, "queue_root": str(Path(root).resolve()), "frames_limit": frames,
                   "source_sha256": hashlib.sha256(SOURCE.encode()).hexdigest()}
        path = directory / (token + ".json")
        if path.exists():
            if json.loads(path.read_text()) != request:
                raise ValueError("Параметры этого smoke изменились; для нового теста укажите новый --token")
        else:
            atomic_json(path, request)
        if current != token:
            atomic_json(current_path, {"token": token})
    return request


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--frames", type=int, default=3, choices=range(1, 13))
    reports = ap.add_mutually_exclusive_group(required=True)
    reports.add_argument("--report")
    reports.add_argument("--report-dir", help="Каталог отчётов, имя выбирается по сохранённому токену")
    ap.add_argument("--token", help="Новый тест требует нового значения; без него возобновляется последняя проверка")
    ap.add_argument("--timeout", type=int, default=3600)
    args = ap.parse_args()
    store = QueueStore(args.root)
    request = prepare_request(store.root, args.token, args.frames)
    report_path = Path(args.report) if args.report else Path(args.report_dir) / ("smoke_" + request["token"] + ".json")
    previous = json.loads(report_path.read_text()) if report_path.exists() else {}
    if previous and any(previous.get(k) != v for k, v in request.items()):
        raise SystemExit("Этот отчёт относится к другой проверке; укажите её --token или другой --report")
    project = store.create_project(title="Проверка выпуска — три кадра", source_text=SOURCE,
                                   minutes=1, style="S0", language="Russian", voice="ru-RU-DmitryNeural",
                                   frame_limit=args.frames, idempotency_key="smoke:" + request["token"])
    pid = project["id"]
    if previous and previous.get("project_id") != pid:
        raise SystemExit("Проект из отчёта не совпадает с сохранённым токеном; очередь не изменена")
    t0 = time.monotonic()
    result = {**request, "project_id": pid, "live_providers": True,
              "started_at": previous.get("started_at", project["created_at"]), "status": "running"}
    atomic_json(report_path, result)
    last = None
    while True:
        p = store.get_project(pid)
        state = (p["status"], p.get("current_step"), p.get("ready_frames"))
        if state != last:
            print(json.dumps({"elapsed": round(time.monotonic()-t0), "project_id": pid,
                              "status": state[0], "step": state[1], "ready_frames": state[2]}, ensure_ascii=False), flush=True)
            last = state
        if p["status"] == "error":
            result.update(status="error", error=p.get("error"), error_hint=p.get("error_hint"))
            break
        if p["status"] == "complete":
            root = Path(store.project_path(pid))
            sb = json.loads((root / "text/storyboard.json").read_text())
            rows = sb["frames"]
            if not 1 <= len(rows) <= args.frames:
                result.update(status="error", error="число кадров не соответствует пределу smoke")
                break
            missing = [r["idx"] for r in rows if not valid_image(root / "frames" / f'{r["idx"]:05d}.png')]
            if missing:
                result.update(status="error", error="не хватает целых картинок", missing=missing)
                break
            video = root / "out/video.mp4"
            dec = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-f", "null", "-"],
                                 capture_output=True, text=True, timeout=180)
            result.update(status="complete" if dec.returncode == 0 else "error", frames=len(rows),
                          video=str(video), decode_returncode=dec.returncode,
                          decode_stderr=dec.stderr[-1000:], report=store.report(pid))
            break
        if time.monotonic() - t0 >= args.timeout:
            result.update(status="timeout", error="Тест остаётся в очереди; повторите эту же команду с тем же --token")
            break
        time.sleep(5)
    result["elapsed_seconds"] = round(time.monotonic()-t0, 3)
    atomic_json(report_path, result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    raise SystemExit(0 if result["status"] == "complete" else 1)


if __name__ == "__main__":
    main()
