#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Журнал фаз и журнал строк не растут от повторов одного и того же состояния.

Почему это вообще проверяется. У `pub_engine.log_phase` был докстринг «повтор той же
фазы не создаёт дубля» и безусловный INSERT под ним — код делал обратное тому, что
обещал. Цена видна в бою (срез 18.09): движок публикаций пишет `resource_wait` каждые
30 секунд по каждому стоящему выпуску, при 187 ждущих это ~530 тысяч строк в сутки,
`pub_release_events` доросла до 10,5 млн строк, база кабинета — до 1,64 ГБ, и её целиком
копирует ночной архив. У строкового журнала лимит есть (KEEP_PER_RELEASE=400), но
повторы съедали его за час и вытесняли ровно то, ради чего журнал открывают, — что
происходило ДО ожидания.

    .venv/bin/python tests/test_phase_repeats.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_log                                                    # noqa: E402
import pub_schema                                                 # noqa: E402

FAILS = []
NOW = 1_770_000_000.0


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def fresh():
    """Настоящая схема, а не самодельная: иначе тест не заметит забытую миграцию."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(c)
    return c


def events(c):
    return c.execute("SELECT seq, phase, payload, created, repeats, last_seen "
                     "FROM pub_release_events WHERE release_id='r1' ORDER BY seq").fetchall()


def main() -> int:
    from pub_engine import log_phase, phase_history

    print("Фазы: подряд идущий повтор не заводит строку")

    c = fresh()
    seq1 = log_phase(c, "r1", "resource_wait", NOW, payload={"resource": "rewrite_keys"})
    seqs = [log_phase(c, "r1", "resource_wait", NOW + 30 * i, payload={"resource": "rewrite_keys"})
            for i in range(1, 120)]
    rows = events(c)
    check("час ожидания — одна строка, а не 120", len(rows), 1)
    check("номер фазы не прыгает", {seq1} | set(seqs), {1})
    check("счётчик повторов вырос", rows[0]["repeats"], 120)
    check("«с какого времени ждём» — первый раз", rows[0]["created"], NOW)
    check("«когда проверяли в последний раз» — последний",
          rows[0]["last_seen"], NOW + 30 * 119)

    print("\nНо это именно схлопывание повторов, а не потеря событий")

    log_phase(c, "r1", "resource_wait", NOW + 4000, payload={"resource": "cover_keys"})
    check("другая причина ожидания — новая строка", len(events(c)), 2)
    log_phase(c, "r1", "text_ready", NOW + 5000)
    log_phase(c, "r1", "resource_wait", NOW + 5100, payload={"resource": "cover_keys"})
    check("вернулись к прежней причине после другой фазы — тоже новая строка",
          len(events(c)), 4)
    check("история фаз читается по порядку и ничего не склеила",
          phase_history(c, "r1"),
          ["resource_wait", "resource_wait", "text_ready", "resource_wait"])

    # Тот же ролик, но другая фаза с тем же payload — не повод объединять.
    log_phase(c, "r1", "upload_running", NOW + 5200, video_id="vid1")
    log_phase(c, "r1", "upload_running", NOW + 5300, video_id="vid2")
    check("сменился ролик — новая строка (иначе потеряли бы, к какому видео фаза)",
          [r["seq"] for r in events(c)][-2:], [5, 6])

    print("\nСтроковый журнал: повтор не вытесняет историю")

    c2 = fresh()
    pub_log.add(c2, "r1", "рерайт", "заказали текст, задача 4821", now=NOW)
    for i in range(1, 600):
        pub_log.add(c2, "r1", "рерайт", "ждём: нет доступных ключей провайдера",
                    level="warn", now=NOW + 30 * i)
    lines = pub_log.read(c2, "r1")
    check("одинаковые ожидания сложились в одну строку", len(lines), 2)
    check("первая запись — та, ради которой журнал и открывают",
          [l["message"] for l in lines][-1], "заказали текст, задача 4821")
    wait = [l for l in lines if l["level"] == "warn"][0]
    check("у повтора виден счётчик", wait["repeats"], 599)
    check("и время последнего раза", wait["last_ts"], NOW + 30 * 599)
    check("а время первого раза осталось временем первого раза", wait["ts"], NOW + 30)

    # Проверяем то, ради чего всё делалось: лимит журнала больше не съедается ожиданием.
    # До правки 600 одинаковых строк вытеснили бы первую (KEEP_PER_RELEASE=400).
    check("лимит строк на выпуск не исчерпан ожиданием",
          c2.execute("SELECT COUNT(*) FROM pub_release_log").fetchone()[0] < 10, True)

    print("\nПоздняя проверка публикации спрашивает по ПОСЛЕДНЕМУ разу")

    # `_seen()['at']` решает, не рано ли снова дёргать YouTube Data API. Если бы он читал
    # `created` схлопнутой строки, то у ролика со стабильным ответом «ещё не публичный»
    # это время застыло бы на первой проверке и квота жглась бы каждым проходом.
    import pub_verify
    c3 = fresh()
    log_phase(c3, "r1", "public_check", NOW, video_id="vid1",
              payload={"state": "not_public", "video_id": "vid1"})
    log_phase(c3, "r1", "public_check", NOW + 3600, video_id="vid1",
              payload={"state": "not_public", "video_id": "vid1"})
    check("строка одна", len(events(c3)), 1)
    check("а спрашивали в последний раз час спустя",
          pub_verify._seen(c3, "r1").get("at"), NOW + 3600)

    print("\nСтарые базы читаются: у записей до 19.09 повторов нет")

    c4 = fresh()
    c4.execute("INSERT INTO pub_release_events(release_id,seq,phase,payload,created) "
               "VALUES('r1',1,'created',NULL,?)", (NOW,))
    old = c4.execute("SELECT COALESCE(repeats,1) r, COALESCE(last_seen, created) t "
                     "FROM pub_release_events WHERE release_id='r1'").fetchone()
    check("строка без повторов считается одиночной", (old["r"], old["t"]), (1, NOW))
    check("и следующая та же фаза к ней прибавляется, а не дублируется",
          (log_phase(c4, "r1", "created", NOW + 10), len(events(c4))), (1, 1))

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
