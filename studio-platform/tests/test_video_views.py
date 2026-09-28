#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Просмотры по каждому нашему ролику (28.09.2026).

Владелец: «нам ещё интересна стата по каждому ролику, а именно просмотры».

Спрашиваем только СВОИ ролики — те, что студия залила и у которых знает идентификатор.
Перебирать все ролики канала значило бы платить за числа, которых никто не просил.

Что проверяется и почему:

  • ПАЧКАМИ. `videos.list` — одна единица за полсотни роликов, а не за ролик. Сто
    роликов запросом на каждый стоили бы сотню единиц из общего кошелька, из которого
    живёт проверка публикаций.
  • ОТДЕЛЬНЫЙ ПОТРЕБИТЕЛЬ. Расход виден строкой «videos», а не растворяется в чужой:
    иначе на вопрос «кто выел ключ» таблица ответит неправдой.
  • «НЕ СПРАШИВАЛИ» ≠ «НОЛЬ ПРОСМОТРОВ». У только что залитого ролика чисел ещё нет.
  • РЯД НЕ РАСТЁТ БЕСКОНЕЧНО. Дальше трёх месяцев остаётся по точке в неделю: сто
    каналов за год иначе дают миллионы строк ни за чем.

Сеть не трогается: запрос подменён.

    .venv/bin/python tests/test_video_views.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="video-views-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
os.environ["VS_YT_API_KEY"] = "ключ-для-теста"
sys.path.insert(0, os.path.join(BASE, "server"))

from pub_schema import ensure_pub_schema                           # noqa: E402

_c = sqlite3.connect(DB)
ensure_pub_schema(_c)
_c.executescript("""
  CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
""")
_c.commit()
_c.close()

import pub_calls                                                   # noqa: E402
import pub_stats as S                                              # noqa: E402

FAILS = []
DAY = 86400.0
T0 = 1790000000.0


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


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


def add_upload(channel_id, video_id, tenant="t1"):
    c = conn()
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(pub_upload_jobs)")}
        # Заполняем все обязательные колонки настоящей схемы, а не выдуманный минимум:
        # тест должен ложиться в ту же таблицу, что и бой.
        fields = {"release_id": f"rel-{video_id}", "tenant_id": tenant,
                  "channel_id": channel_id, "video_id": video_id,
                  "uc": f"UC{channel_id}", "channel_url": "https://youtube.com/",
                  "title": f"Ролик {video_id}", "video_path": "/tmp/x.mp4",
                  "slot_at": T0, "created": T0, "updated": T0}
        fields = {k: v for k, v in fields.items() if k in cols}
        names = ",".join(fields)
        marks = ",".join("?" * len(fields))
        c.execute(f"INSERT INTO pub_upload_jobs({names}) VALUES({marks})",
                  list(fields.values()))
        c.commit()
    finally:
        c.close()


ASKED = []


def answer(stats_by_id, missing=()):
    def fetch(url):
        ASKED.append(url)
        ids = []
        for part in url.split("?", 1)[1].split("&"):
            if part.startswith("id="):
                ids = urllib.parse.unquote(part[3:]).split(",")
        items = [{"id": v, "statistics": stats_by_id[v]}
                 for v in ids if v in stats_by_id and v not in missing]
        return {"items": items}
    return fetch


def main() -> int:
    ch = add_channel("Наш канал", "UCnash")
    other = add_channel("Второй", "UCvtoroy")

    print("Пачками, а не по ролику")
    ids = [f"vid{n:08d}" for n in range(120)]
    for v in ids:
        add_upload(ch, v)
    stats = {v: {"viewCount": str(1000 + n), "likeCount": str(n),
                 "commentCount": str(n // 2)} for n, v in enumerate(ids)}
    ASKED.clear()
    out = S.collect_videos(conn(), "t1", fetch=answer(stats), now=T0)
    check("сто двадцать роликов — три запроса", out["calls"], 3)
    check("и три единицы", out["units"], 3)
    check("записаны все", out["saved"], 120)
    check("адрес — именно videos", "youtube/v3/videos" in ASKED[-1], True)
    check("спрашиваем статистику", "part=statistics" in ASKED[-1], True)

    print("Расход записан отдельной строкой")
    usage = {x["consumer"]: x["units"] for x in pub_calls.quota_usage(conn(), now=T0)}
    check("потребитель «videos»", usage.get("videos"), 3)

    print("Числа доступны по идентификатору ролика")
    got = S.videos_of_channel(conn(), ch, now=T0)
    check("ролик нашёлся", got[ids[0]]["views"], 1000)
    check("лайки тоже", got[ids[5]]["likes"], 5)
    check("прироста ещё не знаем", got[ids[0]]["delta"], None)

    print("Неделю спустя виден прирост")
    grown = {v: dict(st, viewCount=str(int(st["viewCount"]) + 500))
             for v, st in stats.items()}
    S.collect_videos(conn(), "t1", fetch=answer(grown), now=T0 + 7 * DAY)
    got = S.videos_of_channel(conn(), ch, now=T0 + 7 * DAY)
    check("просмотры выросли", got[ids[0]]["views"], 1500)
    check("прирост посчитан", got[ids[0]]["delta"], 500)
    check("и сказано, с какого дня", got[ids[0]]["since"], S.day_of(T0))

    print("Ролик, которого ключ не видит")
    S.collect_videos(conn(), "t1", now=T0 + 8 * DAY,
                     fetch=answer(grown, missing=(ids[3],)))
    out2 = S.collect_videos(conn(), "t1", now=T0 + 8 * DAY,
                            fetch=answer(grown, missing=(ids[3],)))
    check("назван поимённо", ids[3] in out2["missing"], True)

    print("Чужой канал в ответе не мешается")
    add_upload(other, "vidOTHER")
    S.collect_videos(conn(), "t1", now=T0 + 9 * DAY, fetch=answer(
        dict(grown, vidOTHER={"viewCount": "77", "likeCount": "1", "commentCount": "0"})))
    got = S.videos_of_channel(conn(), ch, now=T0 + 9 * DAY)
    check("чужого ролика нет в списке нашего канала", "vidOTHER" in got, False)
    got_other = S.videos_of_channel(conn(), other, now=T0 + 9 * DAY)
    check("а у своего канала он есть", got_other["vidOTHER"]["views"], 77)

    print("Без ключа — честная причина, а не нули")
    saved = os.environ.pop("VS_YT_API_KEY")
    try:
        out3 = S.collect_videos(conn(), "t1", now=T0 + 10 * DAY, fetch=answer(grown))
        check("ничего не записали", out3["saved"], 0)
        check("сказано про ключ", "ключа YouTube Data API" in out3["why"], True)
    finally:
        os.environ["VS_YT_API_KEY"] = saved

    print("Кошелёк уважается")
    c = conn()
    pub_calls.quota_block(c, "тест: единицы кончились", now=T0 + 11 * DAY)
    c.commit()
    c.close()
    ASKED.clear()
    out4 = S.collect_videos(conn(), "t1", now=T0 + 11 * DAY, fetch=answer(grown))
    check("в сеть не пошли", ASKED, [])
    check("и сказали почему", "квота" in out4["why"], True)
    c = conn()
    c.execute("DELETE FROM admin_state WHERE k=?", (pub_calls.QUOTA_WAIT_KEY,))
    c.commit()
    c.close()

    print("Старые точки разрежаются до одной в неделю")
    c = conn()
    S.ensure_video_schema(c)
    # Полгода ежедневных точек по одному ролику.
    for n in range(180):
        c.execute("INSERT OR REPLACE INTO pub_video_stats"
                  "(video_id,day,channel_id,views,likes,comments,taken) "
                  "VALUES('old',?,?,?,0,0,?)",
                  (S._shift(S.day_of(T0), n), ch, 100 + n, T0))
    c.commit()
    before = c.execute("SELECT COUNT(*) FROM pub_video_stats WHERE video_id='old'"
                       ).fetchone()[0]
    c.close()
    check("точек накопилось", before, 180)
    S.collect_videos(conn(), "t1", now=T0, fetch=answer(grown))
    c = conn()
    dense = c.execute("SELECT COUNT(*) FROM pub_video_stats WHERE video_id='old' "
                      "AND day >= ?", (S._shift(S.day_of(T0), S.VIDEO_DENSE_DAYS),)
                      ).fetchone()[0]
    old_left = c.execute("SELECT COUNT(*) FROM pub_video_stats WHERE video_id='old' "
                         "AND day < ?", (S._shift(S.day_of(T0), S.VIDEO_DENSE_DAYS),)
                         ).fetchone()[0]
    c.close()
    check("свежие три месяца уцелели целиком", dense, S.VIDEO_DENSE_DAYS + 1)
    check("а старое разрежено до недель", old_left <= 15, True)
    check("но не стёрто вовсе", old_left > 0, True)

    print("Ролики уходят вместе с каналом")
    c = conn()
    S.forget_channel_videos(c, ch)
    left = c.execute("SELECT COUNT(*) FROM pub_video_stats WHERE channel_id=?",
                     (ch,)).fetchone()[0]
    c.commit()
    c.close()
    check("ряды удалены", left, 0)

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
