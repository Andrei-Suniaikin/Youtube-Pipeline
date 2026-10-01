#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Перепись срывов заливки: сколько их в сутки и сколько из них безопасно повторить.

Зачем это, а не автоповтор. Решение владельца 01.10.2026, п. 7: «сначала замерить,
сколько таких случаев в сутки, и решать по числам». Автоповтор заливки — самое дорогое
из возможных решений: ошибка в нём даёт ВТОРОЙ ролик на канале, а это запрещено прямо
(правило №5 ТЗ: «после формы загрузки второй заливки не бывает»). Решать такое по
догадке «ну наверное их штук пять в день» нельзя, а точного числа у нас нет: статус
задания живёт в одной строке `pub_upload_jobs` и перетирается следующим исходом. Срыв,
который потом разобрали руками, не оставляет следа вовсе.

Поэтому здесь ведётся отдельный журнал — одна строка на каждый срыв, с разбором на
четыре класса в тот самый момент, когда ответ ещё известен:

| Класс    | Что это                                     | Повтор |
|----------|---------------------------------------------|--------|
| `safe`   | файл в YouTube не уходил, и это доказано    | безопасен |
| `silent` | нода перестала отвечать на середине         | ЗАПРЕЩЁН: она могла продолжать молча |
| `sent`   | файл уже уходил (есть video_id или фаза)    | ЗАПРЕЩЁН: будет второй ролик |
| `stale`  | повторять нечего: ролик стал другим, время ушло | не повтор, а новая приёмка |

Класс считается не словами из `error`, а той же функцией, которой движок закрывает
необратимость, — `pub_engine.reached_irreversible`. Текст ошибки меняют в любой правке,
и перепись, разбирающая его на слова, через месяц врала бы молча.

`silent` важнее любого другого ответа. Нода, которая перестала отвечать, фазы больше не
присылает — и по фазам такой срыв выглядит как безопасный. Именно так выглядел бы и
срыв в середине загрузки. «Не знаем» здесь не равно нулю.
"""

from __future__ import annotations

import time

KINDS = ("safe", "silent", "sent", "stale")

# Длинно — на экран, где разбирают один срыв. Коротко — в сводку, где их складывают.
СЛОВАМИ = {
    "safe": "файл не уходил — повтор безопасен",
    "silent": "нода замолчала — не знаем, уходил ли файл",
    "sent": "файл уже уходил — повтор дал бы второй ролик",
    "stale": "повторять нечего: ролик или время стали другими",
}
КРАТКО = {
    "safe": "безопасен повтор",
    "silent": "нода молчала",
    "sent": "файл уже уходил",
    "stale": "повторять нечего",
}


def ensure(c) -> None:
    """Таблица переписи. Отдельная от заданий: задание — состояние, это — события."""
    c.execute("CREATE TABLE IF NOT EXISTS pub_upload_failures("
              "id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL, "
              "tenant_id TEXT, channel_id INTEGER, ts REAL NOT NULL, "
              "kind TEXT NOT NULL, phase TEXT, video_id TEXT, reason TEXT)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pub_upload_failures_ts "
              "ON pub_upload_failures(ts)")


def classify(c, release_id: str, silent: bool = False, stale: bool = False) -> str:
    """Класс срыва. Порядок ответов — это и есть осторожность.

    `silent` спрашивается первым и сильнее улик: у замолчавшей ноды фаз нет, и по фазам
    её срыв неотличим от безопасного. `stale` идёт перед уликами по другой причине:
    там повтор не запрещён, он просто бессмысленен — повторять нечего.
    """
    if silent:
        return "silent"
    if stale:
        return "stale"
    import pub_engine                                              # noqa: PLC0415
    return "sent" if pub_engine.reached_irreversible(c, release_id) else "safe"


def note(c, release_id: str, reason: str, now: float = None,
         silent: bool = False, stale: bool = False) -> str:
    """Записать один срыв. Возвращает класс. Сбой записи не ломает заливку.

    Перепись — наблюдение, а не часть работы: её падение не должно уводить в разбор
    выпуск, который и так уже сорвался.
    """
    ts = time.time() if now is None else now
    try:
        kind = classify(c, release_id, silent=silent, stale=stale)
    except Exception:                                              # noqa: BLE001
        # Не смогли разобрать — пишем «не знаем», а не «безопасно». Молчаливое
        # повышение до безопасного ровно здесь и стоило бы второго ролика.
        kind = "silent"
    try:
        ensure(c)
        row = c.execute("SELECT tenant_id, channel_id, phase, video_id "
                        "FROM pub_upload_jobs WHERE release_id=?", (release_id,)).fetchone()
        c.execute("INSERT INTO pub_upload_failures"
                  "(release_id,tenant_id,channel_id,ts,kind,phase,video_id,reason) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  (release_id, (row["tenant_id"] if row else None),
                   (row["channel_id"] if row else None), ts, kind,
                   (row["phase"] if row else None), (row["video_id"] if row else None),
                   " ".join(str(reason or "").split())[:400]))
    except Exception:                                              # noqa: BLE001
        pass
    return kind


def since(c, start: float, until: float = None) -> dict:
    """Числа за окно: сколько срывов и сколько из них какого класса.

    Все четыре класса присутствуют в ответе всегда, в том числе нулями: «нет строки»
    и «ноль» в отчёте выглядят одинаково, а значат разное.
    """
    out = {k: 0 for k in KINDS}
    out["всего"] = 0
    try:
        ensure(c)
        sql = "SELECT kind, COUNT(*) n FROM pub_upload_failures WHERE ts>=?"
        args = [start]
        if until is not None:
            sql += " AND ts<?"
            args.append(until)
        for r in c.execute(sql + " GROUP BY kind", args):
            kind = r["kind"] if not isinstance(r, tuple) else r[0]
            n = r["n"] if not isinstance(r, tuple) else r[1]
            if kind in out:
                out[kind] = n
            out["всего"] += n
    except Exception:                                              # noqa: BLE001
        pass
    return out


def text(counts: dict) -> str:
    """Строка для сводки. Нет срывов — так и сказано, а не пропущено."""
    if not counts.get("всего"):
        return "срывов заливки нет"
    части = [f"{КРАТКО[k]} {counts[k]}" for k in KINDS if counts.get(k)]
    return f"срывов заливки {counts['всего']}: " + ", ".join(части)
