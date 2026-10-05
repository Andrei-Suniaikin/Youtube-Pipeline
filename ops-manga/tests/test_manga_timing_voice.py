#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Манхва: приёмка раскладки и выпавший кусок озвучки FastGen (05.10.2026).

Семь роликов манхвы не прошли приёмку раскладки. У трёх это настоящий брак: озвучка
FastGen выкинула кусок текста, под ним 11–14 кадров подряд по 0,2–0,4 с, а блок читается
в 1,12–1,21 раза быстрее медианы ролика. У четырёх — ложная тревога: проверка считала
короткие кадры по всему ролику, а не подряд, и придиралась к тишине до первого слова.

Проверяется: align.check — серия коротких подряд ловится, разбросанные нет, тишина в
начале до 1 с не пустота, вспышка короче 0,3 с ловится как раньше; voice_fastgen —
быстрый блок находится по медиане СВОЕГО ролика, перезаказывается один раз, метка не даёт
перезаказывать снова, нормальные блоки не трогаются.

    python3 ops-manga/tests/test_manga_timing_voice.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, BASE)

# ── заглушки соседних модулей конвейера (на машине они лежат рядом в app/) ──
DUR = {}


class Block:
    def __init__(self, idx, text):
        self.idx, self.text, self.path, self.words_path, self.seconds = idx, text, "", "", 0.0


sys.modules["align_native"] = types.SimpleNamespace(NativeAlignmentError=RuntimeError)
sys.modules["images"] = types.SimpleNamespace(key=lambda *a, **k: "k", BASE="https://api.example")
sys.modules["voice"] = types.SimpleNamespace(
    Block=Block, duration=lambda p: DUR.get(p, 0.0),
    voice_fingerprint=lambda text, voice, rate: f"fp:{len(text)}:{voice}:{rate}")


def _atomic_json(path, value):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f)


sys.modules["generation_receipts"] = types.SimpleNamespace(atomic_json=_atomic_json)

import align as A                                                   # noqa: E402
import voice_fastgen as V                                           # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def pairs_of(durations, start=0.0):
    out, t = [], start
    for d in durations:
        out.append((round(t, 3), round(t + d, 3)))
        t += d
    return out


def main() -> int:
    print("Приёмка раскладки")
    normal = [4.0] * 100
    scattered = list(normal)
    for i in (10, 40, 77):
        scattered[i] = 0.9
    p = pairs_of(scattered)
    check("3 короткие реплики в разных местах — годится", A.check(p, p[-1][1])["ok"], True)
    run = list(normal)
    for i in range(50, 64):
        run[i] = 0.38
    p = pairs_of(run)
    res = A.check(p, p[-1][1])
    check("14 коротких подряд — брак", res["ok"], False)
    check("в причине — серия и где", any("подряд: 14 (с кадра 51" in x for x in res["problems"]), True)
    two = list(normal)
    two[20] = two[21] = 0.8
    p = pairs_of(two)
    check("2 коротких подряд — ещё монтаж", A.check(p, p[-1][1])["ok"], True)
    p = pairs_of(normal, start=0.56)
    check("тишина 0,56 с до первого слова — не пустота", A.check(p, p[-1][1])["ok"], True)
    p = pairs_of(normal, start=1.5)
    check("1,5 с в начале — пустота", A.check(p, p[-1][1])["ok"], False)
    flash = list(normal)
    flash[31] = 0.2
    p = pairs_of(flash)
    check("вспышка 0,2 с — ловится как раньше", A.check(p, p[-1][1])["ok"], False)

    print("\nВыпавший кусок озвучки")
    tmp = tempfile.mkdtemp(prefix="manga-voice-")
    blocks = []
    for i in range(1, 11):
        b = Block(i, " ".join(["слово"] * 1100))
        b.path = os.path.join(tmp, f"block_{i:04d}.mp3")
        open(b.path, "wb").write(b"x" * 4096)
        DUR[b.path] = 1100 / 160 * 60          # 160 сл/мин — темп ролика
        blocks.append(b)
    DUR[blocks[5].path] = 1100 / 183 * 60      # 183 сл/мин — как блок 6 «La Rivincita»
    DUR[blocks[2].path] = 1100 / 168 * 60      # 1,05 медианы — нормальный разброс
    found = V.tempo_outliers(blocks)
    check("быстрый блок найден", [b.idx for b, _, _ in found], [6])
    check("отношение к медиане — около 1,14", round(found[0][1] / found[0][2], 2), 1.14)
    check("порог 0 — проверка выключена", V.tempo_outliers(blocks, ratio=0), [])
    check("меньше трёх блоков — не сравниваем", V.tempo_outliers(blocks[:2]), [])

    print("\nsynth перезаказывает быстрый блок один раз")
    calls = []

    def fake_create(key, text, voice, mp3_path, ident, block_idx, **kw):
        calls.append(block_idx)
        return f"gid-{block_idx}-{len(calls)}"

    def fake_download(key, gen, mp3_path, block_idx, **kw):
        DUR[mp3_path] = 1100 / 161 * 60        # новый заказ — нормальный темп

    saved = {}
    patch = {"preflight": lambda lang: ("key", "algieba"), "block_ready": lambda b, v: True,
             "check_key": lambda *a, **k: "ok", "generation_to_resume": lambda *a, **k: "",
             "create_generation": fake_create, "wait_generation": lambda *a, **k: {},
             "download": fake_download, "save_receipt": lambda *a, **k: None,
             "pause_until": lambda key: 0}
    for name, fn in patch.items():
        saved[name] = getattr(V, name)
        setattr(V, name, fn)
    log = []
    try:
        V.synth(blocks, tmp, "it", log=log.append)
        check("заказан ровно блок 6, один раз", calls, [6])
        state = json.load(open(blocks[5].path + ".voice.json"))
        check("метка «перезаказан по темпу» стоит", state.get("tempo_reordered"), True)
        check("блок снова готов", state.get("status"), "complete")
        check("в журнале — почему", any("пропустила кусок текста" in x for x in log), True)
        DUR[blocks[5].path] = 1100 / 183 * 60  # даже если снова быстрый —
        calls.clear()
        V.synth(blocks, tmp, "it", log=log.append)
        check("второй проход не перезаказывает", calls, [])
    finally:
        for name, fn in saved.items():
            setattr(V, name, fn)

    print(f"\nпровалов: {len(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
