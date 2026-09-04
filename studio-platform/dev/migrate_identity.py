#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Свести три списка пользователей в один. По умолчанию только показывает.

    dev/migrate_identity.py              карта: кто есть где и что будет
    dev/migrate_identity.py --apply      выполнить (сначала делает бэкап)
    dev/migrate_identity.py --rollback   откатить последний перенос по журналу

Общим идентификатором становится `tenant_id` кабинета — по нему уже раскладываются
проекты, значит после переноса все три сервиса хранят данные человека под одним ключом.

Перенос требует остановленных сервисов: менять user_id под работающим воркером значит
получить задачу, которая ищет свои файлы по старому пути.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import identity as I                                              # noqa: E402

CABINET_DB = os.environ.get("VS_DEV_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
PORTS = ((8800, "кабинет"), (8899, "rewrite"), (8900, "cover"))

STATE_TEXT = {I.OK: "перенести", I.DONE: "уже общий", I.NO_ACCOUNT: "НЕТ В КАБИНЕТЕ",
              I.COLLISION: "СТОЛКНОВЕНИЕ", "—": "нет учётки"}


def running() -> list:
    """Кто из сервисов сейчас слушает порт. Перенос под работающим — это порча данных."""
    alive = []
    for port, name in PORTS:
        s = socket.socket()
        s.settimeout(0.3)
        try:
            s.connect(("127.0.0.1", port))
            alive.append(f"{name} (:{port})")
        except OSError:
            pass
        finally:
            s.close()
    return alive


def show(p: dict, svcs: list) -> None:
    # Идентификаторы у соседей — 12 hex либо зашитый литерал; режем их до хвоста,
    # потому что важно не значение, а то, совпадает оно с целевым или нет.
    def short(uid):
        return uid if len(uid) <= 14 else "…" + uid[-11:]

    print(f"{'логин':<16}{'общий id':<12}{'rewrite':<30}{'cover':<30}")
    print("─" * 88)
    for row in p["rows"]:
        cells = []
        for name in ("rewrite", "cover"):
            svc = row["services"][name]
            mark = STATE_TEXT.get(svc["state"], svc["state"])
            cells.append(f"{short(svc['uid']) if svc['uid'] else '—':<14} {mark}")
        flag = "  (заблокирован)" if row["disabled"] else ""
        print(f"{row['login'][:15]:<16}{(row['target'] or '—'):<12}"
              f"{cells[0]:<30}{cells[1]:<30}{flag}")

    if not p["movers"]:
        print("\nпереносить нечего: все под общим идентификатором либо учёток нет")
        return

    print("\nЧто именно будет тронуто")
    by_name = {s.name: s for s in svcs}
    for row in p["movers"]:
        print(f"\n  {row['login']}  →  {row['target']}")
        for name, svc in row["services"].items():
            if svc["state"] != I.OK:
                continue
            c = I.count(by_name[name], svc["uid"])
            rows = sum(c["rows"].values())
            print(f"    {name}: строк {rows}, путей в задачах {c['json']}, "
                  f"каталогов {len(c['dirs'])}")
            for key, n in sorted(c["rows"].items()):
                print(f"        {key}: {n}")
            for d in c["dirs"]:
                print(f"        {os.path.relpath(d, BASE)}")


def warn_admin(p: dict) -> None:
    """Литерал ADMIN_USER_ID после переноса перестанет совпадать — сказать заранее."""
    ids = I.admin_ids(BASE)
    if not ids:
        return
    moving = {}
    for row in p["movers"]:
        for name, svc in row["services"].items():
            if svc["state"] == I.OK and svc["uid"] in ids.values():
                moving[svc["uid"]] = row["target"]
    if not moving:
        return
    print("\n" + "!" * 80)
    print("ВНИМАНИЕ: у соседей админ опознаётся сравнением с зашитым ADMIN_USER_ID.")
    print("После переноса это значение перестанет совпадать, и админ потеряет права.")
    for rel, val in sorted(ids.items()):
        new = moving.get(val)
        print(f"  {rel}: сейчас «{val}»" + (f"  →  должно стать «{new}»" if new else ""))
    print("Cover читает его из COVER_ADMIN_USER_ID — достаточно переменной окружения.")
    print("Rewrite держит литералом в двух файлах — их надо править.")
    print("!" * 80)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="выполнить перенос")
    ap.add_argument("--rollback", action="store_true", help="откатить последний перенос")
    ap.add_argument("--force", action="store_true",
                    help="не проверять, что сервисы остановлены")
    args = ap.parse_args()

    if not os.path.exists(CABINET_DB):
        raise SystemExit(f"нет базы кабинета {CABINET_DB}")

    if args.rollback:
        alive = running()
        if alive and not args.force:
            raise SystemExit(f"сначала остановите: {', '.join(alive)}")
        out = I.rollback(BASE)
        print(f"\nоткачено: строк {out['rows']}, каталогов {out['dirs']}")
        return 0

    svcs = I.services(BASE)
    p = I.plan(CABINET_DB, BASE)
    show(p, svcs)

    if p["problems"]:
        print("\nПроблемы, которые надо решить до переноса:")
        for why in dict.fromkeys(p["problems"]):
            print(f"  • {why}")
        if args.apply:
            print("\nперенос не выполнен: сначала разберитесь с перечисленным")
            return 1

    warn_admin(p)

    if not p["movers"]:
        return 0
    if not args.apply:
        print("\nэто только показ. Чтобы выполнить: dev/migrate_identity.py --apply")
        return 0

    alive = running()
    if alive and not args.force:
        raise SystemExit(f"\nсначала остановите: {', '.join(alive)}\n"
                         f"менять user_id под работающим воркером — это порча данных")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    into = os.path.join(BASE, "dev", "backup", stamp)
    paths = [CABINET_DB] + [s.db_path(f) for s in svcs for f in s.dbs]
    made = I.backup(paths, into)
    print(f"\nбэкап: {len(made)} баз в {os.path.relpath(into, BASE)}")

    print("\nперенос:")
    by_name = {s.name: s for s in svcs}
    moved, totals = [], {"rows": 0, "json": 0, "dirs": 0}
    for row in p["movers"]:
        print(f"  {row['login']} → {row['target']}")
        for name, svc in row["services"].items():
            if svc["state"] != I.OK:
                continue
            res = I.move_one(by_name[name], svc["uid"], row["target"], log=print)
            moved.append({"service": name, "login": row["login"],
                          "old": svc["uid"], "new": row["target"]})
            totals["rows"] += res["rows"]
            totals["json"] += res["json"]
            totals["dirs"] += len(res["dirs"])
            print(f"    {name}: строк {res['rows']}, путей {res['json']}, "
                  f"каталогов {len(res['dirs'])}")

    journal = I.write_journal(BASE, {"at": time.time(), "stamp": stamp,
                                     "backup": into, "moved": moved})
    print(f"\nготово: строк {totals['rows']}, путей в задачах {totals['json']}, "
          f"каталогов {totals['dirs']}")
    print(f"журнал: {os.path.relpath(journal, BASE)}  "
          f"(откат: dev/migrate_identity.py --rollback)")

    after = I.plan(CABINET_DB, BASE)
    left = [r["login"] for r in after["movers"]]
    print("сверка: " + ("всё под общим идентификатором"
                        if not left else f"ОСТАЛИСЬ {', '.join(left)}"))
    warn_admin(after)
    return 0 if not left else 1


if __name__ == "__main__":
    raise SystemExit(main())
