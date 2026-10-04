#!/usr/bin/env python3
"""Сквозная проверка: движок ↔ кабинет ↔ журнал платных вызовов.

Требует поднятого кабинета (dev/run.sh). Провайдеров не трогает: движок здесь
изображается клиентом call_journal, а «вызов к провайдеру» — заглушкой.

    .venv/bin/python tests/test_calls_api.py
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "workers"))

from call_journal import CallJournal  # noqa: E402

DB = os.environ.get("VS_DEV_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
URL = os.environ.get("VS_CALLS_URL", "http://127.0.0.1:8080")
PROJECT = "proj-dev-0001"

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def db():
    c = sqlite3.connect(DB, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def make_running_job(kind="images") -> tuple:
    """Живая задача в очереди с токеном — от её имени и пишет движок.

    Номер берётся так же, как в `_enqueue` кабинета: максимум по `gen_jobs` и
    `queue_tasks` плюс парная строка в `gen_jobs`. Раньше строка вставлялась в одну
    `queue_tasks` без `claimed`: работающий планировщик сразу забирал её как «зависшую»
    и писал в `job_logs`, а после чистки тот же номер получала настоящая задача и
    дописывала свой лог к чужим строкам (render #15 на dev-стенде, 16.09.2026).
    `claimed` = сейчас — чтобы планировщик не считал задачу зависшей, пока идёт тест.
    """
    token = secrets.token_hex(16)
    now = time.time()
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        nxt = c.execute("SELECT MAX(v) FROM (SELECT MAX(rowid) v FROM gen_jobs "
                        "UNION ALL SELECT MAX(job_id) FROM queue_tasks)").fetchone()[0]
        jid = int(nxt or 0) + 1
        c.execute("INSERT INTO gen_jobs(rowid,user_id,type,status,ts,title) "
                  "VALUES(?,1,?,'running',?,?)", (jid, kind, now, f"test:{kind}:{PROJECT}"))
        c.execute(
            "INSERT INTO queue_tasks(job_id,tenant_id,project_id,kind,status,created,params,uid,"
            "claim_token,claimed,provider) VALUES(?,'dev',?,?,'running',?,'{}',1,?,?,'recrafter')",
            (jid, PROJECT, kind, now, token, now))
        c.commit()
        return jid, token
    finally:
        c.close()


def cleanup(jid):
    """Убрать задачу целиком: строку очереди, парную `gen_jobs` и её лог. Лог без задачи
    — это ровно те осиротевшие строки, которые потом прилипают к задаче с тем же номером."""
    c = db()
    try:
        c.execute("DELETE FROM queue_tasks WHERE job_id=?", (jid,))
        c.execute("DELETE FROM gen_jobs WHERE rowid=?", (jid,))
        c.execute("DELETE FROM job_logs WHERE job_id=?", (jid,))
        c.execute("DELETE FROM provider_calls WHERE project_id=? AND unit LIKE 'test-%'",
                  (PROJECT,))
        c.commit()
    finally:
        c.close()


def calls_base(cabinet="", calls="") -> str:
    """Чему равен CALLS_BASE_URL кабинета при таком окружении.

    Отдельным процессом: константа читается на импорте, и подменять её в уже
    загруженном модуле значило бы проверять не то, что происходит в бою.
    """
    env = dict(os.environ)
    for name in ("VS_CALLS_URL", "VS_CABINET_BASE", "VS_SERVER_PORT"):
        env.pop(name, None)
    if cabinet:
        env["VS_CABINET_BASE"] = cabinet
    if calls:
        env["VS_CALLS_URL"] = calls
    with tempfile.TemporaryDirectory() as tmp:
        env["VS_SERVER_DB"] = os.path.join(tmp, "t.db")
        env["VS_TENANTS_DIR"] = os.path.join(tmp, "tenants")
        # Движки кабинет ищет в ~/VideoStudio/src; для теста хватает прямого пути —
        # проверяем одну константу, а не то, как стенд раскладывает каталоги.
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path[:0] = [%r, %r]; import main; print(main.CALLS_BASE_URL)"
             % (os.path.join(BASE, "server"), os.path.join(BASE, "workers"))],
            capture_output=True, text=True, env=env, timeout=180)
    if out.returncode != 0:
        return f"импорт не удался: {out.stderr.strip()[-200:]}"
    return (out.stdout.strip().splitlines() or [""])[-1]


def main() -> int:
    jid, token = make_running_job()
    j = CallJournal(base=URL, job_id=jid, job_token=token, log=lambda m: None)
    check("журнал включён", j.enabled, True)

    unit = "test-001"
    fp = j.fingerprint(model="NARWHAL", prompt="маяк в тумане", seed=11)

    print("\nПервый прогон кадра")
    d = j.reserve(unit=unit, fingerprint=fp, kind="images",
                  provider="recrafter", model="NARWHAL")
    check("вызов разрешён", d.action, "new")
    check("можно обращаться к провайдеру", d.may_call, True)
    check("отчёт о заказе принят", j.ordered(d.call_id, "op-test-1"), True)
    check("отчёт о результате принят",
          j.delivered(d.call_id, "googler/test-001.jpg", cost=4.0), True)

    print("\nПовтор того же кадра — платить нельзя")
    again = j.reserve(unit=unit, fingerprint=fp, kind="images")
    check("решение skip", again.action, "skip")
    check("обращаться к провайдеру нельзя", again.may_call, False)
    check("отдан готовый файл", again.result_path, "googler/test-001.jpg")

    print("\nОбрыв после создания заказа")
    unit2, fp2 = "test-002", j.fingerprint(model="NARWHAL", prompt="лодка", seed=12)
    d2 = j.reserve(unit=unit2, fingerprint=fp2, kind="images")
    j.ordered(d2.call_id, "op-test-2")
    j.failed(d2.call_id, "read timeout", ordered=True)
    after = j.reserve(unit=unit2, fingerprint=fp2, kind="images")
    check("решение verify", after.action, "verify")
    check("повторно платить нельзя", after.may_call, False)
    check("идентификатор заказа вернулся", after.remote_order_id, "op-test-2")

    print("\nОтказ 429 до создания заказа")
    unit3, fp3 = "test-003", j.fingerprint(model="NARWHAL", prompt="дом", seed=13)
    d3 = j.reserve(unit=unit3, fingerprint=fp3, kind="images")
    j.failed(d3.call_id, "429 too many requests", ordered=False)
    retry = j.reserve(unit=unit3, fingerprint=fp3, kind="images")
    check("повтор разрешён", retry.action, "new")
    check("попытка посчитана", retry.attempts, 2)

    print("\nЗащита от чужого доступа")
    bad = CallJournal(base=URL, job_id=jid, job_token="не тот токен", log=lambda m: None)
    check("чужой токен не пускает", bad.reserve(unit="test-x", fingerprint=fp).action,
          "unknown")
    ghost = CallJournal(base=URL, job_id=999999, job_token=token, log=lambda m: None)
    check("несуществующая задача не пускает",
          ghost.reserve(unit="test-x", fingerprint=fp).action, "unknown")

    print("\nЖурнал не настроен — движок работает как раньше")
    off = CallJournal(base="", job_id="", job_token="")
    d_off = off.reserve(unit="test-y", fingerprint=fp)
    check("решение off", d_off.action, "off")
    check("обращаться к провайдеру можно", d_off.may_call, True)

    print("\nКабинет недоступен — платить не разрешаем")
    down = CallJournal(base="http://127.0.0.1:1", job_id=jid, job_token=token,
                       log=lambda m: None)
    # С 04.10.2026 клиент сперва ждёт кабинет (до 180 с, см. test_images_dropped_frames);
    # здесь проверяется только итог — «unknown», поэтому ждём секунду, а не три минуты.
    down.wait_sec = 1
    d_down = down.reserve(unit="test-z", fingerprint=fp)
    check("решение unknown", d_down.action, "unknown")
    check("обращаться к провайдеру НЕЛЬЗЯ", d_down.may_call, False)

    print("\nАдрес журнала для движка")
    # Ровно та поломка, из-за которой 07.09 пропустились все кадры: в единой платформе
    # кабинет не слушает 8800, а дефолт вёл туда. Движок получал URLError и, по правилу
    # «не платить, если нечем записать», пропускал кадр за кадром — молча.
    check("берётся адрес кабинета, а не зашитый порт",
          calls_base(cabinet="http://127.0.0.1:8080"), "http://127.0.0.1:8080")
    check("явный VS_CALLS_URL сильнее",
          calls_base(cabinet="http://127.0.0.1:8080", calls="http://127.0.0.1:9999"),
          "http://127.0.0.1:9999")
    check("без обоих — прежнее поведение",
          calls_base(), "http://127.0.0.1:8800")

    cleanup(jid)
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
