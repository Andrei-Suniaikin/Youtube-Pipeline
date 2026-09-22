#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Один и тот же донорский ролик не встаёт в план дважды — в любом написании ссылки.

Проверка «этот донор уже взят» сравнивала СТРОКУ ссылки. YouTube отдаёт одно и то же
видео минимум пятью способами: `watch?v=X`, `youtu.be/X`, ссылка с `si=…` из кнопки
«Поделиться», `watch?v=X&t=30` с секундой, `/shorts/X`. Все они проходили как разные
ролики, и канал получал дубль — оплаченный дважды на каждом шаге: субтитры, рерайт,
озвучка, картинки, рендер. Теперь сравниваем по идентификатору ролика (колонка
`donor_video_id`), а строку ссылки держим запасным признаком для не-YouTube доноров.

    .venv/bin/python tests/test_donor_dedup.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_schema                                                 # noqa: E402
import pub_tags                                                   # noqa: E402
from pub_ids import video_id                                      # noqa: E402

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
    c.execute("INSERT INTO pub_channels(tenant_id,uc,name,lang,tz,schedule,horizon_days,"
              "status,created,updated) VALUES('dev','UC1','Демо','pl','Europe/Warsaw',"
              "'{}',3,'active',?,?)", (NOW, NOW))
    c.commit()
    return c, c.execute("SELECT * FROM pub_channels").fetchone()


def add(c, ch, url):
    """Одна ссылка в план. titles={} — сеть не трогаем."""
    return pub_tags.add_links(c, "dev", ch, [url], NOW, titles={})


def rows(c):
    return c.execute("SELECT COUNT(*) FROM pub_plan_items").fetchone()[0]


def main() -> int:
    print("Разбор ссылки: что считается одним роликом")

    same = ["https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ?si=Ab_9-xY",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30s",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "https://www.youtube.com/embed/dQw4w9WgXcQ"]
    check("все написания дают один идентификатор",
          {video_id(u) for u in same}, {"dQw4w9WgXcQ"})
    check("не-YouTube ссылка идентификатора не даёт",
          video_id("https://vimeo.com/12345"), "")
    check("мусор тоже", video_id("не ссылка вовсе"), "")

    print("\nПлан канала: второй раз тот же ролик не встаёт")

    c, ch = fresh()
    first = add(c, ch, same[0])
    check("первая ссылка принята", (len(first["placed"]) + first["queued"],
                                    first["skipped"]), (1, []))
    for url in same[1:]:
        res = add(c, ch, url)
        check(f"пропущен дубль: {url.split('/')[-1][:28]}", res["skipped"], [url])
    check("в плане по-прежнему одна строка", rows(c), 1)

    print("\nНо другой ролик — это другой ролик")

    other = add(c, ch, "https://www.youtube.com/watch?v=zzzzzzzzzzz")
    check("принят", (len(other["placed"]) + other["queued"], other["skipped"]), (1, []))
    check("строк стало две", rows(c), 2)

    print("\nДонор не с YouTube: работает прежняя проверка по ссылке")

    v1 = add(c, ch, "https://vimeo.com/12345")
    check("первый принят", len(v1["placed"]) + v1["queued"], 1)
    v2 = add(c, ch, "https://vimeo.com/12345")
    check("такой же пропущен", v2["skipped"], ["https://vimeo.com/12345"])
    v3 = add(c, ch, "https://vimeo.com/67890")
    check("другой принят", len(v3["placed"]) + v3["queued"], 1)
    check("пустой идентификатор не склеивает разные ссылки", rows(c), 4)

    print("\nОтменённая строка дубля не держит — ролик можно взять заново")

    c2, ch2 = fresh()
    add(c2, ch2, same[0])
    c2.execute("UPDATE pub_plan_items SET status='cancelled'")
    again = add(c2, ch2, same[1])
    check("после отмены ссылка принимается", len(again["placed"]) + again["queued"], 1)

    print("\nСтарая база: колонка добавляется и уже накопленные ссылки разбираются")

    # База, созданная до 19.09: колонки нет, строки есть. Миграция обязана не только
    # добавить колонку, но и разобрать накопленные ссылки — иначе старые строки
    # остались бы без защиты и новая ссылка на тот же ролик к ним не приклеилась бы.
    # Старую схему изображаем честно: сносим колонку вместе с её индексом.
    c3, ch3 = fresh()
    c3.execute("DROP INDEX IF EXISTS idx_pub_plan_donor_vid")
    c3.execute("ALTER TABLE pub_plan_items DROP COLUMN donor_video_id")
    c3.execute("INSERT INTO pub_plan_items(tenant_id,channel_id,donor_url,position,"
               "created,updated) VALUES('dev',?,?,0,?,?)",
               (ch3["id"], "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30s", NOW, NOW))
    c3.execute("INSERT INTO pub_plan_items(tenant_id,channel_id,donor_url,position,"
               "created,updated) VALUES('dev',?,?,0,?,?)",
               (ch3["id"], "https://vimeo.com/12345", NOW, NOW))
    c3.commit()
    check("колонки действительно нет",
          "donor_video_id" in {r[1] for r in c3.execute("PRAGMA table_info(pub_plan_items)")},
          False)

    pub_schema.ensure_pub_schema(c3)                    # это и есть миграция

    filled = dict(c3.execute("SELECT donor_url, donor_video_id FROM pub_plan_items"))
    check("идентификатор у старой строки разобран",
          filled["https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30s"], "dQw4w9WgXcQ")
    check("у не-YouTube строки остался пустым",
          filled["https://vimeo.com/12345"], None)

    # Главное: к старой строке теперь приклеивается новая ссылка в другом написании.
    ch3 = c3.execute("SELECT * FROM pub_channels").fetchone()
    late = add(c3, ch3, "https://youtu.be/dQw4w9WgXcQ")
    check("старый донор защищён так же, как новый", late["skipped"],
          ["https://youtu.be/dQw4w9WgXcQ"])
    check("повторный запуск схемы ничего не ломает",
          (pub_schema.ensure_pub_schema(c3), rows(c3))[1], 2)

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
