#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Заказ рерайта: модель не зашита, а результат принимается по длине (19.09.2026).

Два дефекта, оба из разбора пакета 18.09 (`docs/РАЗБОР_ПАКЕТА_18-09.md`, A2 и A3).

1. В теле заказа стоял литерал `gpt-4.1-nano`. С 17.09 рерайт на серверном ключе
   принимает только модели из своего списка (`rewrite_core.server_key_model_error`,
   live_2026-09-18), остальное — 422. Пауза очереди прятала это: в момент её снятия
   422 получили бы разом все ждущие выпуски, и выглядело бы это как общая поломка.

2. Единственной приёмкой текста была нижняя планка в 500 знаков. В бою пришёл текст
   на 46% длины донора (3271 слово → 1332) со статусом «Готово». Половина сюжета
   поехала бы дальше в озвучку, картинки и рендер — три оплаченные стадии за
   ролик-половинку.

Соседей и сети здесь нет: `requests` и `RewriteClient` подменены.

    .venv/bin/python tests/test_rewrite_model_and_length.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_calls                                                   # noqa: E402
import pub_remote                                                  # noqa: E402
import pub_services                                                # noqa: E402
from pub_engine import ItemFailed                                  # noqa: E402

NOW = 1_800_000_000.0
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


# ─────────────────────────── сосед на бумажке ───────────────────────────

class Resp:
    def __init__(self, code=200, body=None, text=""):
        self.status_code = code
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("ответ не JSON")
        return self._body


class Neighbour:
    """Рерайт, записанный на бумажке: что отдаёт на /models и что — на POST /jobs."""

    models_body = [{"value": "luna", "label": "luna"},
                   {"value": "terra", "label": "terra", "default": True},
                   {"value": "claude-opus-5", "label": "claude-opus-5"}]
    models_code = 200
    models_garbage = False       # код 200, а в теле не JSON (прокси, страница входа)
    jobs_code = 200
    jobs_detail = ""
    seen_models = 0
    seen_bodies = []

    @classmethod
    def reset(cls):
        cls.models_code = 200
        cls.models_garbage = False
        cls.jobs_code = 200
        cls.jobs_detail = ""
        cls.seen_models = 0
        cls.seen_bodies = []
        pub_remote._models_cache.clear()

    @classmethod
    def request(cls, method, url, **kw):
        if url.endswith("/models"):
            cls.seen_models += 1
            if cls.models_garbage:
                return Resp(200, None, text="<html>вход в систему</html>")
            if cls.models_code != 200:
                return Resp(cls.models_code, {"detail": "нет такой ручки"})
            return Resp(200, cls.models_body)
        if url.endswith("/jobs"):
            cls.seen_bodies.append(kw.get("json") or {})
            if cls.jobs_code != 200:
                return Resp(cls.jobs_code, {"detail": cls.jobs_detail})
            return Resp(200, {"job_id": f"job-{len(cls.seen_bodies)}"})
        raise AssertionError(f"неожиданный запрос к соседу: {method} {url}")


# ─────────────────────────── база для стадии рерайта ───────────────────────────

def fresh():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    # Схема как в кабинете: у pub_release_jobs есть payload — именно там с 19.09
    # живёт длина донора, запомненная в момент заказа.
    c.executescript("""
      CREATE TABLE admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
      CREATE TABLE pub_release_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,
        release_id TEXT, service TEXT, kind TEXT, remote_job_id TEXT, status TEXT,
        payload TEXT, error TEXT, created REAL, updated REAL,
        UNIQUE(release_id,service,kind));
      CREATE TABLE pub_release_log(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT,
        ts REAL, level TEXT, step TEXT, message TEXT,
        repeats INTEGER NOT NULL DEFAULT 1, last_ts REAL);
      CREATE TABLE users(id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT,
        key_opts TEXT, keys TEXT);
    """)
    pub_calls.ensure_calls_schema(c)
    c.execute("INSERT INTO admin_state VALUES('pub_key_rewrite','k',0)")
    c.execute("INSERT INTO users VALUES(1,'dev','dev','{}','{}')")
    c.commit()
    return c


REL = {"release_id": "rel-1", "tenant_id": "dev", "project_id": "proj-1",
       "lang": "pl", "preset_rewrite": "Базовый", "ch_name": "Демо",
       "donor_url": "https://youtu.be/aaa", "updated": NOW}

DONOR = "Исходный текст донора. " * 200          # 4600 знаков


class TextClient:
    """Клиент рерайта для стадии: интересует длина текста и ПОРЯДОК обращений.

    `seen` — журнал вызовов по порядку. Он и проверяет, что модель выясняется до
    загрузки донора соседу, а не внутри `create_job`, как было до правки.
    """

    result = "П" * 4600
    model_error = None          # чем падает resolve_model, когда так задумано тестом
    seen = []

    def __init__(self, *_a, **_k):
        pass

    def resolve_model(self, preset):
        TextClient.seen.append("resolve_model")
        if TextClient.model_error is not None:
            raise TextClient.model_error
        return (preset or {}).get("model") or "terra"

    def upload_text(self, text, filename=""):
        TextClient.seen.append("upload_text")
        return "file-1"

    def create_job(self, file_id, lang, preset, channel_name="", idempotency_key="",
                   model=""):
        TextClient.seen.append(f"create_job:{model}")
        return "job-1"

    def job(self, job_id):
        return {"status": "done"}

    def result_code(self, info, lang):
        return lang

    def result_text(self, job_id, code):
        return TextClient.result


def services(store):
    s = pub_services.PubServices({"create_project": lambda *a, **k: "",
                                  "enqueue": lambda *a, **k: None,
                                  "project_dir": lambda *a, **k: ""})
    s.store_source_text = lambda c, rid, text, upd: store.append(text)
    TextClient.seen = []
    return s


def forget_job(c):
    """Потерять нашу строку о задаче соседа, оставив строку журнала оплат.

    Так выглядит выпуск после ручного сброса или после переезда базы: деньги
    в `provider_calls` записаны, а `pub_release_jobs` пуста — и следующий тик
    идёт в ветку «уже оплачен/уже заказан», а не создаёт второй заказ.
    """
    c.execute("DELETE FROM pub_release_jobs WHERE service='rewrite' AND kind='job'")
    c.commit()


def job_payload(c):
    row = c.execute("SELECT payload FROM pub_release_jobs WHERE service='rewrite' "
                    "AND kind='job'").fetchone()
    return row["payload"] if row else None


def messages(c):
    return [r[0] for r in c.execute("SELECT message FROM pub_release_log")]


# ─────────────────────────── A2: модель заказа ───────────────────────────

def part_model():
    print("Модель заказа больше не зашита в код")

    src = open(os.path.join(BASE, "server", "pub_remote.py"), encoding="utf-8").read()
    # Сторож на будущее: имя модели не должно вернуться в код «временно, чтобы
    # работало». Ищем именно строковый литерал в кавычках — упоминание в комментарии
    # («раньше здесь стоял gpt-4.1-nano») это история, а не поведение.
    check("строкового литерала с моделью в клиенте нет",
          any(q + "gpt-4.1-nano" + q in src for q in ('"', "'")), False)

    saved = pub_remote.requests.request
    pub_remote.requests.request = Neighbour.request
    try:
        Neighbour.reset()
        client = pub_remote.RewriteClient("ключ-1")

        print("\n  выбор владельца сильнее всего")
        client.create_job("f1", "pl", {"model": "claude-opus-5"})
        check("заказ ушёл на модели из пресета",
              Neighbour.seen_bodies[-1]["model"], "claude-opus-5")
        check("за списком к соседу не ходили", Neighbour.seen_models, 0)

        print("\n  пресет молчит — спрашиваем соседа и берём помеченную по умолчанию")
        Neighbour.reset()
        client.create_job("f1", "pl", {})
        check("взята помеченная default", Neighbour.seen_bodies[-1]["model"], "terra")
        check("список спросили один раз", Neighbour.seen_models, 1)

        print("\n  список запоминается: пачка заказов не опрашивает соседа заново")
        client.create_job("f2", "cs", {})
        client.create_job("f3", "ro", {})
        check("три заказа — один запрос списка", Neighbour.seen_models, 1)
        check("и все три ушли на той же модели",
              [b["model"] for b in Neighbour.seen_bodies], ["terra"] * 3)

        print("\n  память — по ключу: у другого кабинета свой список")
        other = pub_remote.RewriteClient("ключ-2")
        other.create_job("f4", "pl", {})
        check("чужой ключ спросил свой список", Neighbour.seen_models, 2)

        print("\n  нулевое время жизни памяти — спрашиваем каждый раз")
        Neighbour.reset()
        ttl = pub_remote.MODELS_TTL
        pub_remote.MODELS_TTL = 0
        try:
            client.create_job("f5", "pl", {})
            client.create_job("f6", "pl", {})
        finally:
            pub_remote.MODELS_TTL = ttl
        check("два заказа — два запроса списка", Neighbour.seen_models, 2)

        print("\n  списка нет — честный отказ, а не другой литерал")
        Neighbour.reset()
        Neighbour.models_code = 404          # у нашего форка ручки /models ещё нет
        try:
            client.create_job("f7", "pl", {})
            check("заказ остановлен", "не остановлен", "ItemFailed")
        except ItemFailed as exc:
            check("заказ остановлен", "ItemFailed", "ItemFailed")
            check("человеку сказано, что делать",
                  "выберите модель в пресете" in str(exc).lower(), True)
        check("к POST /jobs дело не дошло — денег не потратили",
              Neighbour.seen_bodies, [])

        print("\n  код 200, а тело не JSON — это отказ, а не загадочная поломка")
        Neighbour.reset()
        Neighbour.models_garbage = True
        try:
            client.create_job("f7a", "pl", {})
            check("заказ остановлен", "не остановлен", "ItemFailed")
        except ItemFailed as exc:
            # Главное здесь — КЛАСС отказа. До правки `.json()` бросал голый
            # ValueError мимо `except ItemFailed` в resolve_model, и наверх уходила
            # не «выберите модель», а трассировка неизвестного происхождения.
            check("отказ нашего класса, а не ValueError", "ItemFailed", "ItemFailed")
            check("человеку сказано, что делать",
                  "выберите модель в пресете" in str(exc).lower(), True)
        except ValueError as exc:                                      # noqa: BLE001
            check("отказ нашего класса, а не ValueError", f"ValueError: {exc}",
                  "ItemFailed")
        check("до POST /jobs не дошли", Neighbour.seen_bodies, [])

        print("\n  битый ответ не запоминается: следующий заказ спрашивает заново")
        try:
            client.create_job("f7b", "pl", {})
        except Exception:                                              # noqa: BLE001
            pass
        check("список спросили второй раз", Neighbour.seen_models, 2)

        print("\n  пустой список при коде 200 — замаскированный отказ, не запоминаем")
        Neighbour.reset()
        Neighbour.models_body = []
        for _ in range(3):
            try:
                client.create_job("f7c", "pl", {})
            except ItemFailed:
                pass
        # Если запомнить пустой разбор на пять минут, вся идущая следом пачка
        # выпусков уедет в карантин, а починка у соседа не будет видна.
        check("каждый заказ спрашивает соседа заново", Neighbour.seen_models, 3)
        Neighbour.models_body = [{"value": "luna", "label": "luna"},
                                 {"value": "terra", "label": "terra", "default": True}]

        print("\n  сосед не пометил ни одной по умолчанию — выбирать за владельца не будем")
        Neighbour.reset()
        Neighbour.models_body = [{"value": "luna"}, {"value": "claude-opus-5"}]
        try:
            client.create_job("f8", "pl", {})
            check("заказ остановлен", "не остановлен", "ItemFailed")
        except ItemFailed as exc:
            check("заказ остановлен", "ItemFailed", "ItemFailed")
            check("в отказе перечислено, из чего выбирать",
                  "luna" in str(exc) and "claude-opus-5" in str(exc), True)

        print("\n  модель одна — выбирать не из чего, берём её")
        Neighbour.reset()
        Neighbour.models_body = [{"value": "luna"}]
        client.create_job("f9", "pl", {})
        check("единственная модель принята", Neighbour.seen_bodies[-1]["model"], "luna")

        print("\n  422 про модель доезжает до человека словами")
        Neighbour.reset()
        Neighbour.models_body = [{"value": "terra", "default": True}]
        Neighbour.jobs_code = 422
        Neighbour.jobs_detail = ("Модель terra недоступна на серверных ключах. "
                                 "Выберите одну из: luna, sol")
        try:
            client.create_job("fa", "pl", {})
            check("отказ проброшен", "не проброшен", "ItemFailed")
        except ItemFailed as exc:
            check("названа отвергнутая модель", "«terra»" in str(exc), True)
            check("сказано, что выбрать другую", "выберите другую" in str(exc).lower(),
                  True)
            check("и приведена причина соседа", "luna, sol" in str(exc), True)

        print("\n  422 не про модель остаётся как есть")
        Neighbour.reset()
        Neighbour.jobs_code = 422
        Neighbour.jobs_detail = "languages[0].code: неизвестный язык"
        try:
            client.create_job("fb", "pl", {"model": "terra"})
            check("отказ проброшен", "не проброшен", "ItemFailed")
        except ItemFailed as exc:
            check("модель не приплетена", "не принял модель" in str(exc), False)
            check("видна настоящая причина", "неизвестный язык" in str(exc), True)
    finally:
        pub_remote.requests.request = saved
        Neighbour.models_body = [{"value": "luna", "label": "luna"},
                                 {"value": "terra", "label": "terra", "default": True},
                                 {"value": "claude-opus-5", "label": "claude-opus-5"}]
        pub_remote._models_cache.clear()


# ─────────────────────────── A3: приёмка текста по длине ───────────────────────────

def part_length():
    print("\nПриёмка текста рерайта по длине донора")

    saved_client, saved_text = pub_services.RewriteClient, pub_services.donor_text
    saved_preset = pub_services.rewrite_preset_config
    pub_services.RewriteClient = TextClient
    pub_services.donor_text = lambda url: DONOR
    pub_services.rewrite_preset_config = lambda uid, name: {"model": "terra"}
    try:
        print("\n  длина донора запоминается в момент заказа")
        c = fresh()
        store = []
        svc = services(store)
        svc.do_source_ready(c, REL)
        row = c.execute("SELECT payload FROM pub_release_jobs WHERE service='rewrite' "
                        "AND kind='job'").fetchone()
        check("донор записан вместе с задачей",
              json.loads(row["payload"])["donor_chars"], len(DONOR))

        print("\n  результат в половину донора дальше не идёт")
        TextClient.result = "П" * int(len(DONOR) * 0.46)       # случай из боя: 46%
        try:
            svc.do_source_ready(c, REL)
            check("выпуск остановлен", "не остановлен", "ItemFailed")
        except ItemFailed as exc:
            check("выпуск остановлен", "ItemFailed", "ItemFailed")
            check("названы обе длины",
                  str(len(DONOR)) in str(exc) and str(len(TextClient.result)) in str(exc),
                  True)
            check("названа доля", "46.0%" in str(exc), True)
        check("текст НЕ сохранён — озвучка его не увидит", store, [])
        said = [m for m in messages(c) if "короче донора" in m]
        check("в журнале понятная строка", len(said), 1)
        check("и в ней обе длины и порог",
              all(part in said[0] for part in (str(len(DONOR)), "46.0%", "60.0%")), True)
        job = c.execute("SELECT status FROM pub_release_jobs WHERE service='rewrite' "
                        "AND kind='job'").fetchone()
        check("задача помечена неудачной", job["status"], "error")
        call = c.execute("SELECT status FROM provider_calls").fetchone()
        check("деньги за неё считаются потраченными", call["status"], "ordered")

        print("\n  нормальный результат проходит")
        c2 = fresh()
        store2 = []
        svc2 = services(store2)
        svc2.do_source_ready(c2, REL)
        TextClient.result = "П" * len(DONOR)
        out = svc2.do_source_ready(c2, REL)
        check("шаг завершён", out, {"payload": {"chars": len(DONOR)}})
        check("текст сохранён", len(store2), 1)
        check("в журнале видна доля донора",
              any("100.0% донора" in m for m in messages(c2)), True)

        print("\n  результат чуть короче донора — это норма, не брак")
        c3 = fresh()
        store3 = []
        svc3 = services(store3)
        svc3.do_source_ready(c3, REL)
        TextClient.result = "П" * int(len(DONOR) * 0.75)
        svc3.do_source_ready(c3, REL)
        check("текст принят", len(store3), 1)

        print("\n  порог — настройка, а не приговор")
        c4 = fresh()
        store4 = []
        svc4 = services(store4)
        svc4.do_source_ready(c4, REL)
        TextClient.result = "П" * int(len(DONOR) * 0.46)
        was = pub_services.TEXT_MIN_RATIO
        pub_services.TEXT_MIN_RATIO = 0.3
        try:
            svc4.do_source_ready(c4, REL)
        finally:
            pub_services.TEXT_MIN_RATIO = was
        check("с порогом 30% тот же текст принят", len(store4), 1)

        print("\n  донор неизвестен (задача заказана до 19.09) — молчим честно")
        c5 = fresh()
        store5 = []
        svc5 = services(store5)
        svc5.do_source_ready(c5, REL)
        c5.execute("UPDATE pub_release_jobs SET payload=NULL WHERE service='rewrite' "
                   "AND kind='job'")
        c5.commit()
        TextClient.result = "П" * int(len(DONOR) * 0.46)
        svc5.do_source_ready(c5, REL)
        check("без длины донора доля не выдумывается", len(store5), 1)

        print("\n  нижняя планка в знаках осталась на месте")
        c6 = fresh()
        store6 = []
        svc6 = services(store6)
        svc6.do_source_ready(c6, REL)
        TextClient.result = "П" * 200
        try:
            svc6.do_source_ready(c6, REL)
            check("совсем короткий текст остановлен", "не остановлен", "ItemFailed")
        except ItemFailed as exc:
            check("совсем короткий текст остановлен", "ItemFailed", "ItemFailed")
            check("причина — минимум знаков", "минимуме 500" in str(exc), True)
        check("и он тоже не сохранён", store6, [])

        # ─── главное: длина донора доезжает и на ветках, где заказ НЕ создаётся ───

        print("\n  задача уже заказана раньше — подхватываю, и донор помню")
        c7 = fresh()
        store7 = []
        svc7 = services(store7)
        TextClient.result = "П" * len(DONOR)
        svc7.do_source_ready(c7, REL)                  # заказ создан, деньги записаны
        forget_job(c7)                                 # нашей строки о задаче больше нет
        svc7.do_source_ready(c7, REL)                  # ветка «уже заказана, подхватываю»
        check("в журнале видно, что нового заказа не создавали",
              len([m for m in messages(c7) if "уже заказана" in m]), 1)
        payload = job_payload(c7)
        check("подхваченная задача помнит длину донора", payload is not None, True)
        check("и это именно длина донора",
              json.loads(payload or "{}").get("donor_chars"), len(DONOR))
        # Ради этого всё и писалось: 146 оплаченных и не забранных текстов приходят
        # именно через эту ветку, и проверка доли обязана работать на них.
        TextClient.result = "П" * int(len(DONOR) * 0.46)
        try:
            svc7.do_source_ready(c7, REL)
            check("половина донора остановлена и на подхваченной задаче",
                  "не остановлен", "ItemFailed")
        except ItemFailed:
            check("половина донора остановлена и на подхваченной задаче",
                  "ItemFailed", "ItemFailed")
        check("текст не сохранён", store7, [])

        print("\n  этот текст уже оплачен раньше — и здесь донор помню")
        c8 = fresh()
        store8 = []
        svc8 = services(store8)
        TextClient.result = "П" * len(DONOR)
        svc8.do_source_ready(c8, REL)                  # заказ
        svc8.do_source_ready(c8, REL)                  # текст забран, строка delivered
        forget_job(c8)
        svc8.do_source_ready(c8, REL)                  # ветка «уже оплачен раньше»
        check("в журнале видно, что новый заказ не создавали",
              len([m for m in messages(c8) if "уже оплачен раньше" in m]), 1)
        payload = job_payload(c8)
        check("оплаченная раньше задача помнит длину донора", payload is not None, True)
        check("и это именно длина донора",
              json.loads(payload or "{}").get("donor_chars"), len(DONOR))
        TextClient.result = "П" * int(len(DONOR) * 0.46)
        try:
            svc8.do_source_ready(c8, REL)
            check("и её результат тоже проверяется по доле",
                  "не остановлен", "ItemFailed")
        except ItemFailed:
            check("и её результат тоже проверяется по доле", "ItemFailed", "ItemFailed")
        check("второй текст не сохранён", len(store8), 1)

        # ─── как числа читаются человеком ───

        print("\n  59,6% — это меньше 60%, и в строке это видно")
        c9 = fresh()
        store9 = []
        svc9 = services(store9)
        svc9.do_source_ready(c9, REL)
        TextClient.result = "П" * int(len(DONOR) * 0.596)
        said = ""
        try:
            svc9.do_source_ready(c9, REL)
        except ItemFailed as exc:
            said = str(exc)
        check("доля с десятыми", "59.6%" in said, True)
        check("и порог с десятыми", "60.0%" in said, True)
        # С округлением до целых человек читал «(60% при пороге 60%) — дальше не пускаю»
        # и шёл искать несуществующий баг в строгом сравнении.
        check("равенства в строке нет", "(60% при пороге 60%)" in said, False)

        print("\n  в удачной строке одна мера длины: хвост пустых строк не ломает счёт")
        c10 = fresh()
        store10 = []
        svc10 = services(store10)
        svc10.do_source_ready(c10, REL)
        TextClient.result = "П" * len(DONOR) + "\n\n\n   "
        out = svc10.do_source_ready(c10, REL)
        check("в шаг уходит длина без хвоста", out, {"payload": {"chars": len(DONOR)}})
        line = [m for m in messages(c10) if "текст готов" in m][0]
        check("в журнале та же длина, от которой считалась доля",
              f"{len(DONOR)} символов" in line and "100.0% донора" in line, True)

        # ─── когда выясняется модель ───

        print("\n  модель выясняется ДО загрузки донора соседу")
        c11 = fresh()
        store11 = []
        svc11 = services(store11)
        TextClient.result = "П" * len(DONOR)
        svc11.do_source_ready(c11, REL)
        check("порядок обращений к соседу", TextClient.seen,
              ["resolve_model", "upload_text", "create_job:terra"])

        print("\n  модель не выбрана — донор соседу не уезжает и заказ не создаётся")
        c12 = fresh()
        store12 = []
        svc12 = services(store12)
        TextClient.model_error = ItemFailed(
            "в пресете не выбрана модель, а сосед вернул пустой список моделей")
        try:
            svc12.do_source_ready(c12, REL)
            check("выпуск остановлен", "не остановлен", "ItemFailed")
        except ItemFailed as exc:
            check("выпуск остановлен", "ItemFailed", "ItemFailed")
            check("причина названа", "не выбрана модель" in str(exc), True)
        check("до загрузки донора и до заказа дело не дошло",
              TextClient.seen, ["resolve_model"])
        call = c12.execute("SELECT status FROM provider_calls").fetchone()
        check("строка журнала закрыта как «до провайдера не дошли»",
              call["status"], "failed")

        print("\n  ...а уже оплаченный текст подхватывается и без списка моделей")
        c13 = fresh()
        store13 = []
        svc13 = services(store13)
        TextClient.model_error = None
        svc13.do_source_ready(c13, REL)                # заказ создан, деньги записаны
        forget_job(c13)
        TextClient.model_error = ItemFailed("список моделей получить не удалось")
        svc13.do_source_ready(c13, REL)                # ветка verify падать не должна
        check("подхват оплаченной задачи не зависит от списка моделей",
              json.loads(job_payload(c13) or "{}").get("donor_chars"), len(DONOR))
    finally:
        pub_services.RewriteClient = saved_client
        pub_services.donor_text = saved_text
        pub_services.rewrite_preset_config = saved_preset
        TextClient.result = "П" * 4600
        TextClient.model_error = None
        TextClient.seen = []


# ────────────── A2, запасной путь: новая ручка рерайта GET /api/v1/models ──────────────

def part_models_endpoint():
    """Ручка, которой в бою нет: список моделей по api-ключу, а не по сессии.

    Без неё запасной путь A2 недостижим — `RewriteClient.models()` ходит на
    `/api/v1/models`, а такого маршрута нет ни в нашем форке, ни в бою 18.09 (там
    список отдаёт сессионный `GET /models`, server.py:2283). Ребейз B7 её не принесёт.
    """
    print("\nНовая ручка рерайта GET /api/v1/models (по ключу, а не по сессии)")
    sys.path.insert(0, os.path.join(BASE, "rewrite"))
    import api as rewrite_api                                         # noqa: E402
    import rewrite_core                                              # noqa: E402

    router = rewrite_api.build_api_router(
        queue=None, upload_dir=Path("/tmp"), output_dir=Path("/tmp"),
        job_stop_events={}, job_stop_events_lock=threading.Lock())
    found = [r for r in router.routes if getattr(r, "path", "") == "/api/v1/models"]
    check("маршрут зарегистрирован", len(found), 1)
    if not found:
        return
    handler = found[0].endpoint

    creds = {"openai": "none", "claude": "none", "codex": "none"}
    allowed = {"openai": set(), "claude": set(), "codex": set()}
    saved = {name: getattr(rewrite_api, name) for name in (
        "credential_source", "provider_user_allowed", "get_allowed_openai_models",
        "get_allowed_claude_models", "get_allowed_codex_models", "get_keys_config")}
    rewrite_api.credential_source = lambda provider, uid: creds.get(provider, "none")
    rewrite_api.provider_user_allowed = lambda provider, uid: True
    rewrite_api.get_allowed_openai_models = lambda: set(allowed["openai"])
    rewrite_api.get_allowed_claude_models = lambda: set(allowed["claude"])
    rewrite_api.get_allowed_codex_models = lambda: set(allowed["codex"])
    rewrite_api.get_keys_config = lambda uid: {}
    was_env = os.environ.pop("REWRITE_STUDIO_CODEX_MODEL", None)
    try:
        print("\n  кредов нет — список пуст, выдумывать нечего")
        check("пустой список", handler(user={"user_id": "u1"}), [])

        print("\n  есть Codex — помечена та модель, которую сервер и так подставит")
        creds["codex"] = "server"
        allowed["codex"] = {"codex:gpt-5.4", "codex:gpt-5.9-sol"}
        body = handler(user={"user_id": "u1"})
        marked = [i["value"] for i in body if i.get("default")]
        # Значение обязано совпадать с запасным в server._resolve_studio_model:375
        # нашего форка. Разойдутся — ручка пометит одну модель, а сервер закажет другую.
        check("помечена ровно одна", marked, ["codex:gpt-5.4"])
        check("а вторая без пометки", len(body), 2)

        print("\n  ответ понятен кабинету без переводчика")
        items = pub_remote._parse_models(body)
        check("кабинет разобрал ответ", [i["value"] for i in items],
              ["codex:gpt-5.4", "codex:gpt-5.9-sol"])
        check("и выбрал бы помеченную", _chosen_from(items), "codex:gpt-5.4")

        print("\n  переменная сервера переставляет пометку")
        os.environ["REWRITE_STUDIO_CODEX_MODEL"] = "codex:gpt-5.9-sol"
        body = handler(user={"user_id": "u1"})
        check("помечена назначенная сервером",
              [i["value"] for i in body if i.get("default")], ["codex:gpt-5.9-sol"])
        os.environ.pop("REWRITE_STUDIO_CODEX_MODEL")

        print("\n  осознанно: не-Codex модели НЕ помечаются, и кабинет честно отказывает")
        creds["codex"] = "none"
        allowed["codex"] = set()
        creds["openai"] = "own"
        creds["claude"] = "own"
        allowed["openai"] = {"gpt-4.1-nano", "gpt-4.1-mini"}
        allowed["claude"] = {"claude-opus-5"}
        body = handler(user={"user_id": "u1"})
        check("модели отданы все три", len(body), 3)
        # Выбрать за владельца первую по алфавиту — это ровно тот дефект, который бой
        # чинил 13.09 (попадал на снятую gpt-5.4-mini), только за чужие деньги.
        check("пометки по умолчанию нет ни у одной",
              [i for i in body if i.get("default")], [])
        check("значит кабинет на пустом пресете откажет, а не закажет наугад",
              _refusal_for(pub_remote._parse_models(body)), True)

        print("\n  отсев по серверным ключам подхватится сам после ребейза B7")
        rewrite_core.server_key_model_error = (
            lambda model, user_id="": "" if model == "claude-opus-5" else "нельзя")
        try:
            body = handler(user={"user_id": "u1"})
            check("остались только разрешённые серверным ключом",
                  [i["value"] for i in body], ["claude-opus-5"])
        finally:
            del rewrite_core.server_key_model_error
        body = handler(user={"user_id": "u1"})
        check("без этой функции ручка работает как раньше", len(body), 3)
    finally:
        for name, fn in saved.items():
            setattr(rewrite_api, name, fn)
        if was_env is not None:
            os.environ["REWRITE_STUDIO_CODEX_MODEL"] = was_env
        else:
            os.environ.pop("REWRITE_STUDIO_CODEX_MODEL", None)


def _with_list(items, fn):
    """Прогнать настоящий resolve_model кабинета поверх ответа новой ручки."""
    saved = pub_remote.RewriteClient.models
    pub_remote.RewriteClient.models = lambda self: list(items)
    try:
        return fn(pub_remote.RewriteClient("k"))
    finally:
        pub_remote.RewriteClient.models = saved


def _chosen_from(items) -> str:
    """Что кабинет закажет, получив такой список и пустую модель в пресете.

    Отказ здесь не авария теста, а ответ «ничего»: так проверка читается строкой
    «ожидалось codex:gpt-5.4, получено ''», а не трассировкой.
    """
    try:
        return _with_list(items, lambda cl: cl.resolve_model({}))
    except ItemFailed:
        return ""


def _refusal_for(items) -> bool:
    """Откажет ли кабинет на пустой модели в пресете при таком списке соседа."""
    try:
        _with_list(items, lambda cl: cl.resolve_model({}))
        return False
    except ItemFailed:
        return True


def main() -> int:
    part_model()
    part_models_endpoint()
    part_length()
    if FAILS:
        print(f"\nПРОВАЛЕНО: {len(FAILS)}")
        for label in FAILS:
            print(f"  - {label}")
        return 1
    print("\nвсе проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
