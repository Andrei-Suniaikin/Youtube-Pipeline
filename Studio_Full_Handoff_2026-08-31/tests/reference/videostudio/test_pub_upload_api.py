#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты обмена с машиной заливки.

Проверяется не «ручка ответила», а четыре защиты от второго ролика на канале:
один профиль — одна заливка, повторный запрос возвращает то же задание, истёкшая
аренда ведёт в карантин, и успех определяется подтверждением Studio, а не кодом возврата.
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from pub_schema import ensure_pub_schema  # noqa: E402
from pub_upload_api import (  # noqa: E402
    UPLOAD_LEASE_SEC,
    read_verdict,
    sweep_stuck,
)

NOW = 1786900000.0


@pytest.fixture()
def db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_pub_schema(c)
    yield c
    c.close()


def job(c, release_id="r_1", profile=9002, status="queued", lease=None, host=None):
    c.execute(
        "INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,profile_id,"
        "channel_url,title,video_path,slot_at,status,lease_until,host,claim_token,"
        "created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (release_id, "t1", 1, "UC" + "a" * 22, profile,
         "https://studio.youtube.com/channel/UC" + "a" * 22 + "/", "Название ролика",
         "/data/p/render/1.mp4", NOW, status, lease, host,
         "upc_x" if status == "running" else None, NOW, NOW))
    return c.execute("SELECT * FROM pub_upload_jobs WHERE release_id=?",
                     (release_id,)).fetchone()


# ─── разбор вердикта ───

def test_scheduled_is_success():
    got = read_verdict({"result": {"video_id": "abc", "success": True,
                                   "exact_studio_evidence": {"studio_state": "SCHEDULED"}}})
    assert got["kind"] == "scheduled" and got["state"] == "done" and got["video_id"] == "abc"


def test_scheduled_without_success_flag_is_repair():
    got = read_verdict({"result": {"video_id": "abc", "success": False,
                                   "exact_studio_evidence": {"studio_state": "SCHEDULED"}}})
    assert got["kind"] == "needs_repair"


def test_public_is_published():
    got = read_verdict({"result": {"video_id": "abc",
                                   "exact_studio_evidence": {"studio_state": "PUBLIC"}}})
    assert got["kind"] == "published"


def test_success_flag_alone_is_not_enough():
    """Главная ловушка: успешный код возврата без подтверждения Studio — не успех."""
    got = read_verdict({"result": {"success": True, "video_id": "", "error": ""}})
    assert got["kind"] == "failed"


def test_video_id_without_confirmation_is_repair_not_success():
    got = read_verdict({"result": {"video_id": "abc", "success": False,
                                   "exact_studio_evidence": {"studio_state": "SCHEDULED_WRONG_TIME"}}})
    assert got["kind"] == "needs_repair"
    assert got["state"] == "repair"
    assert "SCHEDULED_WRONG_TIME" in got["error"]


def test_do_not_reupload_forbids_retry_even_without_video_id():
    got = read_verdict({"result": {"success": False, "do_not_reupload": True,
                                   "error": "RECEIPT_PERSIST_FAILED"}})
    assert got["kind"] == "needs_repair"


def test_plain_failure_can_be_retried():
    got = read_verdict({"result": {"success": False, "error": "browser died"}})
    assert got["kind"] == "failed" and got["state"] == "error"


def test_row_evidence_alone_is_repair_not_success():
    """Строковое подтверждение без точного — «нужен разбор»: слишком легко спутать ролики."""
    got = read_verdict({"result": {"video_id": "abc", "success": True,
                                   "studio_row_evidence": {"studio_state": "SCHEDULED"}}})
    assert got["kind"] == "needs_repair"


def test_garbage_body_is_broken():
    assert read_verdict("не объект")["kind"] == "broken"


def test_flat_body_without_result_key():
    got = read_verdict({"video_id": "abc", "success": True,
                        "exact_studio_evidence": {"studio_state": "SCHEDULED"}})
    assert got["kind"] == "scheduled"


def test_wrong_channel_in_evidence_is_repair():
    """Studio подтвердила SCHEDULED, но НЕ на том канале — это разбор, а не успех."""
    got = read_verdict({"result": {"video_id": "abc", "success": True,
                                   "exact_studio_evidence": {"studio_state": "SCHEDULED",
                                                             "verified_channel_uc": "UC_чужой"}}},
                       expected_uc="UC_наш")
    assert got["kind"] == "needs_repair"
    assert "не совпал" in got["error"]


def test_wrong_time_in_evidence_is_repair():
    got = read_verdict({"result": {"video_id": "abc", "success": True,
                                   "exact_studio_evidence": {"studio_state": "SCHEDULED",
                                                             "scheduled_time_seconds": 100}}},
                       expected_iso="2026-08-20T10:00+02:00")
    assert got["kind"] == "needs_repair"
    assert "время" in got["error"]


def test_matching_evidence_is_success():
    import datetime as dt
    want = int(dt.datetime.fromisoformat("2026-08-20T10:00+02:00").timestamp())
    got = read_verdict({"result": {"video_id": "abc", "success": True,
                                   "exact_studio_evidence": {"studio_state": "SCHEDULED",
                                                             "verified_channel_uc": "UC_наш",
                                                             "scheduled_time_seconds": want}}},
                       expected_uc="UC_наш", expected_iso="2026-08-20T10:00+02:00")
    assert got["kind"] == "scheduled"


# ─── зависшая нода ───

def test_expired_lease_goes_to_quarantine_not_back_to_queue(db):
    """Возврат в очередь здесь = второй ролик: нода могла молча продолжать лить."""
    job(db, status="running", lease=NOW - 1, host="minsk")
    moved = sweep_stuck(db, NOW)
    row = db.execute("SELECT status, error, claim_token FROM pub_upload_jobs").fetchone()
    assert moved == ["r_1"]
    assert row["status"] == "error"
    assert row["status"] != "queued"
    # Клейм СОХРАНЯЕТСЯ: молчавшая нода могла довести мастер до конца, и её поздний
    # вердикт с этим клеймом обязан доехать и поднять запись до успеха.
    assert row["claim_token"] == "upc_x"
    assert "молчит" in row["error"]


def test_live_lease_is_left_alone(db):
    job(db, status="running", lease=NOW + UPLOAD_LEASE_SEC, host="minsk")
    assert sweep_stuck(db, NOW) == []
    assert db.execute("SELECT status FROM pub_upload_jobs").fetchone()[0] == "running"


def test_queued_job_is_not_swept(db):
    job(db, status="queued")
    assert sweep_stuck(db, NOW) == []


# ─── хранилище задания ───

def test_one_job_per_release(db):
    job(db, "r_1")
    with pytest.raises(sqlite3.IntegrityError):
        job(db, "r_1")


def test_queue_is_ordered_by_slot(db):
    db.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,channel_url,"
               "title,video_path,slot_at,created,updated) VALUES('r_late','t1',1,'x','u','t',"
               "'/v.mp4',?,?,?)", (NOW + 999, NOW, NOW))
    job(db, "r_soon")
    order = [r[0] for r in db.execute(
        "SELECT release_id FROM pub_upload_jobs WHERE status='queued' ORDER BY slot_at, id")]
    assert order == ["r_soon", "r_late"]


# ─── правило по профилям: один занят, другие свободны ───

def _pick(db, host="minsk", worker="1", now=NOW):
    """Повторяем логику выдачи задания: она и есть правило параллели."""
    mine = db.execute("SELECT * FROM pub_upload_jobs WHERE status='running' AND host=?"
                      " AND worker=? ORDER BY claimed LIMIT 1", (host, worker)).fetchone()
    if mine is not None:
        return mine, True
    running = db.execute("SELECT profile_id, uc FROM pub_upload_jobs"
                         " WHERE status='running'").fetchall()
    busy_p = {r["profile_id"] for r in running if r["profile_id"] is not None}
    busy_c = {r["uc"] for r in running}
    rows = db.execute("SELECT * FROM pub_upload_jobs WHERE status='queued'"
                      " ORDER BY slot_at, id").fetchall()
    task = next((r for r in rows if r["profile_id"] not in busy_p and r["uc"] not in busy_c), None)
    return task, False


def job2(c, release_id, profile, uc, status="queued", host=None, worker=None, slot=NOW):
    c.execute(
        "INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,profile_id,channel_url,"
        "title,video_path,slot_at,status,host,worker,claim_token,lease_until,created,updated)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (release_id, "t1", 1, uc, profile, "https://studio/", "Название", "/v.mp4", slot,
         status, host, worker, "upc_x" if status == "running" else None,
         NOW + UPLOAD_LEASE_SEC if status == "running" else None, NOW, NOW))


def test_same_profile_is_never_given_twice(db):
    """Два ролика в одном профиле ломают мастер Studio — второе задание не выдаём."""
    job2(db, "r_busy", 9001, "UC_a", status="running", host="minsk", worker="1")
    job2(db, "r_wait", 9001, "UC_b")                   # тот же профиль
    task, _ = _pick(db, worker="2")
    assert task is None


def test_different_profiles_go_in_parallel(db):
    """А вот разные профили — независимые браузеры, их можно лить одновременно."""
    job2(db, "r_busy", 9001, "UC_a", status="running", host="minsk", worker="1")
    job2(db, "r_free", 9002, "UC_b")                   # другой профиль
    task, repeat = _pick(db, worker="2")
    assert task is not None and task["release_id"] == "r_free" and repeat is False


def test_one_node_can_hold_several_uploads(db):
    """Раньше повтор был привязан к машине — Минск смог бы лить только один ролик."""
    job2(db, "r_first", 9001, "UC_a", status="running", host="minsk", worker="1")
    job2(db, "r_second", 9002, "UC_b")
    task, _ = _pick(db, host="minsk", worker="2")
    assert task["release_id"] == "r_second"


def test_same_worker_gets_its_own_task_back(db):
    job2(db, "r_mine", 9001, "UC_a", status="running", host="minsk", worker="1")
    job2(db, "r_other", 9002, "UC_b")
    task, repeat = _pick(db, host="minsk", worker="1")
    assert task["release_id"] == "r_mine" and repeat is True


def test_same_channel_is_not_given_twice(db):
    """Один канал в двух профилях — ошибка данных, но два мастера на нём тоже нельзя."""
    job2(db, "r_busy", 9001, "UC_same", status="running", host="minsk", worker="1")
    job2(db, "r_dup", 9002, "UC_same")
    task, _ = _pick(db, worker="2")
    assert task is None


def test_nearest_slot_wins_among_free_profiles(db):
    job2(db, "r_late", 9002, "UC_a", slot=NOW + 500)
    job2(db, "r_soon", 9003, "UC_b", slot=NOW)
    task, _ = _pick(db)
    assert task["release_id"] == "r_soon"
