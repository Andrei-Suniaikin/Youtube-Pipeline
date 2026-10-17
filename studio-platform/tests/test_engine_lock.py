#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Замок движка: фон поднимается ровно в одном процессе (shared/engine_lock.py).

Зачем. Выкатка без простоя (deploy/BLUE_GREEN.md) держит рядом два экземпляра студии
с одной базой. На старте каждое подприложение возвращает «running»-задачи в очередь —
после падения это правильно, а при живом соседе это двойной запуск за двойные деньги.
Замок `flock` решает, кто ведёт фон; второй ждёт и забирает замок, когда первый вышел.

Проверяется:
  1. без STUDIO_ENGINE_LOCK замок выключен — функция выполняется сразу (прежнее поведение);
  2. свободный замок берётся сразу, занятый — ждёт, и очередь отложенных выполняется по
     порядку после освобождения; упавшая функция не мешает следующей;
  3. между ПРОЦЕССАМИ: пока замок держит другой процесс, мы standby; вышел — мы owner.
     Именно так и живут два uvicorn'а;
  4. дочерний процесс владельца (воркер через subprocess) замок НЕ уносит: после выхода
     владельца замок свободен, хоть его воркер ещё работает;
  5. кабинет (`server/main.py`): при чужом замке `/healthz` говорит scheduler «standby» и
     отдаёт ok — иначе выкатка сочла бы новый экземпляр мёртвым до передачи движка.

    VS_ALERTS=0 .venv/bin/python tests/test_engine_lock.py
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
BASE = HERE.parent
TMP = tempfile.mkdtemp(prefix="engine-lock-")
LOCK_PATH = os.path.join(TMP, "engine.lock")

_spec = importlib.util.spec_from_file_location("engine_lock_under_test", BASE / "shared" / "engine_lock.py")
EL = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(EL)

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def wait_until(pred, timeout=5.0, step=0.02):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(step)
    return pred()


# Чужой процесс, который держит замок, пока ему не скажут выйти (закрытие stdin).
HOLDER_SRC = r"""
import sys, os, fcntl
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
os.ftruncate(fd, 0); os.write(fd, b"holder pid=%d\n" % os.getpid())
sys.stdout.write("held\n"); sys.stdout.flush()
sys.stdin.readline()          # ждём команду выйти
"""


def start_holder(path):
    p = subprocess.Popen([sys.executable, "-c", HOLDER_SRC, path], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True)
    line = p.stdout.readline()
    assert line.strip() == "held", line
    return p


def stop_holder(p):
    try:
        p.stdin.write("quit\n"); p.stdin.flush()
    except OSError:
        pass
    p.wait(timeout=10)


def test_disabled():
    print("\n1. Без STUDIO_ENGINE_LOCK замок выключен, фон стартует сразу")
    lk = EL.EngineLock("", "x", log=lambda s: None)
    ran = []
    now = lk.run_when_owner("фон", lambda: ran.append(1))
    check("выполнено сразу", (now, ran), (True, [1]))
    check("состояние off", lk.state(), "off")
    check("is_owner — да (как раньше)", lk.is_owner(), True)
    check("is_standby — нет", lk.is_standby(), False)


def test_in_process_queue():
    print("\n2. Свободный берётся сразу, занятый — ждёт; очередь по порядку, падение не мешает")
    path = os.path.join(TMP, "q.lock")
    first = EL.EngineLock(path, "a", poll=0.05, log=lambda s: None)
    ran = []
    check("первый — сразу", first.run_when_owner("a-фон", lambda: ran.append("a")), True)
    check("первый — owner", first.state(), "owner")
    check("в файле — кто держит", first.holder().startswith("a pid="), True)

    # Второй замок на тот же файл — другое открытие файла, flock их различает даже в
    # одном процессе; этого хватает, чтобы проверить очередь без второго интерпретатора.
    second = EL.EngineLock(path, "b", poll=0.05, log=lambda s: None)

    def boom():
        raise RuntimeError("нарочно")

    check("второй — отложен", second.run_when_owner("b-1", lambda: ran.append("b1")), False)
    second.run_when_owner("b-падает", boom)
    second.run_when_owner("b-2", lambda: ran.append("b2"))
    check("второй — standby", second.state(), "standby")
    check("у второго в очереди трое", second.status()["pending"], ["b-1", "b-падает", "b-2"])
    time.sleep(0.2)
    check("пока первый жив — второй ничего не запустил", ran, ["a"])

    first.release()
    check("после освобождения второй забрал и выполнил по порядку",
          wait_until(lambda: ran == ["a", "b1", "b2"]), True)
    check("второй — owner", wait_until(lambda: second.state() == "owner"), True)
    check("упавшая функция не записана в started, остальные записаны",
          second.status()["started"], ["b-1", "b-2"])
    check("очередь пуста", second.status()["pending"], [])
    second.release()


def test_cross_process():
    print("\n3. Между процессами: чужой держит — standby; вышел — owner")
    holder = start_holder(LOCK_PATH)
    lk = EL.EngineLock(LOCK_PATH, "new", poll=0.05, log=lambda s: None)
    ran = []
    check("при чужом замке — отложено", lk.run_when_owner("фон", lambda: ran.append(1)), False)
    check("состояние standby", lk.state(), "standby")
    check("holder показывает чужой pid", lk.holder().startswith("holder pid="), True)
    time.sleep(0.3)
    check("фон не запущен, пока чужой жив", ran, [])
    stop_holder(holder)
    check("чужой вышел — фон запущен", wait_until(lambda: ran == [1]), True)
    check("теперь owner", lk.state(), "owner")
    check("в файле теперь мы", lk.holder().startswith("new pid="), True)
    lk.release()


def test_child_does_not_inherit():
    print("\n4. Дочерний процесс владельца замок не уносит (воркеры через subprocess)")
    path = os.path.join(TMP, "child.lock")
    owner = EL.EngineLock(path, "owner", poll=0.05, log=lambda s: None)
    check("владелец взял", owner.try_acquire(), True)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        owner.release()
        other = EL.EngineLock(path, "other", poll=0.05, log=lambda s: None)
        check("после выхода владельца замок свободен, хоть его воркер жив",
              other.try_acquire(), True)
        other.release()
    finally:
        child.kill(); child.wait()


def test_cabinet_healthz_standby():
    print("\n5. Кабинет при чужом замке: /healthz — scheduler «standby», ok")
    holder = start_holder(LOCK_PATH)
    try:
        db_src = BASE / "dev" / "vsgen.dev.db"
        db = os.path.join(TMP, "vsgen.db")
        if db_src.exists():
            shutil.copy(db_src, db)
        os.environ["STUDIO_ENGINE_LOCK"] = LOCK_PATH
        os.environ["STUDIO_ENGINE_LOCK_POLL"] = "0.05"
        os.environ["STUDIO_INSTANCE"] = "b"
        os.environ["VS_SERVER_DISABLE_BACKGROUND"] = ""          # фон ВКЛЮЧЁН, как в бою
        os.environ["VS_SERVER_DB"] = db
        os.environ["VS_DEV_DB"] = db
        os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
        os.environ.setdefault("HOME", str(BASE / "dev" / "home"))
        sys.path.insert(0, str(BASE / "server"))
        sys.path.insert(0, str(BASE / "workers"))
        import main as M                                          # noqa: PLC0415
        check("кабинет видит, что фон включён", M.BACKGROUND_ENABLED, True)
        check("замок кабинета — standby", M.engine_lock.LOCK.state(), "standby")
        h = M._sched_health()
        check("scheduler.state = standby", h["state"], "standby")
        check("scheduler.ok = True (это не поломка)", h["ok"], True)
        from fastapi.testclient import TestClient                  # noqa: PLC0415
        with TestClient(M.app) as c:                               # lifespan: фон отложен, замок чужой
            r = c.get("/healthz")
            check("/healthz отвечает 200", r.status_code, 200)
            body = r.json()
            check("в ответе engine_lock = standby", body["scheduler"]["engine_lock"], "standby")
            check("поток планировщика НЕ поднят при чужом замке", body["scheduler"]["thread_alive"], False)
            check("фон кабинета стоит в очереди замка",
                  M.engine_lock.status()["pending"],
                  ["кабинет: движок выпусков, планировщик, пул воркеров"])
            # Чужой вышел → замок наш → фон поднимается сам, без перезапуска.
            stop_holder(holder)
            check("после выхода соседа — owner", wait_until(lambda: M.engine_lock.LOCK.state() == "owner"), True)
            check("планировщик поднялся сам",
                  wait_until(lambda: M._sched_health()["thread_alive"], timeout=10), True)
            r = c.get("/healthz")
            check("/healthz после передачи — 200", r.status_code, 200)
            check("engine_lock = owner", r.json()["scheduler"]["engine_lock"], "owner")
    finally:
        if holder.poll() is None:
            stop_holder(holder)


if __name__ == "__main__":
    test_disabled()
    test_in_process_queue()
    test_cross_process()
    test_child_does_not_inherit()
    test_cabinet_healthz_standby()
    print()
    if FAILS:
        print(f"ПРОВАЛОВ: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("Все проверки прошли")
    # Фон кабинета уже поднят (daemon-потоки) — выходим явно, не дожидаясь их.
    # os._exit не сбрасывает буферы, а вывод при запуске через конвейер буферизован.
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(0)
