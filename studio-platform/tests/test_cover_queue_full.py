#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""«Очередь обложек заполнена» — ожидание и новый заказ, а не разбор (05.10.2026).

Что случилось. После выкатки сервис обложек вернул прерванные задачи в очередь, а у
владельца уже стояло 40 ждущих. Лишние он пометил failed: «Восстановление остановлено:
очередь заполнена (40/40). Повторите retry позже». Движок прочитал failed как брак
обложки и увёл в разбор три выпуска — хотя обложка даже не начиналась.

Теперь такой отказ: задача забывается, выпуск ждёт («ресурс недоступен»), и на
следующем круге обложка заказывается заново; если очередь всё ещё полна, сервис сам
ответит 429 — это тоже ожидание. Настоящий отказ обложки — по-прежнему разбор.

    .venv/bin/python tests/test_cover_queue_full.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "workers"))

import pub_schema                                                  # noqa: E402
import pub_services as S                                           # noqa: E402

NOW = 1_800_000_000.0
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class FakeCover:
    created, info = [], {}

    def __init__(self, key):
        pass

    def create_job(self, preset, lang, url):
        FakeCover.created.append(url)
        return f"new-{len(FakeCover.created)}"

    def job(self, job_id):
        return dict(FakeCover.info)


def db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(c)
    c.execute("INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,"
              "error,created,updated) VALUES('rel-1','cover','job','cj1','running',NULL,?,?)",
              (NOW, NOW))
    c.commit()
    return c


def run(info):
    pdir = tempfile.mkdtemp(prefix="cov-full-")
    svc = S.PubServices({"create_project": None, "enqueue": None,
                         "project_dir": lambda *a, **k: pdir})
    rel = {"release_id": "rel-1", "tenant_id": "dev", "project_id": "p1", "lang": "pl",
           "preset_cover": "обложки", "donor_url": "https://youtu.be/aaa", "updated": NOW}
    FakeCover.created, FakeCover.info = [], info
    c = db()
    try:
        out = svc.do_cover_ready(c, rel)
        got = ("ok", out)
    except S.ResourceDown as exc:
        got = ("ждём", str(exc))
    except S.ItemFailed as exc:
        got = ("разбор", str(exc))
    out2 = None
    if got[0] == "ждём":
        out2 = svc.do_cover_ready(c, rel)          # следующий круг движка
    return got, c, out2


def main() -> int:
    was_client, was_key = S.CoverClient, S.get_key
    S.CoverClient, S.get_key = FakeCover, (lambda c, name: "ключ")
    try:
        print("Сервис не принял задачу обратно: очередь заполнена")
        got, c, out2 = run({"status": "failed", "error": "Восстановление остановлено: "
                            "очередь заполнена (40/40). Повторите retry позже."})
        check("не разбор, а ожидание", got[0], "ждём")
        jobs = [r[0] for r in c.execute("SELECT remote_job_id FROM pub_release_jobs WHERE service='cover'")]
        check("старая задача забыта, за выпуском — новая", jobs, ["new-1"])
        check("на следующем круге обложка заказана заново", FakeCover.created, ["https://youtu.be/aaa"])
        check("и выпуск ждёт новую", out2, {"wait": True})
        msgs = " ".join(r[0] for r in c.execute("SELECT message FROM pub_release_log"))
        check("в журнале — почему", "закажу заново" in msgs, True)

        print("\nНастоящий отказ обложки — разбор, как раньше")
        got, c, _ = run({"status": "failed", "error": "генератор вернул пустой ответ"})
        check("разбор с причиной", got, ("разбор", "обложка не сделалась: генератор вернул пустой ответ"))
        check("новой не заказано", FakeCover.created, [])
    finally:
        S.CoverClient, S.get_key = was_client, was_key
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
