#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Снимок публичной статистики каналов: подписчики, просмотры, ролики.

Запускается таймером раз в сутки. Смысл в регулярности, а не в свежести: YouTube
отдаёт только «сколько сейчас», прироста в его ответе нет, и «+120 за неделю»
существует ровно потому, что неделю назад мы записали своё число. Пропущенный день —
дыра в графике, которую уже ничем не закрыть.

    dev/stats.py            показать, что накоплено, никуда не ходя
    dev/stats.py --once     один проход со снимком
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

from pub_env import load_env                                      # noqa: E402

load_env()

import pub_stats                                                  # noqa: E402

DB = os.environ.get("VS_DEV_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
TENANT = os.environ.get("VS_DEV_TENANT") or None


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _num(value) -> str:
    return "—" if value is None else f"{int(value):,}".replace(",", " ")


def _delta(value) -> str:
    if value is None:
        return "—"
    return f"+{int(value):,}".replace(",", " ") if value > 0 else \
        f"{int(value):,}".replace(",", " ")


def show(c) -> None:
    state = pub_stats.overview(c, TENANT)
    if not state["key"]:
        print("ключа YouTube Data API нет: admin_state.youtube_api_key или VS_YT_API_KEY")
    print(f"каналов: {state['total']}, со снимком: {state['have']}, "
          f"без UC: {state['no_uc']}")
    q = state["quota"]
    print(f"квота за сутки {q['day']}: потрачено {q['used']} из {q['budget']}")
    for ch in state["channels"]:
        d7 = ch["delta"]["7"]
        print(f"  {ch['name'][:34]:<34} подписчиков {_num(ch['subs']):>10} "
              f"({_delta(d7['subs']):>7} за 7 дн) просмотров {_num(ch['views']):>13} "
              f"({_delta(d7['views']):>10}) {ch['day'] or 'снимка нет'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true", help="сделать снимок")
    args = ap.parse_args()

    c = conn()
    try:
        if args.once:
            out = pub_stats.collect(c, TENANT, log=lambda s: print(s))
            print(f"спрошено каналов: {out['asked']}, записано: {out['saved']}, "
                  f"единиц потрачено: {out['units']}")
            if out["no_uc"]:
                print(f"без UC (не спрашивали): {out['no_uc']}")
            for miss in out["missing"]:
                print(f"  YouTube не знает канала: {miss['name']} ({miss['uc']})")
            if out["why"]:
                print(f"причина остановки: {out['why']}")
        show(c)
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
