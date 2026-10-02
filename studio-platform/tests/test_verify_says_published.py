#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проход проверки публикации действительно ГОВОРИТ, что ролики вышли (02.10.2026).

Владелец: «да, я хочу знать, что видосы готовы». До этой работы выход ролика не
сообщался никак: `pub_verify.run` поднимал `phase='published'` в базе и молчал. Знать
из базы — это не знать, тем более когда у человека из интернета грузится только телега.

Текст сообщения проверяется в `tests/test_verify_public.py`. Здесь проверяется ПРОВОДКА:
что `dev/verify_public.py --run` после прохода зовёт отправку, а не просто печатает
счётчики. Мутация «убрать вызов из скрипта» текстовые проверки не гасила вовсе — этот
файл закрывает ровно ту дыру.

Сеть не трогаем: `pub_verify.run` подменён, ключ подставной, телеграм выключен через
VS_ALERTS=0 (тогда скрипт печатает сообщение вместо отправки — это и ловим).

    .venv/bin/python tests/test_verify_says_published.py
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def база(путь: str) -> None:
    c = sqlite3.connect(путь)
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
    c.execute("INSERT INTO pub_channels VALUES('UC_1','Реванш Манхва','ru')")
    # Слот прошёл давно — значит спрашивать пора, и проход состоится.
    c.execute("INSERT INTO pub_releases VALUES(1,'r1','dev',1,1,'scheduled','done',"
              "'vid1',NULL,1,'UC_1')")
    c.commit()
    c.close()


def прогон(вышли) -> str:
    """Запускает настоящий main() скрипта с подменённым проходом. Отдаёт его вывод."""
    with tempfile.TemporaryDirectory() as tmp:
        путь = os.path.join(tmp, "t.db")
        база(путь)
        for p in ("server", "dev"):
            if os.path.join(BASE, p) not in sys.path:
                sys.path.insert(0, os.path.join(BASE, p))
        os.environ["VS_DEV_DB"] = путь
        os.environ["VS_DEV_TENANT"] = "dev"
        os.environ["VS_ALERTS"] = "0"
        for имя in list(sys.modules):
            if имя in ("verify_public", "pub_verify"):
                del sys.modules[имя]
        import pub_verify as V
        import verify_public as S
        S.DB = путь
        подменён = {"звали": 0}

        def поддельный_run(c, tenant, now, *, key=None, log=None, fetch=None):
            подменён["звали"] += 1
            return {"вышли": вышли, "checked": len(вышли), "published": len(вышли),
                    "unlisted": 0, "waiting": 0, "stale": 0, "unknown": 0, "skipped": 0,
                    "quota_wait": 0, "deferred": 0, "why": "",
                    "quota": {"day": "2026-10-02", "used": len(вышли), "budget": 10000}}
        V.run = поддельный_run
        S.V = V
        старый_argv, sys.argv = sys.argv, ["verify_public.py", "--run", "--key", "k"]
        поток, sys.stdout = sys.stdout, io.StringIO()
        try:
            S.main()
            напечатано = sys.stdout.getvalue()
        finally:
            sys.stdout = поток
            sys.argv = старый_argv
        assert подменён["звали"] == 1, "подменённый проход не вызвали"
        return напечатано


def main() -> int:
    print("Вышел ролик — скрипт про это говорит")
    вывод = прогон([{"release_id": "r1", "video_id": "vid1", "channel_id": "UC_1"}])
    check("сообщение построено", "Вышли на YouTube: 1 ролик" in вывод, True)
    check("канал назван", "Реванш Манхва (ru)" in вывод, True)
    check("ссылка на ролик есть", "youtu.be/vid1" in вывод, True)
    # VS_ALERTS=0 — копия базы на стенде не должна писать владельцу (правило с 21.09).
    check("и сказано, почему не отправлено", "VS_ALERTS=0" in вывод, True)

    print("\nВыходить некому — ни слова")
    тихо = прогон([])
    check("про выход не сказано", "Вышли на YouTube" in тихо, False)
    check("но проход отчитался как обычно", "квота за сутки" in тихо, True)

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
