#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Раскладка пачки ссылок по каналам — сервер (владелец 30.09.2026).

«Сделать чекбокс, что завести много на много каналов — и тогда открывается попап, где
делаешь контрол-в, и он создаёт кучу карточек со вставленными ссылками и названиями
видео в скобочках в заголовке карточки; и тогда просто справа в дропдауне по названию
канал выбираешь».

Что здесь проверяется и почему именно это:

  • ПОДСКАЗКА НИКОГДА НЕ СТАВИТ САМА. Ролик, ушедший на чужой канал, необратим — он уже
    у YouTube. Поэтому в ответе раскладки поля «выбранный канал» нет вовсе, а подсказка
    лежит отдельно; и поэтому же подсказка без уверенности — это пустота и сказанная
    словами причина, а не похожий канал наугад.
  • ДУБЛЬ ПОМЕЧЕН, НО НЕ ВЫБРОШЕН. Один донор иногда нарочно идёт на два языка. Ищется
    он по идентификатору ролика, а не по строке ссылки: `youtu.be/X` и `watch?v=X&t=30`
    — один ролик, и сравнение строк объявило бы их разными.
  • «НАЗВАНИЯ НЕТ» ≠ ПУСТОТА. Строка без названия обязана сказать, что название не
    добыли: по заголовку карточки человек решает, тот ли это ролик.
  • СТРОКА БЕЗ КАНАЛА ЖДЁТ. Не ставится никуда и ни при каких условиях.
  • ЧАСТИЧНЫЙ ОТКАЗ НЕ ОТМЕНЯЕТ УДАВШЕЕСЯ. Сотня ссылок в полтораста каналов: общая
    транзакция откатила бы уже поставленное, и человек не знал бы, что встало.
  • ЗА НАЗВАНИЯМИ ХОДИМ ОДИН РАЗ. Второй показ той же пачки в сеть не идёт: названия
    лежат в общем кэше, откуда их возьмёт и проверка донора, и сам выпуск.

    .venv/bin/python tests/test_links_spread.py
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="links-spread-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "web"))

from pub_schema import ensure_pub_schema                           # noqa: E402

_c = sqlite3.connect(DB)
ensure_pub_schema(_c)
_c.executescript("""
  CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE,
    token TEXT, role TEXT, created TEXT, password_hash TEXT, daily_quota INTEGER,
    tenant_id TEXT, disabled INTEGER DEFAULT 0, keys TEXT);
  CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, user_id INTEGER, expires REAL);
  CREATE TABLE IF NOT EXISTS tenants(id TEXT PRIMARY KEY, name TEXT, created TEXT,
    is_active INTEGER, daily_quota INTEGER);
  CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT);
""")
_c.commit()
_c.close()

import link_spread                                                 # noqa: E402
import app as W                                                    # noqa: E402
from fastapi import HTTPException                                  # noqa: E402

TENANT = "t1"
NOW = 1_800_000_000.0
FAILS = []

# Ссылки одного и того же ролика в разных написаниях — на них держится проверка дубля.
V1 = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
V1_SHORT = "https://youtu.be/aaaaaaaaaaa?si=Zz_1-x"
V2 = "https://www.youtube.com/watch?v=bbbbbbbbbbb"
V3 = "https://www.youtube.com/watch?v=ccccccccccc"
V4 = "https://www.youtube.com/watch?v=ddddddddddd"
V5 = "https://www.youtube.com/watch?v=eeeeeeeeeee"
V6 = "https://www.youtube.com/watch?v=fffffffffff"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def refused(label, fn, code, part=""):
    try:
        fn()
        check(label, "прошло без отказа", f"отказ {code}")
    except HTTPException as exc:
        check(label, (exc.status_code, part in str(exc.detail)), (code, True))
    except Exception as exc:                                       # noqa: BLE001
        # Падение — тоже не отказ. Без этой ветки поломка в разборе запроса роняла бы
        # весь тест трассировкой вместо того, чтобы назвать провалившуюся проверку.
        check(label, f"{type(exc).__name__}: {exc}"[:60], f"отказ {code}")


def safe(fn):
    """Значение или название поломки строкой.

    Проверка обязана НАЗВАТЬ провалившееся место, а не уронить весь прогон
    трассировкой: иначе непонятно, что именно сломалось и что ещё не проверено.
    """
    try:
        return fn()
    except Exception as exc:                                       # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"[:60]


def run(q, args=()):
    c = sqlite3.connect(DB)
    try:
        c.execute(q, args)
        c.commit()
    finally:
        c.close()


def sql(q, args=()):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(q, args)]
    finally:
        c.close()


def channel(name, lang, niche="", status="active", schedule='{"*":["10:00"]}',
            tenant=TENANT):
    run("INSERT INTO pub_channels(tenant_id,name,uc,lang,niche,status,tz,schedule,"
        "horizon_days,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (tenant, name, "UC" + name.ljust(22, "x")[:22], lang, niche, status,
         "Europe/Warsaw", schedule, 30, NOW, NOW))
    return sql("SELECT id FROM pub_channels WHERE name=? AND tenant_id=?",
               (name, tenant))[0]["id"]


# ───────────── сеть подменена: ни одного настоящего похода в oembed ─────────────

TITLES = {
    V1: "Nocna opowieść o Wrocławiu",          # польский: ł, ś
    V2: "Noční příběh z Prahy",   # чешский: ř, ě
    V3: "",                                    # названия не добыли
    V4: "Historia bitwy — dokument",           # без исключительных букв: язык не виден
    V5: "Ostatni transport",
    V6: "Wojenne opowieści",
}
CALLS = []
CACHE = {}


def fake_title(url):
    CALLS.append(url)
    return TITLES.get(url, "")


def fake_cache_title(vid):
    return CACHE.get(vid, ("", 0.0))


def fake_cache_put(vid, title, now=None):
    CACHE[vid] = (title, float(now or NOW))


W.donor_title = fake_title
W.donor_cache_title = fake_cache_title
W.donor_cache_put_title = fake_cache_put


def preview(links):
    return asyncio.run(W.ui_spread_preview(W.SpreadIn(links=links)))


def apply_rows(pairs):
    rows = [W.SpreadRowIn(url=u, channel_id=cid) for u, cid in pairs]
    return asyncio.run(W.ui_spread_apply(W.SpreadApplyIn(rows=rows)))


def row_of(out, url):
    for r in out["rows"]:
        if r["url"] == url:
            return r
    return {}


def main() -> int:
    pl = channel("Nocne Biografie", "pl", "биографии")
    cs = channel("Noční Portréty", "cs", "портреты")
    pl2 = channel("Front Wschodni", "pl", "история")
    es = channel("Amor en Viñetas", "es", "романтика", status="paused")
    arch = channel("Stary Kanał", "pl", "история", status="archived")
    channel("Чужой канал", "pl", "история", tenant="t2")

    print("Язык названия — только по исключительным буквам")
    check("польский по ł", link_spread.lang_of_title("Nocna opowieść o Wrocławiu"), "pl")
    check("чешский по ř/ě", link_spread.lang_of_title("Noční příběh z Prahy"), "cs")
    check("испанский по ñ", link_spread.lang_of_title("Amor en Viñetas"), "es")
    # «ó» есть и у поляков, и у чехов, и у испанцев — она не говорит ни о чём.
    check("общая буква языком не считается", link_spread.lang_of_title("Sóda"), "")
    check("без диакритики языка не видно",
          link_spread.lang_of_title("Historia bitwy"), "")
    check("две разные пометки — это «не знаем», а не две подсказки",
          link_spread.lang_of_title("Wrocław a Praha: příběh"), "")

    print("\nРазбор пачки: ссылки берутся тем же разбором, что и у карточки канала")
    out = preview(f"вот что нашёл:\n{V1}\nи ещё {V2} , потом\n{V3}\n{V4}\n{V1}")
    check("строк столько, сколько разных ссылок", len(out["rows"]), 4)
    check("порядок сохранён", [r["url"] for r in out["rows"]], [V1, V2, V3, V4])
    check("повтор в пачке схлопнут", [r["url"] for r in out["rows"]].count(V1), 1)
    check("текст вокруг ссылок не мешает", row_of(out, V1).get("video_id"), "aaaaaaaaaaa")

    print("\nНазвание ролика и честное «названия не добыли»")
    check("название добыто", row_of(out, V1).get("title"), TITLES[V1])
    check("и приписки к нему нет", row_of(out, V1).get("title_note"), "")
    check("названия нет — сказано словами", row_of(out, V3).get("title_note"),
          link_spread.TITLE_UNKNOWN)
    check("а само название осталось пустым", row_of(out, V3).get("title"), "")

    print("\nВ сеть за названием ходим один раз на ролик")
    before = len(CALLS)
    again = preview(f"{V1}\n{V2}")
    check("второй разбор той же пачки в сеть не пошёл", len(CALLS) - before, 0)
    check("а название всё равно показано", row_of(again, V1).get("title"), TITLES[V1])

    print("\nПодсказка канала: только при уверенности")
    check("единственный чешский канал подсказан",
          (row_of(out, V2).get("hint") or {}).get("channel_id"), cs)
    check("и сказано почему", "cs" in (row_of(out, V2).get("hint") or {}).get("why", ""), True)
    # Польских каналов два, ниша «биографии» в названии не упомянута — выбирать не нам.
    check("два канала на языке — подсказки нет",
          (row_of(out, V1).get("hint") or {}).get("channel_id"), 0)
    check("и это объяснено словами",
          "выберите сами" in (row_of(out, V1).get("hint") or {}).get("why", ""), True)
    check("язык не виден — подсказки нет",
          (row_of(out, V4).get("hint") or {}).get("channel_id"), 0)
    check("названия нет — подсказки тоже нет",
          (row_of(out, V3).get("hint") or {}).get("channel_id"), 0)

    print("\nПодсказка — это не выбор: выбранного канала в ответе нет вовсе")
    check("поля «канал» у строки нет", [k for k in row_of(out, V1) if k == "channel_id"],
          [])
    check("нечего ставить, пока человек не выбрал",
          safe(lambda: W.spread_urls([W.SpreadRowIn(url=V2, channel_id=None)])),
          [])
    refused("и запрос без единого выбора отклонён",
            lambda: apply_rows([(V2, None)]), 400, "не выбран канал")

    print("\nПодсказка ниже по языку и нише")
    chans = [{"id": pl, "name": "Nocne Biografie", "lang": "pl", "niche": "биографии",
              "status": "active", "tags": []},
             {"id": pl2, "name": "Front Wschodni", "lang": "pl", "niche": "dokument",
              "status": "active", "tags": []}]
    # Ниша сужает спор только тогда, когда её слово ВИДНО в названии ролика. Ниша
    # «биографии» по-русски в польском названии не встретится никогда — и подсказки по
    # ней не будет: выдумывать совпадение опаснее, чем промолчать.
    check("ниша, видная в названии, решает спор",
          link_spread.suggest("Historia bitwy — dokument", "pl", chans)["channel_id"],
          pl2)
    check("ниша, которой в названии нет, спор не решает",
          link_spread.suggest("Historia bitwy", "pl", chans)["channel_id"], 0)
    check("известный язык ролика сильнее названия",
          link_spread.suggest("Historia bitwy", "cs",
                              [{"id": cs, "name": "N", "lang": "cs", "niche": "",
                                "status": "active", "tags": []}])["channel_id"], cs)
    check("остановленный канал не подсказываем",
          link_spread.suggest("Amor en Viñetas", "",
                              [{"id": es, "name": "A", "lang": "es", "niche": "",
                                "status": "paused", "tags": []}])["channel_id"], 0)

    print("\nСписок каналов для выбора справа")
    names = [ch["name"] for ch in out["channels"]]
    check("архивный канал в списке не предлагается", "Stary Kanał" in names, False)
    check("остановленный — предлагается", "Amor en Viñetas" in names, True)
    check("чужой канал не виден", "Чужой канал" in names, False)
    check("список длиннее пяти — экран даст ему поиск сам", len(names) >= 4, True)

    print("\nПостановка: что встало на дату, что без даты")
    done = apply_rows([(V1, pl), (V2, cs)])
    check("обе строки поставлены", done["counts"]["placed"], 2)
    check("у каждой сказан канал",
          sorted(r["channel"] for r in done["rows"]),
          ["Nocne Biografie", "Noční Portréty"])
    check("дата названа", bool(row_of(done, V1).get("slot")), True)
    check("в плане две строки",
          sql("SELECT COUNT(*) n FROM pub_plan_items")[0]["n"], 2)
    check("название доехало до плана",
          sql("SELECT donor_title t FROM pub_plan_items WHERE donor_url=?",
              (V1,))[0]["t"], TITLES[V1])

    print("\nДубль ищется по ролику, а не по строке ссылки")
    seen = preview(f"{V1_SHORT}\n{V2}")
    dup = row_of(seen, V1_SHORT).get("duplicate")
    check("другое написание того же ролика найдено", bool(dup), True)
    check("и названо имя канала", (dup or {}).get("channel"), "Nocne Biografie")
    check("канал в пометке — тот самый", (dup or {}).get("channel_id"), pl)
    check("чужой ролик дублем не считается",
          row_of(preview(V4), V4).get("duplicate"), None)

    print("\nДубль помечен, но человек вправе поставить его руками")
    check("выбора за человека не сделано",
          [k for k in row_of(seen, V1_SHORT) if k == "channel_id"], [])
    hand = apply_rows([(V1_SHORT, pl2)])
    check("на другой канал тот же донор встаёт", hand["counts"]["placed"], 1)
    back = apply_rows([(V1_SHORT, pl)])
    check("а на тот же канал — нет", back["counts"]["skipped"], 1)
    check("и сказано почему", "уже стоит" in row_of(back, V1_SHORT).get("why"), True)

    print("\nБез расписания дата не назначается, и причина названа своя")
    # Разбор 30.09.2026: «в горизонте нет свободного выхода» и «расписания нет вовсе» —
    # РАЗНЫЕ вещи. Первое проходит само: выход освободится, ссылка встанет. Второе —
    # тупик: освобождаться нечему, и обещание «встанет, как только выход освободится»
    # прямая неправда. Пустое расписание — состояние каждого свежего канала.
    nosched = channel("Bez planu", "pl", "история", schedule="{}")
    quiet = apply_rows([(V3, nosched)])
    check("строка встала без даты", quiet["counts"]["queued"], 1)
    почему = row_of(quiet, V3).get("why") or ""
    check("сказано про незаданное расписание", "не задано расписание" in почему, True)
    check("и что из очереди она не выйдет", "не выйдет" in почему, True)
    check("автоматической постановки НЕ обещаем", "освободится" in почему, False)
    check("в плане она есть",
          sql("SELECT COUNT(*) n FROM pub_plan_items WHERE channel_id=?",
              (nosched,))[0]["n"], 1)

    print("\nОтказ по одной строке не отменяет остальные")
    mixed = apply_rows([(V4, pl), (V2, 999_999), (V3, cs)])
    check("две строки встали", mixed["counts"]["placed"], 2)
    check("одна отказана", mixed["counts"]["failed"], 1)
    check("отказ назван поимённо", row_of(mixed, V2).get("state"), "failed")
    check("и объяснён", "нет" in row_of(mixed, V2).get("why"), True)
    check("удавшееся записано, а не откачено",
          sql("SELECT COUNT(*) n FROM pub_plan_items WHERE donor_url=? AND channel_id=?",
              (V4, pl))[0]["n"], 1)
    check("чужой канал отвечает как несуществующий",
          apply_rows([(V4, channel("Ещё чужой", "pl", tenant="t2"))])["counts"]["failed"],
          1)

    print("\nПолная проверка доноров: та же машинерия, что у карточки канала")
    # Отдельной кнопкой и по требованию (решение владельца): она ходит через выходы
    # субтитров, и на сотне ссылок это минуты. Здесь берём не-YouTube ссылку — на ней
    # проверка отвечает не сходив в сеть, и тест остаётся без сети.
    not_yt = "https://vimeo.com/12345678901"
    seen_check = W.ui_spread_check(W.SpreadCheckIn(urls=[not_yt, not_yt]))
    check("повтор в пачке схлопнут и посчитан", seen_check["duplicates"], 1)
    check("строк столько, сколько разных ссылок", len(seen_check["donors"]), 1)
    check("не-YouTube ссылка не выдаётся за проверенную",
          seen_check["donors"][0]["verdict"], "reject")
    check("и сказано, что проверка денег не стоит",
          "бесплатна" in seen_check["donor_note"], True)

    print("\nСломанное расписание канала — строка отказа, а не падение всей пачки")
    # Расписание читается в момент постановки, и канал с испорченным расписанием роняет
    # `add_links`. Сотня ссылок не имеет права осыпаться из-за одного такого канала.
    broken = channel("Сломанное расписание", "pl", schedule='{"понедельник":["10:00"]}')
    rough = apply_rows([(V5, broken), (V6, pl)])
    check("сломанный канал отказал", row_of(rough, V5).get("state"), "failed")
    check("и причина названа", "понедельник" in row_of(rough, V5).get("why"), True)
    check("следующая строка всё равно встала", row_of(rough, V6).get("state"), "placed")
    check("и записана", sql("SELECT COUNT(*) n FROM pub_plan_items WHERE donor_url=?",
                            (V6,))[0]["n"], 1)

    print("\nГраницы запроса")
    refused("пустое поле отклонено", lambda: preview("   "), 400, "ни одной ссылки")
    many = "\n".join(f"https://youtu.be/{n:011d}" for n in range(link_spread.SPREAD_LIMIT
                                                                + 1))
    refused("пачка сверх предела отклонена", lambda: preview(many), 400, "не больше")
    refused("проверка доноров без ссылок отклонена",
            lambda: W.ui_spread_check(W.SpreadCheckIn(urls=[])), 400, "нечего проверять")

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
