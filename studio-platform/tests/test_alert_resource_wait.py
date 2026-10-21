#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""§16 тревог: выпуски стоят в «ждём» из-за ключа обложек или подписки Codex (21.10.2026).

С 20.10 лимиты не роняют выпуск, а переводят его в `pending` с `blocked_reason="ждём: …"`.
Ни один прежний повод этого состояния не читал: три дня 24–28 выпусков ждали ключ xAI
(402 «token allowance exhausted»), и в Телеграме было тихо. Владелец: «нет сообщений, что
ключ исчерпался, некая иллюзия того, что всё работает».

Проверяем: повод появляется от двух выпусков и держится полчаса (MIN_HOLD), один выпуск
между повторами — не повод, рабочие ожидания («в отложке канала уже 5 роликов») — тоже;
«починилось» уходит, когда ожидание прошло.

    .venv/bin/python tests/test_alert_resource_wait.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="alert-wait-")
os.environ["VS_DEV_DB"] = os.path.join(TMP, "vsgen.db")
os.environ["VS_SERVER_DB"] = os.environ["VS_DEV_DB"]
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_alerts as A                                            # noqa: E402
import pub_schema                                                 # noqa: E402

FAILS = []
NOW = 1_792_080_000.0
COVER = ("ждём: сервис обложек не сделал сцену для надписи (лимит ключа или сбой) — "
         "повторю через 15 минут")
CODEX = ("ждём: подписка Codex не дала название (502): SUBSCRIPTION_LIMIT:reset=1792569191|"
         "частота|codex: все ключи отдыхают, ближайший освободится через 28 с")
QUEUE = "ждём: в отложке канала уже 5 роликов — ждём места"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def world(reasons):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(c)
    c.execute("CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL)")
    c.executescript("""
      CREATE TABLE IF NOT EXISTS queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT, project_id TEXT,
        kind TEXT, status TEXT, created REAL, started REAL, finished REAL, error TEXT, provider TEXT);
      CREATE TABLE IF NOT EXISTS job_logs(id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, ts REAL, line TEXT);
    """)
    c.execute("INSERT INTO pub_channels(tenant_id,uc,name,lang,tz,schedule,deferred,"
              "deferred_limit,created,updated) VALUES('3','UC1','Канал','pl','Europe/Warsaw',"
              "'{}',1,0,?,?)", (NOW, NOW))
    for n, why in enumerate(reasons, 1):
        c.execute("INSERT INTO pub_plan_items(id,tenant_id,channel_id,version,donor_url,"
                  "donor_video_id,donor_title,slot_at,status,created,updated) "
                  "VALUES(?,'3',1,1,'https://youtu.be/x','x','Донор',?,'running',?,?)",
                  (n, NOW + 86400 + n * 3600, NOW, NOW))
        c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
                  "channel_id,uc,slot_at,project_id,status,phase,attempts,deadline,created,"
                  "updated,blocked_reason) VALUES(?,'3',?,1,1,'UC1',?,?,'pending','cover_ready',"
                  "0,?,?,?,?)", (f"r_{n:03d}", n, NOW + 86400 + n * 3600, f"p{n}", NOW + 3600, NOW - 60,
                                 NOW, why))
    c.commit()
    return c


def main() -> int:
    print("1. Три выпуска ждут сервис обложек — повод «обложки»")
    c = world([COVER, COVER, COVER, QUEUE])
    found = {a.key: a for a in A.check_resource_wait(c, NOW, tenant="3")}
    check("ключ повода", sorted(found), ["resource_wait:обложки"])
    a = found["resource_wait:обложки"]
    check("главная цифра — сколько ждут", a.figure, 3)
    check("в тексте — что ответил сервис", "лимит ключа" in a.text, True)
    check("в тексте — что делать", "xAI" in a.text and "15 минут" in a.text, True)

    print("\n2. Один выпуск между повторами — не повод; рабочая очередь — не повод")
    check("один выпуск", A.check_resource_wait(world([COVER, QUEUE, QUEUE]), NOW, "3"), [])
    check("только отложка", A.check_resource_wait(world([QUEUE] * 5), NOW, "3"), [])

    print("\n3. Подписка Codex в лимите — свой повод, с кодом ответа")
    found = {a.key: a for a in A.check_resource_wait(world([CODEX, CODEX, COVER]), NOW, "3")}
    check("ключ повода", sorted(found), ["resource_wait:подписка Codex"])
    check("код в тексте", "502" in found["resource_wait:подписка Codex"].text, True)

    print("\n4. Проход тревог: полчаса держим, потом говорим, после — «починилось»")
    A.CHECKS = (A.check_resource_wait,)        # остальным поводам нужен весь мир кабинета
    sent = []
    send = lambda text: (sent.append(text), True)[1]
    c = world([COVER, COVER])
    out = A.tick(c, send, now=NOW, tenant="3")
    check("сразу — молчим (повод должен продержаться)", [k for k, _, _ in out], [])
    out = A.tick(c, send, now=NOW + 31 * 60, tenant="3")
    check("через 31 минуту — тревога", [(k, kind) for k, kind, _ in out],
          [("resource_wait:обложки", "alarm")])
    check("ушло в телеграм", any("обложки" in t.lower() for t in sent), True)
    out = A.tick(c, send, now=NOW + 40 * 60, tenant="3")
    check("через 9 минут — не повторяем", out, [])
    c.execute("UPDATE pub_releases SET status='running', blocked_reason=NULL")
    c.commit()
    before = len(sent)
    A.tick(c, send, now=NOW + 50 * 60, tenant="3")      # «починилось» в список tick не входит
    check("ожидание прошло — «починилось» ушло в телеграм",
          any(t.startswith("ПОЧИНИЛОСЬ") and "Обложки" in t for t in sent[before:]), True)
    check("память повода очищена", A.load_state(c), {})

    print("\nитог:", "все проверки прошли" if not FAILS else f"ПРОВАЛОВ {len(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
