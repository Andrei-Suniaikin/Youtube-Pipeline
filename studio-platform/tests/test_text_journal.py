#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Рерайт в журнале платных вызовов: заказ, повтор и подхват.

Соседей и сети здесь нет — RewriteClient подменён. Проверяется то, ради чего журнал
и заводился: один и тот же текст с теми же настройками не оплачивается дважды, а
после обрыва подхватывается уже созданный заказ.

    .venv/bin/python tests/test_text_journal.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_calls                                                  # noqa: E402
import pub_services                                               # noqa: E402
from pub_engine import ItemFailed, ResourceDown                   # noqa: E402

NOW = 1_800_000_000.0
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class FakeClient:
    """Rewrite, записанный на бумажке: сколько раз с него просили деньги."""

    made = 0
    fail_create = False
    keys = []
    missing = False

    def __init__(self, *_a, **_k):
        pass

    def upload_text(self, text, filename=""):
        return "file-1"

    def create_job(self, file_id, lang, preset, channel_name="", idempotency_key=""):
        if FakeClient.fail_create:
            raise RuntimeError("рерайт отказал до создания задачи")
        FakeClient.made += 1
        FakeClient.keys.append(idempotency_key)
        return f"job-{FakeClient.made}"

    def job(self, job_id):
        if FakeClient.missing:
            from pub_remote import RemoteJobMissing
            raise RemoteJobMissing("рерайт: сохранённая задача отсутствует")
        return {"status": "done"}

    def result_code(self, info, lang):
        return lang

    def result_text(self, job_id, code):
        return "П" * 900


def fresh():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
      CREATE TABLE pub_release_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,
        release_id TEXT, service TEXT, kind TEXT, remote_job_id TEXT, status TEXT,
        error TEXT, created REAL, updated REAL, UNIQUE(release_id,service,kind));
      CREATE TABLE pub_release_log(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT,
        ts REAL, level TEXT, step TEXT, message TEXT,
        repeats INTEGER NOT NULL DEFAULT 1, last_ts REAL);
      CREATE TABLE pub_releases(id INTEGER PRIMARY KEY, release_id TEXT, tenant_id TEXT,
        project_id TEXT, lang TEXT, preset_rewrite TEXT, updated REAL);
      CREATE TABLE users(id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT,
        key_opts TEXT, keys TEXT);
      CREATE TABLE projects(id TEXT PRIMARY KEY, tenant_id TEXT, settings TEXT);
    """)
    pub_calls.ensure_calls_schema(c)
    c.execute("INSERT INTO admin_state VALUES('pub_key_rewrite','k',0)")
    c.execute("INSERT INTO users VALUES(1,'dev','dev','{}','{}')")
    c.commit()
    return c


REL = {"release_id": "rel-1", "tenant_id": "dev", "project_id": "proj-1",
       "lang": "pl", "preset_rewrite": "Базовый", "ch_name": "Демо",
       "donor_url": "https://youtu.be/aaa", "updated": NOW}


def services(store):
    s = pub_services.PubServices({"create_project": lambda *a, **k: "",
                                  "enqueue": lambda *a, **k: None,
                                  "project_dir": lambda *a, **k: ""})
    s.store_source_text = lambda c, rid, text, upd: store.append(text)
    return s


def main() -> int:
    saved_client, saved_text = pub_services.RewriteClient, pub_services.donor_text
    saved_preset = pub_services.rewrite_preset_config
    pub_services.RewriteClient = FakeClient
    pub_services.donor_text = lambda url: "Исходный текст донора. " * 40
    pub_services.rewrite_preset_config = lambda uid, name: {"model": "gpt-5"}
    try:
        print("Первый заказ")
        c = fresh()
        FakeClient.made = 0
        store = []
        svc = services(store)
        out = svc.do_source_ready(c, REL)
        check("ждём результата", out, {"wait": True})
        check("заказ создан один раз", FakeClient.made, 1)
        row = c.execute("SELECT * FROM provider_calls").fetchone()
        check("строка журнала одна",
              c.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0], 1)
        check("вид работы — текст", row["kind"], "text")
        check("единица — язык", row["unit"], "pl")
        check("статус «заказано», а не «готово»", row["status"], "ordered")
        check("id задачи записан как чек", row["remote_order_id"], "job-1")

        print("\n  результат забран — строка закрывается")
        out = svc.do_source_ready(c, REL)
        check("текст сохранён", len(store), 1)
        row = c.execute("SELECT * FROM provider_calls").fetchone()
        check("теперь «готово»", row["status"], "delivered")

        print("\nТот же текст и те же настройки — второй раз не платим")
        c2 = fresh()
        FakeClient.made = 0
        svc2 = services([])
        svc2.do_source_ready(c2, REL)
        check("первый раз заказ был", FakeClient.made, 1)
        # Забываем, что задача заказывалась (как после потери pub_release_jobs),
        # но журнал помнит.
        c2.execute("DELETE FROM pub_release_jobs")
        c2.commit()
        out = svc2.do_source_ready(c2, REL)
        check("второго заказа не создано", FakeClient.made, 1)
        check("ждём ту же задачу", out, {"wait": True})
        known = c2.execute("SELECT remote_job_id FROM pub_release_jobs").fetchone()
        check("подхвачена именно она", known["remote_job_id"], "job-1")
        check("и сказано об этом словами",
              any("нового заказа не создаю" in r[0] for r in
                  c2.execute("SELECT message FROM pub_release_log")), True)

        print("\nДругой язык — это другая работа, её оплачиваем")
        FakeClient.made = 0
        c3 = fresh()
        svc3 = services([])
        svc3.do_source_ready(c3, dict(REL, lang="pl"))
        c3.execute("DELETE FROM pub_release_jobs"); c3.commit()
        svc3.do_source_ready(c3, dict(REL, lang="cs"))
        check("два заказа на два языка", FakeClient.made, 2)
        check("две строки журнала",
              c3.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0], 2)

        print("\nОтказ ДО создания задачи — денег нет, повтор безопасен")
        c4 = fresh()
        FakeClient.made = 0
        FakeClient.fail_create = True
        svc4 = services([])
        try:
            svc4.do_source_ready(c4, REL)
            check("отказ проброшен", "не проброшен", "RuntimeError")
        except RuntimeError:
            check("отказ проброшен", "RuntimeError", "RuntimeError")
        row = c4.execute("SELECT * FROM provider_calls").fetchone()
        check("строка помечена как неудачная без заказа", row["status"], "failed")
        FakeClient.fail_create = False
        svc4.do_source_ready(c4, REL)
        check("повтор разрешён и заказ создан", FakeClient.made, 1)

        print("\nЗадача упала у соседа — повтор не создаёт вторую")
        c5 = fresh()
        FakeClient.made = 0
        svc5 = services([])
        svc5.do_source_ready(c5, REL)
        bad = FakeClient.job
        FakeClient.job = lambda self, jid: {"status": "error", "error": "сломалось"}
        try:
            svc5.do_source_ready(c5, REL)
            check("падение выпуска", "не упал", "ItemFailed")
        except ItemFailed:
            check("падение выпуска", "ItemFailed", "ItemFailed")
        finally:
            FakeClient.job = bad
        row = c5.execute("SELECT * FROM provider_calls").fetchone()
        check("деньги считаются потраченными", row["status"], "ordered")
        c5.execute("DELETE FROM pub_release_jobs"); c5.commit()
        out = svc5.do_source_ready(c5, REL)
        check("новый заказ НЕ создан", FakeClient.made, 1)

        print("\nКлюч повтора уходит вместе с заказом (перенос правки production 14.09)")
        # Если ответ на POST потерялся по дороге, тот же ключ вернёт ТУ ЖЕ задачу.
        # Ключ — «release:<выпуск>», то есть один заказ на один выпуск.
        check("ключ повтора привязан к выпуску",
              [k for k in FakeClient.keys if k], ["release:rel-1"] * len(
                  [k for k in FakeClient.keys if k]))
        check("ключ вообще передавался", bool([k for k in FakeClient.keys if k]), True)

        print("\nЗадача пропала у соседа — дубль не заказывается, строка ждёт человека")
        c6 = fresh()
        FakeClient.made = 0
        svc6 = services([])
        svc6.do_source_ready(c6, REL)
        FakeClient.missing = True
        try:
            svc6.do_source_ready(c6, REL)
            check("выпуск остановлен", "не остановлен", "RemoteJobMissing")
        except ItemFailed as exc:
            check("выпуск остановлен", type(exc).__name__, "RemoteJobMissing")
        finally:
            FakeClient.missing = False
        row = c6.execute("SELECT status, error FROM pub_release_jobs "
                         "WHERE service='rewrite' AND kind='job'").fetchone()
        check("задача помечена пропавшей", row["status"], "missing")
        check("причина сохранена", "отсутствует" in (row["error"] or ""), True)
        # Самое важное: следующий тик не заказывает заново — это были бы вторые деньги.
        try:
            svc6.do_source_ready(c6, REL)
            check("повтор остановлен тоже", "не остановлен", "ItemFailed")
        except ItemFailed:
            check("повтор остановлен тоже", "ItemFailed", "ItemFailed")
        check("нового заказа не создано", FakeClient.made, 1)
        # Строка журнала остаётся «заказано»: заказ БЫЛ и деньги за него ушли. Именно
        # поэтому повтор пойдёт по ветке «подхватить существующий», а не «заказать».
        call = c6.execute("SELECT status, error FROM provider_calls").fetchone()
        check("деньги за пропавшую задачу считаются потраченными", call["status"], "ordered")
        check("и причина записана в журнал вызовов",
              "отсутствует" in (call["error"] or ""), True)
    finally:
        pub_services.RewriteClient = saved_client
        pub_services.donor_text = saved_text
        pub_services.rewrite_preset_config = saved_preset

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
