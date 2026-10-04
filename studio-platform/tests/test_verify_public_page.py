#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Выход ролика подтверждается без ключа — по публичной странице (04.10.2026).

Решение владельца: ключ YouTube Data API не нужен. До этого новая студия не подтвердила
выход ни одного ролика: проверка без ключа честно отказывалась работать, а маршрут
передачи считает канал принятым только при подтверждённом фактическом выходе.

Способ тот же, что давно работает у старого кабинета (published_check.py, 432 из 434):
публичный oEmbed отвечает 200 на ролик, который может посмотреть любой; иначе страница
ролика своими словами говорит почему. Тяжёлую страницу открываем только когда терпение
кончилось — до срока «не виден» означает обычное ожидание.

Сети нет: транспорт подменён.

    .venv/bin/python tests/test_verify_public_page.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))
os.environ.pop("VS_YT_API_KEY", None)

import pub_digest as D                                             # noqa: E402
import pub_verify as V                                             # noqa: E402

NOW = 1_800_000_000.0
HOUR = 3600.0
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def fresh(slot=NOW - 3 * HOUR, video_id="vidP"):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
      CREATE TABLE pub_releases(id INTEGER PRIMARY KEY, release_id TEXT, tenant_id TEXT,
                                plan_item_id INT, slot_at REAL, phase TEXT, status TEXT,
                                video_id TEXT, blocked_reason TEXT, updated REAL,
                                channel_id TEXT);
      CREATE TABLE pub_release_events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                                release_id TEXT, seq INTEGER, phase TEXT, video_id TEXT,
                                payload TEXT, created REAL,
                                repeats INTEGER NOT NULL DEFAULT 1, last_seen REAL,
                                UNIQUE(release_id,seq));
      CREATE TABLE pub_release_log(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT,
                                ts REAL, level TEXT, step TEXT, message TEXT,
                                repeats INTEGER NOT NULL DEFAULT 1, last_ts REAL);
      CREATE TABLE pub_channels(uc TEXT PRIMARY KEY, name TEXT, lang TEXT);
    """)
    c.execute("INSERT INTO pub_channels VALUES('UC_1','Nocne Biografie','pl')")
    c.execute("INSERT INTO pub_releases VALUES(1,'r1','dev',1,?,'scheduled','done',?,NULL,?,'UC_1')",
              (slot, video_id, NOW))
    c.commit()
    return c


class Page:
    """Транспорт YouTube под запись: что спросили и что ответили."""

    def __init__(self, oembed, watch=(200, "")):
        self.oembed, self.watch, self.urls = oembed, watch, []

    def __call__(self, url):
        self.urls.append(url)
        if "/oembed" in url:
            return self.oembed
        return self.watch


TOO_LONG = ('..."playabilityStatus":{"status":"ERROR","reason":"Video unavailable",'
            '"errorScreen":{"playerErrorMessageRenderer":{"subreason":{}},'
            '"description":{"content":"This video was removed because it was too long"}}}...')


def row(c):
    return c.execute("SELECT phase, blocked_reason FROM pub_releases WHERE id=1").fetchone()


def main() -> int:
    print("Ролик публичный — выход подтверждён без ключа")
    c = fresh()
    page = Page((200, '{"title": "Nocne Biografie: Kopernik"}'))
    out = V.run(c, "dev", NOW, key="", page=page)
    check("проверен", out["checked"], 1)
    check("вышел", out["published"], 1)
    check("путь — страница", out["via"], V.VIA_PAGE)
    check("фаза повышена до «вышел»", row(c)["phase"], "published")
    check("спросили только oEmbed, тяжёлую страницу не открывали",
          [u.split("?")[0] for u in page.urls], [V.OEMBED])
    check("в oEmbed ушёл номер ролика", "vidP" in page.urls[0], True)
    check("квоту API не тратили", out["quota"].get("used", 0), 0)

    print("\nСлот прошёл недавно, ролика не видно — ждём, страницу не открываем")
    c = fresh(slot=NOW - 2 * HOUR)
    page = Page((401, ""))
    out = V.run(c, "dev", NOW, key="", page=page)
    check("ждём", out["waiting"], 1)
    check("фаза не тронута", row(c)["phase"], "scheduled")
    check("причина не поставлена", row(c)["blocked_reason"], None)
    check("тяжёлую страницу не открывали", len(page.urls), 1)

    print("\nТерпение кончилось — страница говорит почему")
    c = fresh(slot=NOW - V.GIVE_UP_SEC - HOUR)
    page = Page((404, ""), watch=(200, TOO_LONG))
    out = V.run(c, "dev", NOW, key="", page=page)
    check("помечен к разбору", out["stale"], 1)
    reason = row(c)["blocked_reason"] or ""
    check("причина — слова YouTube", "This video was removed because it was too long" in reason, True)
    check("и что делать", "телефоном" in reason, True)
    check("фаза не повышена", row(c)["phase"], "scheduled")
    ev = c.execute("SELECT payload FROM pub_release_events WHERE phase='public_check'").fetchall()
    check("ответ проверки записан один раз и с причиной",
          len(ev) == 1 and "too long" in ev[0][0], True)
    checked = {"r1": __import__("json").loads(ev[0][0])}
    rel = c.execute("SELECT *, 0 AS hand_confirmed FROM pub_releases WHERE id=1").fetchone()
    kind, why = D._confirm_state(rel, checked, NOW, True, {})
    check("сводка называет задержку той же причиной", (kind, "too long" in why), ("delayed", True))

    print("\nYouTube не ответил — это не улика")
    c = fresh()
    out = V.run(c, "dev", NOW, key="", page=Page((None, "URLError")))
    check("вывода нет", out["unknown"], 1)
    check("фаза не тронута", row(c)["phase"], "scheduled")
    check("пометки нет", row(c)["blocked_reason"], None)

    print("\n5xx YouTube — тоже не улика; 400 — «не виден»")
    check("503 → вывода нет", V.check_page("x", get=lambda u: (503, ""))["state"], V.UNKNOWN)
    check("400 → не виден", V.check_page("x", get=lambda u: (400, ""))["state"], V.NOT_PUBLIC)

    print("\nПричина со страницы без блока playabilityStatus")
    check("приватный", V.page_reason("x", get=lambda u: (200, "<html>This video is private</html>")),
          "ролик приватный — отложка не сработала")
    check("страница не открылась", V.page_reason("x", get=lambda u: (None, "Timeout")).startswith(
        "страница ролика не открылась"), True)

    print("\nКлюч есть — путь прежний, через API")
    c = fresh()
    out = V.run(c, "dev", NOW, key="k", page=None,
                fetch=lambda url: {"items": [{"status": {"privacyStatus": "public"},
                                              "snippet": {}}]})
    check("путь — ключ", out["via"], V.VIA_KEY)
    check("вышел", out["published"], 1)

    print("\nСводке есть чем подтверждать")
    c = fresh()
    check("без ключа, со страницей — можно", V.can_check(c), True)
    V.PAGE_CHECK, was = False, V.PAGE_CHECK
    check("страницу выключили — нечем", V.can_check(c), False)
    V.PAGE_CHECK = was

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
