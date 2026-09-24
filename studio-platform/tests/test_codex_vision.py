#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex получает картинку флагом -i, а текстовый вызов остаётся прежним (24.09.2026).

Что подписка ChatGPT умеет смотреть картинки — проверено не по документации, а живым
вызовом на боевой машине: изображение с надписью «STUDIO 42» было прочитано верно.
Здесь проверяется наша сторона: файл действительно кладётся рядом и попадает в команду,
содержимое картинки не утекает в журнал, а вызовы БЕЗ картинок собираются ровно так же,
как до этой работы, — иначе тысячи текстовых задач почувствовали бы правку.

Настоящий CLI не запускается: подменён запуск процесса.

    .venv/bin/python tests/test_codex_vision.py
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
for path in ("rewrite", "shared"):
    sys.path.insert(0, os.path.join(BASE, path))

import rewrite_core as R                                           # noqa: E402

FAILS = []
CALLS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class FakeProc:
    """Процесс, который «ответил» и завершился: нам важна команда, а не ответ."""

    returncode = 0

    def __init__(self, cmd, **kw):
        CALLS.append({"cmd": list(cmd), "cwd": kw.get("cwd") or ""})
        self.stdin = open(os.devnull, "w")
        # Ответ CLI кладёт в файл, указанный флагом -o: найдём его и напишем туда.
        out = ""
        for i, part in enumerate(cmd):
            if part == "-o" and i + 1 < len(cmd):
                out = cmd[i + 1]
        if out:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write("ответ модели")

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def run(images=None):
    CALLS.clear()
    ok, text = R.call_codex_subscription(
        messages=[{"role": "user", "content": "что на картинке?"}],
        model="gpt-5.1-codex-max", _credential_leased=True,
        _codex_home_override="", images=images)
    return ok, text, (CALLS[-1] if CALLS else {})


def main() -> int:
    R.subprocess.Popen = FakeProc                                  # noqa: SLF001
    R.shutil.which = lambda name: "/usr/bin/codex"

    print("Без картинок команда прежняя")
    ok, text, call = run()
    check("вызов прошёл", ok, True)
    check("ответ прочитан", text, "ответ модели")
    cmd = call.get("cmd") or []
    check("флага картинки нет", "-i" in cmd, False)
    check("последним стоит дефис — промпт идёт потоком", cmd[-1], "-")
    check("модель на месте", "gpt-5.1-codex-max" in cmd, True)

    print("С картинкой — файл рядом и флаг в команде")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
    ok, text, call = run(images=[png])
    cmd = call.get("cmd") or []
    check("вызов прошёл", ok, True)
    check("флаг появился", cmd.count("-i"), 1)
    image_path = cmd[cmd.index("-i") + 1]
    check("файл лежит в рабочем каталоге вызова",
          os.path.dirname(image_path), call.get("cwd"))
    check("это png", image_path.endswith(".png"), True)
    check("дефис остался последним", cmd[-1], "-")

    print("Две картинки — два флага, разные файлы")
    ok, text, call = run(images=[png, png])
    cmd = call.get("cmd") or []
    check("флагов два", cmd.count("-i"), 2)
    paths = [cmd[i + 1] for i, p in enumerate(cmd) if p == "-i"]
    check("файлы разные", len(set(paths)), 2)

    print("Содержимое картинки не утекает")
    joined = " ".join(str(x) for x in (call.get("cmd") or []))
    check("в команде только пути", "PNG" in joined, False)

    print("Каталог вызова исчезает вместе с картинками")
    check("временный каталог убран", os.path.exists(call.get("cwd") or "/"), False)

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
