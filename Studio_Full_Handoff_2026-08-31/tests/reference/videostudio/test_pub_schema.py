#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты схемы контура публикаций.

Проверяются не «таблица создалась», а гарантии, ради которых схема такая:
один канал = один UC, один слот = один ролик, одна версия строки = один выпуск.
Внешних ключей в базе нет, поэтому ловить дубли больше нечем — если эти индексы
пропадут, ошибка будет тихой.
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from pub_schema import ensure_pub_schema  # noqa: E402

NOW = 1786900000.0


@pytest.fixture()
def db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_pub_schema(c)
    yield c
    c.close()


def add_channel(c, uc="UC" + "a" * 22, name="Канал", tenant="t1"):
    cur = c.execute(
        "INSERT INTO pub_channels(tenant_id,uc,name,created,updated) VALUES(?,?,?,?,?)",
        (tenant, uc, name, NOW, NOW),
    )
    return cur.lastrowid


def add_plan(c, channel_id, slot_at, status="planned", tenant="t1", url="https://x/1"):
    cur = c.execute(
        "INSERT INTO pub_plan_items(tenant_id,channel_id,donor_url,slot_at,status,created,updated)"
        " VALUES(?,?,?,?,?,?,?)",
        (tenant, channel_id, url, slot_at, status, NOW, NOW),
    )
    return cur.lastrowid


def add_release(c, plan_id, version=1, rid="r_1", channel_id=1, tenant="t1"):
    cur = c.execute(
        "INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
        "channel_id,uc,slot_at,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
        (rid, tenant, plan_id, version, channel_id, "UC" + "a" * 22, NOW, NOW, NOW),
    )
    return cur.lastrowid


# ─── состав схемы ───

def test_creates_all_tables(db):
    got = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'pub_%'")}
    assert got == {
        "pub_hosts", "pub_profiles", "pub_channels", "pub_tags", "pub_channel_tags",
        "pub_plan_items", "pub_releases", "pub_release_events", "pub_release_jobs",
        "pub_upload_jobs", "pub_release_log",
    }


def test_is_idempotent(db):
    ensure_pub_schema(db)
    ensure_pub_schema(db)
    assert db.execute("SELECT COUNT(*) FROM pub_channels").fetchone()[0] == 0


def test_existing_data_survives_repeat_call(db):
    add_channel(db)
    ensure_pub_schema(db)
    assert db.execute("SELECT COUNT(*) FROM pub_channels").fetchone()[0] == 1


# ─── канал ───

def test_uc_is_unique_across_whole_system(db):
    add_channel(db, uc="UC" + "b" * 22, tenant="t1")
    with pytest.raises(sqlite3.IntegrityError):
        add_channel(db, uc="UC" + "b" * 22, tenant="t2")  # даже у другого пользователя


def test_two_channels_may_share_profile(db):
    a = add_channel(db, uc="UC" + "c" * 22)
    b = add_channel(db, uc="UC" + "d" * 22)
    db.execute("UPDATE pub_channels SET profile_id=9002 WHERE id IN (?,?)", (a, b))
    assert db.execute("SELECT COUNT(*) FROM pub_channels WHERE profile_id=9002").fetchone()[0] == 2


def test_channel_defaults_are_safe(db):
    cid = add_channel(db)
    row = db.execute("SELECT * FROM pub_channels WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "active"
    assert row["horizon_days"] == 3
    assert row["video_mode"] == "slideshow"
    assert row["t2v_first_n"] == 0
    assert row["schedule"] == "{}"


def test_name_is_required(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO pub_channels(tenant_id,uc,created,updated) VALUES(?,?,?,?)",
                   ("t1", "UC" + "e" * 22, NOW, NOW))


# ─── слоты ───

def test_one_slot_holds_one_item(db):
    cid = add_channel(db)
    add_plan(db, cid, 1786900000.0)
    with pytest.raises(sqlite3.IntegrityError):
        add_plan(db, cid, 1786900000.0)


def test_same_slot_on_another_channel_is_fine(db):
    a = add_channel(db, uc="UC" + "f" * 22)
    b = add_channel(db, uc="UC" + "g" * 22)
    add_plan(db, a, 1786900000.0)
    add_plan(db, b, 1786900000.0)  # каналы независимы
    assert db.execute("SELECT COUNT(*) FROM pub_plan_items").fetchone()[0] == 2


def test_cancelled_item_frees_its_slot(db):
    cid = add_channel(db)
    first = add_plan(db, cid, 1786900000.0)
    db.execute("UPDATE pub_plan_items SET status='cancelled' WHERE id=?", (first,))
    add_plan(db, cid, 1786900000.0)  # отменённая строка слот больше не держит
    assert db.execute(
        "SELECT COUNT(*) FROM pub_plan_items WHERE status='planned'").fetchone()[0] == 1


def test_unscheduled_items_do_not_collide(db):
    cid = add_channel(db)
    add_plan(db, cid, None)
    add_plan(db, cid, None)  # ещё не назначены во время — не конфликтуют
    assert db.execute("SELECT COUNT(*) FROM pub_plan_items").fetchone()[0] == 2


def test_running_item_still_holds_slot(db):
    cid = add_channel(db)
    first = add_plan(db, cid, 1786900000.0)
    db.execute("UPDATE pub_plan_items SET status='running' WHERE id=?", (first,))
    with pytest.raises(sqlite3.IntegrityError):
        add_plan(db, cid, 1786900000.0)


# ─── выпуски ───

def test_one_release_per_plan_version(db):
    cid = add_channel(db)
    pid = add_plan(db, cid, 1786900000.0)
    add_release(db, pid, version=1, rid="r_1", channel_id=cid)
    with pytest.raises(sqlite3.IntegrityError):
        add_release(db, pid, version=1, rid="r_2", channel_id=cid)


def test_new_version_gets_its_own_release(db):
    cid = add_channel(db)
    pid = add_plan(db, cid, 1786900000.0)
    add_release(db, pid, version=1, rid="r_1", channel_id=cid)
    add_release(db, pid, version=2, rid="r_2", channel_id=cid)
    assert db.execute("SELECT COUNT(*) FROM pub_releases").fetchone()[0] == 2


def test_release_id_is_unique(db):
    cid = add_channel(db)
    p1 = add_plan(db, cid, 1786900000.0)
    p2 = add_plan(db, cid, 1786903600.0)
    add_release(db, p1, rid="r_same", channel_id=cid)
    with pytest.raises(sqlite3.IntegrityError):
        add_release(db, p2, rid="r_same", channel_id=cid)


def test_release_starts_pending(db):
    cid = add_channel(db)
    pid = add_plan(db, cid, 1786900000.0)
    add_release(db, pid, channel_id=cid)
    row = db.execute("SELECT * FROM pub_releases WHERE release_id='r_1'").fetchone()
    assert (row["status"], row["phase"], row["attempts"]) == ("pending", "created", 0)


# ─── журнал фаз ───

def test_phase_sequence_cannot_repeat(db):
    db.execute("INSERT INTO pub_release_events(release_id,seq,phase,created) VALUES(?,?,?,?)",
               ("r_1", 1, "upload_started", NOW))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO pub_release_events(release_id,seq,phase,created) VALUES(?,?,?,?)",
                   ("r_1", 1, "upload_started", NOW))


def test_phase_sequence_is_per_release(db):
    for rid in ("r_1", "r_2"):
        db.execute("INSERT INTO pub_release_events(release_id,seq,phase,created) VALUES(?,?,?,?)",
                   (rid, 1, "created", NOW))
    assert db.execute("SELECT COUNT(*) FROM pub_release_events").fetchone()[0] == 2


# ─── задачи соседним сервисам ───

def test_one_job_per_service_and_kind(db):
    db.execute("INSERT INTO pub_release_jobs(release_id,service,kind,created,updated)"
               " VALUES(?,?,?,?,?)", ("r_1", "rewrite", "rewrite", NOW, NOW))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO pub_release_jobs(release_id,service,kind,created,updated)"
                   " VALUES(?,?,?,?,?)", ("r_1", "rewrite", "rewrite", NOW, NOW))


def test_different_kinds_coexist(db):
    for service, kind in (("rewrite", "rewrite"), ("vsgen", "tts"), ("cover", "cover")):
        db.execute("INSERT INTO pub_release_jobs(release_id,service,kind,created,updated)"
                   " VALUES(?,?,?,?,?)", ("r_1", service, kind, NOW, NOW))
    assert db.execute("SELECT COUNT(*) FROM pub_release_jobs").fetchone()[0] == 3


# ─── справочники и теги ───

def test_host_code_unique_per_tenant_only(db):
    db.execute("INSERT INTO pub_hosts(tenant_id,code,created) VALUES(?,?,?)", ("t1", "mac", NOW))
    db.execute("INSERT INTO pub_hosts(tenant_id,code,created) VALUES(?,?,?)", ("t2", "mac", NOW))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO pub_hosts(tenant_id,code,created) VALUES(?,?,?)", ("t1", "mac", NOW))


def test_profile_serial_unique_per_tenant(db):
    db.execute("INSERT INTO pub_profiles(tenant_id,serial,created) VALUES(?,?,?)", ("t1", "9002", NOW))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO pub_profiles(tenant_id,serial,created) VALUES(?,?,?)", ("t1", "9002", NOW))


def test_tag_cannot_stick_twice(db):
    cid = add_channel(db)
    db.execute("INSERT INTO pub_tags(tenant_id,name,created) VALUES(?,?,?)", ("t1", "ww2", NOW))
    tid = db.execute("SELECT id FROM pub_tags").fetchone()[0]
    db.execute("INSERT INTO pub_channel_tags(channel_id,tag_id) VALUES(?,?)", (cid, tid))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO pub_channel_tags(channel_id,tag_id) VALUES(?,?)", (cid, tid))
