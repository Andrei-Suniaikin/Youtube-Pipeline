#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Выпавшие кадры дорисовываются сами; перезапуск службы кадры больше не роняет (04.10.2026).

Тревога владельца: «Ещё 4 задачи без отдельного сообщения» — у всех

    ⛔ [271] Журнал недоступен (URLError) — кадр пропущен: повторная оплата дороже пропуска
    ⚠️ Не хватает 216 кадров из 272 …

Задачи упали в 11:58:47–11:59:55, ровно на выкатке: служба перезапустилась, очередь подняла
прерванные задачи картинок раньше, чем сервер начал принимать соединения. Журнал платных
вызовов не отвечал, и каждый кадр честно пропускался — платить вслепую нельзя. За 30 секунд
задачи кончились с дырами. Хуже того: у одного выпуска движок смотрел на свою давно
сделанную задачу промптов и шёл к сборке — ролик собрался бы с недостающими кадрами.

Владелец: «можем ли мы автоматом запускать, чтобы он фотки переделывал, если какие-то
дропались». Что проверяется:

  • клиент журнала ждёт кабинет, который не отвечает (отказ в соединении, 502/503/504),
    до VS_CALLS_WAIT_SEC, — и только потом говорит «unknown»; отказ по существу (401)
    не ждёт; провайдеру отсюда не звонят никогда;
  • упавшая из-за выпавших кадров задача картинок ставится заново — до двух раз, потом
    разбор; другая причина падения (схема, промпты) повтора не получает;
  • если последний прогон картинок упал, а задача выпуска — старая и «done», к сборке
    не идём: картинки дорисовываются, а при исчерпанных повторах — разбор;
  • картинки, которые ещё делаются, ждём, а не ставим сборку.

    .venv/bin/python tests/test_images_dropped_frames.py
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import sqlite3
import sys
import threading
import time
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "workers"))

import call_journal as J                                          # noqa: E402
import pub_services                                               # noqa: E402

NOW = 1_800_000_000.0
FAILS = []
DROPPED = ("[11:59:20.430] ⚠️ ⚠️ Не хватает 216 кадров из 272: сцены 57, 58, 59",
           "[11:59:20.352] ❌ ⛔ [271] Журнал недоступен (URLError) — кадр пропущен: "
           "повторная оплата дороже пропуска")


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


# ─────────────────────────── клиент журнала ───────────────────────────

class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def journal(script, wait=180):
    """Журнал, у которого _post по очереди отдаёт/бросает элементы `script`."""
    j = J.CallJournal(base="http://127.0.0.1:9", job_id="7", job_token="tok")
    clock, lines, calls = Clock(), [], []
    j.wait_sec, j._clock, j._sleep, j.log = wait, clock.now, clock.sleep, lines.append
    seq = list(script)

    def fake(path, payload):
        calls.append(path)
        item = seq.pop(0) if seq else seq_last[0]
        if isinstance(item, BaseException):
            raise item
        return item
    seq_last = [script[-1]]
    j._post = fake
    return j, clock, lines, calls


def refused():
    return urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))


def http_err(code):
    return urllib.error.HTTPError("http://x", code, "x", {}, None)


def test_journal_waits() -> None:
    print("\nКлиент журнала: кабинет перезапускается — ждём, а не пропускаем кадр")
    j, clock, lines, calls = journal([refused(), refused(), {"action": "new", "call_id": 5}])
    d = j.reserve(unit="001", fingerprint="f")
    check("дождались ответа кабинета", (d.action, d.call_id), ("new", 5))
    check("спросили трижды", len(calls), 3)
    check("паузы растут", clock.slept, [1.0, 2.0])
    check("в журнале задачи одна строка, а не по строке на повтор", len(lines), 1)
    check("...и в ней сказано, что вслепую не платим", "вслепую" in (lines[0] if lines else ""), True)

    print("\n  прокси отвечает 503, пока кабинет поднимается — тоже ждём")
    j, clock, lines, calls = journal([http_err(503), http_err(502), {"action": "skip", "result_path": "a.png"}])
    d = j.reserve(unit="001", fingerprint="f")
    check("дождались", (d.action, d.result_path), ("skip", "a.png"))

    print("\n  кабинет так и не поднялся — «unknown», как раньше, но после ожидания")
    j, clock, lines, calls = journal([refused()], wait=30)
    d = j.reserve(unit="001", fingerprint="f")
    check("решение — не платить", (d.action, d.may_call), ("unknown", False))
    check("ждали не дольше срока", clock.t <= 30, True)
    check("и ждали, а не сдались сразу", clock.t >= 15, True)

    print("\n  отказ по существу (401) — сразу, без ожидания")
    j, clock, lines, calls = journal([http_err(401)])
    d = j.reserve(unit="001", fingerprint="f")
    check("unknown с кодом", (d.action, d.error), ("unknown", "HTTP 401"))
    check("без пауз", clock.slept, [])

    print("\n  отчёт «заказ создан» тоже доезжает после перерыва — иначе следующий прогон оплатит дважды")
    j, clock, lines, calls = journal([refused(), {}])
    check("отчёт записан", j.ordered(5, "gen_1"), True)
    check("со второго раза", len(calls), 2)


def test_journal_real_socket() -> None:
    print("\nНастоящий отказ в соединении: сервер поднимается через секунду")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.dumps({"action": "new", "call_id": 9}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    holder = {}

    def start_later():
        time.sleep(1.2)
        srv = http.server.HTTPServer(("127.0.0.1", port), H)
        holder["srv"] = srv
        srv.serve_forever()

    threading.Thread(target=start_later, daemon=True).start()
    j = J.CallJournal(base=f"http://127.0.0.1:{port}", job_id="7", job_token="tok")
    j.wait_sec = 20
    t0 = time.time()
    d = j.reserve(unit="001", fingerprint="f")
    check("получили разрешение от поднявшегося кабинета", (d.action, d.call_id), ("new", 9))
    check("ждали секунды, а не срок целиком", time.time() - t0 < 10, True)
    if holder.get("srv"):
        holder["srv"].shutdown()


def test_default_wait() -> None:
    print("\nСрок ожидания по умолчанию")
    check("180 с", J.WAIT_SEC, 180.0)


# ─────────────────────────── движок ───────────────────────────

def fresh(images_status="error", images_mark=None, logs=DROPPED, mirror_job=12,
          later=None):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
      CREATE TABLE users(id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT,
                         key_opts TEXT, keys TEXT);
      CREATE TABLE projects(id TEXT PRIMARY KEY, tenant_id TEXT, settings TEXT);
      CREATE TABLE queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT,
                               project_id TEXT, kind TEXT, status TEXT, error TEXT);
      CREATE TABLE job_logs(id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER,
                            ts REAL, line TEXT);
      CREATE TABLE pub_release_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,
        release_id TEXT, service TEXT, kind TEXT, remote_job_id TEXT, status TEXT,
        error TEXT, created REAL, updated REAL, UNIQUE(release_id,service,kind));
      CREATE TABLE pub_release_log(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT,
        ts REAL, level TEXT, step TEXT, message TEXT,
        repeats INTEGER NOT NULL DEFAULT 1, last_ts REAL);
    """)
    c.execute("INSERT INTO users VALUES(1,'dev','dev','{}','{}')")
    c.execute("INSERT INTO queue_tasks VALUES(10,'dev','proj-1','tts','done',NULL)")
    c.execute("INSERT INTO queue_tasks VALUES(11,'dev','proj-1','prompts','done',NULL)")
    c.execute("INSERT INTO queue_tasks VALUES(12,'dev','proj-1','images',?,?)",
              (images_status, "rc=1" if images_status == "error" else None))
    failed_job = 12
    if later:
        c.execute("INSERT INTO queue_tasks VALUES(20,'dev','proj-1','images',?,'rc=1')", (later,))
        failed_job = 20
    for line in logs:
        c.execute("INSERT INTO job_logs(job_id,ts,line) VALUES(?,?,?)", (failed_job, NOW, line))
    for kind, jid, st, err in (("tts", "10", "done", None),
                               ("images", str(mirror_job),
                                "done" if mirror_job == 11 else images_status, images_mark)):
        c.execute("INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,"
                  "error,created,updated) VALUES('rel-1','vsgen',?,?,?,?,?,?)",
                  (kind, jid, st, err, NOW, NOW))
    c.commit()
    return c


REL = {"release_id": "rel-1", "tenant_id": "dev", "project_id": "proj-1", "lang": "pl",
       "preset_cover": "", "donor_url": "https://youtu.be/aaa", "updated": NOW}
PDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_dropped_frames")
os.makedirs(os.path.join(PDIR, "googler"), exist_ok=True)
open(os.path.join(PDIR, "googler", "001.jpg"), "wb").write(b"x")


def services(placed):
    return pub_services.PubServices({
        "create_project": lambda *a, **k: "proj-1",
        "enqueue": lambda uid, tid, pid, kind, params: (placed.append(kind), 100 + len(placed))[1],
        "project_dir": lambda *a, **k: PDIR})


def images_row(c):
    return dict(c.execute("SELECT remote_job_id, status, error FROM pub_release_jobs "
                          "WHERE kind='images'").fetchone())


def last_msg(c):
    r = c.execute("SELECT message FROM pub_release_log ORDER BY id DESC LIMIT 1").fetchone()
    return r[0] if r else ""


def test_engine_retries_dropped() -> None:
    print("\nДвижок: задача картинок упала из-за выпавших кадров — ставим заново")
    c = fresh()
    placed = []
    out = services(placed).do_media_ready(c, REL)
    check("не разбор, а ожидание", out, {"wait": True})
    check("поставлены картинки, не сборка", placed, ["images"])
    check("за выпуском новая задача, повтор 1",
          images_row(c), {"remote_job_id": "101", "status": "queued", "error": "images-retry:1"})
    msg = last_msg(c)
    check("в журнале — что дорисуются только недостающие и без второй оплаты",
          "недостающие" in msg and "оплатить не даст" in msg and "1 из 2" in msg, True)

    print("\n  второй раз — ещё повтор; третий — разбор")
    c = fresh(images_mark="images-retry:1")
    placed = []
    services(placed).do_media_ready(c, REL)
    check("второй повтор", (placed, images_row(c)["error"]), (["images"], "images-retry:2"))
    c = fresh(images_mark="images-retry:2")
    placed = []
    try:
        services(placed).do_media_ready(c, REL)
        check("третий — разбор", "не упало", "ItemFailed")
    except pub_services.ItemFailed as exc:
        check("третий — разбор", "images" in str(exc), True)
    check("и больше не ставили", placed, [])


def test_engine_other_cause() -> None:
    print("\nДругая причина падения (кадры не выпадали) — без повтора, как раньше")
    c = fresh(logs=("❌ План сцен отклонён: semantic block 2/56: схема не исправлена",))
    placed = []
    try:
        services(placed).do_media_ready(c, REL)
        check("разбор", "не упало", "ItemFailed")
    except pub_services.ItemFailed:
        check("разбор, без повтора", placed, [])
    c = fresh(logs=())
    placed = []
    try:
        services(placed).do_media_ready(c, REL)
        check("журнала нет — разбор", "не упало", "ItemFailed")
    except pub_services.ItemFailed:
        check("журнала нет — разбор, без повтора", placed, [])


def test_engine_no_render_with_holes() -> None:
    print("\nЗа выпуском старая «done»-задача, а последний прогон картинок упал — к сборке не идём")
    c = fresh(images_status="done", mirror_job=11, later="error")
    placed = []
    out = services(placed).do_media_ready(c, REL)
    check("сборку не ставили", "render" in placed, False)
    check("поставили картинки", placed, ["images"])
    check("и ждём", out, {"wait": True})
    check("повтор 1 записан", images_row(c)["error"], "images-retry:1")

    print("\n  повторы исчерпаны — разбор, а не сборка с дырами")
    c = fresh(images_status="done", mirror_job=11, later="error", images_mark="images-retry:2")
    placed = []
    try:
        services(placed).do_media_ready(c, REL)
        check("разбор", "не упало", "ItemFailed")
    except pub_services.ItemFailed as exc:
        check("разбор с причиной", "кадры выпали" in str(exc), True)
    check("сборку не ставили", placed, [])

    print("\n  последний прогон упал по другой причине — поведение прежнее (не наш случай)")
    c = fresh(images_status="done", mirror_job=11, later="error", logs=("❌ схема",))
    placed = []
    services(placed).do_media_ready(c, REL)
    check("картинки заново не ставили", "images" in placed, False)

    print("\n  картинки ещё делаются — ждём их, сборку не ставим")
    c = fresh(images_status="done", mirror_job=11, later="running", logs=())
    placed = []
    out = services(placed).do_media_ready(c, REL)
    check("ничего не ставили", placed, [])
    check("ждём", out, {"wait": True})
    check("за выпуском — идущая задача", images_row(c)["remote_job_id"], "20")
    check("в журнале — «жду их»", "жду их" in last_msg(c), True)

    print("\n  без новых задач картинок — как раньше, к сборке")
    c = fresh(images_status="done", mirror_job=11, logs=())
    placed = []
    services(placed).do_media_ready(c, REL)
    check("сборка поставлена", placed, ["render"])


def main() -> int:
    print("Проверка: выпавшие кадры дорисовываются сами")
    test_journal_waits()
    test_journal_real_socket()
    test_default_wait()
    test_engine_retries_dropped()
    test_engine_other_cause()
    test_engine_no_render_with_holes()
    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        import shutil
        shutil.rmtree(PDIR, ignore_errors=True)
