#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты расчёта слотов публикации канала."""

import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from channel_slots import (  # noqa: E402
    ScheduleError,
    next_free_slot,
    normalize_schedule,
    parse_time,
    slots_by_day,
    upcoming_slots,
)

from zoneinfo import ZoneInfo  # noqa: E402

WARSAW = ZoneInfo("Europe/Warsaw")
BANGKOK = ZoneInfo("Asia/Bangkok")

DAILY_TWICE = {"*": ["10:00", "18:00"]}


def at(y, m, d, hh, mm, zone=WARSAW):
    return dt.datetime(y, m, d, hh, mm, tzinfo=zone)


# ─── разбор расписания ───

def test_parse_time_ok():
    assert parse_time("10:00") == (10, 0)
    assert parse_time(" 9:05 ") == (9, 5)
    assert parse_time("23:59") == (23, 59)


@pytest.mark.parametrize("bad", ["24:00", "10:60", "10", "abc", "", "10:0"])
def test_parse_time_rejects_garbage(bad):
    with pytest.raises(ScheduleError):
        parse_time(bad)


def test_normalize_sorts_and_dedupes():
    got = normalize_schedule({"*": ["18:00", "10:00", "10:00"]})
    assert got == {"*": [(10, 0), (18, 0)]}


def test_normalize_named_days():
    got = normalize_schedule({"mon": ["10:00"], "sat": ["12:30"]})
    assert got == {"mon": [(10, 0)], "sat": [(12, 30)]}


def test_normalize_rejects_mixing_star_and_days():
    with pytest.raises(ScheduleError):
        normalize_schedule({"*": ["10:00"], "mon": ["12:00"]})


def test_normalize_rejects_unknown_day():
    with pytest.raises(ScheduleError):
        normalize_schedule({"понедельник": ["10:00"]})


def test_normalize_rejects_empty():
    with pytest.raises(ScheduleError):
        normalize_schedule({})


def test_single_time_as_string_is_accepted():
    assert normalize_schedule({"*": "10:00"}) == {"*": [(10, 0)]}


# ─── горизонт ───

def test_two_per_day_three_days_gives_six_slots():
    now = at(2026, 8, 17, 6, 0)
    slots = upcoming_slots(DAILY_TWICE, "Europe/Warsaw", 3, now)
    assert len(slots) == 6
    assert slots[0] == at(2026, 8, 17, 10, 0)
    assert slots[-1] == at(2026, 8, 19, 18, 0)


def test_past_times_today_are_skipped():
    now = at(2026, 8, 17, 12, 0)  # 10:00 уже прошло
    slots = upcoming_slots(DAILY_TWICE, "Europe/Warsaw", 3, now)
    assert len(slots) == 5
    assert slots[0] == at(2026, 8, 17, 18, 0)


def test_slot_exactly_now_is_not_free():
    now = at(2026, 8, 17, 10, 0)
    slots = upcoming_slots(DAILY_TWICE, "Europe/Warsaw", 1, now)
    assert slots == [at(2026, 8, 17, 18, 0)]


def test_result_is_sorted():
    now = at(2026, 8, 17, 0, 30)
    slots = upcoming_slots({"*": ["22:00", "07:00", "13:00"]}, "Europe/Warsaw", 2, now)
    assert slots == sorted(slots)
    assert len(slots) == 6


def test_named_days_only_fire_on_their_weekday():
    # 17.08.2026 — понедельник.
    now = at(2026, 8, 17, 0, 1)
    slots = upcoming_slots({"mon": ["10:00"], "wed": ["10:00"]}, "Europe/Warsaw", 3, now)
    assert [s.date().day for s in slots] == [17, 19]


@pytest.mark.parametrize("bad", [0, -1, 91, 1.5, True])
def test_horizon_is_validated(bad):
    with pytest.raises(ScheduleError):
        upcoming_slots(DAILY_TWICE, "Europe/Warsaw", bad, at(2026, 8, 17, 6, 0))


def test_unknown_timezone_rejected():
    with pytest.raises(ScheduleError):
        upcoming_slots(DAILY_TWICE, "Europe/Атлантида", 3, at(2026, 8, 17, 6, 0))


# ─── перевод часов ───

def test_spring_forward_skips_nonexistent_time():
    """29.03.2026 в Варшаве 02:00→03:00: слота 02:30 в этот день не существует."""
    now = dt.datetime(2026, 3, 28, 12, 0, tzinfo=WARSAW)
    slots = upcoming_slots({"*": ["02:30"]}, "Europe/Warsaw", 3, now)
    dates = [s.date() for s in slots]
    assert dt.date(2026, 3, 29) not in dates
    assert dt.date(2026, 3, 30) in dates


def test_normal_time_survives_dst_night():
    """В ту же ночь 10:00 никуда не девается — пропускаем только несуществующий час."""
    now = dt.datetime(2026, 3, 28, 12, 0, tzinfo=WARSAW)
    slots = upcoming_slots({"*": ["10:00"]}, "Europe/Warsaw", 3, now)
    assert [s.date() for s in slots] == [
        dt.date(2026, 3, 29),
        dt.date(2026, 3, 30),
    ]


def test_local_time_stays_the_same_across_dst():
    """Смысл таймзоны: 10:00 остаётся 10:00, хотя смещение от UTC меняется."""
    now = dt.datetime(2026, 3, 28, 6, 0, tzinfo=WARSAW)
    slots = upcoming_slots({"*": ["10:00"]}, "Europe/Warsaw", 3, now)
    assert all(s.hour == 10 for s in slots)
    offsets = {s.utcoffset() for s in slots}
    assert len(offsets) == 2  # до перевода и после — смещения разные


def test_zone_without_dst_is_stable():
    now = dt.datetime(2026, 3, 28, 6, 0, tzinfo=BANGKOK)
    slots = upcoming_slots({"*": ["10:00"]}, "Asia/Bangkok", 3, now)
    assert {s.utcoffset() for s in slots} == {dt.timedelta(hours=7)}


def test_slots_are_absolute_moments_not_wall_clock():
    """Один и тот же слот, посчитанный из разных зон «сейчас», совпадает."""
    a = upcoming_slots(DAILY_TWICE, "Europe/Warsaw", 1, at(2026, 8, 17, 6, 0))
    b = upcoming_slots(
        DAILY_TWICE, "Europe/Warsaw", 1, dt.datetime(2026, 8, 17, 11, 0, tzinfo=BANGKOK)
    )
    assert a == b


# ─── ближайший свободный ───

def test_next_free_slot_takes_first_when_nothing_busy():
    now = at(2026, 8, 17, 6, 0)
    assert next_free_slot(DAILY_TWICE, "Europe/Warsaw", [], 3, now) == at(2026, 8, 17, 10, 0)


def test_next_free_slot_skips_taken():
    now = at(2026, 8, 17, 6, 0)
    taken = [at(2026, 8, 17, 10, 0), at(2026, 8, 17, 18, 0)]
    assert next_free_slot(DAILY_TWICE, "Europe/Warsaw", taken, 3, now) == at(2026, 8, 18, 10, 0)


def test_next_free_slot_compares_by_absolute_moment():
    """Занятый слот, записанный в другой зоне, всё равно считается занятым."""
    now = at(2026, 8, 17, 6, 0)
    taken = [dt.datetime(2026, 8, 17, 15, 0, tzinfo=BANGKOK)]  # = 10:00 в Варшаве
    assert next_free_slot(DAILY_TWICE, "Europe/Warsaw", taken, 3, now) == at(2026, 8, 17, 18, 0)


def test_next_free_slot_returns_none_when_horizon_full():
    now = at(2026, 8, 17, 6, 0)
    taken = upcoming_slots(DAILY_TWICE, "Europe/Warsaw", 2, now)
    assert next_free_slot(DAILY_TWICE, "Europe/Warsaw", taken, 2, now) is None


def test_next_free_slot_ignores_none_entries():
    now = at(2026, 8, 17, 6, 0)
    taken = [None, at(2026, 8, 17, 10, 0)]
    assert next_free_slot(DAILY_TWICE, "Europe/Warsaw", taken, 3, now) == at(2026, 8, 17, 18, 0)


# ─── строка календаря ───

def test_slots_by_day_keeps_empty_days():
    now = at(2026, 8, 17, 0, 1)  # понедельник
    rows = slots_by_day({"mon": ["10:00"]}, "Europe/Warsaw", 3, now)
    assert [len(v) for _, v in rows] == [1, 0, 0]
    assert [d.day for d, _ in rows] == [17, 18, 19]


def test_slots_by_day_covers_full_horizon():
    now = at(2026, 8, 17, 20, 0)  # оба сегодняшних слота уже прошли
    rows = slots_by_day(DAILY_TWICE, "Europe/Warsaw", 3, now)
    assert len(rows) == 3
    assert [len(v) for _, v in rows] == [0, 2, 2]
