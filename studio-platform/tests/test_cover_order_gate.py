#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обложки под лимитом подписки Codex: по одной, а не пачкой (21.10.2026, день).

С 10:28 до 12:20 студия сделала 280 заказов обложек и получила 17: каждый пустой ответ
«Ошибка генерации сцены: подписка codex: SUBSCRIPTION_LIMIT…|частота» тут же порождал
повтор по ссылке, потом раунд смягчения, а раз в 15 минут все ждущие возвращались разом.
Та же подписка переставала давать названия и смягчение озвучки. Теперь:
  • пустой ответ по вине подписки — не повтор и не раунд: выпуск ждёт COVER_LIMIT_RETRY_SEC
    и заказывает то же самое заново (раунд 0 — по ссылке донора);
  • пока лимит свежий (15 минут), вся студия заказывает обложки не чаще раза в
    COVER_ORDER_GAP_SEC — второй заказ в ту же минуту ждёт ресурса «обложки»;
  • без лимита темп прежний: заказы идут подряд.

    .venv/bin/python tests/test_cover_order_gate.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(BASE, "server"))
os.environ.setdefault("VS_ALERTS", "0")

import pub_schema                                                  # noqa: E402
import pub_services as S                                           # noqa: E402

NOW = 1_800_000_000.0
FAILS = []
LIMIT_NOTE = ("❌ Ошибка генерации сцены: подписка codex: SUBSCRIPTION_LIMIT:reset=1792578030|частота|"
              "codex: все ключи отдыхают, ближайший освободится через 55 с")


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class FakeCover:
    created, title_jobs, info = [], [], {}

    def __init__(self, key):
        pass

    def create_job(self, preset, lang, url):
        FakeCover.created.append(url); return f"url-{len(FakeCover.created)}"

    def create_title_job(self, preset, lang, text, extra_prompt=""):
        FakeCover.title_jobs.append(text); return f"title-{len(FakeCover.title_jobs)}"

    def job(self, job_id):
        return dict(FakeCover.info)


def db(rows):
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(c)
    for rid, error, updated in rows:
        c.execute("INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,error,created,updated) "
                  "VALUES(?,'cover','job','cj-'||?,'running',?,?,?)", (rid, rid, error, updated, updated))
    c.commit(); return c


def rel(rid):
    return {"release_id": rid, "tenant_id": "dev", "project_id": "p-" + rid, "lang": "pl",
            "preset_cover": "обложки", "donor_url": "https://youtu.be/" + rid, "updated": NOW}


def run(svc, c, rid, info, clock):
    svc._clock = lambda: clock
    FakeCover.info = info
    try:
        return ("ok", svc.do_cover_ready(c, rel(rid)))
    except S.ResourceDown as exc:
        return ("ждём", exc.resource, str(exc)[:160])
    except S.ItemFailed as exc:
        return ("разбор", str(exc)[:60])


def main() -> int:
    was = (S.CoverClient, S.get_key)
    S.CoverClient, S.get_key = FakeCover, (lambda c, name: "ключ")
    pdir = tempfile.mkdtemp(prefix="cov-gate-")
    svc = S.PubServices({"create_project": None, "enqueue": None, "project_dir": lambda *a, **k: pdir})
    fault = {"status": "completed_partial", "results_count": 0, "output_files": [], "finished_at": NOW,
             "progress_detail": "0/1 обложек", "log": "", "check_notes": [LIMIT_NOTE]}
    empty = dict(fault, check_notes=[])
    try:
        print("Первый пустой ответ по вине подписки — ожидание, а не повтор и не смягчение")
        FakeCover.created, FakeCover.title_jobs = [], []
        c = db([("r1", None, NOW)])
        got = run(svc, c, "r1", fault, NOW + 10)
        check("выпуск ждёт ресурс «обложки»", got[:2], ("ждём", "обложки"))
        check("повтора по ссылке нет", FakeCover.created, [])
        check("раунда смягчения нет", FakeCover.title_jobs, [])
        row = c.execute("SELECT error FROM pub_release_jobs WHERE release_id='r1'").fetchone()
        check("метка повтора не поставлена", row["error"], None)

        print("\nЧерез COVER_LIMIT_RETRY_SEC — тот же заказ по ссылке донора заново, раунд не потрачен")
        got = run(svc, c, "r1", fault, NOW + S.PubServices.COVER_LIMIT_RETRY_SEC + 1)
        check("выпуск снова ждёт результата", got, ("ok", {"wait": True}))
        check("заказан заново по ссылке донора", FakeCover.created, ["https://youtu.be/r1"])
        row = c.execute("SELECT remote_job_id, error FROM pub_release_jobs WHERE release_id='r1'").fetchone()
        check("зеркало: новая задача, без метки раунда", (row["remote_job_id"], row["error"]), ("url-1", None))

        print("\nЛимит свежий — второй выпуск в ту же минуту ждёт, через COVER_ORDER_GAP_SEC — заказывает")
        t = NOW + S.PubServices.COVER_LIMIT_RETRY_SEC + 1
        c2 = db([])
        got = run(svc, c2, "r2", empty, t + 5)
        check("второй заказ подряд — ожидание «обложки»", got[:2], ("ждём", "обложки"))
        check("в тексте — про частоту и «по одной»", "по одной" in got[2], True)
        check("заказа нет", FakeCover.created, ["https://youtu.be/r1"])
        got = run(svc, c2, "r2", empty, t + S.PubServices.COVER_ORDER_GAP_SEC + 1)
        check("после промежутка — заказан", (got, FakeCover.created[-1]), (("ok", {"wait": True}), "https://youtu.be/r2"))

        print("\nЛимит забыт через 15 минут — заказы идут подряд, как раньше")
        t2 = t + S.PubServices.COVER_LIMIT_MEMORY_SEC + 100
        c3 = db([])
        got_a = run(svc, c3, "r3", empty, t2)
        got_b = run(svc, c3, "r4", empty, t2 + 1)
        check("два заказа подряд без ожидания", (got_a, got_b), (("ok", {"wait": True}), ("ok", {"wait": True})))
        check("оба заказаны", FakeCover.created[-2:], ["https://youtu.be/r3", "https://youtu.be/r4"])

        print("\nПустой ответ без вины подписки — повтор по ссылке сразу, как раньше")
        FakeCover.created = []
        c4 = db([("r5", None, t2)])
        got = run(svc, c4, "r5", empty, t2 + 200)
        check("повтор по ссылке", (got, FakeCover.created), (("ok", {"wait": True}), ["https://youtu.be/r5"]))

        print("\nПредзаказ рядом с производством под лимитом — молча пропускается")
        svc._note_cover_limit()
        svc._cover_last_order = svc._clock()
        c5 = db([])
        FakeCover.created = []
        svc._preorder_cover(c5, rel("r6"))
        check("предзаказа нет", FakeCover.created, [])
        check("нет ни строки «не вышло» в журнале выпуска",
              c5.execute("SELECT count(*) FROM pub_release_log WHERE release_id='r6' AND message LIKE '%не вышло%'").fetchone()[0], 0)
    finally:
        S.CoverClient, S.get_key = was
    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}"); [print("  -", f) for f in FAILS]; return 1
    print("все проверки прошли"); return 0


if __name__ == "__main__":
    sys.exit(main())
