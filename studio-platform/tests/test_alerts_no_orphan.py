#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Цикл тревог не переживает свой стенд.

21.09.2026 владелец написал: «можешь ещё все студии поубивать, а то они мне в телеграм
спамят сильно». Оказалось, одновременно работали ДВАДЦАТЬ ТРИ цикла `dev/alerts.py
--loop`: каждый запуск стенда заводит свой, и все они пережили свои стенды.

`trap 'kill 0' EXIT` в dev/run.sh спасает только при Ctrl+C. Если стенд гасят по имени
процесса (`pkill -f "port 8084"` — именно так их гасил я сам весь день), умирает один
uvicorn, а цикл остаётся сиротой и продолжает слать тревоги каждую минуту. При `kill -9`
родителя trap не сработает тем более.

Поэтому цикл следит за родителем сам и выходит, оставшись без него. Тест проверяет
именно тяжёлый случай — `kill -9`, где на уборку родителем рассчитывать нельзя.

Сети и телеграма тут нет: токен пуст, тревоги определяются, но не отправляются.

    .venv/bin/python tests/test_alerts_no_orphan.py
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
PY = os.path.join(BASE, ".venv", "bin", "python")

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main() -> int:
    print("Проверка: цикл тревог умирает вместе со стендом")
    if not os.path.exists(PY):
        print("  ПРОПУЩЕНО: нет .venv")
        return 2
    src = os.path.join(BASE, "dev", "vsgen.dev.db")
    if not os.path.exists(src):
        print("  ПРОПУЩЕНО: нет базы стенда")
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "alerts.db")
        # Копия через backup API: простое копирование файла при живом WAL даёт
        # «database disk image is malformed» — на этом уже спотыкались.
        s, d = sqlite3.connect(src), sqlite3.connect(db)
        s.backup(d); d.close(); s.close()

        env = dict(os.environ, VS_DEV_DB=db, VS_SERVER_DB=db,
                   VS_TG_TOKEN="", VS_TG_CHAT="")
        # Промежуточная оболочка играет роль стенда: её и убьём жёстко.
        parent = subprocess.Popen(
            ["/bin/sh", "-c",
             f'"{PY}" "{os.path.join(BASE, "dev", "alerts.py")}" --loop --every 5 '
             f'>/dev/null 2>&1 & echo $!; sleep 120'],
            cwd=BASE, env=env, stdout=subprocess.PIPE, text=True)
        try:
            child = int((parent.stdout.readline() or "0").strip())
            time.sleep(6)
            check("цикл запустился", child > 0 and alive(child), True)

            parent.kill()          # kill -9: никакой trap тут не поможет
            parent.wait(timeout=10)

            gone = False
            for _ in range(24):    # цикл просыпается раз в 5 с, даём запас
                if not alive(child):
                    gone = True
                    break
                time.sleep(1)
            check("после жёсткого убийства родителя цикл ушёл сам", gone, True)
        finally:
            try:
                if child:
                    os.kill(child, 9)
            except Exception:                                      # noqa: BLE001
                pass
            try:
                parent.kill()
            except Exception:                                      # noqa: BLE001
                pass

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
