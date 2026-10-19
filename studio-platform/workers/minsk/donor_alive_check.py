#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка доноров плана на «жив ли ролик на YouTube» (18.10.2026).

ЗАПУСКАЕТСЯ НА VPS. Володя заменил часть доноров, удалённых с YouTube; надо увидеть, не
осталось ли мёртвых в будущих строках плана — по такому донору выпуск неизбежно встанет,
потому что скачать субтитры и звук неоткуда.

Как проверяем. Публичная ручка oEmbed: 200 — ролик есть, 404 — удалён или приватный,
400 — номер кривой. Ключей и авторизации не требует, тела не читаем — только код ответа.
Запросы идут в несколько потоков с паузой, чтобы не злить YouTube.

Что печатает: по каждому мёртвому донору — строку плана, канал, дату выхода и номер ролика,
плюс итог по видам. Ничего не меняет: --apply здесь нет намеренно, удаление строк плана —
отдельное решение владельца.

    python3 donor_alive_check.py                 # все будущие и активные строки плана
    python3 donor_alive_check.py --all           # вообще все строки с донором
    python3 donor_alive_check.py --workers 6
"""
import argparse
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DB = "/srv/studio/data/vsgen.db"
OEMBED = "https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={vid}&format=json"
ACTIVE = ("planned", "frozen", "running", "error")


def probe(vid: str) -> int:
    """Код ответа oEmbed. 0 — сеть не ответила (ролик не виним)."""
    req = urllib.request.Request(OEMBED.format(vid=vid), method="GET",
                                 headers={"User-Agent": "studio-donor-check/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            return exc.code
        except Exception:                                  # noqa: BLE001
            time.sleep(2 * (attempt + 1))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--workers", type=int, default=5)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    where = "" if args.all else (
        " AND pi.status IN (%s) AND pi.slot_at > strftime('%%s','now')-86400"
        % ",".join("'%s'" % s for s in ACTIVE))
    rows = conn.execute(
        "SELECT pi.id, pi.donor_video_id vid, pi.donor_title title, pi.status, "
        "date(pi.slot_at,'unixepoch') slot, c.name channel, p.serial profile "
        "FROM pub_plan_items pi JOIN pub_channels c ON c.id=pi.channel_id "
        "LEFT JOIN pub_profiles p ON p.id=c.profile_id "
        "WHERE COALESCE(pi.donor_video_id,'')<>''" + where +
        " ORDER BY pi.slot_at").fetchall()
    conn.close()

    by_vid = {}
    for r in rows:
        by_vid.setdefault(r["vid"], []).append(r)
    print(f"строк плана с донором: {len(rows)}, разных роликов: {len(by_vid)}")

    codes = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for vid, code in zip(by_vid, pool.map(probe, list(by_vid))):
            codes[vid] = code

    dead, broken, unknown = [], [], []
    for vid, code in codes.items():
        if code == 200:
            continue
        (dead if code in (401, 403, 404) else broken if code == 400 else unknown).append(vid)

    def show(title, vids, note):
        if not vids:
            return
        items = sum(len(by_vid[v]) for v in vids)
        print(f"\n{title}: роликов {len(vids)}, строк плана {items} — {note}")
        for v in sorted(vids, key=lambda x: by_vid[x][0]["slot"] or ""):
            for r in by_vid[v]:
                print(f"  строка {r['id']:>5} · профиль {r['profile'] or '-':>3} · {r['slot']} · "
                      f"{(r['channel'] or '')[:26]:26} · {v} · {(r['title'] or '')[:40]}")

    show("УДАЛЕНЫ или приватные", dead, "выпуск по такому донору встанет: субтитры и звук неоткуда брать")
    show("КРИВОЙ номер ролика", broken, "в строке плана не похожий на YouTube номер")
    show("НЕ ПРОВЕРЕНЫ", unknown, "сеть не ответила — проверить ещё раз, ролик не виним")
    alive = sum(1 for c in codes.values() if c == 200)
    print(f"\nитог: живых {alive}, удалённых {len(dead)}, кривых {len(broken)}, "
          f"непроверенных {len(unknown)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
