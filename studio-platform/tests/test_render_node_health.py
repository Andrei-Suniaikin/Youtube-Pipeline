#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пульс машины рендера: место, неотданное готовое, версии (24.09.2026).

Боевая нода зовёт `/api/render/node-health` раз в 30 секунд с 18.09. У студии этой
ручки не было, и в журнале машины раз в полминуты капала ошибка — на настоящую поломку
пришлось бы смотреть сквозь шум из ложных.

Проверяется: чужой токен не пускают; снимок кладётся по машинам и обновляется, а не
копится; место уходит туда же, куда его кладёт выдача работ (иначе тревога «мало места»
не узнает про машину рендера, пока очередь пуста); мусор в теле не роняет ручку.

    VS_TEST_NO_NET=1 .venv/bin/python tests/test_render_node_health.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="render-health-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_DB"] = DB
os.environ["PULLER_TOKEN"] = "pul_Zx9-test-token"   # как в бою: только ASCII
os.environ["VS_SERVER_DISABLE_BACKGROUND"] = "1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
# Кабинет добавляет в путь поиска `~/VideoStudio/src` — в стенде это ссылка на наши
# воркеры. HOME задаём принудительно: у запускающего он свой, и без подмены импорт
# кабинета не состоится.
os.environ["HOME"] = os.path.join(BASE, "dev", "home")
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "workers"))

import main as cabinet                                             # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402

FAILS = []
client = TestClient(cabinet.app, raise_server_exceptions=False)


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def beat(payload, token="pul_Zx9-test-token"):
    return client.post("/api/render/node-health", json=payload,
                       headers={"X-Puller-Token": token.encode("utf-8")})


def state(key="render_node_health"):
    c = sqlite3.connect(DB)
    try:
        row = c.execute("SELECT v FROM admin_state WHERE k=?", (key,)).fetchone()
        return json.loads(row[0]) if row and row[0] else {}
    finally:
        c.close()


GB = 1024 ** 3


def main() -> int:
    print("Кого не пускаем")
    check("без токена — отказ", beat({"free_bytes": GB}, token="").status_code, 403)
    check("чужой токен — отказ",
          beat({"free_bytes": GB}, token="pul_чужой").status_code, 403)
    # Токен владельца может быть каким угодно, в том числе с кириллицей: отказ должен
    # остаться отказом, а не стать пятисоткой (та же болезнь была у заливки).
    check("мятый токен — отказ, а не пятисотка",
          beat({"free_bytes": GB}, token="мусор").status_code, 403)

    print("Снимок машины")
    r = beat({"free_bytes": 300 * GB, "outbox_retained_bytes": 2 * GB,
              "outbox_attention": 1, "outbox_stuck": 2, "active_jobs": 3,
              "puller_sha256": "a" * 64, "worker_sha256": "b" * 64,
              "host": "minsk"})
    check("принято", (r.status_code, r.json()), (200, {"ok": True}))
    snap = state().get("minsk") or {}
    check("место переведено в гигабайты", snap.get("free_gb"), 300.0)
    check("неотданное готовое видно", snap.get("outbox_gb"), 2.0)
    check("сколько требует внимания", snap.get("outbox_attention"), 1)
    check("сколько просрочено", snap.get("outbox_stuck"), 2)
    check("сколько роликов в работе", snap.get("active_jobs"), 3)
    check("версия пуллера — хвостом", snap.get("puller"), "a" * 12)
    check("версия рендерера — хвостом", snap.get("worker"), "b" * 12)
    check("время снято", isinstance(snap.get("at"), float), True)

    print("Место уходит туда же, куда его кладёт выдача работ")
    disk = state("node_disk")
    check("машина записана", "minsk" in json.dumps(disk, ensure_ascii=False), True)
    check("и с местом", "300" in json.dumps(disk, ensure_ascii=False), True)

    print("Снимок обновляется, а не копится")
    beat({"free_bytes": 250 * GB, "host": "minsk"})
    check("машина по-прежнему одна", sorted(state()), ["minsk"])
    check("место свежее", (state()["minsk"] or {}).get("free_gb"), 250.0)
    beat({"free_bytes": 100 * GB, "host": "mac"})
    check("вторая машина добавилась", sorted(state()), ["mac", "minsk"])
    check("первая не затёрта", (state()["minsk"] or {}).get("free_gb"), 250.0)

    print("Мусор в теле не роняет ручку")
    check("пустое тело", beat({}).status_code, 200)
    check("машина без имени легла под «render»", "render" in state(), True)
    check("строки вместо чисел", beat({"free_bytes": "много", "active_jobs": "три",
                                       "host": "minsk"}).status_code, 200)
    check("место стало неизвестным, а не выдуманным",
          (state()["minsk"] or {}).get("free_gb"), None)
    r = client.post("/api/render/node-health", content="не json".encode("utf-8"),
                    headers={"X-Puller-Token": b"pul_Zx9-test-token",
                             "Content-Type": "application/json"})
    check("не разобранное тело — не пятисотка", r.status_code, 200)

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
