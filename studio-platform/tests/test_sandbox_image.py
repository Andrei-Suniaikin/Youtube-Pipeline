#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Песочница: настоящий движок картинок против подставного провайдера.

Это единственный тест, который проходит платный путь ЦЕЛИКОМ — заявка, заказ, опрос,
скачивание, файл на диске, закрытие журнала — и при этом ничего не стоит. Раньше
проверить это можно было только заказав настоящий кадр.

Провайдер поднимается отдельным процессом на свободном порту и отвечает по тому же
контракту, что боевой: POST /api/v6/generations → id, GET … → download_url → PNG.

    .venv/bin/python tests/test_sandbox_image.py
"""
from __future__ import annotations

import base64
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_up(port: int, seconds: float = 10) -> bool:
    until = time.time() + seconds
    while time.time() < until:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v6/generations/x",
                                   timeout=1).close()
            return True
        except Exception:                                          # noqa: BLE001
            time.sleep(0.2)
    return False


class Bag:
    """Журнал-заглушка: важен порядок обращений и то, чем закрылась строка."""

    enabled = True

    def __init__(self):
        self.calls = []

    @staticmethod
    def fingerprint(**parts):
        return "fp"

    def reserve(self, **kw):
        from call_journal import Decision
        self.calls.append(("reserve", kw.get("unit")))
        return Decision("new", call_id=1)

    def ordered(self, call_id, remote_order_id):
        self.calls.append(("ordered", remote_order_id))
        return True

    def delivered(self, call_id, result_path, cost=None):
        self.calls.append(("delivered", os.path.basename(result_path or "")))
        return True

    def failed(self, call_id, error, ordered=False):
        self.calls.append(("failed", bool(ordered), str(error)[:60]))
        return True

    def verbs(self):
        return [c[0] for c in self.calls]


def main() -> int:
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BASE, "dev", "fake_provider.py"),
         "--port", str(port), "--quiet"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    root = tempfile.mkdtemp(prefix="sandbox-image-")
    try:
        if not wait_up(port):
            print("подставной провайдер не поднялся")
            return 1
        # Адрес подставляется ДО импорта: константы движка читаются один раз, как в бою.
        os.environ["VS_GOOGLER_BASE"] = f"http://127.0.0.1:{port}"
        os.environ["VS_VEO_BASE"] = f"http://127.0.0.1:{port}"
        sys.path.insert(0, os.path.join(BASE, "workers"))
        import googler_worker as G                                # noqa: E402

        check("движок смотрит в песочницу",
              G.GOOGLER_BASE_URL_V6, f"http://127.0.0.1:{port}/api/v6")

        G.GOOGLER_API_KEY = "fake-key"
        G.GOOGLER_MODEL = "NARWHAL"          # есть в IMAGE_MODEL_V6 → путь v6
        bag = Bag()
        G._JOURNAL_SINGLETON = bag

        out = os.path.join(root, "007.jpg")
        print("\nКадр целиком: заявка → заказ → опрос → файл")
        idx, data = G.generate_image(
            {"visual_prompt": "маяк в тумане, длинная выдержка",
             "camera_angle": "Wide Shot"},
            style="кинематографично", output_path=out, index=6, journal_unit="007")

        check("кадр получен", bool(data), True)
        check("файл лежит на диске", os.path.exists(out), True)
        check("это настоящий PNG",
              open(out, "rb").read(8), b"\x89PNG\r\n\x1a\n")
        check("порядок обращений", bag.verbs(), ["reserve", "ordered", "delivered"])
        check("единица — номер кадра", bag.calls[0][1], "007")
        check("id заказа записан до опроса",
              bag.calls[1][1].startswith("fake-"), True)
        check("готово доказано файлом", bag.calls[2][1], "007.jpg")

        # Случай «провайдер отказал» здесь сознательно не проверяется: у v6-запроса
        # свои шесть попыток с нарастающей паузой, и тест превратился бы в двухминутное
        # ожидание. Отказ и его последствия для журнала проверяет tests/test_clip_journal.py,
        # где провайдер подменён на уровне функции.
    finally:
        if proc.poll() is None:
            proc.terminate()
        shutil.rmtree(root, ignore_errors=True)
        os.environ.pop("VS_GOOGLER_BASE", None)
        os.environ.pop("VS_VEO_BASE", None)

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
