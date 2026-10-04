#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Готовые ролики старого кабинета с «состарившимся» заданием получают новый выход.

04.10.2026: 17 готовых роликов ВВ2 вернули в очередь с прежним, уже прошедшим временем, и
ворота выдачи через минуту снова их убили. Проверяется, что инструмент:
  • берёт только безопасные: файл не уходил (нет номера ролика, фаза до передачи), канал
    активен, причина — именно «состарилось»;
  • ставит на ближайший СВОБОДНЫЙ выход по расписанию канала, не ближе запаса, двум
    роликам одного канала — разные выходы;
  • пишет согласованно план, выпуск и задание, время — со смещением пояса канала;
  • без --apply не меняет в базе ни байта;
  • не трогает выпуск, который изменился между осмотром и записью, и откатывает целиком,
    если выход заняли тем временем.

Таблицы — дословно из базы старого кабинета (sqlite_master, 04.10.2026).

    studio-platform/.venv/bin/python ops-ww2/tests/test_reslot_aged.py
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
os.environ["RESLOT_SERVER_DIR"] = os.path.join(ROOT, "studio-platform", "server")
sys.path.insert(0, os.path.join(ROOT, "ops-ww2"))

import reslot_aged as R                                           # noqa: E402

FAILS = []
# 04.10.2026 12:00 по Мадриду (10:00 UTC)
NOW = dt.datetime(2026, 10, 4, 10, 0, tzinfo=dt.timezone.utc).timestamp()
AGED = "к времени выхода не успели — задание состарилось в очереди"

SCHEMA = """
CREATE TABLE pub_upload_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL UNIQUE, tenant_id TEXT NOT NULL, channel_id INTEGER NOT NULL, uc TEXT NOT NULL, profile_id INTEGER, host_id INTEGER, channel_url TEXT NOT NULL, title TEXT NOT NULL, description TEXT, schedule_iso TEXT, visibility TEXT NOT NULL DEFAULT 'public', collaborator TEXT, video_path TEXT NOT NULL, video_sha256 TEXT, cover_path TEXT, slot_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'queued', phase TEXT, claim_token TEXT, host TEXT, worker TEXT, claimed REAL, lease_until REAL, heartbeat REAL, verdict TEXT, video_id TEXT, evidence TEXT, error TEXT, finished REAL, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE pub_plan_items(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, channel_id INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1, donor_url TEXT NOT NULL, position REAL NOT NULL DEFAULT 0, slot_at REAL, slot_mode TEXT NOT NULL DEFAULT 'auto', overrides TEXT NOT NULL DEFAULT '{}', note TEXT, status TEXT NOT NULL DEFAULT 'planned', frozen INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL, donor_title TEXT);
CREATE UNIQUE INDEX idx_pub_plan_slot_busy_v2 ON pub_plan_items(channel_id,slot_at) WHERE slot_at IS NOT NULL AND status IN ('planned','frozen','running','done','error');
CREATE TABLE pub_releases(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL UNIQUE, tenant_id TEXT NOT NULL, plan_item_id INTEGER NOT NULL, plan_item_version INTEGER NOT NULL, channel_id INTEGER NOT NULL, uc TEXT NOT NULL, slot_at REAL NOT NULL, project_id TEXT, status TEXT NOT NULL DEFAULT 'pending', phase TEXT NOT NULL DEFAULT 'created', video_id TEXT, blocked_reason TEXT, evidence TEXT, heartbeat REAL, deadline REAL, attempts INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(plan_item_id,plan_item_version));
CREATE TABLE pub_channels(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, uc TEXT NOT NULL UNIQUE, name TEXT NOT NULL, handle TEXT, lang TEXT, niche TEXT, profile_id INTEGER, host_id INTEGER, tz TEXT NOT NULL DEFAULT 'Europe/Warsaw', schedule TEXT NOT NULL DEFAULT '{}', horizon_days INTEGER NOT NULL DEFAULT 3, preset_rewrite TEXT, preset_vsgen_id INTEGER, preset_cover TEXT, collaborator TEXT, video_mode TEXT NOT NULL DEFAULT 'slideshow', t2v_first_n INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active', note TEXT, created REAL NOT NULL, updated REAL NOT NULL, lead_days INTEGER NOT NULL DEFAULT 2, deferred INTEGER NOT NULL DEFAULT 1, deferred_limit INTEGER NOT NULL DEFAULT 5);
CREATE TABLE pub_release_log(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL, ts REAL NOT NULL, level TEXT NOT NULL DEFAULT 'info', step TEXT NOT NULL, message TEXT NOT NULL);
"""


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def stamp(y, m, d, hh, tz="Europe/Madrid"):
    from zoneinfo import ZoneInfo
    return dt.datetime(y, m, d, hh, 0, tzinfo=ZoneInfo(tz)).timestamp()


def db(path=":memory:"):
    c = sqlite3.connect(path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    for cid, name, st in ((17, "Sin Cuartel 1945 (ES)", "active"), (18, "Пауза", "paused")):
        c.execute("INSERT INTO pub_channels(id,tenant_id,uc,name,tz,schedule,horizon_days,status,"
                  "created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (cid, "3", f"UC{cid}", name, "Europe/Madrid", json.dumps({"*": ["10:00"]}),
                   18, st, NOW, NOW))
    return c


SEQ = [0]


def release(c, rid, ch=17, old=None, reason=AGED, jstatus="error", vid="", phase=None,
            rstatus="blocked"):
    SEQ[0] += 1
    n = SEQ[0]
    old = old or stamp(2026, 9, 26, 10)
    c.execute("INSERT INTO pub_plan_items(id,tenant_id,channel_id,donor_url,slot_at,status,"
              "frozen,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
              (n, "3", ch, f"https://youtu.be/{rid}", old + n, "error", 1, NOW, NOW))
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
              "channel_id,uc,slot_at,project_id,status,phase,blocked_reason,created,updated) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (rid, "3", n, 1, ch, f"UC{ch}", old + n, f"p_{rid}", rstatus, "upload_running",
               reason, NOW, NOW))
    c.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,channel_url,title,"
              "schedule_iso,video_path,slot_at,status,phase,claim_token,video_id,error,created,"
              "updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (rid, "3", ch, f"UC{ch}", "https://studio", "Название", "2026-09-26T10:00+02:00",
               "/x.mp4", old + n, jstatus,
               phase if phase is not None else json.dumps({"phase": "preflight"}),
               "старый-токен", vid, AGED, NOW, NOW))


def busy(c, ch, when):
    SEQ[0] += 1
    c.execute("INSERT INTO pub_plan_items(id,tenant_id,channel_id,donor_url,slot_at,status,"
              "created,updated) VALUES(?,?,?,?,?,?,?,?)",
              (SEQ[0], "3", ch, "https://youtu.be/занято", when, "planned", NOW, NOW))


def quiet(*_a):
    pass


def test_selection_and_slots() -> None:
    print("\nКого берём и куда ставим")
    c = db()
    release(c, "ok1")
    release(c, "ok2")
    release(c, "sent", vid="dQw4w9WgXcQ")
    release(c, "uploading", phase=json.dumps({"phase": "uploading"}))
    release(c, "paused", ch=18)
    release(c, "other", reason="зависло на шаге «upload_running» и попытки исчерпаны")
    busy(c, 17, stamp(2026, 10, 5, 10))         # завтрашний выход занят планом
    rep = R.run(c, NOW, apply=False, say=quiet)
    moved = {rid: iso for rid, _ch, iso in rep["moved"]}
    skipped = dict(rep["skipped"])
    check("берём два безопасных", sorted(moved), ["ok1", "ok2"])
    check("занятый 05.10 пропущен, первым — 06.10", moved.get("ok1"), "2026-10-06T10:00+02:00")
    check("второму того же канала — следующий день", moved.get("ok2"), "2026-10-07T10:00+02:00")
    check("ушедший в YouTube не трогаем", "файл уже уходил" in skipped.get("sent", ""), True)
    check("начатую передачу не трогаем", "передача могла начаться" in skipped.get("uploading", ""), True)
    check("канал на паузе не трогаем", skipped.get("paused"), "канал paused")
    check("другая причина разбора — не наш случай", "other" in moved or "other" in skipped, False)

    print("\n  запас: ближе LEAD_H часов выход не берём")
    c = db()
    release(c, "late")
    near = stamp(2026, 10, 4, 10) + 86400            # 05.10 10:00
    rep = R.run(c, near - 3 * 3600, apply=False, lead_h=4, say=quiet)
    check("выход через 3 ч пропущен — берём следующий",
          rep["moved"][0][2] if rep["moved"] else None, "2026-10-06T10:00+02:00")
    rep = R.run(c, near - 5 * 3600, apply=False, lead_h=4, say=quiet)
    check("выход через 5 ч годится",
          rep["moved"][0][2] if rep["moved"] else None, "2026-10-05T10:00+02:00")


def test_apply_writes_consistently() -> None:
    print("\nЗапись: план, выпуск и задание согласованы")
    c = db()
    release(c, "ok1")
    rep = R.run(c, NOW, apply=True, say=quiet)
    want = stamp(2026, 10, 5, 10)
    r = c.execute("SELECT * FROM pub_releases WHERE release_id='ok1'").fetchone()
    p = c.execute("SELECT * FROM pub_plan_items WHERE id=?", (r["plan_item_id"],)).fetchone()
    j = c.execute("SELECT * FROM pub_upload_jobs WHERE release_id='ok1'").fetchone()
    check("перенесён один", len(rep["moved"]), 1)
    check("выход в плане", p["slot_at"], want)
    check("план снова в работе", p["status"], "running")
    check("выход в выпуске", r["slot_at"], want)
    check("выпуск в работе, причина снята", (r["status"], r["blocked_reason"]), ("running", None))
    check("срок выпуска дальше, чем срок шага заливки", r["deadline"] > NOW + 1800, True)
    check("выход в задании", j["slot_at"], want)
    check("время для заливщика — со смещением пояса", j["schedule_iso"], "2026-10-05T10:00+02:00")
    check("задание снова в очереди", j["status"], "queued")
    check("старая аренда снята", (j["claim_token"], j["error"], j["verdict"]), (None, None, None))
    log = c.execute("SELECT message FROM pub_release_log WHERE release_id='ok1'").fetchone()
    check("в журнале выпуска сказано, что сделали",
          bool(log) and "перенёс выход" in log[0] and "не уходил" in log[0], True)

    print("\n  второй запуск ничего не делает")
    rep = R.run(c, NOW, apply=True, say=quiet)
    check("нечего переносить", (rep["moved"], rep["skipped"]), ([], []))


def test_dry_run_untouched() -> None:
    print("\nПроба не меняет базу ни на байт")
    path = os.path.join(tempfile.mkdtemp(prefix="reslot-"), "vsgen.db")
    c = db(path)
    release(c, "ok1")
    c.close()
    before = hashlib.sha256(open(path, "rb").read()).hexdigest()
    R.main(["--db", path])
    check("файл базы тот же", hashlib.sha256(open(path, "rb").read()).hexdigest(), before)
    R.main(["--db", path, "--apply"])
    c = sqlite3.connect(path)
    check("а с --apply — записано",
          c.execute("SELECT status FROM pub_upload_jobs").fetchone()[0], "queued")


def test_races() -> None:
    print("\nГонки: изменился выпуск или заняли выход — откат целиком")
    c = db()
    release(c, "ok1")
    cands = R.candidates(c)
    r = cands[0][0]
    c.execute("UPDATE pub_releases SET status='running', blocked_reason=NULL")
    err = R.apply_one(c, r, round(stamp(2026, 10, 5, 10)), "2026-10-05T10:00+02:00", NOW)
    check("выпуск изменился — не трогаем", "изменился" in err, True)
    check("задание осталось как было",
          c.execute("SELECT status FROM pub_upload_jobs").fetchone()[0], "error")

    c = db()
    release(c, "ok1")
    r = R.candidates(c)[0][0]
    # Кто-то вернул выпуск в работу (как /root/ops/requeue.py), причину не стерев.
    c.execute("UPDATE pub_releases SET status='running'")
    err = R.apply_one(c, r, round(stamp(2026, 10, 5, 10)), "2026-10-05T10:00+02:00", NOW)
    check("выпуск уже в работе — не трогаем, даже если причина осталась", "изменился" in err, True)

    c = db()
    release(c, "ok1")
    r = R.candidates(c)[0][0]
    when = round(stamp(2026, 10, 5, 10))
    busy(c, 17, when)                                # выход заняли после осмотра
    err = R.apply_one(c, r, when, "2026-10-05T10:00+02:00", NOW)
    check("выход занят — отказ", "занят" in err, True)
    check("выпуск не тронут",
          c.execute("SELECT status FROM pub_releases WHERE release_id='ok1'").fetchone()[0],
          "blocked")
    check("задание не тронуто",
          c.execute("SELECT status FROM pub_upload_jobs").fetchone()[0], "error")
    check("журнал не тронут", c.execute("SELECT COUNT(*) FROM pub_release_log").fetchone()[0], 0)


def main() -> int:
    print("Проверка: перенос выхода у готовых роликов старого кабинета")
    test_selection_and_slots()
    test_apply_writes_consistently()
    test_dry_run_untouched()
    test_races()
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
