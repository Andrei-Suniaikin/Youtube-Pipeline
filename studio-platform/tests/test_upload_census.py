#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Перепись срывов заливки: сначала числа, потом автоповтор (01.10.2026).

Решение владельца, п. 7: «автоповтор заливки — сначала замерить, сколько таких случаев
в сутки, и решать по числам». Автоповтор — самое дорогое решение из всего разбора:
ошибка в нём даёт второй ролик на канале, что запрещено прямо. Числа у нас взять было
негде: статус задания живёт одной строкой и перетирается следующим исходом, а срыв,
разобранный руками, следа не оставляет вовсе.

Здесь проверяется то, на чём эти числа стоят или рушатся — РАЗБОР срыва на классы:

1. **`silent` сильнее улик.** У замолчавшей ноды фаз нет, и по фазам её срыв неотличим
   от безопасного. Она могла продолжать загрузку молча. Это главная проверка файла:
   именно здесь «не знаем» легче всего превратить в «ноль».
2. **`sent` берётся уликой, а не словом.** `needs_repair` означает, что ролик на канале
   есть. Класс считается той же `reached_irreversible`, которой движок закрывает
   необратимость, а не разбором текста ошибки на слова.
3. **`stale` — не повтор.** Пересобранный ролик и просроченное время выхода повторять
   не запрещено, а бессмысленно: нужна новая приёмка.
4. **Сбой самой переписи не повышает класс.** Не смогли разобрать — пишем «не знаем».

    .venv/bin/python tests/test_upload_census.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_schema                                                  # noqa: E402
import pub_upload_census as census                                 # noqa: E402

NOW = 1_800_000_000.0
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def fresh():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(c)
    return c


def job(c, release_id, phase=None, video_id=None, status="running"):
    c.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,"
              "channel_url,title,video_path,slot_at,status,phase,video_id,created,updated)"
              " VALUES(?,'dev',1,'UC1','http://c','Ролик','/v.mp4',?,?,?,?,?,?)",
              (release_id, NOW, status, phase, video_id, NOW, NOW))
    c.commit()


def kinds(c):
    return [r["kind"] for r in c.execute(
        "SELECT kind FROM pub_upload_failures ORDER BY id")]


def main() -> int:
    print("Таблица переписи создаётся вместе со схемой")
    c = fresh()
    check("таблица есть", bool(c.execute(
        "SELECT 1 FROM sqlite_master WHERE name='pub_upload_failures'").fetchone()), True)

    print("\nФаз нет вовсе — файл не уходил, повтор безопасен")
    job(c, "r-safe")
    check("класс safe", census.note(c, "r-safe", "браузер не открылся", now=NOW), "safe")

    print("\nМашина дошла только до открытия профиля — тоже безопасно")
    job(c, "r-pre", phase='{"phase":"profile_opened"}')
    check("класс safe", census.note(c, "r-pre", "профиль не открылся", now=NOW), "safe")

    print("\nУ ролика есть номер на YouTube — файл уже уходил")
    job(c, "r-sent", phase='{"phase":"upload_running"}', video_id="ytAbC")
    check("класс sent", census.note(c, "r-sent", "needs_repair: не та дата", now=NOW),
          "sent")

    print("\nНода замолчала: «не знаем» сильнее любых улик")
    # Самая важная проверка файла. Фаз у замолчавшей ноды НЕТ — по уликам это выглядит
    # безопасным срывом. Молчание не доказывает, что файл не ушёл.
    job(c, "r-silent")
    check("класс silent, а не safe",
          census.note(c, "r-silent", "нода молчит дольше 40 минут", now=NOW, silent=True),
          "silent")

    print("\nРолик пересобран или время ушло — повторять нечего")
    job(c, "r-stale")
    check("класс stale", census.note(c, "r-stale", "ролик пересобран", now=NOW,
                                     stale=True), "stale")
    job(c, "r-old")
    check("и просроченное задание тоже",
          census.note(c, "r-old", "время выхода прошло", now=NOW, stale=True), "stale")

    print("\nМолчание сильнее даже улики «файл уходил»")
    # Порядок ответов проверяется ЯВНО: если когда-нибудь `silent` опустят ниже улик,
    # замолчавшая нода с чужой фазой в базе начнёт считаться безопасной.
    job(c, "r-both", phase='{"phase":"upload_running"}', video_id="ytXyz")
    check("класс silent", census.note(c, "r-both", "молчит", now=NOW, silent=True),
          "silent")

    print("\nНе смогли разобрать — это «не знаем», а не «безопасно»")
    # Сбой разбора не имеет права повышать класс до безопасного: ровно здесь
    # молчаливое повышение и стоило бы второго ролика на канале.
    было = census.classify
    census.classify = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("база заперта"))
    try:
        check("класс silent", census.note(c, "r-safe", "что-то пошло не так", now=NOW),
              "silent")
    finally:
        census.classify = было

    print("\nЧисла за сутки: все четыре класса всегда названы")
    итог = census.since(c, NOW - 10, NOW + 10)
    check("безопасных", итог["safe"], 2)
    check("молчания", итог["silent"], 3)
    check("ушедших", итог["sent"], 1)
    check("нечего повторять", итог["stale"], 2)
    check("всего", итог["всего"], 8)
    # Ноль обязан присутствовать числом: «нет строки» и «ноль» в отчёте выглядят
    # одинаково, а значат разное.
    пусто = census.since(fresh(), NOW - 10, NOW + 10)
    check("в пустой базе все классы названы нулями",
          [пусто[k] for k in census.KINDS] + [пусто["всего"]], [0, 0, 0, 0, 0])

    print("\nОкно суток не захватывает чужие сутки")
    c2 = fresh()
    job(c2, "r-1")
    census.note(c2, "r-1", "вчера", now=NOW - 100000)
    census.note(c2, "r-1", "сегодня", now=NOW)
    check("за сутки — один", census.since(c2, NOW - 10, NOW + 10)["всего"], 1)

    print("\nСтрока для сводки")
    check("нет срывов — так и сказано", census.text({"всего": 0}), "срывов заливки нет")
    строка = census.text({"всего": 3, "safe": 2, "silent": 1, "sent": 0, "stale": 0})
    check("названо общее число", "срывов заливки 3" in строка, True)
    check("и сколько безопасно повторить", "безопасен повтор 2" in строка, True)
    check("и что нода молчала", "нода молчала 1" in строка, True)
    check("классов без случаев в строке нет", "файл уже уходил" in строка, False)

    print("\nПерепись не ломает заливку, даже если таблицы нет")
    # Наблюдение не имеет права уводить в разбор выпуск, который и так сорвался.
    c3 = sqlite3.connect(":memory:")
    c3.row_factory = sqlite3.Row
    check("класс всё равно назван", census.note(c3, "r-x", "без схемы", now=NOW) in
          census.KINDS, True)

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
