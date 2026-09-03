#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты движка выпусков.

Проверяется поведение, ради которого он написан: не задваивать ролики, не терять
работу после падения, не считать зависание работой и не гасить весь день из-за
кончившихся кредитов.
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from pub_engine import (  # noqa: E402
    HARD_STALL,
    MAX_ATTEMPTS,
    PHASES,
    ItemFailed,
    ResourceDown,
    advance,
    live_releases,
    materialize,
    phase_history,
    reached_irreversible,
    tick,
)
from pub_schema import ensure_pub_schema  # noqa: E402

DAY = 86400
NOW = 1786900000.0


class Services:
    """Подставные соседи: по умолчанию каждый шаг проходит сразу."""

    def __init__(self, **overrides):
        self.calls = []
        self.overrides = overrides
        self.released = []

    def release_resources(self, c, rel):
        self.released.append(rel["release_id"])

    def __getattr__(self, name):
        if not name.startswith("do_"):
            raise AttributeError(name)
        phase = name[3:]

        def handler(c, rel):
            self.calls.append(phase)
            rule = self.overrides.get(phase)
            if callable(rule):
                return rule(c, rel)
            if rule is not None:
                return rule
            return {"video_id": "vid123"} if phase == "upload_running" else {}
        return handler


@pytest.fixture()
def db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_pub_schema(c)
    yield c
    c.close()


def make(c, slot_in_days=1, lead=2, ch_status="active", item_status="planned"):
    cid = c.execute(
        "INSERT INTO pub_channels(tenant_id,uc,name,status,lead_days,created,updated)"
        " VALUES('t1','UC" + "a" * 22 + "','Канал',?,?,?,?)",
        (ch_status, lead, NOW, NOW)).lastrowid
    pid = c.execute(
        "INSERT INTO pub_plan_items(tenant_id,channel_id,donor_url,slot_at,status,created,updated)"
        " VALUES('t1',?,?,?,?,?,?)",
        (cid, "https://donor/1", NOW + slot_in_days * DAY, item_status, NOW, NOW)).lastrowid
    return cid, pid


def run_to(c, services, phase, now=NOW, limit=20):
    """Прокрутить тики, пока выпуск не дойдёт до нужной фазы."""
    for _ in range(limit):
        tick(c, services, now)
        rel = c.execute("SELECT * FROM pub_releases").fetchone()
        if rel and (rel["phase"] == phase or rel["status"] in ("blocked", "done")):
            return rel
    return c.execute("SELECT * FROM pub_releases").fetchone()


# ─── заведение ───

def test_release_created_when_time_has_come(db):
    make(db, slot_in_days=1, lead=2)          # слот завтра, готовить за 2 дня → уже пора
    assert len(materialize(db, NOW)) == 1


def test_release_not_created_too_early(db):
    make(db, slot_in_days=10, lead=2)
    assert materialize(db, NOW) == []


def test_paused_channel_is_not_started(db):
    make(db, ch_status="paused")
    assert materialize(db, NOW) == []


def test_materialize_is_idempotent(db):
    make(db)
    materialize(db, NOW)
    materialize(db, NOW)
    materialize(db, NOW)
    assert db.execute("SELECT COUNT(*) FROM pub_releases").fetchone()[0] == 1


def test_plan_item_is_frozen_after_start(db):
    _, pid = make(db)
    materialize(db, NOW)
    row = db.execute("SELECT status, frozen FROM pub_plan_items WHERE id=?", (pid,)).fetchone()
    assert (row["status"], row["frozen"]) == ("frozen", 1)


def test_created_phase_is_journalled(db):
    make(db)
    rid = materialize(db, NOW)[0]
    assert phase_history(db, rid) == ["created"]


# ─── прохождение пути ───

def test_release_walks_all_phases_to_scheduled(db):
    """Успех — подтверждённое расписание: дальше работа закрыта, проверка PUBLIC потом."""
    make(db)
    s = Services()
    rel = run_to(db, s, "scheduled")
    assert rel["phase"] == "scheduled"
    walked = phase_history(db, rel["release_id"])
    assert walked == list(PHASES[:PHASES.index("scheduled") + 1])


def test_scheduled_closes_the_work(db):
    _, pid = make(db)
    s = Services()
    run_to(db, s, "scheduled")
    tick(db, s, NOW)
    assert db.execute("SELECT status FROM pub_releases").fetchone()[0] == "done"
    assert db.execute("SELECT status FROM pub_plan_items WHERE id=?",
                      (pid,)).fetchone()[0] == "done"


def test_finished_release_marks_plan_item_done(db):
    _, pid = make(db)
    s = Services()
    run_to(db, s, "scheduled")
    tick(db, s, NOW)
    assert db.execute("SELECT status FROM pub_plan_items WHERE id=?", (pid,)).fetchone()[0] == "done"
    assert db.execute("SELECT status FROM pub_releases").fetchone()[0] == "done"


def test_video_id_is_remembered(db):
    make(db)
    rel = run_to(db, Services(), "scheduled")
    assert rel["video_id"] == "vid123"


def test_waiting_phase_does_not_advance(db):
    make(db)
    s = Services(source_ready={"wait": True})
    rel = run_to(db, s, "source_ready", limit=3)
    assert rel["phase"] == "created"
    assert rel["status"] == "running"


# ─── не задваивать ───

def test_upload_never_repeats_after_it_started(db):
    make(db)
    s = Services()
    run_to(db, s, "upload_running")
    before = s.calls.count("upload_running")
    # Сервис перезапустился и откатил фазу — движок всё равно не должен грузить второй раз.
    db.execute("UPDATE pub_releases SET phase='upload_pending', status='pending'")
    tick(db, s, NOW)
    assert s.calls.count("upload_running") == before
    rel = db.execute("SELECT status, phase, video_id FROM pub_releases").fetchone()
    assert (rel["status"], rel["phase"], rel["video_id"]) == (
        "pending", "upload_pending", "vid123"
    )


@pytest.mark.parametrize("queue_status", ["queued", "running"])
def test_live_vsgen_queue_is_not_killed_by_hard_stall(db, queue_status):
    """Штатная queued/running задача живёт по lease очереди, а не по общему таймеру."""
    make(db)
    rid = materialize(db, NOW)[0]
    db.execute("CREATE TABLE queue_tasks(job_id INTEGER PRIMARY KEY, status TEXT)")
    db.execute("INSERT INTO queue_tasks(job_id,status) VALUES(101,?)", (queue_status,))
    db.execute(
        "INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,created,updated) "
        "VALUES(?, 'vsgen', 'tts', '101', 'queued', ?, ?)",
        (rid, NOW, NOW),
    )
    db.execute(
        "UPDATE pub_releases SET phase='project_ready', status='running', deadline=? "
        "WHERE release_id=?",
        (NOW - HARD_STALL - 1, rid),
    )
    out = advance(db, db.execute("SELECT * FROM pub_releases").fetchone(),
                  Services(media_ready={"wait": True}), NOW)
    rel = db.execute("SELECT status,attempts FROM pub_releases").fetchone()
    assert out == "waiting"
    assert (rel["status"], rel["attempts"]) == ("running", 0)


def test_old_rewrite_does_not_hide_stuck_media_phase(db):
    """Работа прежней фазы не считается heartbeat текущей сборки ролика."""
    make(db)
    rid = materialize(db, NOW)[0]
    db.execute("CREATE TABLE queue_tasks(job_id INTEGER PRIMARY KEY, status TEXT)")
    db.execute(
        "INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,created,updated) "
        "VALUES(?, 'rewrite', 'job', 'rw-1', 'running', ?, ?)",
        (rid, NOW, NOW),
    )
    db.execute(
        "UPDATE pub_releases SET phase='project_ready', status='running', deadline=? "
        "WHERE release_id=?",
        (NOW - 1, rid),
    )
    out = advance(db, db.execute("SELECT * FROM pub_releases").fetchone(), Services(), NOW)
    assert out == "retried"
    assert db.execute("SELECT attempts FROM pub_releases").fetchone()[0] == 1


def test_remote_mirror_without_heartbeat_keeps_bounded_hard_stall(db):
    make(db)
    rid = materialize(db, NOW)[0]
    db.execute(
        "INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,created,updated) "
        "VALUES(?, 'rewrite', 'job', 'rw-1', 'running', ?, ?)",
        (rid, NOW, NOW),
    )
    db.execute("UPDATE pub_releases SET deadline=? WHERE release_id=?",
               (NOW - HARD_STALL - 1, rid))
    out = advance(db, db.execute("SELECT * FROM pub_releases").fetchone(),
                  Services(source_ready={"wait": True}), NOW)
    assert out == "retried"
    assert db.execute("SELECT attempts FROM pub_releases").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("upload_status", "heartbeat"),
    [("queued", None), ("running", NOW - 1)],
)
def test_live_upload_is_not_killed_by_hard_stall(db, upload_status, heartbeat):
    make(db)
    rid = materialize(db, NOW)[0]
    rel = db.execute("SELECT channel_id,uc,slot_at FROM pub_releases").fetchone()
    db.execute(
        "INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,channel_url,title,"
        "video_path,slot_at,status,heartbeat,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, "t1", rel["channel_id"], rel["uc"], "https://studio", "title", "/v.mp4",
         rel["slot_at"], upload_status, heartbeat, NOW, NOW),
    )
    db.execute(
        "UPDATE pub_releases SET phase='upload_pending', status='running', deadline=? "
        "WHERE release_id=?",
        (NOW - HARD_STALL - 1, rid),
    )
    out = advance(db, db.execute("SELECT * FROM pub_releases").fetchone(),
                  Services(upload_running={"wait": True}), NOW)
    assert out == "waiting"
    assert db.execute("SELECT attempts FROM pub_releases").fetchone()[0] == 0


def test_reached_irreversible_reads_journal_not_current_phase(db):
    make(db)
    s = Services()
    run_to(db, s, "upload_running")
    rid = db.execute("SELECT release_id FROM pub_releases").fetchone()[0]
    db.execute("UPDATE pub_releases SET phase='created'")
    assert reached_irreversible(db, rid) is True


def test_second_release_for_same_item_impossible(db):
    _, pid = make(db)
    materialize(db, NOW)
    db.execute("UPDATE pub_plan_items SET status='planned' WHERE id=?", (pid,))
    materialize(db, NOW)
    assert db.execute("SELECT COUNT(*) FROM pub_releases").fetchone()[0] == 1


# ─── падения и зависания ───

def test_stuck_phase_is_retried_and_resources_freed(db):
    make(db)
    s = Services(source_ready={"wait": True})
    tick(db, s, NOW)
    tick(db, s, NOW)                       # ушёл в ожидание, срок ещё не вышел
    rel = db.execute("SELECT * FROM pub_releases").fetchone()
    tick(db, s, rel["deadline"] + 1)       # срок вышел
    rel = db.execute("SELECT * FROM pub_releases").fetchone()
    assert rel["attempts"] == 1
    assert s.released, "ресурсы зависшего выпуска должны освобождаться"
    assert "stalled" in phase_history(db, rel["release_id"])


def test_stuck_goes_to_quarantine_after_attempts(db):
    make(db)
    s = Services(source_ready={"wait": True})
    now = NOW
    for _ in range(MAX_ATTEMPTS + 3):
        tick(db, s, now)
        rel = db.execute("SELECT * FROM pub_releases").fetchone()
        now = (rel["deadline"] or now) + 1
    rel = db.execute("SELECT * FROM pub_releases").fetchone()
    assert rel["status"] == "blocked"
    assert "зависло" in rel["blocked_reason"]


def test_stuck_after_upload_started_goes_straight_to_quarantine(db):
    make(db)
    s = Services()
    run_to(db, s, "upload_running")
    rel = db.execute("SELECT * FROM pub_releases").fetchone()
    db.execute("UPDATE pub_releases SET status='running', deadline=?", (NOW - 1,))
    tick(db, s, NOW)
    rel = db.execute("SELECT * FROM pub_releases").fetchone()
    assert rel["status"] == "blocked"          # повторять заливку нельзя даже один раз


def test_item_failure_quarantines_only_that_row(db):
    make(db)                                    # первый канал
    cid2 = db.execute(
        "INSERT INTO pub_channels(tenant_id,uc,name,lead_days,created,updated)"
        " VALUES('t1','UC" + "b" * 22 + "','Второй',2,?,?)", (NOW, NOW)).lastrowid
    db.execute("INSERT INTO pub_plan_items(tenant_id,channel_id,donor_url,slot_at,created,updated)"
               " VALUES('t1',?,?,?,?,?)", (cid2, "https://donor/2", NOW + DAY, NOW, NOW))

    def boom(c, rel):
        if rel["channel_id"] != cid2:
            raise ItemFailed("донор недоступен")
        return {}

    s = Services(source_ready=boom)
    for _ in range(12):
        tick(db, s, NOW)
    rows = {r["channel_id"]: r for r in db.execute("SELECT * FROM pub_releases")}
    assert rows[cid2]["status"] in ("running", "done")
    bad = [r for cid, r in rows.items() if cid != cid2][0]
    assert bad["status"] == "blocked"
    assert "донор недоступен" in bad["blocked_reason"]


def test_handler_crash_is_quarantine_not_engine_crash(db):
    make(db)

    def boom(c, rel):
        raise RuntimeError("что-то лопнуло")

    rel = run_to(db, Services(source_ready=boom), "source_ready", limit=3)
    assert rel["status"] == "blocked"
    assert "что-то лопнуло" in rel["blocked_reason"]


# ─── общий ресурс ───

def test_resource_down_does_not_quarantine(db):
    make(db)

    def out_of_credits(c, rel):
        raise ResourceDown("картинки", "кончились кредиты")

    s = Services(media_ready=out_of_credits)
    for _ in range(6):
        tick(db, s, NOW)
    rel = db.execute("SELECT * FROM pub_releases").fetchone()
    assert rel["status"] == "pending"
    assert "ждём" in (rel["blocked_reason"] or "")
    assert db.execute("SELECT status FROM pub_plan_items").fetchone()[0] == "frozen"


def test_resource_recovery_continues_by_itself(db):
    make(db)
    state = {"down": True}

    def flaky(c, rel):
        if state["down"]:
            raise ResourceDown("картинки", "кончились кредиты")
        return {}

    s = Services(media_ready=flaky)
    for _ in range(4):
        tick(db, s, NOW)
    assert db.execute("SELECT phase FROM pub_releases").fetchone()[0] == "project_ready"
    state["down"] = False
    rel = run_to(db, s, "scheduled", limit=12)
    assert rel["phase"] == "scheduled"


def test_resource_wait_is_journalled(db):
    make(db)

    def down(c, rel):
        raise ResourceDown("рерайт", "сосед не отвечает")

    tick(db, Services(source_ready=down), NOW)
    rid = db.execute("SELECT release_id FROM pub_releases").fetchone()[0]
    assert "resource_wait" in phase_history(db, rid)


# ─── общее поведение тика ───

def test_tick_survives_broken_release(db):
    make(db)
    materialize(db, NOW)
    db.execute("UPDATE pub_releases SET phase='чепуха'")
    out = tick(db, Services(), NOW)
    assert out["failed"] == []


def test_blocked_release_is_not_picked_again(db):
    make(db)

    def boom(c, rel):
        raise ItemFailed("сломалось")

    s = Services(source_ready=boom)
    tick(db, s, NOW)
    before = len(s.calls)
    tick(db, s, NOW)
    assert len(s.calls) == before
    assert live_releases(db) == []


def test_no_handler_means_quarantine_not_crash(db):
    class Empty:
        def release_resources(self, c, rel):
            pass

    make(db)
    rel = run_to(db, Empty(), "source_ready", limit=3)
    assert rel["status"] == "blocked"
    assert "нет обработчика" in rel["blocked_reason"]
