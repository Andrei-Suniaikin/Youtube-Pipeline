#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Статистика каналов: снимки, прирост и цена вопроса (25.09.2026).

Владелец: «что нужно будет сделать, чтобы видеть стату по каналам… если это реально
делать уже сейчас, то на уровне канала го делать».

Что здесь проверяется и почему именно это.

  • ПАЧКАМИ. `channels.list` стоит одну единицу за полсотни каналов, а не за канал.
    Запрос на канал сделал бы сто двадцать шесть единиц вместо трёх — и однажды съел
    бы кошелёк, из которого живёт проверка публикаций.
  • РАСХОД ПО ФАКТУ. Единицы списываются после ответа и под своим именем: на вопрос
    «кто выел общий ключ» отвечает таблица, а не догадка.
  • «НЕ ЗНАЕМ» ≠ НОЛЬ. У канала, за которым мы не наблюдали неделю, прироста нет не
    потому, что его не было. Ноль на экране здесь — прямая ложь.
  • СНИМКИ УМИРАЮТ ВМЕСТЕ С КАНАЛОМ. Внешних ключей нет, номера каналов
    переиспользуются: осиротевший ряд однажды покажет чужой прирост как свой.

Сеть не трогается: запрос подменён.

    .venv/bin/python tests/test_channel_stats.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="channel-stats-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
os.environ["VS_YT_API_KEY"] = "ключ-для-теста"
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "web"))

from pub_schema import ensure_pub_schema                           # noqa: E402

_c = sqlite3.connect(DB)
ensure_pub_schema(_c)
_c.executescript("""
  CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
    role TEXT, tenant_id TEXT, disabled INTEGER DEFAULT 0);
  CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
""")
_c.commit()
_c.close()

import pub_calls                                                   # noqa: E402
import pub_stats as S                                              # noqa: E402

FAILS = []
DAY = 86400.0
T0 = 1790000000.0        # неподвижное «сейчас»: снимки должны ложиться по суткам


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
        cur = c.execute(
            "INSERT INTO pub_channels(tenant_id,uc,name,created,updated) "
            "VALUES(?,?,?,?,?)", (tenant, uc, name, T0, T0))
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


ASKED = []


def answer(stats_by_uc, missing=()):
    """Подменённый запрос: помнит, о чём спросили, и отвечает как YouTube."""
    def fetch(url):
        ASKED.append(url)
        ids = []
        for part in url.split("?", 1)[1].split("&"):
            if part.startswith("id="):
                import urllib.parse
                ids = urllib.parse.unquote(part[3:]).split(",")
        items = [{"id": uc, "statistics": stats_by_uc[uc]}
                 for uc in ids if uc in stats_by_uc and uc not in missing]
        return {"items": items}
    return fetch


def main() -> int:
    print("Пачками, а не по каналу")
    ids = [add_channel(f"Канал {n:03d}", f"UC{n:03d}") for n in range(120)]
    stats = {f"UC{n:03d}": {"subscriberCount": str(1000 + n),
                            "viewCount": str(100000 + n * 10),
                            "videoCount": str(50 + n)} for n in range(120)}
    ASKED.clear()
    out = S.collect(conn(), "t1", fetch=answer(stats), now=T0)
    check("сто двадцать каналов — три запроса", out["calls"], 3)
    check("и три единицы, а не сто двадцать", out["units"], 3)
    check("спрошены все", out["asked"], 120)
    check("записаны все", out["saved"], 120)
    check("в пачке не больше полусотни",
          max(u.count("UC") for u in ASKED) <= S.BATCH, True)

    print("Расход записан под своим именем")
    usage = {x["consumer"]: x["units"] for x in pub_calls.quota_usage(conn(), now=T0)}
    check("потребитель «stats»", usage.get("stats"), 3)

    print("Числа легли в снимок")
    state = S.overview(conn(), "t1", now=T0)
    first = [x for x in state["channels"] if x["id"] == ids[0]][0]
    check("подписчики", first["subs"], 1000)
    check("просмотры", first["views"], 100000)
    check("роликов", first["videos"], 50)
    check("сумма по портфелю", state["subs_total"], sum(1000 + n for n in range(120)))

    print("Прирост — только из наших же снимков")
    check("недели наблюдений ещё нет — «не знаем», а не ноль",
          first["delta"]["7"]["subs"], None)
    # Неделю спустя числа выросли.
    grown = {uc: dict(v, subscriberCount=str(int(v["subscriberCount"]) + 120))
             for uc, v in stats.items()}
    S.collect(conn(), "t1", fetch=answer(grown), now=T0 + 7 * DAY)
    state = S.overview(conn(), "t1", now=T0 + 7 * DAY)
    first = [x for x in state["channels"] if x["id"] == ids[0]][0]
    check("теперь прирост виден", first["delta"]["7"]["subs"], 120)
    check("и сказано, с какого дня", first["delta"]["7"]["since"], S.day_of(T0))
    check("за месяц наблюдений нет — «не знаем»", first["delta"]["28"]["subs"], None)

    print("Два снимка в одни сутки — одна точка")
    S.collect(conn(), "t1", fetch=answer(grown), now=T0 + 7 * DAY + 3600)
    row = conn().execute(
        "SELECT COUNT(*) FROM pub_channel_stats WHERE channel_id=? AND day=?",
        (ids[0], S.day_of(T0 + 7 * DAY))).fetchone()[0]
    check("точка одна", row, 1)

    print("Скрытые подписчики — «скрыто», а не ноль")
    hid = add_channel("Скрытый", "UChidden")
    S.collect(conn(), "t1", now=T0 + 8 * DAY, fetch=answer(
        {"UChidden": {"hiddenSubscriberCount": True, "subscriberCount": "0",
                      "viewCount": "777", "videoCount": "9"}}))
    state = S.overview(conn(), "t1", now=T0 + 8 * DAY)
    got = [x for x in state["channels"] if x["id"] == hid][0]
    check("подписчиков нет как числа", got["subs"], None)
    check("и это помечено", got["hidden"], True)
    check("просмотры при этом есть", got["views"], 777)

    print("Канал, которого YouTube не знает")
    bad = add_channel("Опечатка", "UCнетакого")
    out = S.collect(conn(), "t1", now=T0 + 9 * DAY,
                    fetch=answer({}, missing=("UCнетакого",)))
    named = [m["id"] for m in out["missing"]]
    check("назван поимённо", bad in named, True)

    print("Канал без UC — не спрашиваем, но и не прячем")
    add_channel("Без номера", "")
    out = S.collect(conn(), "t1", now=T0 + 10 * DAY, fetch=answer(stats))
    check("посчитан отдельно", out["no_uc"], 1)

    print("Кошелёк уважается")
    c = conn()
    pub_calls.quota_block(c, "тест: единицы кончились", now=T0 + 11 * DAY)
    c.commit()
    ASKED.clear()
    out = S.collect(conn(), "t1", now=T0 + 11 * DAY, fetch=answer(stats))
    check("в сеть не пошли", ASKED, [])
    check("и сказали почему", "квота" in out["why"], True)
    c = conn()
    c.execute("DELETE FROM admin_state WHERE k=?", (pub_calls.QUOTA_WAIT_KEY,))
    c.commit()
    c.close()

    print("Отказ Google про квоту не выглядит поломкой каналов")
    def boom(url):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
    out = S.collect(conn(), "t1", now=T0 + 12 * DAY, fetch=boom)
    check("снимков не появилось", out["saved"], 0)
    check("причина названа", bool(out["why"]), True)

    print("Кнопка «обновить» не жжёт кошелёк")
    ASKED.clear()
    S.collect_if_stale(conn(), "t1", now=T0 + 14 * DAY, fetch=answer(stats))
    check("первое нажатие идёт за числами", bool(ASKED), True)
    ASKED.clear()
    out = S.collect_if_stale(conn(), "t1", now=T0 + 14 * DAY + 60, fetch=answer(stats))
    check("второе подряд — уже нет", ASKED, [])
    check("и объясняет, что снимок свежий", out.get("fresh"), True)
    ASKED.clear()
    S.collect_if_stale(conn(), "t1", now=T0 + 14 * DAY + S.FRESH_SEC + 1,
                       fetch=answer(stats))
    check("а когда снимок устарел — идёт снова", bool(ASKED), True)

    print("Без ключа — честная причина, а не нули")
    saved_key = os.environ.pop("VS_YT_API_KEY")
    try:
        out = S.collect(conn(), "t1", now=T0 + 13 * DAY, fetch=answer(stats))
        check("ничего не записали", out["saved"], 0)
        check("сказано про ключ", "ключа YouTube Data API" in out["why"], True)
        check("и экран это знает", S.overview(conn(), "t1")["key"], False)
    finally:
        os.environ["VS_YT_API_KEY"] = saved_key

    print("Снимки уходят вместе с каналом")
    c = conn()
    S.forget_channel(c, ids[0])
    c.commit()
    left = c.execute("SELECT COUNT(*) FROM pub_channel_stats WHERE channel_id=?",
                     (ids[0],)).fetchone()[0]
    c.close()
    check("ряд удалён", left, 0)

    print("Архивный канал снимается наравне с рабочим")
    # Пропуск оставил бы в ряду дыру ровно на время архива, а пачка стоит одну единицу
    # независимо от числа каналов в ней — экономить тут не на чем.
    arch = add_channel("В архиве", "UCarch")
    c = conn()
    c.execute("UPDATE pub_channels SET status='archived' WHERE id=?", (arch,))
    c.commit()
    c.close()
    S.collect(conn(), "t1", now=T0 + 15 * DAY,
              fetch=answer({"UCarch": {"subscriberCount": "5", "viewCount": "50",
                                       "videoCount": "1"}}))
    got = [x for x in S.overview(conn(), "t1", now=T0 + 15 * DAY)["channels"]
           if x["id"] == arch]
    check("снимок сделан", (got or [{}])[0].get("subs"), 5)

    print("Маршрут ведёт к статистике, а не к каналу с именем «stats»")
    # Этой ошибкой уже ломался `/api/v1/models` (22.09.2026): новый маршрут встал так,
    # что запрос попадал в соседнюю функцию. Проверяем не наличие кода, а разбор пути
    # самим приложением.
    import app as W
    from fastapi.routing import APIRoute
    hit = [r for r in W.app.routes
           if isinstance(r, APIRoute) and r.matches(
               {"type": "http", "method": "GET", "path": "/ui/channels/stats",
                "path_params": {}, "headers": [], "query_string": b"",
                "root_path": ""})[0].value == 2]
    check("путь ведёт в статистику", [r.name for r in hit][:1], ["ui_channels_stats"])
    got = W.ui_channels_stats()
    check("ответ — сводка по каналам", isinstance(got.get("channels"), list), True)
    check("и говорит про ключ", "key" in got, True)

    print("Чужой тенант не попадает в нашу сводку")
    other = add_channel("Соседский", "UCother", tenant="t2")
    state = S.overview(conn(), "t1", now=T0 + 7 * DAY)
    check("его в списке нет", other in [x["id"] for x in state["channels"]], False)

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
