# -*- coding: utf-8 -*-
"""Прогон сценария внутри настоящего `web/index.html` в headless Chrome — без стенда и сети.

Запуск приложения (`render()`) заменяется сценарием. Сценарий обязан записать итог в
`document.documentElement` атрибутом `data-result` (JSON). Время в браузере
виртуальное (`--virtual-time-budget`): таймеры по 3 с проходят мгновенно.
Не тест сам по себе — общий помощник для `tests/test_*_browser.py`.
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, "..", ".."))
BOOT = "addEventListener('hashchange', render);\nrender();"


def find_chrome() -> str:
    for c in (os.environ.get("CHROME_BIN", ""),
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              shutil.which("google-chrome") or "", shutil.which("chromium") or ""):
        if c and os.path.exists(c):
            return c
    return ""


def run_scenario(scenario_js: str, budget_ms: int = 5000) -> dict:
    """Итог сценария или {"error": ...}, если браузера нет или сценарий не отчитался."""
    chrome = find_chrome()
    if not chrome:
        return {"error": "Chrome не найден (CHROME_BIN)"}
    page_html = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
    if page_html.count(BOOT) != 1:
        return {"error": "точка запуска приложения в index.html изменилась"}
    page_html = page_html.replace(BOOT, scenario_js)
    with tempfile.TemporaryDirectory() as tmp:
        page = os.path.join(tmp, "harness.html")
        with open(page, "w", encoding="utf-8") as f:
            f.write(page_html)
        # Chrome печатает DOM и остаётся висеть (фоновые службы профиля), поэтому
        # читаем вывод до конца документа и закрываем процесс сами.
        proc = subprocess.Popen(
            [chrome, "--headless", "--disable-gpu", "--no-first-run", "--disable-extensions",
             "--disable-sync", "--disable-background-networking",
             f"--user-data-dir={os.path.join(tmp, 'profile')}",
             f"--virtual-time-budget={budget_ms}", "--dump-dom", "file://" + page],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        timer = threading.Timer(120, proc.kill)
        timer.start()
        dom = []
        try:
            for line in proc.stdout:
                dom.append(line)
                if "</html>" in line:
                    break
        finally:
            timer.cancel()
            proc.kill()
            proc.wait()
    m = re.search(r'data-result="([^"]*)"', "".join(dom))
    if not m:
        return {"error": "сценарий не записал data-result"}
    return json.loads(_html.unescape(m.group(1)))
