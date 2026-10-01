#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пресет рерайта выбран, но не найден: предупреждение в журнале (01.10.2026).

Дефект Д24 из разбора манхвы и ВВ2. Публикационный контур спрашивает пресет рерайта у
соседа по идентификатору владельца, и ненайденный пресет возвращается НЕ ошибкой, а
пустым словарём. Задача после этого уходит на зашитых умолчаниях рерайта: не та модель,
не тот размер куска, без ведущего. В журнал при этом писалось «пресет «<выбранное имя>»»
— ровно то же, что и при успехе. Отличить одно от другого было нельзя ни запросом к
базе, ни глазами: канал месяцами работал мимо своих настроек и выглядел исправным.

Решение владельца 01.10.2026, п. 5: **сначала предупреждение, не отказ.** Отказ здесь
выглядит соблазнительно (не делай вслепую — остановись), но каналы, годами работающие на
умолчаниях с непустым полем пресета, встали бы все разом на первом же тике. Поэтому
проверяется именно пара свойств: строка в журнале появилась — и работа при этом НЕ
остановлена.

Второе: строка заказа теперь называет фактически применённые модель и ведущего. По
«пресет «X»» нельзя было понять, применился он или молча не нашёлся.

Соседей и сети здесь нет: `RewriteClient` и `donor_text` подменены.

    .venv/bin/python tests/test_rewrite_preset_missing.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_calls                                                   # noqa: E402
import pub_services                                                # noqa: E402

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

DONOR = "Исходный текст донора. " * 200


class TextClient:
    """Рерайт на бумажке. Запоминает, с какими настройками ушёл заказ."""

    orders = []

    def __init__(self, *_a, **_k):
        pass

    def resolve_model(self, preset):
        # Так ведёт себя настоящий клиент: пресет молчит — модель подсказывает сосед.
        return (preset or {}).get("model") or "terra"

    def upload_text(self, text, filename=""):
        return "file-1"

    def create_job(self, file_id, lang, preset, channel_name="", idempotency_key="",
                   model="", source_sha=""):
        TextClient.orders.append({"preset": dict(preset or {}), "model": model})
        return "job-1"

    def job(self, job_id):
        return {"status": "done"}

    def result_code(self, info, lang):
        return lang

    def result_text(self, job_id, code):
        return "П" * len(DONOR)


def services(store):
    s = pub_services.PubServices({"create_project": lambda *a, **k: "",
                                  "enqueue": lambda *a, **k: None,
                                  "project_dir": lambda *a, **k: ""})
    s.store_source_text = lambda c, rid, text, upd: store.append(text)
    TextClient.orders = []
    return s


def log(c):
    return [dict(r) for r in c.execute(
        "SELECT level, step, message FROM pub_release_log ORDER BY id")]


def warns(c, word):
    return [r for r in log(c) if r["level"] == "warn" and word in r["message"]]


def order_line(c):
    said = [r["message"] for r in log(c) if "заказал задачу" in r["message"]]
    return said[0] if said else ""


def run(rel, preset):
    """Один тик стадии рерайта с заданным ответом соседа про пресет."""
    c = fresh()
    store = []
    svc = services(store)
    pub_services.rewrite_preset_config = lambda uid, name: preset
    svc.do_source_ready(c, rel)
    return c, store


def main() -> int:
    saved = (pub_services.RewriteClient, pub_services.donor_text,
             pub_services.rewrite_preset_config)
    pub_services.RewriteClient = TextClient
    pub_services.donor_text = lambda url: DONOR
    try:
        print("Пресет выбран, а сосед его не знает")
        c, store = run(REL, {})
        сказано = warns(c, "не найден")
        check("в журнале есть предупреждение", len(сказано), 1)
        if сказано:
            check("и это уровень предупреждения, а не ошибки", сказано[0]["level"], "warn")
            check("названо имя пресета, которое выбрал человек",
                  "Базовый" in сказано[0]["message"], True)
            check("и сказано, что задача уйдёт на умолчаниях",
                  "умолчани" in сказано[0]["message"], True)
        # Главное в решении владельца: это ПРЕДУПРЕЖДЕНИЕ. Работа не остановлена —
        # иначе встали бы все каналы, годами работающие на умолчаниях.
        check("задача соседу всё равно заказана", len(TextClient.orders), 1)
        job = c.execute("SELECT status, remote_job_id FROM pub_release_jobs "
                        "WHERE service='rewrite' AND kind='job'").fetchone()
        check("и записана как идущая", (job["status"], job["remote_job_id"]),
              ("running", "job-1"))
        check("деньги за неё посчитаны",
              c.execute("SELECT status FROM provider_calls").fetchone()["status"],
              "ordered")

        print("\nСтрока заказа называет фактически применённое, а не выбранное имя")
        строка = order_line(c)
        check("сказано, что пресет не найден", "(не найден, умолчания)" in строка, True)
        check("названа модель, с которой ушёл заказ", "модель terra" in строка, True)
        check("и что ведущий не задан", "ведущий не задан" in строка, True)

        print("\nПресет нашёлся — предупреждать не о чем")
        c2, _ = run(REL, {"model": "luna", "host_name": "Мария"})
        check("предупреждения нет", warns(c2, "не найден"), [])
        строка = order_line(c2)
        check("в строке заказа модель пресета", "модель luna" in строка, True)
        check("и ведущий пресета", "ведущий Мария" in строка, True)
        check("и пометки «не найден» нет", "не найден" in строка, False)

        print("\nПресет не выбран вовсе — тоже не о чем предупреждать")
        # Пустое поле — это осознанный выбор «как получится», и он не ложь. Ругаться
        # здесь значило бы завалить журнал каждого такого канала на каждом тике.
        пусто = dict(REL, preset_rewrite="")
        c3, _ = run(пусто, {})
        check("предупреждения нет", warns(c3, "не найден"), [])
        check("в строке заказа так и сказано",
              "пресет «не выбран»" in order_line(c3), True)

        print("\nПробелы в поле — это тоже «не выбран»")
        c4, _ = run(dict(REL, preset_rewrite="   "), {})
        check("предупреждения нет", warns(c4, "не найден"), [])
    finally:
        (pub_services.RewriteClient, pub_services.donor_text,
         pub_services.rewrite_preset_config) = saved

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
