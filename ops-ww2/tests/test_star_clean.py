#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Очистка звезды Gemini: оригинал не теряется, мост видит отметку (А6, 05.10.2026).

Сеть и ffmpeg подменены: проверяется то, что делается с файлами на VPS, — подмена файла
на место оригинала, сохранение оригинала в .star, отметка рядом, восстановление после
обрыва, отказ при несовпавшей длительности; и что мост признаёт отметку только для того
файла, который лежит сейчас.

    python3 ops-ww2/tests/test_star_clean.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import bridge_link as L                                            # noqa: E402
import star_clean as S                                             # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def render(data=b"STAR" * 100):
    d = tempfile.mkdtemp(prefix="render-")
    p = os.path.join(d, "13660.mp4")
    open(p, "wb").write(data)
    return p


class FakeRun:
    """ssh/scp/ffprobe под запись: scp «с Минска» кладёт очищенный файл."""

    def __init__(self, clean=b"CLEAN" * 90, durations=(7175.6, 7175.7)):
        self.clean, self.durations, self.calls = clean, list(durations), []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[0] == "scp" and cmd[-1].endswith(".clean.tmp"):
            open(cmd[-1], "wb").write(self.clean)
        out = ""
        if cmd[0] == "ffprobe" and any("bit_rate" in x for x in cmd):
            out = "333017\n482390"
        elif cmd[0] == "ffprobe":
            out = str(self.durations.pop(0))
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")


def main() -> int:
    print("Подмена: очищенный на месте, оригинал в .star, отметка рядом")
    p = render()
    run = FakeRun()
    S.clean_one("p_x", 13660, p, runner=run)
    check("на месте — очищенный", open(p, "rb").read(), b"CLEAN" * 90)
    check("оригинал сохранён", open(p + ".star", "rb").read(), b"STAR" * 100)
    mark = L.star_cleaned(p)
    check("отметка признаётся мостом", (mark.get("clean_size"), mark.get("original_size")), (450, 400))
    check("ffmpeg — с delogo и кодировщиком видеокарты, с низким приоритетом",
          any("delogo=" in " ".join(c) and "h264_nvenc" in " ".join(c) and "nice -n 15" in " ".join(c)
              for c in run.calls), True)
    check("временные файлы на Минске убраны", any("rm -f" in " ".join(c) for c in run.calls), True)
    check("кодирование — в битрейт исходника, а не «по качеству» (раздувало втрое)",
          any("-b:v 432922" in " ".join(c) and "-cq" not in " ".join(c) for c in run.calls), True)

    print("\nДлительность не совпала — файл не трогаем")
    p = render()
    try:
        S.clean_one("p_y", 13660, p, runner=FakeRun(durations=(7175.6, 3600.0)))
        got = "подменил"
    except RuntimeError as exc:
        got = str(exc)
    check("отказ словами", got.startswith("длительность не совпала"), True)
    check("оригинал на месте", open(p, "rb").read(), b"STAR" * 100)
    check(".star не появился, временного нет",
          (os.path.exists(p + ".star"), os.path.exists(p + ".clean.tmp")), (False, False))

    print("\nОригинал не теряется никогда")
    p = render(b"CLEAN" * 90)
    open(p + ".star", "wb").write(b"STAR" * 100)
    open(p + ".clean.tmp", "wb").write(b"CLEAN2")
    S.swap_in_clean(p, p + ".clean.tmp")
    check("уже был .star — оригинал не перезаписан", open(p + ".star", "rb").read(), b"STAR" * 100)
    check("а отметка дописана", bool(L.star_cleaned(p)), True)
    p = render()
    os.replace(p, p + ".star")
    open(p + ".clean.tmp", "wb").write(b"half")
    check("обрыв посередине — восстановление", S.recover(p), "оригинал возвращён на место; временный файл убран")
    check("оригинал снова на месте", open(p, "rb").read(), b"STAR" * 100)

    print("\nМост признаёт отметку только для того файла, что лежит сейчас")
    p = render()
    S.clean_one("p_z", 13660, p, runner=FakeRun())
    check("свежая отметка — да", bool(L.star_cleaned(p)), True)
    open(p, "ab").write(b"x")
    check("файл подменили после очистки — нет", L.star_cleaned(p), {})

    print("\nМост устроен так")
    src = open(os.path.join(os.path.dirname(HERE), "pub_bridge.py"), encoding="utf-8").read()
    check("старый рендер пропускается только с отметкой",
          "< min_render_at and not star" in src and "!= size and not star" in src, True)

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
