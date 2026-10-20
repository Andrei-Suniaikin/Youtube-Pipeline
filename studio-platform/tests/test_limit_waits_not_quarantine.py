#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Лимит подписки на любом шаге движка — ожидание, а не карантин (20.10.2026, ночь).

Что случилось. Выпуск r_f6c094c1d8 вернули из разбора после правок лимита — и через
минуту он снова лежал там с «ошибка на шаге «cover_ready»: подписка Codex не ответила
(502): SUBSCRIPTION_LIMIT…|все ключи отдыхают…». Две причины:

* смягчение надписи обложки ловило только свой `cover_soften.SoftenError`, а общий
  HTTP-вызов поднимает `tts_soften.SoftenError` — другой класс; отказ подписки пролетал
  мимо и падал в движок как «поломка обработчика»;
* движок любую неизвестную ошибку обработчика уводит в карантин, не глядя на текст.

Теперь: смягчение при лимите поднимает `ResourceDown("codex")` (раунд смягчения не
тратится), а движок на ЛЮБОМ шаге узнаёт лимит по тексту и ведёт его как занятый
ресурс — строка ждёт, следующий круг попробует снова, оплаченное не трогается.
Обычная поломка по-прежнему уходит в карантин: лечить ожиданием то, что не починится
само, — значит прятать беду.

    .venv/bin/python tests/test_limit_waits_not_quarantine.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server"))

import test_cover_soften as T                                     # noqa: E402  обвязка обложки
import pub_engine as E                                            # noqa: E402
import pub_schema                                                 # noqa: E402

FAILS = []
NOW = 1_800_000_000.0
LIMIT = ("подписка Codex не ответила (502): SUBSCRIPTION_LIMIT:reset=1792527257|частота|"
         "codex: все ключи отдыхают, ближайший освободится через 48 с")


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class Boom:
    """Обработчик шага, который падает заданной ошибкой."""

    def __init__(self, exc):
        self.exc = exc

    def do_source_ready(self, c, rel):
        raise self.exc

    def release_resources(self, c, rel):
        pass


def fresh_release():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(c)
    c.execute("INSERT INTO pub_channels(tenant_id,uc,name,lang,tz,schedule,deferred,"
              "deferred_limit,created,updated) VALUES('dev','UC1','Канал','pl','Europe/Warsaw',"
              "'{}',1,0,?,?)", (NOW, NOW))
    c.execute("INSERT INTO pub_plan_items(id,tenant_id,channel_id,version,donor_url,"
              "donor_video_id,donor_title,slot_at,status,created,updated) "
              "VALUES(1,'dev',1,1,'https://youtu.be/x','x','Донор',?,'running',?,?)",
              (NOW + 86400, NOW, NOW))
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
              "channel_id,uc,slot_at,project_id,status,phase,attempts,deadline,created,updated) "
              "VALUES('rel-1','dev',1,1,1,'UC1',?,'p1','running','created',0,?,?,?)",
              (NOW + 86400, NOW + 3600, NOW - 60, NOW - 60))
    c.commit()
    return c


def advance_with(exc):
    c = fresh_release()
    rel = c.execute("SELECT * FROM pub_releases WHERE release_id='rel-1'").fetchone()
    out = E.advance(c, rel, Boom(exc), NOW)
    c.commit()
    row = c.execute("SELECT status, phase, blocked_reason FROM pub_releases "
                    "WHERE release_id='rel-1'").fetchone()
    return out, row["status"], row["phase"], str(row["blocked_reason"] or "")


def main() -> int:
    print("1. Движок: лимит в тексте любой ошибки — ждём, а не карантин")
    out, status, phase, why = advance_with(RuntimeError(LIMIT))
    check("исход шага — ожидание ресурса", out, "resource_wait")
    check("выпуск ждёт, не остановлен", status, "pending")
    check("фаза не тронута", phase, "created")
    check("причина начинается с «ждём: подписка занята»",
          why.startswith("ждём: подписка занята"), True)

    print("\n2. Движок: обычная поломка по-прежнему в карантин")
    out, status, _, why = advance_with(RuntimeError("KeyError: 'preset'"))
    check("исход — карантин", out, "quarantined")
    check("выпуск остановлен", status, "blocked")
    check("причина названа как ошибка шага", why.startswith("ошибка на шаге"), True)

    print("\n3. Обложка: лимит на смягчении — ResourceDown, раунд не потрачен")
    S, cover_soften = T.S, T.cover_soften
    was = (S.CoverClient, S.get_key, cover_soften.tts_soften.chat_once)
    S.CoverClient, S.get_key = T.FakeCover, (lambda c, name: "ключ")
    empty = {"status": "completed_partial", "results_count": 0, "output_files": [],
             "progress_detail": "0/1 обложек", "finished_at": T.NOW, "log": T.LOG_URL}
    try:
        def limited(messages, key, base, **kw):
            raise cover_soften.tts_soften.SoftenError(LIMIT)
        cover_soften.tts_soften.chat_once = limited
        T.FakeCover.created, T.FakeCover.title_jobs = [], []
        c = T.db("re-ordered")
        try:
            got = T.step(c, empty)
            check("смягчение при лимите поднимает ResourceDown", "не поднял", "ResourceDown")
        except S.ResourceDown as exc:
            check("смягчение при лимите поднимает ResourceDown", True, True)
            check("ресурс назван", exc.resource, "codex")
        check("новая обложка не заказана", T.FakeCover.title_jobs, [])
        check("раунд смягчения не потрачен", T.job_row(c)["error"], "re-ordered")

        print("\n4. Обложка: обычный отказ сервиса — как раньше: в разбор, без заказа")
        # Не-лимитный отказ (сеть, таймаут) лечить ожиданием нельзя — он не пройдёт сам.
        # Прежнее поведение: смягчение не удалось → обложки нет → разбор. Оно сохранено.
        def refused(messages, key, base, **kw):
            raise cover_soften.tts_soften.SoftenError("сервис текста не ответил: Timeout")
        cover_soften.tts_soften.chat_once = refused
        c = T.db("re-ordered")
        got = T.step(c, empty)
        check("выпуск уходит в разбор, как и раньше", got[0], "разбор")
        check("причина — обложка не нарисовалась", "обложка не нарисовалась" in got[1], True)
        check("заказов нет", T.FakeCover.title_jobs, [])
    finally:
        S.CoverClient, S.get_key, cover_soften.tts_soften.chat_once = was

    print("\nитог:", "все проверки прошли" if not FAILS else f"ПРОВАЛОВ {len(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
