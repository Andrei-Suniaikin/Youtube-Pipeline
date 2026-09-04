#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Подставной провайдер: отвечает как настоящий, но ничего не стоит.

Нужен для одной вещи, которой раньше не было вообще: **прогнать платный путь целиком,
не заплатив**. До этого единственный способ проверить, что кадр заказывается, опрос
идёт, журнал закрывается и файл ложится на диск, — это заказать настоящий кадр.

Отвечает по контракту тех провайдеров, с которыми работают движки:

    POST /api/v6/generations          → {"id": …}         затем
    GET  /api/v6/generations/{id}     → results[].download_url
    POST /api/v4/{кто-то}/image/generate → {"operation_id": …}
    GET  /api/v4/operations/{id}      → {"status": "completed", …}
    POST /api/v1/video/{вид}          → {"data": {"task_id": …}}
    GET  /api/v1/video/task/{id}      → готовый результат
    GET  /file/{id}.png               → сами байты

Чем он НЕ является: это не имитация качества. Картинка — одноцветный PNG с номером,
видео — короткий валидный контейнер. Проверяется путь, а не результат.

Умеет вести себя плохо — за этим он и нужен:

    --fail-rate 0.3      треть вызовов отвечает отказом
    --slow 5             ответ приходит через 5 секунд (проверка таймаутов)
    --die-after 10       после десяти вызовов перестаёт отвечать вовсе

    dev/fake_provider.py --port 9099
"""
from __future__ import annotations

import argparse
import base64
import json
import random
import struct
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"calls": 0, "fail_rate": 0.0, "slow": 0.0, "die_after": 0, "quiet": False}
JOBS = {}
LOCK = threading.Lock()


def png(width: int = 640, height: int = 360, seed: int = 0) -> bytes:
    """Настоящий PNG нужного размера — движки его открывают и проверяют."""
    r, g, b = (60 + seed * 37) % 200, (90 + seed * 61) % 200, (140 + seed * 23) % 200
    raw = b"".join(b"\x00" + bytes([r, g, b]) * width for _ in range(height))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def mp4() -> bytes:
    """Минимальный валидный контейнер: движку важно, что файл открывается."""
    ftyp = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41"
    return ftyp + b"\x00\x00\x00\x08free"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if not STATE["quiet"]:
            print(f"  {self.command} {self.path}")

    # ── ответы ──
    def _send(self, code: int, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data):
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _gate(self) -> bool:
        """Плохое поведение по требованию. Возвращает False — ответ уже отправлен."""
        with LOCK:
            STATE["calls"] += 1
            n = STATE["calls"]
        if STATE["die_after"] and n > STATE["die_after"]:
            self.close_connection = True
            return False                 # молчим вовсе: так выглядит зависший провайдер
        if STATE["slow"]:
            time.sleep(STATE["slow"])
        if STATE["fail_rate"] and random.random() < STATE["fail_rate"]:
            self._json(500, {"error": "подставной отказ провайдера"})
            return False
        return True

    def do_GET(self):
        if not self._gate():
            return
        path = self.path.split("?", 1)[0]
        if path.startswith("/file/"):
            name = path.rsplit("/", 1)[-1]
            if name.endswith(".mp4"):
                return self._send(200, mp4(), "video/mp4")
            seed = sum(ord(ch) for ch in name)
            return self._send(200, png(seed=seed), "image/png")
        if path.startswith("/api/v6/generations/"):
            gid = path.rsplit("/", 1)[-1]
            return self._json(200, {"id": gid, "status": "succeeded", "results": [
                {"download_url": f"http://{self.headers.get('Host')}/file/{gid}.png"}]})
        if path.startswith("/api/v4/operations/"):
            oid = path.rsplit("/", 1)[-1]
            return self._json(200, {"operation_id": oid, "status": "completed",
                                    "result": {"url": f"http://{self.headers.get('Host')}/file/{oid}.png"}})
        if "/video/task/" in path or path.startswith("/api/v1/video/"):
            tid = path.rsplit("/", 1)[-1]
            return self._json(200, {"data": {"task_id": tid, "status": "completed",
                                             "video_url": f"http://{self.headers.get('Host')}/file/{tid}.mp4"}})
        return self._json(404, {"error": f"подставной провайдер не знает {path}"})

    def do_POST(self):
        if not self._gate():
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            body = {}
        path = self.path.split("?", 1)[0]
        ident = f"fake-{STATE['calls']:06d}"
        with LOCK:
            JOBS[ident] = {"body": body, "at": time.time()}
        if path.startswith("/api/v6/generations"):
            return self._json(200, {"id": ident, "status": "queued"})
        if "/image/generate" in path or "/image/" in path:
            return self._json(200, {"operation_id": ident, "status": "pending"})
        if "/video/" in path:
            return self._json(200, {"data": {"task_id": ident, "status": "queued"}})
        if "chat/completions" in path:
            # Для смягчения промптов и прочих текстовых вызовов: отвечаем тем же текстом.
            said = ((body.get("messages") or [{}])[-1] or {}).get("content") or ""
            return self._json(200, {"choices": [{"message": {"role": "assistant",
                                                             "content": str(said)[:2000]}}]})
        return self._json(404, {"error": f"подставной провайдер не знает {path}"})


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=9099)
    p.add_argument("--fail-rate", type=float, default=0.0,
                   help="доля вызовов, отвечающих отказом (0..1)")
    p.add_argument("--slow", type=float, default=0.0, help="задержка ответа, секунд")
    p.add_argument("--die-after", type=int, default=0,
                   help="после скольких вызовов замолчать совсем")
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args()
    STATE.update(fail_rate=a.fail_rate, slow=a.slow, die_after=a.die_after,
                 quiet=a.quiet)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"подставной провайдер на http://127.0.0.1:{a.port}")
    if a.fail_rate or a.slow or a.die_after:
        print(f"  ведёт себя плохо: отказов {a.fail_rate:.0%}, задержка {a.slow} с, "
              f"замолкает после {a.die_after or '—'}")
    print("  движкам подставить: VS_GOOGLER_BASE и VS_VEO_BASE на этот адрес")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
