#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""project_stage_status(st) — статус проекта из пяти этапов, батч C «экран проекта», Ф1.

Проверяет:
1. Что чистая функция `project_stage_status` даёт ожидаемые segments/caption/tone на наборе
   входов (пустой проект, только текст, идут картинки N/M, ошибка рендера, готово — плюс
   варианты с непустыми running/queued/errored, включая приоритет выбора причины при
   нескольких одновременных running/errored/queued этапах и картинки без известного m).
2. Что `mc_stepper(st)` после рефакторинга рисует БАЙТ-В-БАЙТ ту же HTML-строку, что рисовал
   дореформенный алгоритм на тех же входах — сравнение идёт с хардкод-копией старой логики
   прямо в этом файле (не импортом из main.py), чтобы тест не обесценился после рефакторинга.
   Копия снята с `git show b50266c:studio-platform/server/main.py` (последний коммит перед
   батчем C) — тело функции mc_stepper строка в строку.
3. Что категория `covers` в `_DL_CATS`/`_DL_PATTERNS` реально отдаёт `cover.jpg` через тот же
   путь данных, которым пользуется ручка скачивания кабинета `GET /api/projects/{pid}/download/
   covers` (`_dl_items` + `_zip_stream`) — без подъёма стенда и HTTP, но по тем же функциям,
   которые вызывает сама ручка (main.py:5642, project_download).

Без стенда, без сети: только импорт main тем же способом, что и tests/test_preset_links.py.

    .venv/bin/python tests/test_project_stage_status.py
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
os.environ.setdefault("VS_SERVER_DISABLE_BACKGROUND", "1")
os.environ.setdefault("VS_SERVER_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
os.environ.setdefault("VS_TENANTS_DIR", os.path.join(BASE, "dev", "data", "tenants"))
# Кабинет тянет движки по зашитому пути ~/VideoStudio/src — тот же изолированный HOME,
# что и у dev/run.sh. Без него импорт main.py падает на googler_worker.
os.environ.setdefault("HOME", os.path.join(BASE, "dev", "home"))
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "workers"))

import main as M                                                  # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def _old_mc_stepper(st: dict) -> str:
    """Дореформенный mc_stepper — снят с git show b50266c:studio-platform/server/main.py
    (строка в строку, только переименование не делалось). Эталон для сравнения побайтово."""
    running = st.get("running") or set()
    queued = st.get("queued") or set()
    errored = st.get("errored") or set()
    n = st.get("images", 0); m = st.get("images_expected") or 0
    has_clips = bool(st.get("video"))
    has_render = bool(st.get("render"))
    img_done = n > 0 and (m == 0 or n >= m)

    def seg(stage, done):
        if stage in running:
            return "running"
        if stage in queued:
            return "queued"
        if stage in errored:
            return "error"
        return "done" if done else "pending"
    s_text = seg("text", st.get("text"))
    s_audio = seg("tts", st.get("audio"))
    s_img = seg("images", img_done)
    s_vid = seg("video", has_clips or has_render)
    s_render = seg("render", has_render)

    _LBL = {"tts": "озвучка", "prompts": "промпты", "images": "картинки", "video": "видео", "render": "рендер"}
    if running:
        parts = []
        for k in ("tts", "prompts", "images", "video", "render"):
            if k in running:
                parts.append((f"картинки {n}/{m}" if m else f"картинки {n}") if k == "images" else _LBL[k])
        cap, col = ("Идёт: " + ", ".join(parts) if parts else "Идёт обработка"), "var(--running)"
    elif errored:
        _ek = next((k for k in ("render", "video", "images", "tts") if k in errored), None)
        cap = {"tts": "Ошибка озвучки", "images": "Ошибка картинок",
               "video": "Ошибка видео", "render": "Ошибка рендера"}.get(_ek, "Ошибка")
        col = "var(--error)"
    elif queued:
        parts = [_LBL[k] for k in ("tts", "images", "video", "render") if k in queued]
        cap, col = ("В очереди: " + ", ".join(parts) if parts else "В очереди"), "var(--queued)"
    elif has_render:
        cap, col = "Готово", "var(--done)"
    elif has_clips or img_done:
        cap, col = "Ждёт рендер", "var(--muted)"
    elif n > 0:
        cap, col = "Ждёт видео", "var(--muted)"
    elif st.get("audio"):
        cap, col = "Ждёт картинки", "var(--muted)"
    elif st.get("text"):
        cap, col = "Только текст", "var(--dim)"
    else:
        cap, col = "Пусто", "var(--dim)"
    segs = "".join(f'<span class="seg {c}"></span>' for c in (s_text, s_audio, s_img, s_vid, s_render))
    return f'<div class="stepper">{segs}</div><div class="stepper-cap" style="color:{col}">{cap}</div>'


# (метка, вход st, ожидаемые segments (5 штук в порядке text,tts,images,video,render), caption, tone)
CASES = [
    ("пустой проект", {},
     ["pending", "pending", "pending", "pending", "pending"], "Пусто", "dim"),

    ("только текст", {"text": True},
     ["done", "pending", "pending", "pending", "pending"], "Только текст", "dim"),

    ("идут картинки 38/60 (running)", {
        "text": True, "audio": True, "images": 38, "images_expected": 60,
        "running": {"images"},
    }, ["done", "done", "running", "pending", "pending"], "Идёт: картинки 38/60", "running"),

    ("ошибка рендера (errored)", {
        "text": True, "audio": True, "images": 60, "images_expected": 60, "video": True,
        "render": False, "errored": {"render"},
    }, ["done", "done", "done", "done", "error"], "Ошибка рендера", "error"),

    ("готово", {
        "text": True, "audio": True, "images": 60, "images_expected": 60, "video": True,
        "render": True,
    }, ["done", "done", "done", "done", "done"], "Готово", "done"),

    ("озвучка и картинки в очереди (queued)", {
        "text": True, "queued": {"tts", "images"},
    }, ["done", "queued", "queued", "pending", "pending"], "В очереди: озвучка, картинки", "queued"),

    ("ошибка картинок (errored, не render)", {
        "text": True, "errored": {"images"},
    }, ["done", "pending", "error", "pending", "pending"], "Ошибка картинок", "error"),

    ("ждёт картинки (есть озвучка, картинок нет)", {
        "text": True, "audio": True,
    }, ["done", "done", "pending", "pending", "pending"], "Ждёт картинки", "muted"),

    # --- дополнительные сценарии тестера: приоритет причины при нескольких этапах разом ---

    ("running сразу tts и render — порядок в подписи как в цикле tts,prompts,images,video,render", {
        "text": True, "running": {"render", "tts"},
    }, ["done", "running", "pending", "pending", "running"], "Идёт: озвучка, рендер", "running"),

    ("running картинки без известного m (m=0) — подпись без /M", {
        "text": True, "audio": True, "images": 5, "images_expected": 0,
        "running": {"images"},
    }, ["done", "done", "running", "pending", "pending"], "Идёт: картинки 5", "running"),

    ("errored сразу render и tts — приоритет причины: render, а не tts", {
        "text": True, "audio": True, "errored": {"render", "tts"},
    }, ["done", "error", "pending", "pending", "error"], "Ошибка рендера", "error"),

    ("errored только video", {
        "text": True, "audio": True, "images": 10, "images_expected": 10,
        "errored": {"video"},
    }, ["done", "done", "done", "error", "pending"], "Ошибка видео", "error"),

    ("errored только tts", {
        "text": True, "errored": {"tts"},
    }, ["done", "error", "pending", "pending", "pending"], "Ошибка озвучки", "error"),

    ("queued только render", {
        "text": True, "audio": True, "images": 10, "images_expected": 10, "video": True,
        "queued": {"render"},
    }, ["done", "done", "done", "done", "queued"], "В очереди: рендер", "queued"),

    ("есть клипы, но рендера и картинок нет (video без render/images) — «ждёт рендер»", {
        "text": True, "audio": True, "video": True,
    }, ["done", "done", "pending", "done", "pending"], "Ждёт рендер", "muted"),

    ("running и errored одновременно — running выигрывает приоритет (running проверяется первым)", {
        "text": True, "running": {"tts"}, "errored": {"images"},
    }, ["done", "running", "error", "pending", "pending"], "Идёт: озвучка", "running"),
]

STAGES = ("text", "tts", "images", "video", "render")


def main() -> int:
    for label, st, want_states, want_caption, want_tone in CASES:
        got = M.project_stage_status(dict(st))
        got_states = [x["state"] for x in got["segments"]]
        got_stages = [x["stage"] for x in got["segments"]]
        check(f"{label}: порядок этапов", got_stages, list(STAGES))
        check(f"{label}: segments", got_states, want_states)
        check(f"{label}: caption", got["caption"], want_caption)
        check(f"{label}: tone", got["tone"], want_tone)

        # побайтовое сравнение HTML с дореформенным алгоритмом на том же входе
        old_html = _old_mc_stepper(dict(st))
        new_html = M.mc_stepper(dict(st))
        check(f"{label}: mc_stepper байт-в-байт как до рефакторинга", new_html, old_html)

    print("\nКатегория covers: словари")
    check("_DL_CATS['covers'] — человеческое название",
          M._DL_CATS.get("covers"), "обложки")
    check("_DL_PATTERNS['covers'] — cover.jpg без подкаталогов",
          M._DL_PATTERNS.get("covers"), ((("cover.jpg",), False),))

    print("\nКатегория covers: реально отдаётся тем же путём данных, что и ручка скачивания "
          "кабинета GET /api/projects/{pid}/download/covers (main.py:5642, project_download "
          "зовёт _dl_items(pdir, what) + _zip_stream(items) — оба вызваны здесь напрямую, без "
          "стенда/HTTP, поправка 3 оркестратора)")
    with tempfile.TemporaryDirectory() as pdir:
        # проекта без cover.jpg — категория пуста, ручка отдала бы 404 «скачивать нечего»
        # (main.py:5656-5658), поведение как у остальных категорий (edge case §7.6 спеки)
        empty_items = M._dl_items(pdir, "covers")
        check("нет cover.jpg на диске — _dl_items(covers) пуст", empty_items, [])

        cover_bytes = b"\xff\xd8\xff\xe0" + b"proba-jpeg-bytes" * 100  # не настоящий jpeg, но байты узнаваемы
        with open(os.path.join(pdir, "cover.jpg"), "wb") as f:
            f.write(cover_bytes)
        # рядом — файл другой категории и подкаталог, чтобы убедиться, что шаблон
        # ((("cover.jpg",), False),) не подхватывает лишнее (без рекурсии, ровно один файл)
        os.mkdir(os.path.join(pdir, "sub"))
        with open(os.path.join(pdir, "sub", "cover.jpg"), "wb") as f:
            f.write(b"chuzhoi vlozhenny cover.jpg - ne dolzhen popast")
        with open(os.path.join(pdir, "cover.png"), "wb") as f:
            f.write(b"ne tot format - ne dolzhen popast")

        items = M._dl_items(pdir, "covers")
        check("_dl_items(covers) находит ровно один файл — верхнеуровневый cover.jpg",
              [arc for _full, arc in items], ["cover.jpg"])
        full_path, _arc = items[0]
        check("путь ведёт на настоящий верхнеуровневый cover.jpg",
              os.path.realpath(full_path), os.path.realpath(os.path.join(pdir, "cover.jpg")))

        # тот же generator, которым ручка project_download стримит zip клиенту
        zip_bytes = b"".join(M._zip_stream([(full_path, "проект/cover.jpg")]))
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        check("zip содержит ровно один файл", zf.namelist(), ["проект/cover.jpg"])
        check("байты обложки в zip совпадают с исходным файлом на диске",
              zf.read("проект/cover.jpg"), cover_bytes)

        # категория covers участвует в общем списке скачиваемых категорий кабинета
        check("'covers' объявлена в _DL_CATS (её видит project_download при проверке "
              "what not in _DL_CATS)", "covers" in M._DL_CATS, True)

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
