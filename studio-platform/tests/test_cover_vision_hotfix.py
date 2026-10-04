#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сверка обложек видит картинку: три правки, сделанные на сервере 04.10.2026.

Студия ни разу не делала обложек — выпуски тридцатки стояли на шаге обложки. Правки
сделаны прямо на боевой машине и перенесены сюда, чтобы выкатка их не затёрла:

1. `rewrite_core.call_codex_subscription` терял `images` на обоих внутренних поворотах
   (альт-маршрут и аренда учётки) — Codex получал задание без картинок; а флаг
   `-i/--image <FILE>...` забирал следующий `-` себе как ещё одну картинку. Теперь
   картинки доезжают, и перед `-` стоит `--`.
2. `cover_core._subscription_vision` отправлял две PNG в полном размере (~4 МБ base64) —
   сервис текста отвечал 413. Теперь картинки уменьшаются до 1024 по длинной стороне.
3. `deploy/start-studio.sh` не выдавал модулю обложек ключ FastGen — `/cover/api/v1/jobs`
   отвечал 503. Теперь `COVER_FAST_GEN_KEYS` берётся из `googler` + `googler_pool`.

    .venv/bin/python tests/test_cover_vision_hotfix.py
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
os.environ.setdefault("VIDEO_STUDIO_SHARED_DIR", os.path.join(BASE, "shared"))
for path in ("rewrite", "shared", "cover"):
    sys.path.insert(0, os.path.join(BASE, path))

import rewrite_core as R                                          # noqa: E402

FAILS = []
CALLS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class FakeProc:
    returncode = 0

    def __init__(self, cmd, **kw):
        CALLS.append(list(cmd))
        self.stdin = open(os.devnull, "w")
        for i, part in enumerate(cmd):
            if part == "-o" and i + 1 < len(cmd):
                with open(cmd[i + 1], "w", encoding="utf-8") as fh:
                    fh.write("на картинке надпись STUDIO 42")

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class FakeLease:
    def __enter__(self):
        return ""

    def __exit__(self, *a):
        return False


class FakePool:
    def acquire(self, timeout=None):
        return FakeLease()

    def clear_limit(self, value):
        pass

    def mark_limited(self, value, until):
        pass


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


def codex_part():
    R.subprocess.Popen = FakeProc                                  # noqa: SLF001
    R.shutil.which = lambda name: "/usr/bin/codex"
    R.ensure_provider_execution_allowed = lambda provider: None
    R._note_codex_result = lambda *a, **k: None

    print("Codex: картинки доезжают по обоим внутренним поворотам")
    R.alt_route_for = lambda provider: True
    R._alt_slot = lambda provider, model: contextlib.nullcontext()
    CALLS.clear()
    ok, _ = R.call_codex_subscription(messages=[{"role": "user", "content": "что тут?"}],
                                      model="m", images=[PNG])
    cmd = CALLS[-1] if CALLS else []
    check("альт-маршрут: картинка в команде", cmd.count("-i"), 1)

    R.alt_route_for = lambda provider: False
    R.get_effective_credential_pool = lambda provider, *a, **k: FakePool()
    CALLS.clear()
    ok, _ = R.call_codex_subscription(messages=[{"role": "user", "content": "что тут?"}],
                                      model="m", images=[PNG, PNG])
    cmd = CALLS[-1] if CALLS else []
    check("аренда учётки: обе картинки в команде", cmd.count("-i"), 2)

    print("\n  `-i` не съедает дефис задания")
    check("перед дефисом стоит `--`", cmd[-2:], ["--", "-"])
    CALLS.clear()
    R.call_codex_subscription(messages=[{"role": "user", "content": "просто текст"}], model="m")
    cmd = CALLS[-1] if CALLS else []
    check("без картинок `--` нет — текстовый вызов прежний", "--" in cmd, False)
    check("и дефис последний", cmd[-1:], ["-"])


def cover_part():
    print("\nОбложки: картинки для сверки уменьшаются")
    import cover_core as C                                         # noqa: PLC0415
    from PIL import Image                                          # noqa: PLC0415
    seen = {}

    def fake_call(provider, messages, model="", images=None, **kw):
        seen["images"] = images
        return "ok"

    saved = C._subscription_call
    C._subscription_call = fake_call
    try:
        big = Image.new("RGB", (1376, 768), (200, 30, 30))
        tall = Image.new("RGB", (900, 2000), (30, 30, 200))
        C._subscription_vision([big, tall], "прочитай надпись", "codex", "m")
    finally:
        C._subscription_call = saved
    sizes = [Image.open(io.BytesIO(base64.b64decode(b))).size for b in seen.get("images") or []]
    check("обе картинки ушли", len(sizes), 2)
    check("длинная сторона не больше 1024", all(max(s) <= 1024 for s in sizes), True)
    check("пропорции сохранены", sizes[0], (1024, 572))
    check("исходные картинки не испорчены", big.size, (1376, 768))


def start_script_part():
    print("\nСкрипт запуска: ключ FastGen для обложек — из файла ключей")
    src = open(os.path.join(BASE, "deploy", "start-studio.sh"), encoding="utf-8").read()
    m = re.search(r"<<'CFGK'\n(.*?)\nCFGK", src, re.S)
    check("блок ключей обложек есть", bool(m), True)
    check("и стоит до запуска службы", src.find("COVER_FAST_GEN_KEYS") < src.find('cd "$APP"'), True)
    check("уважает уже заданное значение",
          'if [ -z "${COVER_FAST_GEN_KEYS:-}" ] && [ -z "${COVER_FAST_GEN_KEY:-}" ]' in src, True)
    with tempfile.TemporaryDirectory() as tmp:
        keys = os.path.join(tmp, "keys.json")
        json.dump({"googler": "fg_main", "googler_pool": ["fg_two", "fg_main", " ", 5]},
                  open(keys, "w"))
        out = subprocess.run([sys.executable, "-", keys], input=m.group(1) if m else "",
                             capture_output=True, text=True).stdout.strip()
        check("основной и пул, без повторов и мусора", out, "fg_main,fg_two")
        # Весь bash-блок целиком, как его исполнит служба: ключи обязаны попасть именно
        # в переменную окружения, а не только напечататься.
        start = src.find("# --- ключ FastGen для модуля обложек")
        end = src.find("\nfi\n", start) + 4
        block = src[start:end]
        env = dict(os.environ, PY=sys.executable, VS_SERVER_KEYS_PATH=keys)
        env.pop("COVER_FAST_GEN_KEYS", None)
        env.pop("COVER_FAST_GEN_KEY", None)
        got = subprocess.run(["bash", "-c", block + '\necho "[$COVER_FAST_GEN_KEYS]"'],
                             capture_output=True, text=True, env=env).stdout.strip()
        check("служба получает COVER_FAST_GEN_KEYS", got, "[fg_main,fg_two]")
        env["COVER_FAST_GEN_KEYS"] = "свой"
        got = subprocess.run(["bash", "-c", block + '\necho "[$COVER_FAST_GEN_KEYS]"'],
                             capture_output=True, text=True, env=env).stdout.strip()
        check("заданный снаружи ключ не перетирается", got, "[свой]")
        out = subprocess.run([sys.executable, "-", os.path.join(tmp, "нет.json")],
                             input=m.group(1) if m else "", capture_output=True,
                             text=True).stdout.strip()
        check("нет файла ключей — пусто, без падения", out, "")


def main() -> int:
    saved = (subprocess.Popen, R.shutil.which)
    try:
        codex_part()
    finally:
        # R.subprocess — это общий модуль subprocess: подмену возвращаем, иначе ниже
        # настоящий subprocess.run получит подделку.
        subprocess.Popen, R.shutil.which = saved
    cover_part()
    start_script_part()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
