#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Наполнить справочник машин и профилей живым списком из AdsPower.

Разовый инструмент на время, пока опись профилей не переехала в сам пульт. Читает
список профилей у машины по её адресу AdsPower и складывает в `pub_hosts` /
`pub_profiles`. Ничего не удаляет: уже известные профили остаются, новые добавляются.

    /srv/studio/.venv/bin/python deploy/seed_profiles.py \
        --url http://100.64.72.68:50325 --code minsk --title "Минская машина" \
        --db /srv/studio/data/vsgen.db --tenant 3

КАТЕГОРИЧЕСКИ: `username` и `password` из ответа AdsPower не читаются и не хранятся —
это учётные данные Google-аккаунтов. Берём только номер профиля и его название.

Машина держит примерно один запрос в секунду на всех, и лимит общий с идущими
заливками, поэтому между страницами пауза, а страниц берём не больше разумного.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.request

MAX_PAGES = 20
PAGE_SIZE = 100
PAUSE_SEC = 1.1


def fetch(url: str) -> list:
    out = []
    for page in range(1, MAX_PAGES + 1):
        req = f"{url.rstrip('/')}/api/v1/user/list?page={page}&page_size={PAGE_SIZE}"
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read() or b"{}")
        if body.get("code") != 0:
            raise SystemExit(f"AdsPower отказал: {str(body.get('msg'))[:100]}")
        rows = (body.get("data") or {}).get("list") or []
        for p in rows:
            serial = str(p.get("serial_number") or "").strip()
            if serial:
                out.append((serial, (p.get("name") or p.get("remark") or "").strip()[:80]))
        if len(rows) < PAGE_SIZE:
            break
        time.sleep(PAUSE_SEC)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="опись профилей AdsPower")
    ap.add_argument("--url", required=True, help="адрес AdsPower машины")
    ap.add_argument("--code", required=True, help="короткое имя машины, например minsk")
    ap.add_argument("--title", default="", help="как называть машину человеку")
    ap.add_argument("--db", required=True)
    ap.add_argument("--tenant", required=True)
    args = ap.parse_args()

    profiles = fetch(args.url)
    if not profiles:
        # Пустой ответ — это сбой, а не «профилей нет»: затирать им справочник нельзя.
        print("машина вернула пустой список — справочник не трогаю")
        return 1

    c = sqlite3.connect(args.db)
    now = time.time()
    try:
        c.execute("INSERT OR IGNORE INTO pub_hosts(tenant_id,code,title,adspower_url,"
                  "status,created) VALUES(?,?,?,?,'active',?)",
                  (args.tenant, args.code, args.title or args.code, args.url, now))
        host_id = c.execute("SELECT id FROM pub_hosts WHERE tenant_id=? AND code=?",
                            (args.tenant, args.code)).fetchone()[0]
        added = 0
        for serial, title in profiles:
            cur = c.execute(
                "INSERT OR IGNORE INTO pub_profiles(tenant_id,serial,title,"
                "default_host_id,status,created) VALUES(?,?,?,?,'active',?)",
                (args.tenant, serial, title, host_id, now))
            added += cur.rowcount
        c.commit()
    finally:
        c.close()
    print(f"на машине профилей: {len(profiles)}, записано новых: {added}")
    print("первые:", ", ".join(f"{s} {t[:18]}" for s, t in profiles[:5]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
