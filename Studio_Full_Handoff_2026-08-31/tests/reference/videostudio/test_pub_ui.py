#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты вкладки «Каналы»: проверка ввода, показ, перенос списком."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from channel_slots import ScheduleError  # noqa: E402
from pub_ui import (  # noqa: E402
    ChannelError,
    clean_int,
    clean_schedule,
    clean_uc,
    many,
    one,
    parse_import,
    read_channel_form,
    read_import_row,
    schedule_text,
)

GOOD_UC = "UCFAKE000000000000000999"


def form(**kw):
    """Форма в том виде, в каком её отдаёт браузер: каждый ключ → список значений."""
    out = {}
    for k, v in kw.items():
        out[k] = v if isinstance(v, list) else [v]
    return out


# ─── ID канала ───

def test_good_uc_passes():
    assert clean_uc(" " + GOOD_UC + " ") == GOOD_UC


@pytest.mark.parametrize("bad", ["", "UC123", "XX" + "a" * 22, GOOD_UC + "x", "UC" + "a" * 21])
def test_bad_uc_rejected_with_readable_message(bad):
    with pytest.raises(ChannelError) as exc:
        clean_uc(bad)
    assert "UC" in str(exc.value)


# ─── расписание ───

def test_all_days_collapse_to_star():
    got = clean_schedule("10:00, 18:00", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
    assert json.loads(got) == {"*": ["10:00", "18:00"]}


def test_no_days_means_every_day():
    assert json.loads(clean_schedule("10:00", [])) == {"*": ["10:00"]}


def test_selected_days_are_kept_separately():
    got = json.loads(clean_schedule("10:00", ["mon", "wed"]))
    assert got == {"mon": ["10:00"], "wed": ["10:00"]}


def test_empty_schedule_is_allowed():
    assert clean_schedule("", ["mon"]) == "{}"


def test_bad_time_rejected():
    with pytest.raises(ScheduleError):
        clean_schedule("25:00", [])


def test_times_may_be_separated_by_spaces():
    assert json.loads(clean_schedule("10:00 18:00", [])) == {"*": ["10:00", "18:00"]}


def test_unknown_days_ignored_not_crashed():
    assert json.loads(clean_schedule("10:00", ["mon", "мон"])) == {"mon": ["10:00"]}


# ─── числа ───

def test_int_defaults_on_empty():
    assert clean_int("", "горизонт", 1, 90, 3) == 3


def test_int_out_of_range_rejected():
    with pytest.raises(ChannelError):
        clean_int("500", "горизонт", 1, 90, 3)


def test_int_garbage_rejected():
    with pytest.raises(ChannelError):
        clean_int("завтра", "горизонт", 1, 90, 3)


# ─── форма канала ───

def test_form_keeps_all_checked_days():
    """Главная ловушка кабинета: штатный разбор оставил бы только первый день."""
    data = read_channel_form(form(name="К", times="10:00", days=["mon", "wed", "fri"]))
    assert json.loads(data["schedule"]) == {
        "mon": ["10:00"], "wed": ["10:00"], "fri": ["10:00"]}


def test_form_requires_name():
    with pytest.raises(ChannelError):
        read_channel_form(form(name=""))


def test_form_normalizes_collaborator():
    assert read_channel_form(form(name="К", collaborator="canal"))["collaborator"] == "@canal"
    assert read_channel_form(form(name="К", collaborator="@canal"))["collaborator"] == "@canal"


def test_form_ignores_video_mode_it_lives_in_preset():
    """Режим видео — настройка пресета VideoStudio, канал его не хранит."""
    data = read_channel_form(form(name="К", video_mode="magic"))
    assert "video_mode" not in data


def test_form_rejects_unknown_timezone():
    with pytest.raises(ScheduleError):
        read_channel_form(form(name="К", times="10:00", tz="Europe/Атлантида"))


def test_form_defaults():
    data = read_channel_form(form(name="К"))
    assert data["tz"] == "Europe/Warsaw"
    assert data["horizon_days"] == 3
    assert data["schedule"] == "{}"


def test_helpers_on_missing_keys():
    assert one({}, "нет", "по умолчанию") == "по умолчанию"
    assert many({}, "нет") == []


# ─── перенос списком ───

def test_import_row_returns_only_given_fields():
    """Пустые колонки не должны затирать уже настроенное — это был живой баг."""
    uc, fields = read_import_row({"uc": GOOD_UC, "name": "Новое имя"})
    assert uc == GOOD_UC
    assert fields == {"name": "Новое имя"}
    assert "tz" not in fields and "schedule" not in fields


def test_import_row_blank_values_are_not_fields():
    _, fields = read_import_row({"uc": GOOD_UC, "name": "К", "lang": "", "niche": "   "})
    assert fields == {"name": "К"}


def test_import_row_builds_schedule_when_times_given():
    _, fields = read_import_row({"uc": GOOD_UC, "name": "К", "times": "12:00", "days": "mon tue"})
    assert json.loads(fields["schedule"]) == {"mon": ["12:00"], "tue": ["12:00"]}


def test_import_row_accepts_comma_separated_days():
    _, fields = read_import_row({"uc": GOOD_UC, "name": "К", "times": "12:00", "days": "mon,tue"})
    assert json.loads(fields["schedule"]) == {"mon": ["12:00"], "tue": ["12:00"]}


def test_import_row_lowercases_lang():
    _, fields = read_import_row({"uc": GOOD_UC, "name": "К", "lang": "PL"})
    assert fields["lang"] == "pl"


def test_import_row_rejects_bad_uc():
    with pytest.raises(ChannelError):
        read_import_row({"uc": "мусор", "name": "К"})


def test_import_row_rejects_bad_profile():
    with pytest.raises(ChannelError):
        read_import_row({"uc": GOOD_UC, "name": "К", "profile_id": "девятнадцать"})


def test_import_row_validates_timezone_with_schedule():
    with pytest.raises(ScheduleError):
        read_import_row({"uc": GOOD_UC, "name": "К", "times": "10:00", "tz": "Nowhere/Never"})


# ─── разбор списка ───

def test_parse_json_array():
    assert len(parse_import('[{"uc":"a"},{"uc":"b"}]')) == 2


def test_parse_single_json_object():
    assert len(parse_import('{"uc":"a"}')) == 1


def test_parse_csv_with_header():
    rows = parse_import("uc,name\nUC1,Первый\nUC2,Второй")
    assert [r["name"] for r in rows] == ["Первый", "Второй"]


def test_parse_rejects_empty():
    with pytest.raises(ChannelError):
        parse_import("   ")


def test_parse_rejects_broken_json():
    with pytest.raises(ChannelError):
        parse_import('[{"uc": ')


def test_parse_rejects_csv_without_uc():
    with pytest.raises(ChannelError):
        parse_import("name,lang\nКанал,pl")


# ─── показ ───

def test_schedule_text_every_day():
    assert schedule_text(json.dumps({"*": ["10:00", "18:00"]})) == "10:00, 18:00 · каждый день"


def test_schedule_text_named_days_in_week_order():
    assert schedule_text(json.dumps({"wed": ["10:00"], "mon": ["10:00"]})) == "10:00 · Пн Ср"


def test_schedule_text_handles_empty_and_garbage():
    assert schedule_text("{}") == "не задано"
    assert schedule_text("не json") == "не задано"
    assert schedule_text(None) == "не задано"
