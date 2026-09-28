#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Подробная статистика канала: токен, сбор, уточнение и главная ловушка (28.09.2026).

Владелец: «на каждом канале разный адрес… сделать инпут для ключа для этого канала и
подтягивать стату». Каждый канал живёт на своём гугл-аккаунте, значит согласий столько
же, сколько каналов, и вставляет их человек руками, сотню раз подряд.

Отсюда всё, что здесь проверяется:

  • ЧУЖОЙ ТОКЕН НЕ ДОЛЖЕН ДАВАТЬ ЧИСЛА. Спрашиваем всегда `channel==UC…`, а не «канал
    этого токена»: со вторым Google молча ответил бы статистикой чужого канала, и она
    легла бы в карточку этого. Такую ошибку на экране не отличить от правды.
  • ТОКЕН — СЕКРЕТ. Наружу не отдаётся никогда: ни в состоянии, ни в отказе. Файл 0600,
    каталог 0700.
  • «ОТОЗВАЛИ» ≠ «СЕТЬ ЛЕГЛА». Первое чинит только человек, второе проходит само.
    Перепутав, мы либо молчим о сломанном канале, либо гоняем человека зря.
  • СВЕЖИЕ ДНИ ПЕРЕЗАПРАШИВАЮТСЯ: YouTube уточняет их несколько суток, и записанный
    один раз день остался бы черновиком навсегда.

Сеть не трогается: запросы подменены.

    .venv/bin/python tests/test_channel_analytics.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
import tempfile
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="channel-analytics-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
os.environ["VS_CHANNEL_TOKENS_DIR"] = os.path.join(TMP, "tokens")
os.environ["VS_YT_OAUTH_CLIENT_ID"] = "client-для-теста"
os.environ["VS_YT_OAUTH_CLIENT_SECRET"] = "secret-для-теста"
sys.path.insert(0, os.path.join(BASE, "server"))

from pub_schema import ensure_pub_schema                           # noqa: E402

_c = sqlite3.connect(DB)
ensure_pub_schema(_c)
_c.executescript("""
  CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
""")
_c.commit()
_c.close()

import channel_oauth as O                                          # noqa: E402
import pub_analytics as An                                         # noqa: E402

FAILS = []
DAY = 86400.0
T0 = 1790000000.0
TOKEN = "1//refresh-токен-для-теста-длинный-abcd"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def refused(label, fn, part):
    try:
        fn()
        check(label, "прошло без отказа", f"отказ: {part}")
    except Exception as exc:                                       # noqa: BLE001
        check(label, part in str(exc), True)


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def add_channel(name, uc, tenant="t1"):
    c = conn()
    try:
        cur = c.execute("INSERT INTO pub_channels(tenant_id,uc,name,created,updated) "
                        "VALUES(?,?,?,?,?)", (tenant, uc, name, T0, T0))
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


ASKED = []


def answer(rows_by_uc, forbid=()):
    """Подменённый YouTube: помнит, о чём спросили, и отвечает как настоящий."""
    def get(url, token):
        ASKED.append({"url": url, "token": token})
        uc = ""
        for part in url.split("?", 1)[1].split("&"):
            if part.startswith("ids="):
                uc = urllib.parse.unquote(part[4:]).split("==")[-1]
        if uc in forbid:
            raise urllib.error.HTTPError(
                url, 403, "Forbidden", {},
                _body('Forbidden: user is not the owner of the channel'))
        rows = [[day] + vals for day, vals in (rows_by_uc.get(uc) or [])]
        return {"rows": rows}
    return get


def _body(message):
    import io as _io
    return _io.BytesIO(json.dumps({"error": {"message": message}}).encode())


import urllib.parse                                                # noqa: E402


def token_answer(out=None, error=None):
    def post(url, fields):
        if error:
            raise urllib.error.HTTPError(url, 400, "Bad Request", {}, _body(error))
        return out or {"access_token": "рабочий-ключ", "expires_in": 3600}
    return post


def main() -> int:
    ours = add_channel("Наш канал", "UCnash")
    alien = add_channel("Соседний", "UCchuzhoy")

    print("Токен — секрет, а не поле в базе")
    O.set_token(ours, TOKEN)
    st = O.state(ours)
    check("подключено", st["connected"], True)
    check("виден только хвост", st["tail"], TOKEN[-4:])
    check("значения в состоянии нет", TOKEN in json.dumps(st, ensure_ascii=False), False)
    path = os.path.join(os.environ["VS_CHANNEL_TOKENS_DIR"], f"{ours}.json")
    check("файл на месте", os.path.exists(path), True)
    check("права файла 0600", stat.S_IMODE(os.stat(path).st_mode), 0o600)
    check("права каталога 0700",
          stat.S_IMODE(os.stat(os.environ["VS_CHANNEL_TOKENS_DIR"]).st_mode), 0o700)
    check("в базе токена нет",
          "refresh" in [r[1] for r in conn().execute("PRAGMA table_info(pub_channels)")],
          False)

    print("Мусор вместо токена не принимается")
    refused("слишком коротко", lambda: O.set_token(ours, "abc"), "не похоже на токен")
    refused("скопировали с переносом",
            lambda: O.set_token(ours, TOKEN + "\nещё строка"), "пробелов")
    check("прежний токен уцелел", O.state(ours)["tail"], TOKEN[-4:])

    print("Рабочий ключ берётся по токену и живёт час")
    O.forget_cache()
    key = O.access_token(ours, post=token_answer(), now=T0)
    check("ключ получен", key, "рабочий-ключ")
    calls = []
    def counting(url, fields):
        calls.append(1)
        return {"access_token": "второй", "expires_in": 3600}
    check("через минуту берётся из памяти, а не у Google",
          O.access_token(ours, post=counting, now=T0 + 60), "рабочий-ключ")
    check("к Google не ходили", calls, [])
    check("под конец часа обновляется",
          O.access_token(ours, post=counting, now=T0 + 3500), "второй")

    print("«Согласие отозвано» и «сеть легла» — разные вещи")
    O.forget_cache()
    refused("отозванное названо своими словами",
            lambda: O.access_token(ours, post=token_answer(error="invalid_grant"), now=T0),
            "больше не принимает это согласие")
    check("и запомнено как мёртвое", O.state(ours)["state"], "dead")
    O.set_token(ours, TOKEN)            # человек выдал новый — состояние сброшено
    check("после замены снова новое", O.state(ours)["state"], "new")
    O.forget_cache()
    refused("сетевой сбой — другой текст",
            lambda: O.access_token(ours, post=token_answer(error="backend error"), now=T0),
            "Google не выдал доступ")
    check("и это ещё не приговор", O.state(ours)["state"], "warn")

    print("Спрашиваем ИМЕННО этот канал, а не «канал токена»")
    ASKED.clear()
    O.forget_cache()
    O.set_token(ours, TOKEN)
    # Даты — внутри окна, которое запрашивает модуль: «сегодня» теста приходится на
    # 2026-09-21, а спрашиваем мы по позавчера включительно.
    rows = {"UCnash": [("2026-09-18", [100, 50.0, 300.0, 40.0, 7, 1, 9, 2, 1]),
                       ("2026-09-19", [120, 60.0, 310.0, 41.0, 8, 0, 11, 3, 2])]}
    got = An.collect_channel(conn(), ours, "UCnash", now=T0, get=answer(rows),
                             token="рабочий-ключ")
    check("дни записаны", got["days"], 2)
    url = ASKED[-1]["url"]
    check("канал назван по имени", "channel%3D%3DUCnash" in url, True)
    check("«мой канал» не спрашиваем", "MINE" in url, False)
    check("ключ ушёл заголовком, а не в адресе", TOKEN in url, False)

    print("Чужой токен получает отказ, а не чужие числа")
    O.set_token(alien, TOKEN)
    got = An.collect_channel(conn(), alien, "UCchuzhoy", now=T0,
                             get=answer(rows, forbid=("UCchuzhoy",)),
                             token="рабочий-ключ")
    check("ничего не записалось", got["days"], 0)
    check("сказано, что токен, вероятно, от другого канала",
          "от другого канала" in got["why"], True)
    c = conn()
    An.ensure_analytics_schema(c)
    check("в базе чужого канала пусто",
          c.execute("SELECT COUNT(*) FROM pub_channel_analytics WHERE channel_id=?",
                    (alien,)).fetchone()[0], 0)
    c.close()

    print("Числа легли по колонкам, а не вперемешку")
    c = conn()
    row = c.execute("SELECT * FROM pub_channel_analytics WHERE channel_id=? AND day=?",
                    (ours, "2026-09-19")).fetchone()
    c.close()
    check("просмотры", row["views"], 120)
    check("минуты просмотра", row["minutes"], 60.0)
    check("средняя длительность", row["avg_sec"], 310.0)
    check("досмотренность", row["avg_pct"], 41.0)
    check("подписались", row["subs_gained"], 8)
    check("отписались", row["subs_lost"], 0)
    check("лайки", row["likes"], 11)
    check("комментарии", row["comments"], 3)
    check("показов ещё нет — это не ноль", row["impressions"], None)

    print("Первый заход берёт год прошлого")
    fresh = add_channel("Свежий", "UCfresh")
    O.set_token(fresh, TOKEN)
    ASKED.clear()
    An.collect_channel(conn(), fresh, "UCfresh", now=T0,
                       get=answer({"UCfresh": []}), token="рабочий-ключ")
    url = ASKED[-1]["url"]
    check("начало — год назад", "startDate=2025-09-19" in url, True)
    check("конец — не вчера, а позавчера", "endDate=2026-09-19" in url, True)

    print("Следующий проход уточняет последнюю неделю")
    ASKED.clear()
    An.collect_channel(conn(), ours, "UCnash", now=T0 + DAY, get=answer(rows),
                       token="рабочий-ключ")
    url = ASKED[-1]["url"]
    check("отступили на неделю от последнего записанного",
          "startDate=2026-09-12" in url, True)
    check("а не запросили год заново", "startDate=2025" in url, False)

    print("Уточнённый день переписывается, а показы — уцелевают")
    c = conn()
    c.execute("UPDATE pub_channel_analytics SET impressions=5000, ctr=4.5 "
              "WHERE channel_id=? AND day=?", (ours, "2026-09-19"))
    c.commit()
    c.close()
    better = {"UCnash": [("2026-09-19", [131, 66.0, 315.0, 42.0, 8, 0, 12, 3, 2])]}
    An.collect_channel(conn(), ours, "UCnash", now=T0 + DAY, get=answer(better),
                       token="рабочий-ключ")
    c = conn()
    row = c.execute("SELECT * FROM pub_channel_analytics WHERE channel_id=? AND day=?",
                    (ours, "2026-09-19")).fetchone()
    c.close()
    check("просмотры уточнились", row["views"], 131)
    check("показы не стёрлись", row["impressions"], 5000)
    check("и CTR тоже", row["ctr"], 4.5)

    print("Проверка токена при вставке")
    O.forget_cache()
    out = An.verify(ours, "UCnash", now=T0, get=answer(rows), token="рабочий-ключ")
    check("свой токен принят", out["ok"], True)
    out = An.verify(alien, "UCchuzhoy", now=T0, token="рабочий-ключ",
                    get=answer(rows, forbid=("UCchuzhoy",)))
    check("чужой отвергнут сразу", out["ok"], False)
    check("с объяснением", "от другого канала" in out["why"], True)

    print("Итоги сравниваются с прошлым таким же отрезком")
    c = conn()
    An.ensure_analytics_schema(c)
    for n in range(56):
        day = (__import__("datetime").date(2026, 9, 19)
               - __import__("datetime").timedelta(days=n)).isoformat()
        # Свежая половина вдвое лучше прошлой — это и должно быть видно.
        views = 200 if n < 28 else 100
        c.execute("INSERT OR REPLACE INTO pub_channel_analytics"
                  "(channel_id,day,views,minutes,avg_sec,avg_pct,subs_gained,subs_lost,"
                  "likes,comments,shares,fetched) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (ours, day, views, views / 2, 300.0, 40.0, 5, 1, 3, 1, 0, T0))
    c.commit()
    box = An.totals(c, ours, days=28, now=T0)
    c.close()
    check("за свежие 28 дней", box["now"]["views"], 200 * 28)
    check("за предыдущие 28", box["was"]["views"], 100 * 28)
    check("отрезок назван", box["now"]["to"], "2026-09-19")
    check("и предыдущий тоже", box["was"]["to"], "2026-08-22")

    print("Канал без токена молча пропускается, а не падает")
    add_channel("Неподключённый", "UCnotoken")
    out = An.collect(conn(), "t1", now=T0, get=answer(rows))
    check("пропущенные посчитаны", out["skipped"] >= 1, True)

    print("Снимки уходят вместе с каналом")
    c = conn()
    An.forget_channel(c, ours)
    left = c.execute("SELECT COUNT(*) FROM pub_channel_analytics WHERE channel_id=?",
                     (ours,)).fetchone()[0]
    c.commit()
    c.close()
    check("история удалена", left, 0)
    O.drop(ours)
    check("и токен тоже", O.state(ours)["connected"], False)
    check("файла не осталось", os.path.exists(path), False)

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
