#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GPT Image 2 → GPT Image 2.5 sunburst (фаза 1, §2) и выхлоп рерайта в .gitignore (§7).

Живой GET https://api.openai.com/v1/models показал, что модели "gpt-image-2.5" нет —
есть "gpt-image-2.5-flare" и "gpt-image-2.5-sunburst"; владелец выбрал sunburst. Модель
вынесена в окружение (VS_GPT_IMAGE_MODEL, дефолт "gpt-image-2.5-sunburst"), подписи в
движке и в кабинете приведены к "GPT Image 2.5", а ключ модели ("GPT_IMAGE2") и размеры
(_GPT_IMAGE2_EXACT_SIZES) остались как были — их трогать было прямо запрещено.

    .venv/bin/python tests/test_gpt_image_25.py
"""
from __future__ import annotations

import base64
import io
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
ROOT = os.path.abspath(os.path.join(BASE, ".."))          # корень репозитория (.gitignore)
sys.path.insert(0, os.path.join(BASE, "workers"))

import googler_worker as G                                        # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def worker_constant(name, env_extra=None):
    """Значение константы движка, читаемое отдельным процессом.

    Как GPT_IMAGE_MODEL, так и VS_GOOGLER_BASE читаются один раз на импорте — подмена
    уже загруженного модуля проверяла бы не то, что происходит в бою.
    """
    env = dict(os.environ)
    env.pop("VS_GPT_IMAGE_MODEL", None)
    if env_extra:
        env.update(env_extra)
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path[:0] = [%r]; import googler_worker as G; print(G.%s)"
         % (os.path.join(BASE, "workers"), name)],
        capture_output=True, text=True, env=env, timeout=60)
    if out.returncode != 0:
        return f"импорт не удался: {out.stderr.strip()[-300:]}"
    return (out.stdout.strip().splitlines() or [""])[-1]


def cabinet_image_models_entry(key):
    """Запись IMAGE_MODELS[key] кабинета — тоже отдельным процессом: main.py на импорте
    сеет тестового пользователя и печатает лишнее в stdout, поэтому берём последнюю строку."""
    env = dict(os.environ)
    env["HOME"] = os.path.join(BASE, "dev", "home")
    env["VS_SERVER_DISABLE_BACKGROUND"] = "1"
    with tempfile.TemporaryDirectory() as tmp:
        env["VS_SERVER_DB"] = os.path.join(tmp, "t.db")
        env["VS_TENANTS_DIR"] = os.path.join(tmp, "tenants")
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path[:0] = [%r, %r]; import main; print(main.IMAGE_MODELS[%r])"
             % (os.path.join(BASE, "server"), os.path.join(BASE, "workers"), key)],
            capture_output=True, text=True, env=env, timeout=180)
    if out.returncode != 0:
        return f"импорт не удался: {out.stderr.strip()[-300:]}"
    return (out.stdout.strip().splitlines() or [""])[-1]


def png_b64(width, height):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(1, 2, 3)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def install(model_body="ok"):
    """Готовит движок к вызову GPT_IMAGE2 без сети: requests.post подменён.

    model_body управляет тем, что вернёт подставной OpenAI:
    "ok" — валидный кадр 1536x864, "bad_size" — валидный PNG неверного размера.
    """
    G.GOOGLER_MODEL = "GPT_IMAGE2"
    G.OPENAI_API_KEY = "sk-test-key"
    G._JOURNAL_SINGLETON = None
    G._gen_attempts = lambda default: 1     # без двадцати попыток по минуте

    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        if model_body == "bad_size":
            return FakeResponse(200, {"data": [{"b64_json": png_b64(100, 100)}]})
        return FakeResponse(200, {"data": [{"b64_json": png_b64(1536, 864)}]})

    G.requests.post = fake_post
    return captured


def main() -> int:
    saved_model = G.GOOGLER_MODEL
    saved_key = G.OPENAI_API_KEY
    saved_journal = G._JOURNAL_SINGLETON
    saved_attempts = G._gen_attempts
    saved_post = G.requests.post
    saved_print_user = G.print_user

    print("Константа модели")
    check("по умолчанию — sunburst", worker_constant("GPT_IMAGE_MODEL"),
          "gpt-image-2.5-sunburst")
    check("VS_GPT_IMAGE_MODEL переопределяет",
          worker_constant("GPT_IMAGE_MODEL", {"VS_GPT_IMAGE_MODEL": "gpt-image-2.5-flare"}),
          "gpt-image-2.5-flare")

    print("\nКлюч модели и список моделей — не переименованы")
    check("GPT_IMAGE2 остался ключом в списке доступных моделей движка",
          "GPT_IMAGE2" in G.GOOGLER_AVAILABLE_MODELS, True)
    check("подпись движка обновлена до GPT Image 2.5",
          G.GOOGLER_MODEL_LABELS["GPT_IMAGE2"], "GPT Image 2.5 low (💲платно)")

    print("\nРазмеры не тронуты (проверить поддержку новой моделью нечем без платного вызова)")
    check("_GPT_IMAGE2_EXACT_SIZES", G._GPT_IMAGE2_EXACT_SIZES, {
        "landscape": ("1536x864", (1536, 864)),
        "portrait": ("864x1536", (864, 1536)),
        "square": ("1024x1024", (1024, 1024)),
    })

    print("\nКабинет: подпись в IMAGE_MODELS")
    entry = cabinet_image_models_entry("GPT_IMAGE2")
    check("подпись кабинета — GPT Image 2.5", entry, "('GPT Image 2.5', 'платно · $', 'openai')")

    try:
        print("\nЗапрос провайдеру уходит с константой, а не зашитой строкой")
        captured = install("ok")
        idx, data = G.generate_image(
            {"visual_prompt": "маяк в тумане", "camera_angle": "Wide Shot"},
            style="кинематографично", output_path=None, index=0)
        check("кадр получен", bool(data), True)
        check("в теле запроса — GPT_IMAGE_MODEL", captured["json"]["model"], G.GPT_IMAGE_MODEL)
        check("не зашитая устаревшая модель", captured["json"]["model"] != "gpt-image-2", True)

        print("\nОшибка «нет ключа» названа новой моделью")
        msgs = []
        G.print_user = lambda msg, *a, **kw: msgs.append(msg)
        G.OPENAI_API_KEY = None
        idx, data = G.generate_image(
            {"visual_prompt": "маяк в тумане", "camera_angle": "Wide Shot"},
            style="кинематографично", output_path=None, index=0)
        check("кадра нет", data, None)
        joined = "\n".join(msgs)
        check("сообщение про GPT Image 2.5", "GPT Image 2.5" in joined, True)
        check("а не про старую GPT-Image-2", "GPT-Image-2" in joined, False)

        print("\nОшибка «неверное разрешение» тоже названа новой моделью")
        G.OPENAI_API_KEY = "sk-test-key"
        install("bad_size")
        msgs.clear()
        idx, data = G.generate_image(
            {"visual_prompt": "маяк в тумане", "camera_angle": "Wide Shot"},
            style="кинематографично", output_path=None, index=0)
        check("кадр отклонён по размеру", data, None)
        joined = "\n".join(msgs)
        check("причина — неверное разрешение", "неверное разрешение" in joined, True)
        check("сообщение про GPT Image 2.5", "GPT Image 2.5" in joined, True)
    finally:
        G.GOOGLER_MODEL = saved_model
        G.OPENAI_API_KEY = saved_key
        G._JOURNAL_SINGLETON = saved_journal
        G._gen_attempts = saved_attempts
        G.requests.post = saved_post
        G.print_user = saved_print_user

    print("\nВыхлоп рерайта — в .gitignore, а не в репозитории (§7)")
    gitignore = open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read()
    check("outputs/ в .gitignore",
          "studio-platform/rewrite/outputs/" in gitignore, True)
    check("uploads/ в .gitignore",
          "studio-platform/rewrite/uploads/" in gitignore, True)
    check("старый файл выхлопа outputs удалён из рабочего дерева",
          os.path.exists(os.path.join(BASE, "rewrite", "outputs", "dev",
                                       "72fdfbf0a8c7_pl.txt")), False)
    check("старый файл выхлопа uploads удалён из рабочего дерева",
          os.path.exists(os.path.join(BASE, "rewrite", "uploads", "dev",
                                       "вставленный.txt")), False)

    # git check-ignore сверяет путь-пробу с паттернами .gitignore, не заглядывая на диск —
    # каталогов rewrite/outputs и rewrite/uploads в свежем клоне ещё нет (их создаёт только
    # импорт rewrite/server.py), поэтому проверка не должна их создавать сама.
    probe_outputs = os.path.join(BASE, "rewrite", "outputs", "dev", "probe.txt")
    chk = subprocess.run(["git", "check-ignore", "-q", probe_outputs], cwd=ROOT)
    check("путь в rewrite/outputs игнорируется git", chk.returncode, 0)
    probe_uploads = os.path.join(BASE, "rewrite", "uploads", "dev", "probe.txt")
    chk = subprocess.run(["git", "check-ignore", "-q", probe_uploads], cwd=ROOT)
    check("путь в rewrite/uploads игнорируется git", chk.returncode, 0)

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
