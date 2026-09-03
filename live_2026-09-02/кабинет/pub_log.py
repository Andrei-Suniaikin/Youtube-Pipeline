#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Журнал выпуска — место, где после сбоя видно, что именно произошло.

Фазы выпуска отвечают на вопрос «где остановилось». Этот журнал отвечает на «почему»:
что попросили у соседнего сервиса и что он ответил, какое решение принял движок, что
напечатал заливщик на минской машине.

Правила, чтобы журнал остался полезным:

* пишем СЛОВАМИ, а не кодами: строку читает владелец, а не только разработчик;
* каждая запись помечена шагом (`рерайт`, `обложка`, `заливка`) — видно, кто именно сказал;
* длинные ответы обрезаем: смысл в первых строках, а гигабайты чужого html никому не нужны;
* журнал ограничен по числу строк на выпуск, иначе один зациклившийся шаг зальёт базу.
"""

from __future__ import annotations

import time

MAX_MESSAGE = 2000
KEEP_PER_RELEASE = 400

LEVELS = ("info", "warn", "error")


def add(c, release_id: str, step: str, message: str, level: str = "info",
        now: float = None) -> None:
    """Записать строку журнала. Ошибка записи не должна ломать сам процесс."""
    if not release_id:
        return
    text = " ".join(str(message or "").split())[:MAX_MESSAGE]
    if not text:
        return
    try:
        c.execute(
            "INSERT INTO pub_release_log(release_id,ts,level,step,message) VALUES(?,?,?,?,?)",
            (release_id, time.time() if now is None else now,
             level if level in LEVELS else "info", str(step)[:40], text))
        _trim(c, release_id)
    except Exception:
        pass          # журнал — вспомогательный: его сбой не должен рушить выпуск


def _trim(c, release_id: str) -> None:
    """Оставить последние строки: зациклившийся шаг иначе зальёт базу."""
    c.execute(
        "DELETE FROM pub_release_log WHERE release_id=? AND id NOT IN "
        "(SELECT id FROM pub_release_log WHERE release_id=? ORDER BY id DESC LIMIT ?)",
        (release_id, release_id, KEEP_PER_RELEASE))


def read(c, release_id: str, limit: int = 400) -> list:
    return c.execute(
        "SELECT ts, level, step, message FROM pub_release_log WHERE release_id=? "
        "ORDER BY id DESC LIMIT ?", (release_id, limit)).fetchall()


def last_error(c, release_id: str) -> str:
    row = c.execute(
        "SELECT message FROM pub_release_log WHERE release_id=? AND level='error' "
        "ORDER BY id DESC LIMIT 1", (release_id,)).fetchone()
    return row[0] if row else ""


def add_lines(c, release_id: str, step: str, text: str, level: str = "info",
              max_lines: int = 40) -> None:
    """Многострочный вывод чужого процесса — по строке за раз, но не бесконечно."""
    lines = [ln for ln in str(text or "").splitlines() if ln.strip()]
    if len(lines) > max_lines:
        lines = lines[:max_lines // 2] + [f"… пропущено {len(lines) - max_lines} строк …"] \
            + lines[-max_lines // 2:]
    for ln in lines:
        add(c, release_id, step, ln, level)
