#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Поздняя проверка: вышли ли ролики, у которых расписание уже подтверждено.

Ничего не отменяет и не перезаливает — только повышает запись `scheduled` → `published`.

    dev/verify_public.py            что бы проверилось и что известно сейчас
    dev/verify_public.py --run      спросить YouTube и записать результат
    dev/verify_public.py --key ...  разовый ключ, не сохраняя его в базу
    dev/verify_public.py --save-key ...   положить ключ в admin_state

Ключ — YouTube Data API v3 (console.cloud.google.com → APIs → YouTube Data API v3 →
Credentials → API key). Квота бесплатная: `videos.list` стоит 1 единицу из 10 000
в сутки, то есть на 47 роликов в сутки её хватает с тысячекратным запасом.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

from pub_env import load_env, mask                                # noqa: E402

load_env()

import pub_verify as V                                            # noqa: E402

DB = os.environ.get("VS_DEV_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
TENANT = os.environ.get("VS_DEV_TENANT") or None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run", action="store_true", help="спросить YouTube и записать")
    p.add_argument("--key", help="ключ на один запуск")
    p.add_argument("--save-key", help="сохранить ключ в admin_state")
    p.add_argument("--grace", type=int, default=None,
                   help=f"через сколько секунд после слота спрашивать (по умолчанию {V.GRACE_SEC})")
    args = p.parse_args()

    if not os.path.exists(DB):
        raise SystemExit(f"нет базы {DB} — сначала: python3 dev/make_db.py")
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        if args.save_key:
            c.execute("INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES(?,?,?)",
                      (V.KEY_STATE, args.save_key.strip(), time.time()))
            c.commit()
            print(f"ключ сохранён: {mask(args.save_key)}")
            return 0

        key = args.key or V.api_key(c)
        print(f"ключ: {mask(key) if key else 'нет — проверка невозможна'}")
        now = time.time()
        rows = V.due(c, TENANT, now, args.grace)
        print(f"пора спросить: {len(rows)} (слот прошёл больше "
              f"{(V.GRACE_SEC if args.grace is None else args.grace) // 60} мин назад)")
        for r in rows:
            waited = (now - float(r["slot_at"] or now)) / 3600
            print(f"  {r['release_id']:<24} {r['video_id']:<14} слот прошёл {waited:.1f} ч назад")
        if not args.run:
            if rows:
                print("\nэто только показ. Чтобы спросить YouTube: --run")
            return 0
        if not key:
            print("\nбез ключа спрашивать нечем: --save-key ... или VS_YT_API_KEY в .env")
            return 1
        print("\nспрашиваю YouTube:")
        out = V.run(c, TENANT, now, key=key, log=print)
        print(f"\nпроверено {out['checked']}: вышло {out['published']}, "
              f"только по ссылке {out['unlisted']}, ждём {out['waiting']}, "
              f"не подтверждено {out['stale']}, без ответа {out['unknown']}, "
              f"пропущено как недавно спрошенное {out['skipped']}")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    raise SystemExit(main())
