#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Недокачанное тело рендера не остаётся на диске навсегда — и настройка не роняет импорт.

Дефект 1 (Issue 5 ревью). Приём результата рендера пишет тело в `render/.incoming-XXXX`
и убирает его в `finally` своего запроса. `finally` закрывает штатные исходы — 400, 409,
обрыв соединения. Он не закрывает неШТАТНЫЙ: SIGKILL при деплое, падение процесса,
OOM-killer. Тогда файл остаётся навсегда, и это худший вид мусора — невидимый: имя
начинается с точки, поэтому огрызок не виден ни `find_video` (он ищет `*.mp4`), ни
подсчёту занятого места, ни уборке исходников (она ходит по `Images`/`googler`/озвучкам
и в `render/` не заходит вовсе). Несколько прерванных заливок двухчасового ролика — это
десятки гигабайт, которых нет ни в одном отчёте.

Дефект 2 (Issue 6 ревью). `VS_RENDER_MAX_BYTES` читался как
`int(os.environ.get(..., умолчание))`. В .env переменная живёт строкой
`VS_RENDER_MAX_BYTES=` — значение закомментировали, строку оставили, — и тогда getenv
отдаёт пустую строку, а не умолчание. `int("")` на импорте = кабинет не поднимается
целиком из-за настройки одной ручки. Проверяется настоящим импортом в подпроцессе:
внутри уже запущенного процесса такую поломку воспроизвести нечем.

Кабинет здесь настоящий, подменён только каталог проектов.

    .venv/bin/python tests/test_render_incoming.py
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="render-incoming-")

os.environ["VS_SERVER_DISABLE_BACKGROUND"] = "1"
os.environ["VS_SERVER_DB"] = os.path.join(TMP, "vsgen.db")
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
os.environ["PULLER_TOKEN"] = "test-puller-token"
os.environ.setdefault("HOME", os.path.join(BASE, "dev", "home"))
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "workers"))

import main as M                                                  # noqa: E402
import pub_publish                                                # noqa: E402

FAILS = []
TID = "dev"
HOUR = 3600.0


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def render_dir(pid):
    path = os.path.join(M._project_dir(TID, pid), "render")
    os.makedirs(path, exist_ok=True)
    return path


def orphan(pid, age_sec, size=4096, directory=None):
    """Огрызок заливки нужного возраста. Возвращает путь."""
    where = directory or render_dir(pid)
    os.makedirs(where, exist_ok=True)
    fh, path = M._render_staged_file(where)
    with fh:
        fh.write(b"m" * size)
    stamp = time.time() - age_sec
    os.utime(path, (stamp, stamp))
    return path


def alive(path):
    return os.path.exists(path)


def child_import(env_extra):
    """Импорт кабинета отдельным процессом с заданным окружением: rc и хвост stderr."""
    env = dict(os.environ)
    env.update(env_extra)
    env["VS_SERVER_DB"] = os.path.join(TMP, "child.db")
    code = ("import sys; sys.path.insert(0, %r); sys.path.insert(0, %r); "
            "import main; print('RENDER_MAX_BYTES=%%d' %% main.RENDER_MAX_BYTES); "
            "print('INCOMING=%%d' %% int(main.RENDER_INCOMING_MAX_AGE_SEC))"
            % (os.path.join(BASE, "server"), os.path.join(BASE, "workers")))
    done = subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def main() -> int:
    try:
        print("Огрызок прерванной заливки не виден ничему, что смотрит в render/")

        # Это и есть причина, по которой ему нужна СВОЯ уборка: ни один существующий
        # обход его не находит, а место он занимает настоящее.
        pid = "p_hidden"
        stray = orphan(pid, age_sec=48 * HOUR, size=8192)
        check("в render/ он лежит", alive(stray), True)
        check("а glob по *.mp4 его не видит",
              glob.glob(os.path.join(render_dir(pid), "*.mp4")), [])
        check("и find_video тоже", pub_publish.find_video(M._project_dir(TID, pid)), "")

        print("\nУборка сносит брошенные и не трогает свежие")

        old_one = orphan("p_age", age_sec=48 * HOUR, size=8192)
        fresh_one = orphan("p_age", age_sec=60.0, size=8192)
        report = M._sweep_incoming_renders()
        check("брошенный убран", alive(old_one), False)
        check("свежий (в него, может, прямо сейчас пишут) цел", alive(fresh_one), True)
        check("убран и тот, которого никто не видел", alive(stray), False)
        check("посчитано удалённых", report["removed"], 2)
        check("и освобождённые байты названы", report["freed_bytes"], 16384)

        # Порог — настройка, а не константа: доказываем, что он действительно решает.
        check("с порогом в минуту уходит и свежий",
              M._sweep_incoming_renders(max_age_sec=30.0)["removed"], 1)
        check("теперь пусто", alive(fresh_one), False)

        print("\nГраницы: только render/ проекта, только обычный файл")

        # Тот же префикс в корне проекта — не наш огрызок: мы кладём его строго в
        # render/. Сносить всё похожее по имени где попало — способ удалить чужое.
        pid = "p_bounds"
        outside = orphan(pid, age_sec=48 * HOUR, directory=M._project_dir(TID, pid))
        inside = orphan(pid, age_sec=48 * HOUR)
        check("уборка взяла ровно один файл",
              M._sweep_incoming_renders()["removed"], 1)
        check("в render/ — убран", alive(inside), False)
        check("в корне проекта — не тронут", alive(outside), True)

        # Симлинк под таким именем — не наш файл: мы создаём обычный. Пройти по нему
        # значит удалить то, на что он указывает.
        victim = os.path.join(TMP, "чужое.mp4")
        with open(victim, "wb") as fh:
            fh.write(b"x" * 1024)
        link = os.path.join(render_dir("p_link"), ".incoming-подмена")
        os.symlink(victim, link)
        os.utime(link, (time.time() - 48 * HOUR, time.time() - 48 * HOUR),
                 follow_symlinks=False)
        check("симлинк не удаляется", M._sweep_incoming_renders()["removed"], 0)
        check("сам симлинк на месте", os.path.islink(link), True)
        check("и файл, на который он смотрит, цел", alive(victim), True)
        os.unlink(link)

        print("\nГотовый ролик уборка не трогает — она знает только про префикс")

        pid = "p_result"
        done_mp4 = os.path.join(render_dir(pid), "77.mp4")
        with open(done_mp4, "wb") as fh:
            fh.write(b"v" * 4096)
        os.utime(done_mp4, (time.time() - 90 * 24 * HOUR,) * 2)
        check("ничего не убрано", M._sweep_incoming_renders()["removed"], 0)
        check("ролик на месте", alive(done_mp4), True)

        print("\nПустая переменная в .env больше не роняет импорт кабинета")

        # Старое поведение: int("") → ValueError на уровне модуля, то есть НЕ поднимается
        # вообще ничего — ни /healthz, ни админка, ни очередь.
        rc, out = child_import({"VS_RENDER_MAX_BYTES": ""})
        check("пустой VS_RENDER_MAX_BYTES: импорт прошёл", rc, 0)
        check("и взято умолчание",
              "RENDER_MAX_BYTES=%d" % (64 * 1024 ** 3) in out, True)
        rc, out = child_import({"VS_RENDER_INCOMING_MAX_AGE_SEC": ""})
        check("пустой VS_RENDER_INCOMING_MAX_AGE_SEC: импорт прошёл", rc, 0)
        check("и тоже умолчание", "INCOMING=21600" in out, True)
        rc, out = child_import({"VS_RENDER_MAX_BYTES": "  4096  ",
                                "VS_RENDER_INCOMING_MAX_AGE_SEC": "900"})
        check("заданные значения читаются", rc, 0)
        check("потолок mp4", "RENDER_MAX_BYTES=4096" in out, True)
        check("порог огрызков", "INCOMING=900" in out, True)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

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
