#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""«Вернуть в работу» обнуляет счёт повторов картинок (05.10.2026).

Что случилось. Движок сам ставит картинки заново не больше двух раз (выпали кадры) —
пометкой «images-retry:N» в зеркале задачи. После исправления перезаказа отклонённых
кадров семь выпусков вернули в работу, и движок на первом же тике уронил их обратно:
пометка «images-retry:2» осталась исчерпанной. Человек, вернувший выпуск, решил, что
причина устранена, — значит и счёт повторов начинается заново, как `attempts`.

Возврат вслепую (keep_attempts=True) счёт НЕ обнуляет: автоматический возврат не должен
гонять платные прогоны картинок по кругу.

    .venv/bin/python tests/test_revive_resets_retries.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_engine as E                                             # noqa: E402
import pub_schema                                                  # noqa: E402

FAILS = []
NOW = 1_800_000_000.0


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def world(mark):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(c)
    c.execute("INSERT INTO pub_releases(release_id, tenant_id, plan_item_id, plan_item_version, "
              "channel_id, uc, slot_at, project_id, status, phase, blocked_reason, created, updated) "
              "VALUES('r1','3',1,1,1,'UC1',?,'p1','blocked','project_ready',"
              "'этап «images» завершился со статусом error',?,?)", (NOW + 86400, NOW, NOW))
    for kind, err in (("images", mark), ("tts", None)):
        c.execute("INSERT INTO pub_release_jobs(release_id, service, kind, remote_job_id, status, "
                  "error, created, updated) VALUES('r1','vsgen',?,?,'error',?,?,?)",
                  (kind, "11" if kind == "images" else "10", err, NOW, NOW))
    c.commit()
    return c


def mark(c, kind="images"):
    return c.execute("SELECT error FROM pub_release_jobs WHERE release_id='r1' AND kind=?",
                     (kind,)).fetchone()[0]


def main() -> int:
    print("Человек вернул выпуск — счёт повторов картинок заново")
    for m in ("images-retry:2", "prompt-channel:2"):
        c = world(m)
        out = E.revive(c, "r1", NOW)
        check(f"{m}: выпуск вернулся", out.get("ok"), True)
        check(f"{m}: пометка снята", mark(c), None)

    print("\nДругие пометки зеркала не трогаем")
    c = world("re-enqueued")
    E.revive(c, "r1", NOW)
    check("«re-enqueued» осталась", mark(c), "re-enqueued")

    print("\nВозврат вслепую — счёт остаётся")
    c = world("images-retry:2")
    E.revive(c, "r1", NOW, keep_attempts=True)
    check("пометка на месте", mark(c), "images-retry:2")

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
