#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Поздняя проверка выхода не запирает базу и не растёт без предела (29.09.2026).

Что было не так. Весь проход поздней проверки был ОДНОЙ транзакцией с единственным
коммитом в конце. Замок записи SQLite брался на первой же записи (учёт квоты после
первого ответа) и не отпускался до конца прохода — то есть и на время сетевых вопросов
к googleapis, по 15 с таймаута каждый. Проход стоит таймером каждые полчаса, и всё это
время кабинет (ожидание 30 с) и пульт (5 с) получали «database is locked»: движок,
выдающий задания, глохнет целиком.

Длина прохода при этом ничем не ограничивалась: неподтверждённый выпуск остаётся в фазе
`scheduled` навсегда и попадает в список на каждом проходе.

Что проверяется здесь:

  1. во время сетевого вопроса база НЕ заперта — чужая связь успевает записать строку
     (это и есть кабинет с пультом, только честной второй связью к тому же файлу);
  2. за один проход спрашивается не больше ASK_LIMIT роликов;
  3. остаток достаётся следующему проходу, и ни один ролик не спрашивается дважды;
  4. проход, оборванный на полуслове, не теряет уже сделанное и не переспрашивает его
     (оплаченный вопрос не задаётся второй раз);
  5. предел вопросов увязан с таймаутом юнита: ASK_LIMIT × TIMEOUT влезает в
     TimeoutStartSec из deploy/studio-verify.service.

Сети нет: транспорт передаётся отдельно, как и в tests/test_verify_public.py.

    .venv/bin/python tests/test_verify_pass_chunks.py
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

from pub_schema import ensure_pub_schema                           # noqa: E402

import pub_verify as V                                             # noqa: E402

NOW = 1_800_000_000.0
HOUR = 3600.0
# Чужая связь ждёт замок недолго намеренно: «через пять секунд отпустит» — это и есть
# падение пульта. Нам нужен быстрый и однозначный ответ «заперто».
FOREIGN_WAIT = 0.4
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def fresh(count=1, slot=NOW - 3 * HOUR, tenant="dev"):
    """База файлом, а не в памяти: замок проверяется второй связью к тому же файлу."""
    path = os.path.join(tempfile.mkdtemp(prefix="verify-chunks-"), "vsgen.db")
    c = sqlite3.connect(path, timeout=FOREIGN_WAIT)
    c.row_factory = sqlite3.Row
    ensure_pub_schema(c)
    c.execute("CREATE TABLE IF NOT EXISTS admin_state("
              "k TEXT PRIMARY KEY, v TEXT, updated REAL)")
    # WAL — как в бою (server/main.py: db()). Важно для честности проверки: в WAL
    # читатель писателя не ждёт, и «заперто» остаётся ровно про запись.
    c.execute("PRAGMA journal_mode=WAL")
    for i in range(count):
        c.execute(
            "INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,"
            "plan_item_version,channel_id,uc,slot_at,phase,status,video_id,"
            "created,updated) VALUES(?,?,?,1,1,'uc',?,'scheduled','done',?,?,?)",
            (f"r{i}", tenant, i, slot + i, f"vid{i}", NOW, NOW))
    c.commit()
    return c, path


def public(url):
    return {"items": [{"status": {"privacyStatus": "public",
                                  "uploadStatus": "processed"},
                       "snippet": {"publishedAt": "2026-09-01T10:00:00Z"}}]}


def asked_ids(urls):
    """Какие video_id спросили — по порядку. Транспорт получает готовый URL."""
    out = []
    for u in urls:
        m = re.search(r"[?&]id=([^&]+)", u)
        out.append(m.group(1) if m else "?")
    return out


def main() -> int:
    print("Во время сетевого вопроса база не заперта")
    c, path = fresh(count=3)
    foreign = sqlite3.connect(path, timeout=FOREIGN_WAIT)
    locked = []

    def probe(url):
        # Пока проход «ждёт ответа googleapis», чужая связь пробует записать строку —
        # ровно то, что делает кабинет, выдавая задание.
        try:
            foreign.execute("INSERT INTO admin_state(k,v,updated) VALUES(?,?,0)",
                            (f"probe{len(locked)}", "1"))
            foreign.commit()
            locked.append("свободна")
        except sqlite3.OperationalError as exc:
            locked.append(str(exc))
        return public(url)

    out = V.run(c, "dev", NOW, fetch=probe, key="k")
    check("чужая запись прошла на каждом из трёх вопросов",
          locked, ["свободна"] * 3)
    check("и работа прохода при этом сделана", out["published"], 3)
    check("после прохода незакрытой транзакции не осталось", c.in_transaction, False)
    foreign.close()
    c.close()

    print("\nРабота видна чужой связи сразу, а не в конце прохода")
    c, path = fresh(count=3)
    reader = sqlite3.connect(path, timeout=FOREIGN_WAIT)
    seen_mid = []

    def watching(url):
        # Первый вопрос — до любой записи; смотрим на втором и третьем, сколько
        # выпусков УЖЕ зафиксировано для постороннего наблюдателя.
        seen_mid.append(reader.execute(
            "SELECT COUNT(*) FROM pub_releases WHERE phase='published'").fetchone()[0])
        return public(url)

    V.run(c, "dev", NOW, fetch=watching, key="k")
    check("к третьему вопросу первые два уже зафиксированы", seen_mid, [0, 1, 2])
    # Хвост прохода — последний выпуск — тоже обязан быть зафиксирован, а не остаться
    # в незакрытой транзакции до конца процесса.
    check("а после прохода видны все три", reader.execute(
        "SELECT COUNT(*) FROM pub_releases WHERE phase='published'").fetchone()[0], 3)
    reader.close()
    c.close()

    print("\nПредел вопросов за проход")
    limit = V.ASK_LIMIT
    V.ASK_LIMIT = 3
    try:
        c, path = fresh(count=7)
        urls = []

        def counting(url):
            urls.append(url)
            return {"items": []}                     # ключ ролика не видит — ждём

        out = V.run(c, "dev", NOW - 2 * HOUR, fetch=counting, key="k")
        check("спросили ровно предел", len(urls), 3)
        check("столько же и посчитано", out["checked"], 3)
        check("до остальных проход не дошёл", out["deferred"], 4)
        check("и это сказано словами, а не молчанием",
              "следующий" in out["why"] and "потеряно" in out["why"], True)

        print("\nОстаток достаётся следующему проходу, и ни один ролик не спрошен дважды")
        # Время то же: уже спрошенные пропускаются как недавние, доходит очередь до
        # остальных. Три прохода закрывают все семь.
        V.run(c, "dev", NOW - 2 * HOUR, fetch=counting, key="k")
        second = V.run(c, "dev", NOW - 2 * HOUR, fetch=counting, key="k")
        got = asked_ids(urls)
        check("спрошены все семь", sorted(got), sorted(f"vid{i}" for i in range(7)))
        check("каждый ровно по разу", len(got), len(set(got)))
        check("очередь кончилась — откладывать больше нечего", second["deferred"], 0)
        c.close()
    finally:
        V.ASK_LIMIT = limit

    print("\nОборванный проход не теряет сделанное и не переспрашивает")

    class Killed(BaseException):
        """Как SIGTERM от systemd: `check` ловит Exception, а это мимо него."""

    c, path = fresh(count=3)
    urls = []

    def dying(url):
        urls.append(url)
        if len(urls) == 3:
            raise Killed("юнит остановлен на полуслове")
        return public(url)

    try:
        V.run(c, "dev", NOW, fetch=dying, key="k")
    except Killed:
        pass
    reader = sqlite3.connect(path, timeout=FOREIGN_WAIT)
    done = [r[0] for r in reader.execute(
        "SELECT release_id FROM pub_releases WHERE phase='published' ORDER BY id")]
    check("сделанное до обрыва зафиксировано и видно снаружи", done, ["r0", "r1"])
    reader.close()
    # Новый проход (новая связь — старая умерла вместе с процессом).
    c2 = sqlite3.connect(path, timeout=FOREIGN_WAIT)
    c2.row_factory = sqlite3.Row
    again = []

    def after(url):
        again.append(url)
        return public(url)

    out = V.run(c2, "dev", NOW + 60, fetch=after, key="k")
    check("оплаченные вопросы не повторены", asked_ids(again), ["vid2"])
    check("и недоделанный выпуск доведён", out["published"], 1)
    c2.close()
    c.close()

    print("\nПредел увязан с таймаутом юнита")
    unit = open(os.path.join(BASE, "deploy", "studio-verify.service"),
                encoding="utf-8").read()
    m = re.search(r"^TimeoutStartSec=(\d+)", unit, re.M)
    check("TimeoutStartSec в юните найден", bool(m), True)
    if m:
        check(f"{V.ASK_LIMIT} вопросов по {V.TIMEOUT} с влезают в {m.group(1)} с",
              V.ASK_LIMIT * V.TIMEOUT <= int(m.group(1)), True)

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
