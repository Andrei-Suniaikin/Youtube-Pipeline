#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Что лежит на диске, сколько это стоило и что можно убрать.

По умолчанию — только показывает. Удаление требует и срока хранения, и явного согласия.

Срок задаётся в `.env`, чтобы владелец правил его сам, на своём сервере:

    VS_RETENTION_STRIP_DAYS=14   через сколько дней после публикации убрать кадры и звук
    VS_RETENTION_DROP_DAYS=90    через сколько удалить проект целиком
    VS_RETENTION_AUTO=1          убирать самим; без этого — только отчёт
    VS_RETENTION_EVERY_SEC=3600  как часто проверять в режиме --loop

Команды:

    dev/retention.py                     отчёт: проекты, объём, цена содержимого
    dev/retention.py --apply             выполнить то, что показывает отчёт, сейчас
    dev/retention.py --loop              проверять по кругу (в бою — systemd-таймер)
    dev/retention.py --policy strip=14   запасной путь: срок в базе, если `.env` молчит
    dev/retention.py --policy off        снять правило из базы
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

try:
    from pub_alerts_tg import Telegram                            # noqa: E402
except Exception:                                                 # noqa: BLE001
    Telegram = None

DB = os.environ.get("VS_DEV_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
TENANTS = os.environ.get("VS_TENANTS_DIR") or os.path.join(BASE, "dev", "data", "tenants")


SOURCE = {"env": ".env", "db": "база (запасной путь)"}


def describe(pol: dict) -> str:
    if not pol:
        return ("не задан — ничего удаляться не будет "
                f"(см. {R.ENV_STRIP} и {R.ENV_DROP} в .env.example)")
    strip = pol.get("strip_after_days")
    drop = pol.get("drop_after_days")
    parts = []
    if strip is not None:
        parts.append(f"очистка через {strip} дн.")
    if drop is not None:
        parts.append(f"удаление через {drop} дн.")
    return f"{', '.join(parts)}  ← {SOURCE.get(pol.get('source'), '?')}"


def set_db_policy(c, spec: str) -> int:
    """Запасной путь: срок в базе. Работает, только пока `.env` молчит."""
    pol = R.db_policy(c)
    if spec.strip() == "off":
        pol = {}
    else:
        for part in spec.replace(",", " ").split():
            k, _, v = part.partition("=")
            key = {"strip": "strip_after_days", "drop": "drop_after_days"}.get(k)
            if not key:
                raise SystemExit(f"непонятно: {part} (нужно strip=N или drop=N)")
            pol[key] = int(v)
    c.execute("INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES(?,?,?)",
              (R.STATE_KEY, json.dumps(pol), time.time()))
    c.commit()
    print(f"в базе: {pol or 'снято'}")
    if R.env_policy():
        print(f"но действует .env: {describe(R.policy(c))}\n"
              f"пока {R.ENV_STRIP}/{R.ENV_DROP} заданы, значение из базы не применяется")
    return 0


def report(c, tenant) -> int:
    space = R.free_space(TENANTS)
    if space.get("error"):
        print(f"место на диске не читается: {space['error']}")
    else:
        print(f"диск {space['path']}: свободно {space['free_gb']} ГБ "
              f"из {space['total'] / R.GB:.0f} ГБ")

    projects = R.scan(c, TENANTS, tenant)
    pol = R.policy(c)
    print(f"срок хранения: {describe(pol)}")
    print(f"автоуборка: "
          f"{'включена' if R.auto_enabled() else f'выключена ({R.ENV_AUTO} не задан)'}"
          f" · проверка раз в {R.every_sec() // 60} мин")
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
    print(f"{'всего':<22}{R._size(total):>10}{R._size(free):>15}   ← освободится")
    last = R.last_sweep(c)
    if last.get("at"):
        print(f"\nпоследний проход {time.strftime('%d.%m %H:%M', time.localtime(last['at']))}"
              f": {last.get('line') or ''}")
    if free:
        print("\nэто только показ. Выполнить сейчас: dev/retention.py --apply")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true",
                   help="выполнить уборку сейчас, не дожидаясь VS_RETENTION_AUTO")
    p.add_argument("--loop", action="store_true",
                   help=f"проверять каждые {R.ENV_EVERY} секунд")
    p.add_argument("--every", type=int, default=0, help="переопределить период, секунд")
    p.add_argument("--policy", metavar="strip=N|drop=N|off",
                   help="запасной путь: записать срок в базу")
    p.add_argument("--tenant", default=os.environ.get("VS_DEV_TENANT") or None)
    args = p.parse_args()

    if not os.path.exists(DB):
        raise SystemExit(f"нет базы {DB} — сначала: python3 dev/make_db.py")
    c = sqlite3.connect(DB, timeout=10)

    if args.policy:
        return set_db_policy(c, args.policy)
    if not (args.apply or args.loop):
        return report(c, args.tenant)

    # Отчёт в телеграм — только о том, что действительно сделано. Сообщать «я ничего
    # не убрал» каждый час означает приучить не читать эти сообщения.
    tg = Telegram() if Telegram else None
    every = args.every or R.every_sec()
    while True:
        res = R.sweep(c, TENANTS, args.tenant, dry_run=(False if args.apply else None))
        stamp = time.strftime("%H:%M:%S")
        print(f"{stamp}  {res['line']}")
        for e in res["errors"]:
            print(f"          ошибка: {e}")
        if tg and tg.enabled and not res["dry_run"] and (res["strip"] or res["drop"]):
            tg.send(f"Уборка диска\n\n{res['line']}\n"
                    f"срок хранения: {describe(R.policy(c))}")
        if not args.loop:
            return 1 if res["errors"] else 0
        time.sleep(max(R.EVERY_MIN, every))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
