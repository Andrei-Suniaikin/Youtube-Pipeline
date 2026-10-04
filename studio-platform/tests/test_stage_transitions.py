#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Переход между этапами производства — не зависание; этап кабинета не дублируется.

03.10.2026, четыре выпуска в разборе: «зависло на шаге project_ready, попытки
исчерпаны», хотя у всех озвучка, сборка и главы готовы. Фаза «проект готов» длится
часами, её срок — 15 минут от входа; движок считал работу живой, только пока этап стоит
в очереди. В секунды между «этап кончился» и «следующий поставлен» задачи нет — и каждый
успешный переход засчитывался попыткой. Три перехода — разбор.

Второе: главы делались дважды. Кабинет после сборки ставит их сам, движок про это не
знал и ставил свои — сначала очередь отказывала («очередь не приняла этап chapters»),
потом принимала.

    .venv/bin/python tests/test_stage_transitions.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_engine as E                                            # noqa: E402
import pub_schema                                                 # noqa: E402
import pub_services                                               # noqa: E402

NOW = 1_800_000_000.0
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


# ─────────────────────────── движок: что считать зависанием ───────────────────────────

def engine_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(c)
    c.execute("CREATE TABLE IF NOT EXISTS queue_tasks(job_id INTEGER PRIMARY KEY, "
              "tenant_id TEXT, project_id TEXT, kind TEXT, status TEXT, created REAL, "
              "finished REAL)")
    c.execute("INSERT INTO pub_channels(tenant_id,uc,name,lang,tz,schedule,deferred,"
              "deferred_limit,created,updated) VALUES('t1','UC1','Канал','pl',"
              "'Europe/Warsaw','{}',1,0,?,?)", (NOW, NOW))
    c.execute("INSERT INTO pub_plan_items(id,tenant_id,channel_id,version,donor_url,"
              "donor_video_id,donor_title,slot_at,status,created,updated) "
              "VALUES(1,'t1',1,1,'https://youtu.be/x','x','Донор',?,'running',?,?)",
              (NOW + 86400, NOW, NOW))
    # Фаза «проект готов», её срок давно вышел, две попытки уже потрачены.
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
              "channel_id,uc,slot_at,project_id,status,phase,attempts,deadline,created,"
              "updated) VALUES('rel-1','t1',1,1,1,'UC1',?,'p1','running','project_ready',2,?,?,?)",
              (NOW + 86400, NOW - 4 * 3600, NOW - 5 * 3600, NOW - 60))
    c.execute("INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,"
              "created,updated) VALUES('rel-1','vsgen','render','3','done',?,?)", (NOW, NOW))
    c.commit()
    return c


def task(c, job_id, kind, status, finished=None):
    c.execute("INSERT OR REPLACE INTO queue_tasks VALUES(?,?,?,?,?,?,?)",
              (job_id, "t1", "p1", kind, status, NOW - 7200, finished))
    c.commit()


def rel(c):
    return c.execute("SELECT * FROM pub_releases WHERE release_id='rel-1'").fetchone()


class Svc:
    def __init__(self):
        self.called = 0

    def do_media_ready(self, c, rel):
        self.called += 1
        return {"wait": True}

    def release_resources(self, c, rel):
        pass


def engine_part():
    print("Движок: переход между этапами — не зависание")
    c = engine_db()
    task(c, 3, "render", "done", finished=NOW - 30)
    check("сборка кончилась 30 с назад, следующий этап ещё не поставлен — не зависло",
          E.is_stuck(c, rel(c), NOW), False)
    svc = Svc()
    E.advance(c, dict(rel(c)), svc, NOW)
    r = rel(c)
    check("выпуск не снят в разбор", r["status"], "running")
    check("и попытка не засчитана", r["attempts"], 2)
    check("обработчик фазы позвали — следующий этап будет поставлен", svc.called, 1)

    print("\n  этап, поставленный кабинетом без движка, — тоже работа")
    c = engine_db()
    task(c, 3, "render", "done", finished=NOW - 3 * 3600)
    task(c, 4, "chapters", "queued")
    check("главы стоят в очереди (их поставил кабинет) — не зависло",
          E.is_stuck(c, rel(c), NOW), False)

    print("\n  настоящая тишина по-прежнему тишина")
    c = engine_db()
    task(c, 3, "render", "done", finished=NOW - 3 * 3600)
    check("последний этап кончился три часа назад и в очереди пусто — зависло",
          E.is_stuck(c, rel(c), NOW), True)
    c = engine_db()
    task(c, 3, "render", "error", finished=NOW - 30)
    check("упавший этап — не «переход»", E.is_stuck(c, rel(c), NOW), True)
    c = engine_db()
    check("задач нет вовсе — зависло", E.is_stuck(c, rel(c), NOW), True)
    c = engine_db()
    task(c, 3, "render", "done", finished=NOW - E.ALIVE_SILENCE - 5)
    check("граница: дольше ALIVE_SILENCE — уже тишина", E.is_stuck(c, rel(c), NOW), True)


# ─────────────────────────── этап, поставленный кабинетом ───────────────────────────

def svc_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
      CREATE TABLE users(id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT,
                         key_opts TEXT, keys TEXT);
      CREATE TABLE projects(id TEXT PRIMARY KEY, tenant_id TEXT, settings TEXT);
      CREATE TABLE queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT,
                               project_id TEXT, kind TEXT, status TEXT);
      CREATE TABLE pub_release_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,
        release_id TEXT, service TEXT, kind TEXT, remote_job_id TEXT, status TEXT,
        error TEXT, created REAL, updated REAL, UNIQUE(release_id,service,kind));
      CREATE TABLE pub_release_log(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT,
        ts REAL, level TEXT, step TEXT, message TEXT,
        repeats INTEGER NOT NULL DEFAULT 1, last_ts REAL);
    """)
    c.execute("INSERT INTO users VALUES(1,'dev','dev','{}','{}')")
    for jid, kind in ((10, "tts"), (11, "images"), (12, "render")):
        c.execute("INSERT INTO queue_tasks VALUES(?,?,?,?,?)", (jid, "dev", "proj-1", kind, "done"))
        c.execute("INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,"
                  "created,updated) VALUES('rel-1','vsgen',?,?,'done',?,?)",
                  (kind, str(jid), NOW, NOW))
    c.commit()
    return c


PDIR = tempfile.mkdtemp(prefix="stage-")
os.makedirs(os.path.join(PDIR, "googler"))
open(os.path.join(PDIR, "googler", "1.jpg"), "wb").write(b"x")
REL = {"release_id": "rel-1", "tenant_id": "dev", "project_id": "proj-1", "lang": "pl",
       "preset_cover": "", "donor_url": "https://youtu.be/aaa", "updated": NOW}


def services(placed):
    return pub_services.PubServices({
        "create_project": lambda *a, **k: "proj-1",
        "enqueue": lambda uid, tid, pid, kind, params: (placed.append(kind), 100 + len(placed))[1],
        "project_dir": lambda *a, **k: PDIR})


def chapters_row(c):
    r = c.execute("SELECT remote_job_id FROM pub_release_jobs WHERE kind='chapters'").fetchone()
    return r["remote_job_id"] if r else None


def services_part():
    print("\nГлавы, поставленные кабинетом, движок берёт себе, а не ставит вторые")
    c = svc_db()
    c.execute("INSERT INTO queue_tasks VALUES(13,'dev','proj-1','chapters','queued')")
    c.commit()
    placed = []
    out = services(placed).do_media_ready(c, REL)
    check("второй задачи глав не поставлено", placed, [])
    check("за выпуском записана задача кабинета", chapters_row(c), "13")
    check("и движок её ждёт", out, {"wait": True})
    msg = c.execute("SELECT message FROM pub_release_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    check("в журнале сказано, что взял готовую", "уже стоит в очереди" in msg, True)

    print("\n  глав никто не ставил — движок ставит их сам, один раз")
    c = svc_db()
    placed = []
    services(placed).do_media_ready(c, REL)
    check("поставлены главы", placed, ["chapters"])
    check("записана новая задача", chapters_row(c), "101")

    print("\n  старая задача того же вида — прошлый заход, её не берём")
    c = svc_db()
    c.execute("INSERT INTO queue_tasks VALUES(5,'dev','proj-1','chapters','done')")
    c.commit()
    placed = []
    services(placed).do_media_ready(c, REL)
    check("старые главы (до сборки) не приписаны", placed, ["chapters"])

    print("\n  упавшую задачу кабинета тоже не берём")
    c = svc_db()
    c.execute("INSERT INTO queue_tasks VALUES(13,'dev','proj-1','chapters','error')")
    c.commit()
    placed = []
    services(placed).do_media_ready(c, REL)
    check("упавшие главы не приписаны — ставим свои", placed, ["chapters"])

    print("\n  первый этап выпуска берёт только движок")
    c = svc_db()
    c.execute("DELETE FROM pub_release_jobs")
    c.commit()
    placed = []
    services(placed).do_media_ready(c, REL)
    check("озвучка из прошлого захода не выдана за сегодняшнюю", placed, ["tts"])


def rerun_part():
    print("\nЭтап перезапустили руками — выпуск из разбора возвращается сам")
    c = engine_db()
    c.execute("UPDATE pub_releases SET status='blocked', phase='project_ready', blocked_reason="
              "'этап «images» завершился со статусом error' WHERE release_id='rel-1'")
    c.execute("INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,"
              "created,updated) VALUES('rel-1','vsgen','images','12','error',?,?)", (NOW, NOW))
    task(c, 12, "images", "error", finished=NOW - 600)
    check("упавшая задача так и лежит — не трогаем", E.revive_rerun_stages(c, NOW), [])
    task(c, 15, "images", "queued")
    check("поставили новую задачу картинок — выпуск вернулся", E.revive_rerun_stages(c, NOW),
          ["rel-1"])
    check("он в работе", rel(c)["status"], "pending")
    msg = c.execute("SELECT message FROM pub_release_log WHERE release_id='rel-1' "
                    "AND message LIKE '%вернулся в работу сам%'").fetchone()
    check("и сказано почему", bool(msg) and "задача 15" in msg[0], True)

    c = engine_db()
    c.execute("UPDATE pub_releases SET status='blocked', phase='project_ready', blocked_reason="
              "'этап «images» завершился со статусом error' WHERE release_id='rel-1'")
    c.execute("INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,"
              "created,updated) VALUES('rel-1','vsgen','images','12','error',?,?)", (NOW, NOW))
    task(c, 12, "images", "done", finished=NOW - 60)
    check("повтор на месте (та же задача стала done) — тоже возвращается",
          E.revive_rerun_stages(c, NOW), ["rel-1"])

    c = engine_db()
    c.execute("UPDATE pub_releases SET status='blocked', blocked_reason='зависло на шаге «x»' "
              "WHERE release_id='rel-1'")
    task(c, 15, "images", "queued")
    check("другая причина разбора — не наш случай", E.revive_rerun_stages(c, NOW), [])
    import inspect
    check("тик зовёт это каждый раз",
          "revive_rerun_stages(c, now)" in inspect.getsource(E.tick), True)

    print("\nДвижок берёт перезапущенную или уже стоящую задачу, а не роняет выпуск")
    c = svc_db()
    c.execute("UPDATE queue_tasks SET status='error' WHERE job_id=12")
    c.execute("INSERT INTO queue_tasks VALUES(14,'dev','proj-1','render','running')")
    c.commit()
    placed = []
    out = services(placed).do_media_ready(c, REL)
    check("упал рендер, но новый уже идёт — ждём его, не разбор", out, {"wait": True})
    row = c.execute("SELECT remote_job_id, status FROM pub_release_jobs WHERE kind='render'").fetchone()
    check("за выпуском — новая задача", (row["remote_job_id"], row["status"]), ("14", "running"))
    check("новой не ставили", placed, [])

    c = svc_db()
    c.execute("UPDATE queue_tasks SET status='error' WHERE job_id=12")
    c.commit()
    try:
        services([]).do_media_ready(c, REL)
        check("упал и ничего нового — разбор, как раньше", "не упало", "ItemFailed")
    except pub_services.ItemFailed:
        check("упал и ничего нового — разбор, как раньше", True, True)

    print("\n  картинки уже стоят в очереди — «жду их», а не «очередь не приняла»")
    empty = tempfile.mkdtemp(prefix="stage-empty-")
    c = svc_db()
    c.execute("INSERT INTO queue_tasks VALUES(20,'dev','proj-1','images','queued')")
    c.commit()
    svc = pub_services.PubServices({
        "create_project": lambda *a, **k: "proj-1",
        "enqueue": lambda *a, **k: None,                      # очередь отказывает
        "project_dir": lambda *a, **k: empty})                # картинок на диске нет
    out = svc.do_media_ready(c, REL)
    check("ждём стоящие картинки", out, {"wait": True})
    row = c.execute("SELECT remote_job_id, error FROM pub_release_jobs WHERE kind='images'").fetchone()
    check("и они записаны за выпуском", (row["remote_job_id"], row["error"]), ("20", "re-enqueued"))
    msg = c.execute("SELECT message FROM pub_release_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    check("в журнале — «жду их»", "уже стоят в очереди" in msg, True)


def main() -> int:
    engine_part()
    services_part()
    rerun_part()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
