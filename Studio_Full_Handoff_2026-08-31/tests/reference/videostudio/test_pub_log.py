#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты журнала выпуска — того места, куда смотрят после сбоя."""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from pub_log import KEEP_PER_RELEASE, MAX_MESSAGE, add, add_lines, last_error, read  # noqa: E402
from pub_schema import ensure_pub_schema  # noqa: E402

NOW = 1786900000.0


@pytest.fixture()
def db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_pub_schema(c)
    yield c
    c.close()


def test_writes_and_reads_back(db):
    add(db, "r_1", "рерайт", "заказал задачу 12ab", now=NOW)
    rows = read(db, "r_1")
    assert len(rows) == 1
    assert rows[0]["step"] == "рерайт" and "12ab" in rows[0]["message"]


def test_newest_first(db):
    add(db, "r_1", "шаг", "первая", now=NOW)
    add(db, "r_1", "шаг", "вторая", now=NOW + 1)
    assert read(db, "r_1")[0]["message"] == "вторая"


def test_releases_do_not_mix(db):
    add(db, "r_1", "шаг", "моя", now=NOW)
    add(db, "r_2", "шаг", "чужая", now=NOW)
    assert [r["message"] for r in read(db, "r_1")] == ["моя"]


def test_last_error_finds_only_errors(db):
    add(db, "r_1", "шаг", "всё хорошо", now=NOW)
    add(db, "r_1", "рерайт", "нет ключей провайдера", level="error", now=NOW + 1)
    add(db, "r_1", "шаг", "продолжаю", now=NOW + 2)
    assert last_error(db, "r_1") == "нет ключей провайдера"


def test_no_error_gives_empty(db):
    add(db, "r_1", "шаг", "всё хорошо", now=NOW)
    assert last_error(db, "r_1") == ""


def test_long_message_is_cut(db):
    add(db, "r_1", "шаг", "x" * (MAX_MESSAGE + 500), now=NOW)
    assert len(read(db, "r_1")[0]["message"]) <= MAX_MESSAGE


def test_empty_message_is_not_written(db):
    add(db, "r_1", "шаг", "   ", now=NOW)
    add(db, "r_1", "шаг", "", now=NOW)
    assert read(db, "r_1") == []


def test_missing_release_is_ignored(db):
    add(db, "", "шаг", "без выпуска", now=NOW)
    assert db.execute("SELECT COUNT(*) FROM pub_release_log").fetchone()[0] == 0


def test_unknown_level_falls_back_to_info(db):
    add(db, "r_1", "шаг", "текст", level="мусор", now=NOW)
    assert read(db, "r_1")[0]["level"] == "info"


def test_log_is_capped_per_release(db):
    """Зациклившийся шаг не должен залить базу."""
    for i in range(KEEP_PER_RELEASE + 120):
        add(db, "r_1", "шаг", f"строка {i}", now=NOW + i)
    assert len(read(db, "r_1", limit=5000)) == KEEP_PER_RELEASE


def test_cap_keeps_newest(db):
    for i in range(KEEP_PER_RELEASE + 10):
        add(db, "r_1", "шаг", f"строка {i}", now=NOW + i)
    assert "строка " + str(KEEP_PER_RELEASE + 9) in read(db, "r_1")[0]["message"]


def test_broken_table_does_not_raise(db):
    db.execute("DROP TABLE pub_release_log")
    add(db, "r_1", "шаг", "текст", now=NOW)     # журнал не должен ронять процесс


# ─── многострочный вывод чужого процесса ───

def test_lines_are_split(db):
    add_lines(db, "r_1", "заливщик", "первая\nвторая\n\nтретья")
    assert [r["message"] for r in read(db, "r_1")][::-1] == ["первая", "вторая", "третья"]


def test_huge_output_is_shortened(db):
    add_lines(db, "r_1", "заливщик", "\n".join(f"строка {i}" for i in range(200)),
              max_lines=20)
    rows = [r["message"] for r in read(db, "r_1")]
    assert len(rows) <= 21
    assert any("пропущено" in m for m in rows)


def test_output_keeps_beginning_and_end(db):
    add_lines(db, "r_1", "заливщик", "\n".join(f"строка {i}" for i in range(100)),
              max_lines=10)
    rows = [r["message"] for r in read(db, "r_1")][::-1]
    assert rows[0] == "строка 0"
    assert rows[-1] == "строка 99"
