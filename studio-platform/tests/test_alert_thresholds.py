#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пороги тревог правятся с экрана, а не только переменной окружения (24.09.2026).

Владелец: «добавить возможность админу это всё эдитить». Раньше порог менялся по SSH,
правкой окружения и с перезапуском службы.

Старшинство: правка с экрана → переменная окружения → умолчание в коде. Проверяется
именно оно, плюс границы разумного (порог в одну секунду и в год одинаково вредны),
плюс что счётные пороги остаются целыми: `FAILED_MAX` режет список, дробным числом
срез не сделать.

    .venv/bin/python tests/test_alert_thresholds.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="alert-limits-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
# Значение «как на этой машине» для одного порога — чтобы проверить старшинство.
os.environ["VS_ALERT_STALL_SEC"] = "1200"
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "web"))

from pub_schema import ensure_pub_schema                           # noqa: E402

_c = sqlite3.connect(DB)
ensure_pub_schema(_c)
_c.executescript("""
  CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
    role TEXT, tenant_id TEXT, disabled INTEGER DEFAULT 0);
  CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
  -- Схема очереди — как в соседнем тесте тревог: свою выдумывать незачем, проход
  -- спрашивает у неё настоящие колонки.
  CREATE TABLE IF NOT EXISTS queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT,
    kind TEXT, status TEXT, created REAL, finished REAL, project_id TEXT, error TEXT);
  CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT);
""")
from pub_calls import ensure_calls_schema                          # noqa: E402

ensure_calls_schema(_c)
_c.commit()
_c.close()

import pub_alerts as A                                             # noqa: E402
import app as W                                                    # noqa: E402
from fastapi import HTTPException                                  # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def refused(label, fn, code, part=""):
    try:
        fn()
        check(label, "прошло без отказа", f"отказ {code}")
    except HTTPException as exc:
        check(label, (exc.status_code, part in str(exc.detail)), (code, True))


class Req:
    def __init__(self, role="admin"):
        class U(dict):
            def keys(self):
                return super().keys()
        self.state = type("St", (), {"user": U(id=1, name="boss", role=role)})()


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def value(key):
    return {x["key"]: x for x in A.limits_state(conn())["limits"]}[key]


def main() -> int:
    print("Пороги живут отдельной карточкой")
    # 24.09.2026: они были внутри карточки тревог, а та сворачивается. На тихой студии
    # тревоги свёрнуты — и таблица порогов пропадала вместе с ними: владелец не мог их
    # изменить вовсе. Своя карточка не зависит от того, горит что-то или нет.
    page = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
    check("карточка порогов заведена", "const limitsWrap" in page, True)
    check("таблица порогов идёт ПОСЛЕ карточки тревог",
          page.index("const limitsWrap") > page.index("app.append(alertsWrap)"), True)
    check("и сворачивается своим ключом", "'admin:limits'" in page, True)

    print("Пока не правили — берётся то, что на машине")
    check("порог из окружения", value("STALL_SEC")["value"], 1200.0)
    check("он же помечен как не свой", value("STALL_SEC")["own"], False)
    check("умолчание кода там, где окружение молчит",
          value("OLDEST_SEC")["value"], 6 * 3600.0)
    check("в модуле то же самое", A.STALL_SEC, 1200)

    print("Правка с экрана старше окружения")
    out = W.ui_alert_threshold(W.AlertLimitIn(key="STALL_SEC", value=2700), Req())
    check("ответ — весь список", len(out["limits"]) >= 10, True)
    check("значение записано", value("STALL_SEC")["value"], 2700.0)
    check("помечено как своё", value("STALL_SEC")["own"], True)
    check("значение с машины осталось запасным",
          value("STALL_SEC")["default"], 1200.0)
    check("модуль применил сразу", A.STALL_SEC, 2700.0)

    print("Новый проход видит новый порог")
    A.STALL_SEC = 1        # как будто модуль остался со старым значением
    A.detect(conn(), now=1790000000.0)
    check("detect применяет правки", A.STALL_SEC, 2700.0)

    print("Возврат к тому, что на машине")
    W.ui_alert_threshold(W.AlertLimitIn(key="STALL_SEC", value=None), Req())
    check("вернулось значение окружения", value("STALL_SEC")["value"], 1200.0)
    check("метка «своё» снята", value("STALL_SEC")["own"], False)
    check("и модуль вернулся", A.STALL_SEC, 1200.0)

    print("Границы разумного")
    refused("слишком мало", lambda: W.ui_alert_threshold(
        W.AlertLimitIn(key="STALL_SEC", value=5), Req()), 400, "допустимо")
    refused("слишком много", lambda: W.ui_alert_threshold(
        W.AlertLimitIn(key="STALL_SEC", value=10 ** 6), Req()), 400, "допустимо")
    refused("выдуманный порог", lambda: W.ui_alert_threshold(
        W.AlertLimitIn(key="ВСЁ_ПЛОХО", value=60), Req()), 400, "такого порога нет")
    check("после отказов значение не тронуто", value("STALL_SEC")["value"], 1200.0)

    print("Счётные пороги остаются целыми")
    W.ui_alert_threshold(W.AlertLimitIn(key="FAILED_MAX", value=3), Req())
    check("тип целый", isinstance(A.FAILED_MAX, int), True)
    check("и значение то самое", A.FAILED_MAX, 3)
    # Ровно то место, где дробное число ломало проход: срез списка.
    A.detect(conn(), now=1790000000.0)
    check("проход не падает", isinstance(A.FAILED_MAX, int), True)

    print("Правит только администратор")
    refused("обычному нельзя", lambda: W.ui_alert_threshold(
        W.AlertLimitIn(key="STALL_SEC", value=1800), Req(role="user")), 403, "")

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
