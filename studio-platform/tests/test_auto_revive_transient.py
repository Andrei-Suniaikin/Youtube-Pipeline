#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Временный сбой прошёл — выпуск возвращается в работу сам (А13, 05.10.2026).

Владелец 04.10, тест решений А13 «а»: «когда провайдер снова отвечает, выпуски с этой
причиной возвращаются сами». 05.10: «чтобы без нашего участия перезапускалось».

Проверяется: возврат только по улике (тот же провайдер снова доделал работу ПОСЛЕ
остановки), не раньше паузы, не больше двух раз на выпуск; упавшая работа забывается,
иначе движок уронил бы выпуск снова; причины, которые чинят люди, не возвращаются;
зависшая заливка не возвращается; проход стоит в тике движка.

    .venv/bin/python tests/test_auto_revive_transient.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_engine as E                                             # noqa: E402
import pub_schema                                                  # noqa: E402

FAILS = []
NOW = 1_800_000_000.0
BLOCKED = NOW - 3600          # остановлен час назад


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def world(reason, phase="media_ready", blocked_at=BLOCKED):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(c)
    c.execute("CREATE TABLE IF NOT EXISTS queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT, "
              "project_id TEXT, kind TEXT, status TEXT, created REAL, started REAL, finished REAL, "
              "provider TEXT)")
    c.execute("INSERT INTO pub_releases(release_id, tenant_id, plan_item_id, plan_item_version, "
              "channel_id, uc, slot_at, project_id, status, phase, blocked_reason, attempts, "
              "created, updated) VALUES('r1','3',1,1,1,'UC1',?,'p1','blocked',?,?,3,?,?)",
              (NOW + 86400, phase, reason, NOW - 7200, blocked_at))
    E.log_phase(c, "r1", "blocked", blocked_at, payload={"reason": reason})
    c.execute("INSERT INTO pub_release_jobs(release_id, service, kind, remote_job_id, status, "
              "created, updated) VALUES('r1','vsgen','tts','10','error',?,?)", (NOW, NOW))
    c.execute("INSERT INTO pub_release_jobs(release_id, service, kind, remote_job_id, status, "
              "created, updated) VALUES('r1','cover','job','cj1','failed',?,?)", (NOW, NOW))
    c.execute("INSERT INTO queue_tasks VALUES(10,'3','p1','tts','error',?,?,?,'lumean')",
              (NOW - 7200, NOW - 7000, BLOCKED - 10))
    c.commit()
    return c


def status(c):
    return c.execute("SELECT status FROM pub_releases WHERE release_id='r1'").fetchone()[0]


def has_job(c, service, kind):
    return c.execute("SELECT 1 FROM pub_release_jobs WHERE release_id='r1' AND service=? AND kind=?",
                     (service, kind)).fetchone() is not None


def done_task(c, job_id, provider, finished, kind="tts"):
    c.execute("INSERT INTO queue_tasks VALUES(?,?,?,?,'done',?,?,?,?)",
              (job_id, "3", "p9", kind, finished - 600, finished - 300, finished, provider))


def other_release_passed(c, phase, at):
    E.log_phase(c, "r9", phase, at)


def main() -> int:
    tts = "этап «tts» завершился со статусом error"

    print("Озвучка: провайдер снова отвечает")
    c = world(tts)
    check("без улики — ждём", E.revive_transient(c, NOW), [])
    check("без улики — в разборе", status(c), "blocked")
    done_task(c, 20, "fastgen_tts", BLOCKED + 600)
    check("готова озвучка ДРУГОГО провайдера — не улика", E.revive_transient(c, NOW), [])
    done_task(c, 21, "lumean", BLOCKED - 60)
    check("готова до остановки — не улика", E.revive_transient(c, NOW), [])
    done_task(c, 22, "lumean", BLOCKED + 900)
    check("тот же провайдер после остановки — вернул", E.revive_transient(c, NOW), ["r1"])
    check("выпуск в работе", status(c), "pending")
    check("упавшая озвучка забыта — движок поставит заново", has_job(c, "vsgen", "tts"), False)
    check("обложку не трогали", has_job(c, "cover", "job"), True)
    check("счёт автовозвратов 1", E.auto_revives(c, "r1"), 1)
    line = c.execute("SELECT message FROM pub_release_log WHERE release_id='r1' "
                     "ORDER BY id DESC LIMIT 1").fetchone()
    check("в журнале выпуска — улика и счёт",
          bool(line and "задача 22" in line[0] and "1 из 2" in line[0]), True)

    print("\nПауза после остановки")
    c = world(tts, blocked_at=NOW - 600)
    done_task(c, 22, "lumean", NOW - 300)
    check("остановлен 10 минут назад — рано", E.revive_transient(c, NOW), [])

    print("\nНе больше двух раз")
    c = world(tts)
    done_task(c, 22, "lumean", BLOCKED + 900)
    for n in (1, 2):
        E.log_phase(c, "r1", E.AUTO_REVIVE_PHASE, BLOCKED - n, payload={"n": n})
    E.log_phase(c, "r1", "blocked", BLOCKED, payload={"reason": tts})
    check("два автовозврата было — третьего нет", E.revive_transient(c, NOW), [])
    check("остался в разборе", status(c), "blocked")

    print("\nОбложка, рерайт, зависание — по другим выпускам")
    for reason, phase, step in (
            ("обложка не нарисовалась дважды: 0/1 обложек", "media_ready", "cover_ready"),
            ("рерайт не справился: 1 из 1 задач не выполнено", "created", "source_ready"),
            ("зависло на шаге «project_ready» и попытки исчерпаны", "project_ready", "media_ready")):
        c = world(reason, phase=phase)
        check(f"{step}: никто не прошёл шаг — ждём", E.revive_transient(c, NOW), [])
        other_release_passed(c, step, BLOCKED + 300)
        check(f"{step}: другой выпуск прошёл шаг — вернул", E.revive_transient(c, NOW), ["r1"])
        if step == "cover_ready":
            check("упавшее задание обложки забыто", has_job(c, "cover", "job"), False)

    print("\nОбложка: временные отказы сервиса (14.10)")
    for reason in ("обложка не сделалась: не задан ключ доступа к сервису текста "
                   "(COVER_TEXT_SERVICE_KEY): подписками распоряжается он, а не обложки",
                   "обложка не сделалась: timed out",
                   "обложка не сделалась: Error code: 402 - {'error': {'message': "
                   "\"API key token allowance exhausted.",
                   "обложка не сделалась: Error code: 503 - upstream"):
        check(f"временное: {reason[22:60]}…", E.transient_kind(reason), ("cover", ""))
    check("400 — не временное", E.transient_kind(
        "обложка не сделалась: Error code: 400 - bad prompt"), None)

    print("\nТо, что чинят люди, — не трогаем")
    for reason in ("обложка деградировала дважды: текст наложен поверх картинки",
                   "у ролика нет субтитров (VideoUnplayable)",
                   "account recovery needs the owner: no exact account binding",
                   "AdsPower: Check Proxy Fail",
                   "зависло на шаге «upload_running» и попытки исчерпаны",
                   "в пресете не выбрана модель, а сосед вернул пустой список моделей"):
        check(f"не временное: {reason[:40]}…", E.transient_kind(reason), None)

    print("\nЗагрузка уже начиналась — revive откажет, счёт всё равно растёт")
    c = world("нода: HTTP Error 502: Bad Gateway", phase="upload_pending")
    c.execute("INSERT INTO pub_upload_jobs(release_id, tenant_id, channel_id, uc, channel_url, "
              "title, visibility, video_path, slot_at, status, created, updated) "
              "VALUES('r9','3',1,'UC1','u','t','private','v.mp4',?,'done',?,?)",
              (NOW, NOW, BLOCKED + 300))
    E.log_phase(c, "r1", "upload_running", BLOCKED - 100)
    E.log_phase(c, "r1", "blocked", BLOCKED, payload={"reason": "нода: HTTP Error 502: Bad Gateway"})
    check("не вернул", E.revive_transient(c, NOW), [])
    check("попытка засчитана — не спрашиваем каждый тик", E.auto_revives(c, "r1"), 1)

    print("\nПроход стоит в тике движка")
    src = open(os.path.join(BASE, "server", "pub_engine.py"), encoding="utf-8").read()
    tick = src[src.index("def tick("):]
    check("revive_transient зовётся в tick", "revive_transient(c, now)" in tick, True)

    print(f"\nпровалов: {len(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
