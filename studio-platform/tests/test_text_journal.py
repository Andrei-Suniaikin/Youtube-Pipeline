#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Рерайт в журнале платных вызовов: заказ, повтор и подхват.

Соседей и сети здесь нет — RewriteClient подменён. Проверяется то, ради чего журнал
и заводился: один и тот же текст с теми же настройками не оплачивается дважды, а
после обрыва подхватывается уже созданный заказ.

    .venv/bin/python tests/test_text_journal.py
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_calls                                                  # noqa: E402
import pub_remote                                                 # noqa: E402
import pub_engine                                                 # noqa: E402
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

    def resolve_model(self, preset):
        # 19.09.2026: имя модели больше не зашито в теле заказа — кабинет выясняет его
        # до брони и передаёт в create_job готовым (pub_remote.RewriteClient).
        # Здесь деньги считаются, а не модель выбирается, поэтому ответ постоянный.
        return (preset or {}).get("model") or "terra"

    def upload_text(self, text, filename=""):
        return "file-1"

    def create_job(self, file_id, lang, preset, channel_name="", idempotency_key="",
                   model="", source_sha=""):
        # source_sha добавлен 19.09.2026 (ребейз B7): настоящий клиент подмешивает хэш
        # донора в ключ повтора, и подделка обязана принимать тот же набор аргументов —
        # иначе она молча разойдётся с тем, что проверяет тест.
        if FakeClient.fail_create:
            raise RuntimeError("рерайт отказал до создания задачи")
        FakeClient.made += 1
        FakeClient.keys.append(idempotency_key)
        return f"job-{FakeClient.made}"

    retried = []            # какие задачи просили возобновить
    retry_error = None      # чем отвечать на возобновление вместо «принято»

    def retry(self, job_id):
        if FakeClient.retry_error is not None:
            raise FakeClient.retry_error
        FakeClient.retried.append(job_id)
        return {"status": "pending"}

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
        payload TEXT, error TEXT, created REAL, updated REAL,
        UNIQUE(release_id,service,kind));
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


def with_real_client() -> None:
    """Связка кабинет → клиент рерайта целиком, без подделки клиента.

    Зачем отдельно. Подделка `FakeClient` принимает `source_sha` и молчит, если его
    забыли передать: убери `source_sha=text_sha` из `do_source_ready` — все проверки
    выше остаются зелёными, а в бою это ValueError на КАЖДОМ заказе рерайта
    (`pub_remote._order_digest` падает без отпечатка исходника). Поэтому здесь
    подменяется не клиент, а сеть под настоящим клиентом, и одним прогоном
    закрывается сразу две вещи: отпечаток донора доехал, и в заголовок ушёл ровно
    `release:<выпуск>:<отпечаток заказа>`.
    """
    print("\nКабинет и настоящий клиент: отпечаток исходника реально доезжает")
    seen = []          # [(метод, адрес, что послали)] — весь разговор с соседом
    uploads = []       # загрузки донора: у соседа каждая даёт НОВЫЙ file_id

    class Resp:
        status_code = 200

        def __init__(self, data=None, text=""):
            self._data, self.text = data, text

        def json(self):
            return self._data

    def fake_call(method, url, key, service, **kw):
        seen.append((method, url, kw))
        if url.endswith("/files"):                       # загрузка донора
            uploads.append(url)
            return Resp({"file_id": f"file-{len(uploads)}"})
        if "/files/" in url:                             # готовый текст
            return Resp(text="П" * 900)
        if url.endswith("/jobs"):                        # заказ
            return Resp({"job_id": "job-1"})
        return Resp({"status": "done", "output_files": {"pl": "out.txt"}})

    def order(donor: str):
        """Один полный заказ настоящим клиентом. Возвращает (заголовок, тело)."""
        seen.clear()
        pub_services.donor_text = lambda url, **_kw: donor
        svc = services([])
        svc.do_source_ready(fresh(), REL)
        post = [(url, kw) for method, url, kw in seen
                if method == "POST" and url.endswith("/jobs")]
        check("заказ ушёл один раз", len(post), 1)
        _, kw = post[0]
        return (kw.get("headers") or {}).get("Idempotency-Key", ""), kw.get("json") or {}

    saved_call = pub_remote._call
    saved_client = pub_services.RewriteClient
    saved_text = pub_services.donor_text
    pub_remote._call = fake_call
    pub_services.RewriteClient = pub_remote.RewriteClient
    try:
        donor = "Исходный текст донора. " * 40
        key, body = order(donor)
        sha = hashlib.sha256(donor.encode("utf-8")).hexdigest()
        # Ровно то, что должно уйти: выпуск и отпечаток ЭТОГО заказа с ЭТИМ донором.
        check("в заголовке выпуск и отпечаток заказа",
              key, f"release:rel-1:{pub_remote._order_digest(body, sha)}")
        check("отпечаток на месте, а не голый release:",
              bool(re.fullmatch(r"release:rel-1:[0-9a-f]{32}", key)), True)

        # Главное: отпечаток считается от СОДЕРЖИМОГО донора. Другой текст — другой
        # ключ, иначе второй заказ упёрся бы в «тот же ключ, другое тело» навсегда.
        other_key, _ = order(donor + " и ещё абзац")
        check("другой донор — другой ключ", other_key != key, True)

        # И обратно: новая загрузка того же текста даёт новый file_id (подделка сети
        # нумерует их подряд), а ключ обязан остаться прежним — на нём и держится
        # защита от второй оплаты после потерянного ответа на POST.
        again_key, again_body = order(donor)
        check("тот же донор — тот же ключ", again_key, key)
        check("а file_id при этом был другой",
              body["files"] != again_body["files"], True)
    finally:
        pub_remote._call = saved_call
        pub_services.RewriteClient = saved_client
        pub_services.donor_text = saved_text


def main() -> int:
    saved_client, saved_text = pub_services.RewriteClient, pub_services.donor_text
    saved_preset = pub_services.rewrite_preset_config
    pub_services.RewriteClient = FakeClient
    pub_services.donor_text = lambda url, **_kw: "Исходный текст донора. " * 40
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

        print("\n  рерайт прервал задачу — возобновляем ЕЁ ЖЕ, а не заказываем новую")
        # 03.10.2026: рестарт службы оставил десять задач в «interrupted» с 14/18,
        # 13/23, 12/23 готовыми кусками. Сосед сам их не поднимает; новый заказ начал
        # бы с первого куска. Движок зовёт /retry той же задачи — и ждёт дальше.
        c3 = fresh()
        FakeClient.made = 0
        FakeClient.retried = []
        svc3 = services([])
        svc3.do_source_ready(c3, REL)                      # заказ создан: job-1
        saved_job = FakeClient.job

        def mirror():
            return c3.execute("SELECT status, payload FROM pub_release_jobs "
                              "WHERE service='rewrite' AND kind='job'").fetchone()

        def journal():
            return c3.execute("SELECT status FROM provider_calls WHERE kind='text' "
                              "ORDER BY id DESC LIMIT 1").fetchone()[0]

        FakeClient.job = lambda self, job_id: {"status": "interrupted",
                                               "progress_detail": "продолжение 14/18"}
        try:
            out = svc3.do_source_ready(c3, REL)
            check("прерванная задача не «ждём» молча, а возобновлена", out, {"wait": True})
            check("возобновили ТУ ЖЕ задачу", FakeClient.retried, ["job-1"])
            check("нового заказа нет", FakeClient.made, 1)
            check("журнал платных вызовов не тронут: заказ тот же", journal(), "ordered")
            check("зеркало снова «running»", mirror()["status"], "running")
            check("возобновление посчитано", '"resumes": 1' in (mirror()["payload"] or ""), True)
            msg = c3.execute("SELECT message FROM pub_release_log ORDER BY id DESC LIMIT 1"
                             ).fetchone()[0]
            # Владелец 03.10.2026 о прежней строке «рерайт прервал задачу: 1/1 —
            # mzyZGlue_r_0e1ec6aba2.txt (Polish) — возобновил её же…»: «что это значит?»
            check("в журнале — словами человека: что случилось",
                  "рерайт перезапускали, пока он писал текст" in msg, True)
            check("и что сделала студия",
                  "продолжила ту же задачу с места остановки" in msg, True)
            check("и что это не стоит денег второй раз", "второй раз не оплачиваются" in msg, True)
            check("счётчик файлов соседа в строку не попал", "14/18" in msg, False)

            print("\n  три раза подряд — и хватит: дальше разбор")
            svc3.do_source_ready(c3, REL)
            svc3.do_source_ready(c3, REL)
            check("третье возобновление ещё даётся", len(FakeClient.retried), 3)
            try:
                svc3.do_source_ready(c3, REL)
                check("четвёртое — провал этапа", "не упало", "ItemFailed")
            except pub_services.ItemFailed as exc:
                check("четвёртое — провал этапа", "продолжал её 3 раз" in str(exc), True)
            check("а возобновлений по-прежнему три", len(FakeClient.retried), 3)
            check("зеркало закрыто ошибкой", mirror()["status"], "error")
            # Отказ после созданного заказа закрывается как ordered=True: заказ был,
            # деньги потрачены — строка остаётся «ordered», и повтор спросит про него,
            # а не создаст новый (pub_calls.mark_failed).
            check("журнал: заказ остаётся — повтор спросит про него, а не создаст новый",
                  journal(), "ordered")

            print("\n  «вернуть в работу» обнуляет счёт — и задачу снова можно поднять")
            import inspect
            check("«вернуть в работу» действительно снимает счётчик (проводка в revive)",
                  "reset_rewrite_resumes(c, release_id)" in inspect.getsource(pub_engine.revive),
                  True)
            check("счётчик снят", pub_engine.reset_rewrite_resumes(c3, REL["release_id"]), True)
            check("повторный сброс — нечего снимать",
                  pub_engine.reset_rewrite_resumes(c3, REL["release_id"]), False)
            out = svc3.do_source_ready(c3, REL)
            check("после возврата в работу — возобновление, не разбор", out, {"wait": True})
            check("и это снова та же задача", FakeClient.retried[-1], "job-1")
        finally:
            FakeClient.job = saved_job

        print("\n  упала сама — сначала разбор; вернули в работу — поднимаем ту же")
        # До 03.10 «вернуть в работу» после «рерайт не справился» было пустым жестом:
        # зеркало оставалось, движок опрашивал ту же упавшую задачу и тем же тиком
        # снова ронял выпуск в разбор. Обход был один — снести строку и заплатить заново.
        c4 = fresh()
        FakeClient.made = 0
        FakeClient.retried = []
        svc4 = services([])
        svc4.do_source_ready(c4, REL)
        FakeClient.job = lambda self, job_id: {"status": "failed",
                                               "error": "1 из 1 задач не выполнено"}
        try:
            try:
                svc4.do_source_ready(c4, REL)
                check("первый раз упавшая — провал этапа", "не упало", "ItemFailed")
            except pub_services.ItemFailed as exc:
                check("первый раз упавшая — провал этапа", "не справился" in str(exc), True)
            check("возобновлять без человека не стали", FakeClient.retried, [])
            # человек нажал «вернуть в работу»: зеркало в «error», выпуск снова живой
            out = svc4.do_source_ready(c4, REL)
            check("после возврата — та же задача возобновлена", FakeClient.retried, ["job-1"])
            check("и движок ждёт её", out, {"wait": True})
            check("нового заказа нет", FakeClient.made, 1)

            print("\n  «partial» — это конец, а не «ещё работает»")
            FakeClient.job = lambda self, job_id: {"status": "partial", "error": "0/1"}
            c5 = fresh(); FakeClient.made = 0; FakeClient.retried = []
            svc5 = services([])
            svc5.do_source_ready(c5, REL)
            try:
                svc5.do_source_ready(c5, REL)
                check("partial — провал, не ожидание", "не упало", "ItemFailed")
            except pub_services.ItemFailed as exc:
                check("partial — провал, не ожидание", "не справился" in str(exc), True)

            print("\n  сосед лежит в момент возобновления — ждём, а не в разбор")
            FakeClient.job = lambda self, job_id: {"status": "interrupted"}
            FakeClient.retry_error = ResourceDown("рерайт", "рерайт не отвечает")
            c6 = fresh(); FakeClient.made = 0; FakeClient.retried = []
            svc6 = services([])
            svc6.do_source_ready(c6, REL)
            try:
                svc6.do_source_ready(c6, REL)
                check("ResourceDown пробрасывается", "не упало", "ResourceDown")
            except ResourceDown:
                check("ResourceDown пробрасывается", True, True)
            except pub_services.ItemFailed as exc:
                check("ResourceDown пробрасывается", f"ItemFailed: {exc}", "ResourceDown")
            FakeClient.retry_error = RuntimeError("409: retry only for failed jobs")
            try:
                svc6.do_source_ready(c6, REL)
                check("отказ соседа возобновить — провал словами", "не упало", "ItemFailed")
            except pub_services.ItemFailed as exc:
                check("отказ соседа возобновить — провал словами",
                      "не дал возобновить" in str(exc) and "409" in str(exc), True)
        finally:
            FakeClient.job = saved_job
            FakeClient.retry_error = None
            FakeClient.retried = []

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

        print("\nОбрыв ЗАГРУЗКИ донора (до заказа) — бронь не виснет «id неизвестен» (18.10)")
        c7 = fresh()
        FakeClient.made = 0
        svc7 = services([])
        real_upload = FakeClient.upload_text

        def broken_upload(self, text, filename=""):
            raise TimeoutError("ReadTimeout: рерайт не ответил на загрузку")

        FakeClient.upload_text = broken_upload
        try:
            try:
                svc7.do_source_ready(c7, REL)
                check("обрыв проброшен", "не проброшен", "TimeoutError")
            except TimeoutError:
                check("обрыв проброшен", "TimeoutError", "TimeoutError")
        finally:
            FakeClient.upload_text = real_upload
        row = c7.execute("SELECT * FROM provider_calls").fetchone()
        check("бронь закрыта как «до провайдера не дошли»", row["status"], "failed")
        check("в ошибке видно, что это загрузка", (row["error"] or "").startswith("загрузка донора"), True)
        try:
            out = svc7.do_source_ready(c7, REL)
        except ResourceDown as exc:
            out = f"ResourceDown: {exc}"
        check("следующий тик заказывает, а не встаёт «id неизвестен»", out, {"wait": True})
        check("заказ ровно один", FakeClient.made, 1)

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
        # Здесь видно только то, что кабинет ПЕРЕДАЁТ клиенту: «release:<выпуск>».
        # Отпечаток заказа к нему добавляет сам клиент, и то, что реально уходит в
        # заголовок, проверяется ниже — настоящим `RewriteClient`, а не подделкой.
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

        with_real_client()
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
