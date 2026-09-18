# -*- coding: utf-8 -*-
"""Настоящий пульт в headless Chrome на данных стенда — для проверок вёрстки.

Поднимает на свободном порту маленький прокси: `/studio` отдаёт `web/index.html`
с добавленным сценарием замера, всё остальное уходит на стенд с кукой вошедшего
(`vs_session`, получена обычным входом). Браузер видит обычный пульт с живыми
данными, пароль в браузер не вводится.
"""
from __future__ import annotations

import html as _html
import http.server
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request

import browser_harness

BASE = browser_harness.BASE


def login(platform: str, login_: str, password: str) -> str:
    req = urllib.request.Request(platform + "/studio/ui/login",
                                 data=json.dumps({"login": login_, "password": password}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        m = re.search(r"vs_session=([^;]+)", r.headers.get("Set-Cookie", ""))
    if not m:
        raise RuntimeError("вход не выдал куку vs_session")
    return m.group(1)


def serve(platform: str, sid: str, page_js: str):
    """Прокси на свободном порту. Возвращает (адрес, остановка)."""
    page = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
    page = page.replace("</body>", f"<script>{page_js}</script>\n</body>", 1)

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _proxy(self, method):
            if method == "GET" and self.path.split("?")[0] in ("/studio", "/studio/"):
                body = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            n = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(n) if n else None
            req = urllib.request.Request(platform + self.path, data=data, method=method)
            req.add_header("Cookie", f"vs_session={sid}")
            if self.headers.get("Content-Type"):
                req.add_header("Content-Type", self.headers["Content-Type"])
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    code, headers, body = r.status, r.headers, r.read()
            except urllib.error.HTTPError as e:
                code, headers, body = e.code, e.headers, e.read()
            self.send_response(code)
            self.send_header("Content-Type", headers.get("Content-Type", "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._proxy("GET")

        def do_POST(self):
            # Замер вёрстки ничего не меняет: запись на стенд из этого прокси запрещена.
            self.send_response(405)
            self.end_headers()

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}", srv.shutdown


def chrome_dump(url: str, budget_ms: int = 15000, width: int = 1400,
                screenshot: str = "") -> dict:
    chrome = browser_harness.find_chrome()
    if not chrome:
        return {"error": "Chrome не найден (CHROME_BIN)"}
    with tempfile.TemporaryDirectory() as tmp:
        args = [chrome, "--headless", "--disable-gpu", "--no-first-run", "--disable-extensions",
                "--disable-sync", "--disable-background-networking", "--hide-scrollbars",
                f"--user-data-dir={os.path.join(tmp, 'profile')}", f"--window-size={width},2400",
                f"--virtual-time-budget={budget_ms}"]
        if screenshot:
            args.append(f"--screenshot={screenshot}")
        else:
            args.append("--dump-dom")
        proc = subprocess.Popen(args + [url], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        timer = threading.Timer(120, proc.kill)
        timer.start()
        dom = []
        try:
            if screenshot:
                import time
                for _ in range(1200):
                    if os.path.exists(screenshot) and os.path.getsize(screenshot) > 0:
                        time.sleep(0.5)
                        break
                    time.sleep(0.1)
            else:
                for line in proc.stdout:
                    dom.append(line)
                    if "</html>" in line:
                        break
        finally:
            timer.cancel()
            proc.kill()
            proc.wait()
    if screenshot:
        return {"screenshot": screenshot if os.path.exists(screenshot) else ""}
    m = re.search(r'data-result="([^"]*)"', "".join(dom))
    if not m:
        return {"error": "страница не записала data-result"}
    return json.loads(_html.unescape(m.group(1)))
