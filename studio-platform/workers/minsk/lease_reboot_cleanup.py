#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Снять брони профилей, переживших перезагрузку координатора (18.10.2026).

ЗАПУСКАЕТСЯ НА VPS, рядом с координатором /opt/video-ops/upload_profile_lease.py.

Что произошло 18.10. VPS перезагрузился в 11:55 UTC (обновление ядра после 80 дней
работы). Координатор броней считает срок по монотонным часам, а они при перезагрузке
обнуляются, поэтому для броней, взятых ДО неё, он применяет осознанное правило:

    # After a coordinator reboot, never reclaim an old lease until one full
    # maximum TTL has elapsed in the new boot.
    return monotonic_now < MAX_TTL_SECONDS          # 6 часов

Смысл правила верный: нельзя отдать профиль второму владельцу, пока жив первый. Но цена —
до шести часов простоя: в 14:30 UTC, через 2,7 часа после перезагрузки, семь профилей
(26, 27, 28, 34, 35, 63, 77) всё ещё числились занятыми, пять пуллеров стояли в очереди за
ними, а ни браузера, ни заливщика на этих профилях не было. Пришлось снимать руками.

Почему эта уборка безопасна. Живой владелец продлевает бронь каждые 120 секунд (renew), и
продление переписывает и clock_id на текущий, и настенный expires_at. Значит бронь, у
которой clock_id чужой И настенный срок истёк больше STALE_MARGIN секунд назад, не может
принадлежать живому владельцу: он бы её продлил. Инвариант «один владелец» не нарушается.

Уборка намеренно не трогает:
  • брони с текущим clock_id — ими занимается обычный срок;
  • брони, чей настенный срок ещё не истёк или истёк недавно;
  • любые брони в первые GRACE секунд после загрузки — даём владельцам шанс продлить.

    python3 lease_reboot_cleanup.py            # сухой прогон
    python3 lease_reboot_cleanup.py --apply    # снять
"""
import argparse
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

COORDINATOR = "/opt/video-ops/upload_profile_lease.py"
DB = "/var/lib/video-ops-profile-leases/profile-leases.sqlite3"
GRACE_SEC = 300          # столько ждём после загрузки, прежде чем что-то снимать
STALE_MARGIN_SEC = 900   # насколько настенный срок должен быть просрочен (renew идёт раз в 120 с)


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def uptime() -> float:
    try:
        return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    up = uptime()
    if up < GRACE_SEC:
        print(f"аптайм {up:.0f} с меньше {GRACE_SEC} — даю владельцам продлить, ничего не делаю")
        return 0
    current = boot_id()
    if not current:
        print("boot_id не прочитан — не рискую, ничего не делаю")
        return 0

    now = time.time()
    conn = sqlite3.connect(args.db, timeout=20)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT profile, owner_token, holder, clock_id, expires_at FROM profile_leases"
    ).fetchall()
    conn.close()

    stale = []
    for r in rows:
        age = now - float(r["expires_at"] or 0)
        same_boot = (r["clock_id"] or "") == current
        if same_boot:
            print(f"профиль {r['profile']}: бронь этой загрузки — не трогаю")
        elif age <= STALE_MARGIN_SEC:
            print(f"профиль {r['profile']}: срок истёк {age / 60:.0f} мин назад, "
                  f"жду до {STALE_MARGIN_SEC // 60} мин — не трогаю")
        else:
            stale.append(r)
            print(f"профиль {r['profile']}: бронь прошлой загрузки, срок истёк "
                  f"{age / 3600:.1f} ч назад, владелец не продлевал → снять")

    if not stale:
        print("снимать нечего")
        return 0
    if not args.apply:
        print(f"\nсухой прогон: снял бы {len(stale)}. Для записи — --apply")
        return 0

    freed = 0
    for r in stale:
        out = subprocess.run(
            [sys.executable, COORDINATOR, "--db", args.db, "release",
             "--profile", str(r["profile"]), "--owner-token", str(r["owner_token"]),
             "--holder", str(r["holder"])],
            capture_output=True, text=True, timeout=30)
        try:
            ok = json.loads((out.stdout or "").strip().splitlines()[0]).get("released") is True
        except (ValueError, IndexError):
            ok = False
        freed += bool(ok)
        print(f"профиль {r['profile']}: {'снята' if ok else 'НЕ снята: ' + (out.stderr or out.stdout or '')[:80]}")
    print(f"снято {freed} из {len(stale)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
