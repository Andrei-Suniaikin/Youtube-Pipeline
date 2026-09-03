#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты того, что уезжает на канал: название, описание, файл, время выхода."""

import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from pub_publish import (
    TitleUnavailable,  # noqa: E402
    MAX_DESCRIPTION,
    MAX_TITLE,
    TitleError,
    chapters_enabled,
    clean_title,
    find_video,
    make_description,
    make_title,
    read_chapters,
    schedule_iso,
)


# ─── чистка ответа модели ───

def test_strips_quotes_and_labels():
    assert clean_title('  "Тайна тайги"  ') == "Тайна тайги"
    assert clean_title("«Забытый обоз»") == "Забытый обоз"
    assert clean_title("Название: Река смерти") == "Река смерти"


def test_collapses_newlines():
    assert clean_title("Первая строка\nвторая") == "Первая строка вторая"


def test_cuts_by_word_boundary():
    long = " ".join(["слово"] * 40)
    got = clean_title(long)
    assert len(got) <= MAX_TITLE
    assert not got.endswith("сло")          # режем по границе слова, не посередине


def test_short_answer_is_rejected(monkeypatch):
    import pub_publish

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "Да"}}]}

    monkeypatch.setattr(pub_publish, "requests", type("M", (), {
        "post": staticmethod(lambda *a, **k: Resp()),
        "RequestException": Exception})())
    with pytest.raises(TitleError):
        make_title("Оригинальное название ролика", "pl", "sk-test")


def test_title_needs_source_and_key():
    with pytest.raises(TitleError):
        make_title("", "pl", "sk-test")           # нет исходника — вина строки
    with pytest.raises(TitleUnavailable):
        make_title("Название", "pl", "")          # нет ключа — ждём настройки


def test_title_uses_channel_language(monkeypatch):
    import pub_publish
    seen = {}

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "Tajemnica tajgi i zapomniany szlak"}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["body"] = json
        return Resp()

    monkeypatch.setattr(pub_publish, "requests", type("M", (), {
        "post": staticmethod(fake_post), "RequestException": Exception})())
    got = make_title("Тайна тайги", "pl", "sk-test")
    assert got == "Tajemnica tajgi i zapomniany szlak"
    assert "польский" in seen["body"]["messages"][1]["content"]


def test_model_5xx_means_wait_not_death(monkeypatch):
    """Разовый сбой модели не хоронит готовый ролик — очередь ждёт и повторит."""
    import pub_publish

    class Resp:
        status_code = 500
        text = "boom"

    monkeypatch.setattr(pub_publish, "requests", type("M", (), {
        "post": staticmethod(lambda *a, **k: Resp()), "RequestException": Exception})())
    with pytest.raises(TitleUnavailable):
        make_title("Название", "pl", "sk-test")


def test_model_400_is_row_failure(monkeypatch):
    import pub_publish

    class Resp:
        status_code = 400
        text = "bad request"

    monkeypatch.setattr(pub_publish, "requests", type("M", (), {
        "post": staticmethod(lambda *a, **k: Resp()), "RequestException": Exception})())
    with pytest.raises(TitleError):
        make_title("Название", "pl", "sk-test")


# ─── главы и описание ───

@pytest.mark.parametrize("value,expected", [
    ("1", True), (1, True), (True, True), ("true", True), ("on", True),
    ("0", False), (0, False), (None, False), ("", False), ("нет", False)])
def test_chapters_flag_reading(value, expected):
    assert chapters_enabled({"auto_chapters": value}) is expected


def test_chapters_flag_from_json_string():
    assert chapters_enabled('{"auto_chapters": "1"}') is True
    assert chapters_enabled("не json") is False
    assert chapters_enabled(None) is False


def test_description_is_title_when_no_chapters():
    assert make_description("Тайна тайги") == "Тайна тайги"


def test_description_adds_chapters_below():
    got = make_description("Тайна тайги", "0:00 Начало\n2:15 Находка")
    assert got.startswith("Тайна тайги")
    assert got.endswith("2:15 Находка")
    assert "\n\n" in got                      # блок таймкодов отделён пустой строкой


def test_description_is_capped():
    got = make_description("Заголовок", "x" * (MAX_DESCRIPTION + 500))
    assert len(got) <= MAX_DESCRIPTION


def test_read_chapters_missing_is_empty(tmp_path):
    assert read_chapters(str(tmp_path)) == ""


def test_read_chapters_reads_file(tmp_path):
    d = tmp_path / "chapters"
    d.mkdir()
    (d / "description.txt").write_text("0:00 Вступление", encoding="utf-8")
    assert read_chapters(str(tmp_path)) == "0:00 Вступление"


# ─── файл ролика ───

def test_finds_exact_render_file(tmp_path):
    r = tmp_path / "render"
    r.mkdir()
    (r / "9335.mp4").write_bytes(b"x")
    (r / "old.mp4").write_bytes(b"x")
    assert find_video(str(tmp_path), 9335).endswith("9335.mp4")


def test_falls_back_to_newest(tmp_path):
    r = tmp_path / "render"
    r.mkdir()
    (r / "old.mp4").write_bytes(b"x")
    os.utime(r / "old.mp4", (1, 1))
    (r / "new.mp4").write_bytes(b"x")
    assert find_video(str(tmp_path), 999).endswith("new.mp4")


def test_no_video_returns_empty(tmp_path):
    assert find_video(str(tmp_path)) == ""


# ─── время выхода ───

def test_schedule_has_offset_and_minutes():
    ts = dt.datetime(2026, 8, 20, 8, 0, tzinfo=dt.timezone.utc).timestamp()
    got = schedule_iso(ts, "Europe/Warsaw")
    assert got == "2026-08-20T10:00+02:00"


def test_schedule_in_another_zone():
    ts = dt.datetime(2026, 8, 20, 3, 0, tzinfo=dt.timezone.utc).timestamp()
    assert schedule_iso(ts, "Asia/Bangkok") == "2026-08-20T10:00+07:00"


def test_schedule_drops_seconds():
    ts = dt.datetime(2026, 8, 20, 8, 0, 37, tzinfo=dt.timezone.utc).timestamp()
    assert schedule_iso(ts, "Europe/Warsaw").endswith("T10:00+02:00")


def test_unknown_zone_is_refused():
    with pytest.raises(TitleError):
        schedule_iso(1787000000, "Europe/Атлантида")


def test_winter_time_gives_different_offset():
    ts = dt.datetime(2026, 12, 20, 9, 0, tzinfo=dt.timezone.utc).timestamp()
    assert schedule_iso(ts, "Europe/Warsaw") == "2026-12-20T10:00+01:00"
