#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обложка не нарисовалась: смягчение надписи до трёх раундов (владелец 17.10.2026).

Grok трижды молча вернул пустой ответ на «NAZIŚCI PO HITLERZE». Раньше второй пустой ответ
хоронил выпуск («обложка не нарисовалась дважды»). Теперь — как с озвучкой: надпись
переписывает модель (раунд 1 бережно, раунд 3 нейтрально), обложка заказывается заново по
надписи, после трёх раундов — остановка с понятной причиной.

    .venv/bin/python tests/test_cover_soften.py
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

import cover_soften                                                # noqa: E402
import pub_schema                                                  # noqa: E402
import pub_services as S                                           # noqa: E402

NOW = 1_800_000_000.0
FAILS = []
LOG_URL = ("--- [1/1] https://youtu.be/D9smOsrcV4w — Польский ---\n✍️ Адаптация текста на Польский...\n"
           "   Результат: NAZIŚCI PO HITLERZE\n📝 Промпт: YouTube thumbnail, 16:9 aspect ratio\n"
           "   ⚠ Попытка 1/3: пустой результат от API (причина не определена), повтор...\n")
LOG_TITLE = "==================================================\n✏️ Title-mode: base_title='PO 1945: CO DALEJ'\n   Extra: WWII\n"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


print("Надпись из журнала задачи")
check("режим по ссылке: строка «Результат»", cover_soften.adapted_text(LOG_URL), "NAZIŚCI PO HITLERZE")
check("режим по надписи: base_title", cover_soften.adapted_text(LOG_TITLE), "PO 1945: CO DALEJ")
check("многострочная надпись склеивается", cover_soften.adapted_text("Результат: ЯК ЖИЛИ\nНІМЦІ ЗА ГІТЛЕРА?\n📝 Промпт"), "ЯК ЖИЛИ НІМЦІ ЗА ГІТЛЕРА?")
check("нет надписи — пусто", cover_soften.adapted_text("Ошибка"), "")

print("\nПросьба к модели и проверка ответа")
msgs = cover_soften.messages_for("NAZIŚCI PO HITLERZE", "pl", 1, "What Happened to the Nazis")
check("язык назван", "польский" in msgs[1]["content"], True)
check("раунд 3 жёстче раунда 1", "без имён" in cover_soften.messages_for("x", "pl", 3)[1]["content"], True)
check("чистый ответ принимается", cover_soften.clean_reply('"PO 1945: CO DALEJ"', "NAZIŚCI PO HITLERZE"), "PO 1945: CO DALEJ")
for bad, label in (("", "пустой ответ"), ("NAZIŚCI PO HITLERZE", "без изменений"),
                   ("a b c d e f g h i j", "слишком длинная")):
    try:
        cover_soften.clean_reply(bad, "NAZIŚCI PO HITLERZE"); check(label + " — отказ", "принято", "отказ")
    except cover_soften.SoftenError:
        check(label + " — отказ", "отказ", "отказ")
check("подсказка сцены нейтральна", "no flags" in cover_soften.scene_hint(1), True)


class FakeCover:
    created, title_jobs, info = [], [], {}

    def __init__(self, key):
        pass

    def create_job(self, preset, lang, url):
        FakeCover.created.append(url); return f"url-{len(FakeCover.created)}"

    def create_title_job(self, preset, lang, text, extra_prompt=""):
        FakeCover.title_jobs.append((text, bool(extra_prompt))); return f"title-{len(FakeCover.title_jobs)}"

    def job(self, job_id):
        return dict(FakeCover.info)


def db(error):
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(c)
    c.execute("INSERT INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,error,created,updated) "
              "VALUES('rel-1','cover','job','cj1','running',?,?,?)", (error, NOW, NOW))
    c.commit(); return c


def step(c, info):
    pdir = tempfile.mkdtemp(prefix="cov-soft-")
    svc = S.PubServices({"create_project": None, "enqueue": None, "project_dir": lambda *a, **k: pdir})
    rel = {"release_id": "rel-1", "tenant_id": "dev", "project_id": "p1", "lang": "pl",
           "preset_cover": "обложки", "donor_url": "https://youtu.be/aaa", "updated": NOW}
    FakeCover.info = info
    try:
        return ("ok", svc.do_cover_ready(c, rel))
    except S.ItemFailed as exc:
        return ("разбор", str(exc))


def job_row(c):
    return c.execute("SELECT remote_job_id, error FROM pub_release_jobs WHERE service='cover'").fetchone()


def main() -> int:
    was = (S.CoverClient, S.get_key, cover_soften.tts_soften.chat_once)
    answers = []
    S.CoverClient, S.get_key = FakeCover, (lambda c, name: "ключ")
    cover_soften.tts_soften.chat_once = lambda messages, key, base, **kw: answers.pop(0)
    empty = {"status": "completed_partial", "results_count": 0, "output_files": [], "progress_detail": "0/1 обложек",
             "finished_at": NOW, "log": LOG_URL}
    try:
        print("\nДвижок: второй пустой ответ — смягчение, а не разбор")
        FakeCover.created, FakeCover.title_jobs = [], []
        answers[:] = ["PO 1945: CO DALEJ"]
        c = db("re-ordered")
        got = step(c, empty)
        check("выпуск ждёт новую обложку", got, ("ok", {"wait": True}))
        check("заказана обложка по смягчённой надписи с подсказкой сцены", FakeCover.title_jobs, [("PO 1945: CO DALEJ", True)])
        check("по ссылке заново не заказывали", FakeCover.created, [])
        row = job_row(c)
        check("зеркало: новая задача и счёт раунда", (row["remote_job_id"], row["error"]), ("title-1", "cover-soften:1"))
        msgs = " ".join(r[0] for r in c.execute("SELECT message FROM pub_release_log"))
        check("в журнале — старая и новая надпись", ("NAZIŚCI PO HITLERZE" in msgs) and ("PO 1945: CO DALEJ" in msgs), True)

        print("\nРаунды 2 и 3, потом остановка")
        answers[:] = ["LISTY Z FRONTU", "CO SKRYWAŁY DZIENNIKI"]
        title_empty = dict(empty, log=LOG_TITLE)
        got = step(c, title_empty); check("раунд 2 заказан", (got[0], job_row(c)["error"]), ("ok", "cover-soften:2"))
        got = step(c, title_empty); check("раунд 3 заказан", (got[0], job_row(c)["error"]), ("ok", "cover-soften:3"))
        got = step(c, title_empty)
        check("после трёх раундов — разбор с понятной причиной", got[0] == "разбор" and "после 3 смягчений" in got[1], True)
        check("четвёртого заказа нет", len(FakeCover.title_jobs), 3)

        print("\nПо API журнала задачи нет: надпись берём из названия донора, дальше — из нашей копии")
        FakeCover.title_jobs = []
        answers[:] = ["PO 1945: CO DALEJ", "LISTY Z FRONTU"]
        c = db("re-ordered")
        no_log = dict(empty, log="")
        pdir = tempfile.mkdtemp(prefix="cov-soft-")
        svc = S.PubServices({"create_project": None, "enqueue": None, "project_dir": lambda *a, **k: pdir})
        rel = {"release_id": "rel-1", "tenant_id": "dev", "project_id": "p1", "lang": "pl", "preset_cover": "обложки",
               "donor_url": "https://youtu.be/aaa", "updated": NOW, "donor_title": "What Happened to the Nazis after 1945?"}
        FakeCover.info = no_log
        out = svc.do_cover_ready(c, rel)
        check("раунд 1 из названия донора", (out, job_row(c)["error"], FakeCover.title_jobs[-1][0]), ({"wait": True}, "cover-soften:1", "PO 1945: CO DALEJ"))
        row = c.execute("SELECT payload FROM pub_release_jobs WHERE service='cover'").fetchone()
        check("надпись раунда запомнена в копии задачи", "PO 1945: CO DALEJ" in (row["payload"] or ""), True)
        out = svc.do_cover_ready(c, rel)
        check("раунд 2 от надписи раунда 1, не от донора", (job_row(c)["error"], FakeCover.title_jobs[-1][0]), ("cover-soften:2", "LISTY Z FRONTU"))

        print("\nМодель отказалась — прежняя причина, без заказа")
        FakeCover.title_jobs = []
        answers[:] = ["Извините, я не могу помочь с этим запросом"]
        c = db("re-ordered")
        got = step(c, empty)
        check("разбор прежней причиной", got[0] == "разбор" and got[1].startswith("обложка не нарисовалась дважды"), True)
        check("заказа нет", FakeCover.title_jobs, [])

        print("\nПервый пустой ответ — как раньше, просто повтор по ссылке")
        FakeCover.created = []
        c = db(None)
        got = step(c, empty)
        check("повтор по ссылке", (got, FakeCover.created), (("ok", {"wait": True}), ["https://youtu.be/aaa"]))
    finally:
        S.CoverClient, S.get_key, cover_soften.tts_soften.chat_once = was
    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}"); [print("  -", f) for f in FAILS]; return 1
    print("все проверки прошли"); return 0


if __name__ == "__main__":
    sys.exit(main())
