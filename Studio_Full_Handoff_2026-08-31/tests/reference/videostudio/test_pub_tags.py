#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты тегов и очереди донорских ссылок."""

import datetime as dt
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from pub_schema import ensure_pub_schema  # noqa: E402
from pub_tags import (  # noqa: E402
    add_links,
    all_tags,
    coverage,
    parse_links,
    parse_tags,
    plan_rows,
    set_tags,
    tag_names,
    taken_slots,
)

from zoneinfo import ZoneInfo  # noqa: E402

WARSAW = ZoneInfo("Europe/Warsaw")
NOW = dt.datetime(2026, 8, 17, 6, 0, tzinfo=WARSAW)   # понедельник, до первого выхода
TS = NOW.timestamp()


@pytest.fixture()
def db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_pub_schema(c)
    yield c
    c.close()


def channel(c, schedule=None, horizon=3, uc="UC" + "a" * 22, tz="Europe/Warsaw"):
    sched = json.dumps(schedule if schedule is not None else {"*": ["10:00", "18:00"]})
    cid = c.execute(
        "INSERT INTO pub_channels(tenant_id,uc,name,tz,schedule,horizon_days,created,updated)"
        " VALUES('t1',?,?,?,?,?,?,?)", (uc, "Канал", tz, sched, horizon, TS, TS)).lastrowid
    return c.execute("SELECT * FROM pub_channels WHERE id=?", (cid,)).fetchone()


# ─── теги ───

def test_tags_created_on_the_fly(db):
    ch = channel(db)
    set_tags(db, "t1", ch["id"], ["WW2", "Приоритет"], TS)
    assert set(tag_names(db, ch["id"])) == {"ww2", "приоритет"}


def test_tags_are_normalized(db):
    ch = channel(db)
    set_tags(db, "t1", ch["id"], ["  WW2  ", "ww2", "Тёмная   Психология"], TS)
    got = set(tag_names(db, ch["id"]))
    assert got == {"ww2", "тёмная психология"}   # регистр вниз, лишние пробелы схлопнуты


def test_tags_replace_not_append(db):
    ch = channel(db)
    set_tags(db, "t1", ch["id"], ["a", "b"], TS)
    set_tags(db, "t1", ch["id"], ["c"], TS)
    assert tag_names(db, ch["id"]) == ["c"]


def test_same_tag_shared_between_channels(db):
    a = channel(db, uc="UC" + "b" * 22)
    b = channel(db, uc="UC" + "c" * 22)
    set_tags(db, "t1", a["id"], ["ww2"], TS)
    set_tags(db, "t1", b["id"], ["ww2"], TS)
    assert db.execute("SELECT COUNT(*) FROM pub_tags").fetchone()[0] == 1
    assert dict(all_tags(db, "t1"))["ww2"] == 2


def test_unused_tag_not_listed(db):
    ch = channel(db)
    set_tags(db, "t1", ch["id"], ["ww2"], TS)
    set_tags(db, "t1", ch["id"], [], TS)
    assert all_tags(db, "t1") == []


def test_parse_tags_splits_by_comma_and_newline():
    assert parse_tags("ww2, сон\nтест;ещё") == ["ww2", " сон", "тест", "ещё"]


# ─── разбор ссылок ───

def test_parse_links_one_per_line():
    got = parse_links("https://a/1\nhttps://b/2")
    assert got == ["https://a/1", "https://b/2"]


def test_parse_links_dedupes():
    assert parse_links("https://a/1\nhttps://a/1") == ["https://a/1"]


def test_parse_links_ignores_junk():
    assert parse_links("просто текст\n\n  ") == []


def test_parse_links_picks_url_from_text():
    assert parse_links("смотри https://a/1 вот") == ["https://a/1"]


# ─── раскладка по выходам ───

def test_links_take_nearest_free_slots(db):
    ch = channel(db)
    res = add_links(db, "t1", ch, ["https://a/1", "https://a/2"], TS)
    assert len(res["placed"]) == 2
    assert [s.strftime("%d.%m %H:%M") for s in res["placed"]] == ["17.08 10:00", "17.08 18:00"]


def test_second_batch_continues_after_first(db):
    ch = channel(db)
    add_links(db, "t1", ch, ["https://a/1"], TS)
    res = add_links(db, "t1", ch, ["https://a/2"], TS)
    assert res["placed"][0].strftime("%d.%m %H:%M") == "17.08 18:00"


def test_duplicate_link_is_skipped(db):
    ch = channel(db)
    add_links(db, "t1", ch, ["https://a/1"], TS)
    res = add_links(db, "t1", ch, ["https://a/1"], TS)
    assert res["skipped"] == ["https://a/1"]
    assert len(plan_rows(db, ch["id"])) == 1


def test_extra_links_wait_without_slot(db):
    ch = channel(db, horizon=1)          # только сегодня: два выхода
    res = add_links(db, "t1", ch, [f"https://a/{i}" for i in range(5)], TS)
    assert len(res["placed"]) == 2
    assert res["queued"] == 3
    waiting = [r for r in plan_rows(db, ch["id"]) if r["slot_at"] is None]
    assert len(waiting) == 3


def test_links_without_schedule_are_kept_waiting(db):
    ch = channel(db, schedule={})
    res = add_links(db, "t1", ch, ["https://a/1"], TS)
    assert res["queued"] == 1
    assert plan_rows(db, ch["id"])[0]["donor_url"] == "https://a/1"


def test_cancelled_link_frees_its_slot(db):
    ch = channel(db)
    add_links(db, "t1", ch, ["https://a/1"], TS)
    row = plan_rows(db, ch["id"])[0]
    db.execute("UPDATE pub_plan_items SET status='cancelled' WHERE id=?", (row["id"],))
    res = add_links(db, "t1", ch, ["https://a/2"], TS)
    assert res["placed"][0].timestamp() == row["slot_at"]


def test_taken_slots_ignores_cancelled(db):
    ch = channel(db)
    add_links(db, "t1", ch, ["https://a/1"], TS)
    assert len(taken_slots(db, ch["id"])) == 1
    db.execute("UPDATE pub_plan_items SET status='cancelled'")
    assert taken_slots(db, ch["id"]) == []


# ─── покрытие ───

def test_coverage_marks_empty_slots(db):
    ch = channel(db, horizon=2)
    add_links(db, "t1", ch, ["https://a/1"], TS)
    got = coverage(db, ch, NOW)
    assert len(got) == 4                       # два дня по два выхода
    assert got[0][1]["donor_url"] == "https://a/1"
    assert [bool(item) for _, item in got] == [True, False, False, False]


def test_coverage_empty_without_schedule(db):
    ch = channel(db, schedule={})
    assert coverage(db, ch, NOW) == []


def test_coverage_counts_only_live_rows(db):
    ch = channel(db, horizon=1)
    add_links(db, "t1", ch, ["https://a/1"], TS)
    db.execute("UPDATE pub_plan_items SET status='cancelled'")
    assert [bool(i) for _, i in coverage(db, ch, NOW)] == [False, False]


# ─── настоящее состояние канала ───

def test_state_counts_real_links(db):
    from pub_tags import channel_state, plan_by_channel
    ch = channel(db, horizon=2)                      # 2 дня × 2 выхода = 4
    add_links(db, "t1", ch, ["https://a/1", "https://a/2"], TS)
    taken = plan_by_channel(db, "t1", TS - 86400, TS + 10 * 86400).get(ch["id"], set())
    st = channel_state(ch, taken, "", NOW)
    assert st["kind"] == "warn"
    assert st["filled"] == 2 and st["total"] == 4
    assert st["text"] == "не хватает 2 ссылок"


def test_state_ok_when_all_slots_filled(db):
    from pub_tags import channel_state, plan_by_channel
    ch = channel(db, horizon=1)
    add_links(db, "t1", ch, ["https://a/1", "https://a/2"], TS)
    taken = plan_by_channel(db, "t1", TS - 86400, TS + 10 * 86400).get(ch["id"], set())
    st = channel_state(ch, taken, "", NOW)
    assert st["kind"] == "ok" and st["text"] == "закрыто 2 из 2"


def test_state_without_schedule(db):
    from pub_tags import channel_state
    ch = channel(db, schedule={})
    assert channel_state(ch, set(), "", NOW)["kind"] == "none"


def test_trouble_wins_over_counts(db):
    from pub_tags import channel_state
    ch = channel(db)
    st = channel_state(ch, set(), "мёртвый прокси профиля 9005", NOW)
    assert st["kind"] == "error" and "мёртвый прокси" in st["text"]


def test_one_query_for_all_channels(db):
    from pub_tags import plan_by_channel
    a = channel(db, uc="UC" + "x" * 22)
    b = channel(db, uc="UC" + "y" * 22)
    add_links(db, "t1", a, ["https://a/1"], TS)
    add_links(db, "t1", b, ["https://b/1"], TS)
    got = plan_by_channel(db, "t1", TS - 86400, TS + 10 * 86400)
    assert set(got) == {a["id"], b["id"]}


def test_troubles_are_collected(db, monkeypatch):
    import pub_tags
    monkeypatch.setattr(pub_tags, "donor_title", lambda url: "")
    ch = channel(db)
    add_links(db, "t1", ch, ["https://a/1"], TS)
    item = plan_rows(db, ch["id"])[0]
    db.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
               "channel_id,uc,slot_at,status,blocked_reason,created,updated)"
               " VALUES('r_1','t1',?,1,?,?,?,'blocked','нет субтитров',?,?)",
               (item["id"], ch["id"], ch["uc"], TS, TS, TS))
    assert pub_tags.troubles_by_channel(db, "t1")[ch["id"]] == "нет субтитров"


def test_cancelled_row_does_not_paint_channel_broken(db, monkeypatch):
    """Владелец убрал сломанную строку — канал не должен вечно гореть «сбоем»."""
    import pub_tags
    monkeypatch.setattr(pub_tags, "donor_title", lambda url: "")
    ch = channel(db)
    add_links(db, "t1", ch, ["https://a/1"], TS)
    item = plan_rows(db, ch["id"])[0]
    db.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
               "channel_id,uc,slot_at,status,blocked_reason,created,updated)"
               " VALUES('r_1','t1',?,1,?,?,?,'blocked','нет субтитров',?,?)",
               (item["id"], ch["id"], ch["uc"], TS, TS, TS))
    db.execute("UPDATE pub_plan_items SET status='cancelled' WHERE id=?", (item["id"],))
    assert ch["id"] not in pub_tags.troubles_by_channel(db, "t1")


# ─── перетаскивание ───

def test_move_to_empty_slot(db):
    from pub_tags import move_item
    ch = channel(db)
    add_links(db, "t1", ch, ["https://a/1"], TS)
    row = plan_rows(db, ch["id"])[0]
    target = row["slot_at"] + 8 * 3600
    move_item(db, row["id"], target, TS)
    assert plan_rows(db, ch["id"])[0]["slot_at"] == target


def test_move_swaps_two_links(db):
    from pub_tags import move_item
    ch = channel(db)
    add_links(db, "t1", ch, ["https://a/1", "https://a/2"], TS)
    first, second = plan_rows(db, ch["id"])
    move_item(db, first["id"], second["slot_at"], TS)
    now_rows = {r["donor_url"]: r["slot_at"] for r in plan_rows(db, ch["id"])}
    assert now_rows["https://a/1"] == second["slot_at"]
    assert now_rows["https://a/2"] == first["slot_at"]


def test_cannot_move_item_already_in_work(db):
    from pub_tags import move_item
    ch = channel(db)
    add_links(db, "t1", ch, ["https://a/1"], TS)
    row = plan_rows(db, ch["id"])[0]
    db.execute("UPDATE pub_plan_items SET frozen=1 WHERE id=?", (row["id"],))
    with pytest.raises(ValueError):
        move_item(db, row["id"], row["slot_at"] + 3600, TS)


def test_cannot_swap_with_item_in_work(db):
    from pub_tags import move_item
    ch = channel(db)
    add_links(db, "t1", ch, ["https://a/1", "https://a/2"], TS)
    first, second = plan_rows(db, ch["id"])
    db.execute("UPDATE pub_plan_items SET frozen=1 WHERE id=?", (second["id"],))
    with pytest.raises(ValueError):
        move_item(db, first["id"], second["slot_at"], TS)


# ─── ссылка встаёт в выбранный выход ───

def test_link_goes_into_chosen_slot(db, monkeypatch):
    """Поле в пустой строке ставит ссылку ИМЕННО туда, а не в ближайший свободный."""
    import pub_tags
    monkeypatch.setattr(pub_tags, "donor_title", lambda url: "")
    ch = channel(db, horizon=3)
    slots = [s for s, _ in coverage(db, ch, NOW)]
    third = slots[2].timestamp()
    add_links(db, "t1", ch, ["https://a/late"], TS, slot=third)
    row = plan_rows(db, ch["id"])[0]
    assert row["slot_at"] == third
    assert row["slot_mode"] == "exact"


def test_without_slot_takes_nearest(db, monkeypatch):
    import pub_tags
    monkeypatch.setattr(pub_tags, "donor_title", lambda url: "")
    ch = channel(db)
    add_links(db, "t1", ch, ["https://a/1"], TS)
    row = plan_rows(db, ch["id"])[0]
    assert row["slot_mode"] == "auto"
    assert row["slot_at"] == coverage(db, ch, NOW)[0][0].timestamp()


def test_donor_title_is_saved(db, monkeypatch):
    import pub_tags
    monkeypatch.setattr(pub_tags, "donor_title", lambda url: "Настоящее название ролика")
    ch = channel(db)
    add_links(db, "t1", ch, ["https://a/1"], TS)
    assert plan_rows(db, ch["id"])[0]["donor_title"] == "Настоящее название ролика"


def test_missing_title_does_not_break_insert(db, monkeypatch):
    import pub_tags
    monkeypatch.setattr(pub_tags, "donor_title", lambda url: "")
    ch = channel(db)
    add_links(db, "t1", ch, ["https://a/1"], TS)
    assert plan_rows(db, ch["id"])[0]["donor_url"] == "https://a/1"


# ─── снятие ролика из очереди ───

def _release_for(db, item_id, phase="created", status="running"):
    db.execute(
        "INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
        "channel_id,uc,slot_at,phase,status,created,updated)"
        " VALUES('r_c','t1',?,1,1,'UC" + "a" * 22 + "',?,?,?,?,?)",
        (item_id, TS, phase, status, TS, TS))
    db.execute("INSERT INTO pub_release_events(release_id,seq,phase,created)"
               " VALUES('r_c',1,?,?)", (phase, TS))


def test_cancel_stops_started_release(db, monkeypatch):
    """Убрал строку — система должна перестать делать ролик, а не доделать втихую."""
    import pub_tags
    monkeypatch.setattr(pub_tags, "donor_title", lambda url: "")
    ch = channel(db)
    add_links(db, "t1", ch, ["https://a/1"], TS)
    item = plan_rows(db, ch["id"])[0]
    _release_for(db, item["id"], phase="media_ready")
    pub_tags.cancel_item(db, item["id"], TS)
    assert db.execute("SELECT status FROM pub_plan_items WHERE id=?", (item["id"],)).fetchone()[0] == "cancelled"
    assert db.execute("SELECT status FROM pub_releases").fetchone()[0] == "cancelled"


def test_cancel_refuses_when_upload_started(db, monkeypatch):
    """Заливка уже началась — из очереди убирать поздно, иначе соврём про результат."""
    import pub_tags
    monkeypatch.setattr(pub_tags, "donor_title", lambda url: "")
    ch = channel(db)
    add_links(db, "t1", ch, ["https://a/1"], TS)
    item = plan_rows(db, ch["id"])[0]
    _release_for(db, item["id"], phase="upload_running")
    with pytest.raises(ValueError):
        pub_tags.cancel_item(db, item["id"], TS)
    assert db.execute("SELECT status FROM pub_plan_items WHERE id=?", (item["id"],)).fetchone()[0] != "cancelled"


def test_cancel_without_release_just_removes_row(db, monkeypatch):
    import pub_tags
    monkeypatch.setattr(pub_tags, "donor_title", lambda url: "")
    ch = channel(db)
    add_links(db, "t1", ch, ["https://a/1"], TS)
    item = plan_rows(db, ch["id"])[0]
    pub_tags.cancel_item(db, item["id"], TS)
    assert plan_rows(db, ch["id"]) == []


def test_cancelled_slot_can_be_reused(db, monkeypatch):
    import pub_tags
    monkeypatch.setattr(pub_tags, "donor_title", lambda url: "")
    ch = channel(db)
    add_links(db, "t1", ch, ["https://a/1"], TS)
    item = plan_rows(db, ch["id"])[0]
    pub_tags.cancel_item(db, item["id"], TS)
    add_links(db, "t1", ch, ["https://a/2"], TS, slot=item["slot_at"])
    assert plan_rows(db, ch["id"])[0]["slot_at"] == item["slot_at"]
