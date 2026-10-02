#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Поздняя проверка выхода: что она повышает, что помечает и чего не делает.

Сети нет: транспорт передаётся отдельно. Проверяется главное свойство — проверка
только повышает запись и никогда не портит уже закрытый выпуск.

    .venv/bin/python tests/test_verify_public.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_verify as V                                            # noqa: E402

NOW = 1_800_000_000.0
HOUR = 3600.0
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def fresh(phase="scheduled", video_id="vid1", slot=NOW - 3 * HOUR, tenant="dev"):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
      CREATE TABLE pub_releases(id INTEGER PRIMARY KEY, release_id TEXT, tenant_id TEXT,
                                plan_item_id INT, slot_at REAL, phase TEXT, status TEXT,
                                video_id TEXT, blocked_reason TEXT, updated REAL,
                                channel_id TEXT);
      CREATE TABLE pub_release_events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                                release_id TEXT, seq INTEGER, phase TEXT, video_id TEXT,
                                payload TEXT, created REAL,
                                repeats INTEGER NOT NULL DEFAULT 1, last_seen REAL,
                                UNIQUE(release_id,seq));
      CREATE TABLE pub_release_log(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT,
                                ts REAL, level TEXT, step TEXT, message TEXT,
                                repeats INTEGER NOT NULL DEFAULT 1, last_ts REAL);
      CREATE TABLE pub_channels(uc TEXT PRIMARY KEY, name TEXT, lang TEXT);
    """)
    c.execute("INSERT INTO pub_channels VALUES('UC_1','S-Ранг Манхва','ru')")
    c.execute("INSERT INTO pub_releases VALUES(1,'r1',?,1,?,?,'done',?,NULL,?,'UC_1')",
              (tenant, slot, phase, video_id, NOW))
    c.commit()
    return c


def answer(privacy=None, published_at="2026-09-01T10:00:00Z"):
    """Ответ YouTube Data API. privacy=None — пустой список: ключ ролика не видит."""
    if privacy is None:
        return lambda url: {"items": []}
    return lambda url: {"items": [{"status": {"privacyStatus": privacy,
                                              "uploadStatus": "processed"},
                                   "snippet": {"publishedAt": published_at}}]}


def phase_of(c):
    return c.execute("SELECT phase FROM pub_releases WHERE id=1").fetchone()[0]


def main() -> int:
    print("Кого пора спрашивать")
    c = fresh(slot=NOW - 10 * 60)
    check("слот прошёл десять минут назад — рано", len(V.due(c, "dev", NOW)), 0)
    c = fresh(slot=NOW - 3 * HOUR)
    check("три часа назад — пора", len(V.due(c, "dev", NOW)), 1)
    c = fresh(video_id="")
    check("без video_id спрашивать нечего", len(V.due(c, "dev", NOW)), 0)
    c = fresh(phase="published")
    check("уже подтверждённый не спрашиваем", len(V.due(c, "dev", NOW)), 0)
    c = fresh(phase="media_ready")
    check("недоделанный не спрашиваем", len(V.due(c, "dev", NOW)), 0)

    print("\nРолик вышел")
    c = fresh()
    out = V.run(c, "dev", NOW, fetch=answer("public"), key="k")
    check("фаза повышена", phase_of(c), "published")
    check("посчитан как вышедший", out["published"], 1)
    check("в журнал выпуска написано словами",
          "публичный" in c.execute("SELECT message FROM pub_release_log").fetchone()[0],
          True)
    check("ответ провайдера сохранён",
          c.execute("SELECT phase FROM pub_release_events").fetchone()[0], "public_check")

    print("\nРолик вышел, но только по ссылке — это дефект, а не успех")
    c = fresh()
    out = V.run(c, "dev", NOW, fetch=answer("unlisted"), key="k")
    check("фаза НЕ повышена", phase_of(c), "scheduled")
    check("помечен", out["unlisted"], 1)
    check("причина записана",
          c.execute("SELECT blocked_reason FROM pub_releases").fetchone()[0],
          "ролик доступен только по ссылке, а должен быть публичным")

    print("\nКлюч ролика не видит")
    c = fresh(slot=NOW - HOUR)                 # прошёл час: терпение ещё не вышло
    out = V.run(c, "dev", NOW, fetch=answer(None), key="k")
    check("ждём молча", out["waiting"], 1)
    check("фаза не тронута", phase_of(c), "scheduled")
    check("и никаких пометок",
          c.execute("SELECT blocked_reason FROM pub_releases").fetchone()[0], None)

    c = fresh(slot=NOW - 12 * HOUR)            # терпение вышло
    out = V.run(c, "dev", NOW, fetch=answer(None), key="k")
    check("помечен к разбору", out["stale"], 1)
    check("но фаза всё равно не тронута", phase_of(c), "scheduled")
    check("в журнале сказано, чего именно не хватает",
          "OAuth" in c.execute("SELECT message FROM pub_release_log").fetchone()[0], True)

    print("\nОтсутствие ответа не является ответом")
    c = fresh()

    def boom(url):
        raise OSError("сеть недоступна")

    out = V.run(c, "dev", NOW, fetch=boom, key="k")
    check("ничего не решено", out["unknown"], 1)
    check("фаза не тронута", phase_of(c), "scheduled")
    check("выпуск не помечен",
          c.execute("SELECT blocked_reason FROM pub_releases").fetchone()[0], None)

    print("\nБез ключа не выдумываем")
    c = fresh()
    out = V.run(c, "dev", NOW, fetch=answer("public"), key="")
    check("ничего не проверено", out["checked"], 0)
    check("фаза не тронута", phase_of(c), "scheduled")
    check("сказано, чего не хватает", "ключа" in out.get("why", ""), True)

    print("\nКвоту API не жжём: один ролик не спрашиваем дважды подряд")
    c = fresh()
    hits = {"n": 0}

    def counting(url):
        hits["n"] += 1
        return {"items": []}

    V.run(c, "dev", NOW, fetch=counting, key="k")
    V.run(c, "dev", NOW + 60, fetch=counting, key="k")
    check("второй проход через минуту не спрашивал", hits["n"], 1)
    V.run(c, "dev", NOW + V.RECHECK_SEC + 60, fetch=counting, key="k")
    check("а через положенное время спросил", hits["n"], 2)

    print("\nРазбор ответа")
    check("публичный", V.check("v", "k", answer("public"))["state"], V.PUBLIC)
    check("по ссылке", V.check("v", "k", answer("unlisted"))["state"], V.UNLISTED)
    check("приватный", V.check("v", "k", answer("private"))["state"], V.NOT_PUBLIC)
    check("пустой ответ", V.check("v", "k", answer(None))["state"], V.NOT_PUBLIC)
    check("и он честно говорит, что случаев два",
          "либо удалён" in V.check("v", "k", answer(None))["error"], True)
    check("без ключа", V.check("v", "", answer("public"))["state"], V.UNKNOWN)
    check("без video_id", V.check("", "k", answer("public"))["state"], V.UNKNOWN)

    print("\nКлюч: окружение старше базы")
    c = fresh()
    c.execute("INSERT INTO admin_state VALUES('youtube_api_key','из-базы',0)")
    check("берётся из базы", V.api_key(c), "из-базы")
    os.environ["VS_YT_API_KEY"] = "из-окружения"
    try:
        check("окружение перебивает", V.api_key(c), "из-окружения")
    finally:
        os.environ.pop("VS_YT_API_KEY", None)

    print()
    print("\nВышел ролик — про это ГОВОРЯТ, а не только пишут в базу")
    # Владелец 02.10.2026: «да, я хочу знать, что видосы готовы». До этой работы проход
    # поднимал phase=published и молчал: знать из базы — это не знать.
    c = fresh()
    out = V.run(c, "dev", NOW, fetch=answer("public"), key="k")
    check("фаза повышена", phase_of(c), "published")
    check("и проход вернул, КТО именно вышел", len(out["вышли"]), 1)
    текст = V.хорошая_новость(c, out["вышли"])
    check("в сообщении сказано, сколько вышло", "Вышли на YouTube: 1 ролик" in текст, True)
    check("канал назван именем, а не UC", "S-Ранг Манхва (ru)" in текст, True)
    check("и дана ссылка на ролик", "youtu.be/vid1" in текст, True)
    check("сказано, что это ответ YouTube, а не наше расписание",
          "ответ самой YouTube" in текст, True)

    print("\n  выходить некому — сообщения нет вовсе")
    check("пустой список даёт пустой текст", V.хорошая_новость(c, []), "")
    c2 = fresh()
    out2 = V.run(c2, "dev", NOW, fetch=answer("unlisted"), key="k")
    check("ролик «только по ссылке» вышедшим не считается", out2["вышли"], [])
    check("и текста по нему нет", V.хорошая_новость(c2, out2["вышли"]), "")

    print("\n  канала нет в справочнике — молчать всё равно нельзя")
    c3 = fresh()
    c3.execute("DELETE FROM pub_channels")
    out3 = V.run(c3, "dev", NOW, fetch=answer("public"), key="k")
    текст3 = V.хорошая_новость(c3, out3["вышли"])
    check("вместо имени стоит UC", "• UC_1" in текст3, True)
    check("и ссылка на месте", "youtu.be/vid1" in текст3, True)

    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
