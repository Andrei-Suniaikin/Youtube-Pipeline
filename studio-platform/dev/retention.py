#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Что лежит на диске, сколько это стоило и что можно убрать.

По умолчанию — только показывает. Удаление требует и правила владельца, и явного `--apply`.

    dev/retention.py                     отчёт: проекты, объём, цена содержимого
    dev/retention.py --policy strip=14   задать срок очистки промежуточного (дни)
    dev/retention.py --policy drop=90    задать срок удаления проекта целиком
    dev/retention.py --policy off        снять правило: снова ничего не удаляем
    dev/retention.py --apply             выполнить то, что показывает отчёт
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

from pub_env import load_env                                      # noqa: E402

load_env()

import pub_retention as R                                         # noqa: E402

DB = os.environ.get("VS_DEV_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
TENANTS = os.environ.get("VS_TENANTS_DIR") or os.path.join(BASE, "dev", "data", "tenants")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="действительно удалить")
    p.add_argument("--policy", metavar="strip=N|drop=N|off",
                   help="задать срок хранения — это решение владельца")
    p.add_argument("--tenant", default=os.environ.get("VS_DEV_TENANT") or None)
    args = p.parse_args()

    if not os.path.exists(DB):
        raise SystemExit(f"нет базы {DB} — сначала: python3 dev/make_db.py")
    c = sqlite3.connect(DB, timeout=10)

    if args.policy:
        pol = R.policy(c)
        if args.policy.strip() == "off":
            pol = {}
        else:
            for part in args.policy.replace(",", " ").split():
                k, _, v = part.partition("=")
                key = {"strip": "strip_after_days", "drop": "drop_after_days"}.get(k)
                if not key:
                    raise SystemExit(f"непонятно: {part} (нужно strip=N или drop=N)")
                pol[key] = int(v)
        c.execute("INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES(?,?,?)",
                  (R.STATE_KEY, json.dumps(pol), time.time()))
        c.commit()
        print(f"правило хранения: {pol or 'снято — ничего не удаляется'}")
        return 0

    space = R.free_space(TENANTS)
    if space.get("error"):
        print(f"место на диске не читается: {space['error']}")
    else:
        print(f"диск {space['path']}: свободно {space['free_gb']} ГБ "
              f"из {space['total'] / R.GB:.0f} ГБ")

    projects = R.scan(c, TENANTS, args.tenant)
    pol = R.policy(c)
    print(f"правило хранения: "
          f"{pol or 'не задано владельцем — ничего удаляться не будет'}")
    if not projects:
        print(f"\nпроектов нет ({TENANTS})")
        return 0

    acts = R.plan(projects, pol, now=time.time())
    print(f"\n{'проект':<22}{'объём':>10}{'из них лишнее':>15}  {'фаза':<14}решение")
    print("─" * 96)
    total = free = 0
    for pr, action, why, freed in acts:
        total += pr.total
        free += freed
        mark = {"keep": "оставить", "strip": "очистить", "drop": "удалить"}[action]
        print(f"{pr.pid[:21]:<22}{R._size(pr.total):>10}"
              f"{R._size(pr.strippable):>15}  {(pr.phase or '—')[:13]:<14}{mark}")
        print(f"{'':<22}{why}")
        if action != "keep":
            price = R.price_of(pr, action)
            if price["calls"]:
                print(f"{'':<22}безвозвратно теряются {price['calls']} оплаченных "
                      f"вызовов ({price['detail']})")
    print("─" * 96)
    print(f"{'всего':<22}{R._size(total):>10}{R._size(free):>15}"
          f"   ← освободится")

    if not args.apply:
        if free:
            print("\nэто только показ. Чтобы выполнить: dev/retention.py --apply")
        return 0
    print("\nвыполняю:")
    out = R.apply(c, acts, dry_run=False)
    print(f"\nочищено {out['strip']}, удалено {out['drop']}, "
          f"освобождено {R._size(out['freed'])}")
    for e in out["errors"]:
        print(f"  ошибка: {e}")
    return 1 if out["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
