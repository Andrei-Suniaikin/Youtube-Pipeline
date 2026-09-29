#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Запас материала в канале и тревога, когда он кончается (день 3 ТЗ, 29.09.2026).

Что здесь опасно и потому проверяется.

1. Запас, посчитанный неправильно, хуже отсутствия запаса. Число «десять ссылок» не
   отвечает на вопрос владельца «когда вставлять следующие»: десять ссылок, стоящих
   через одну, не спасают от пустого выхода уже завтра. Поэтому считается не число
   ссылок, а сколько ближайших выходов ПОДРЯД обеспечено — и это проверяется на трёх
   разных расписаниях (ежедневное, два раза в день, только по выходным).

2. Ссылки без даты уже в плане и ждут свободного выхода. Если не учесть их в запасе,
   тревога будет кричать на канале, где материала на неделю. Если учесть неправильно
   (не по самым ранним свободным выходам, а как попало) — запас разойдётся с тем, что
   через полминуты сделает движок.

3. «Задание не теряется за горизонтом расписания» — обещание ТЗ, и доказывается оно
   СДВИГОМ ВРЕМЕНИ, а не чтением кода: ссылка, которой не хватило свободной даты,
   обязана встать сама, когда дата освободилась. Проверяются три способа освободить
   дату: время ушло вперёд, строку отменили, канал сняли с паузы.

4. Ноль и «не знаем» — разные вещи. У канала без расписания запас не ноль: считать его
   не от чего. Такой канал не имеет права попасть ни в тревогу, ни в счёт «каналов без
   запаса», иначе владельца зовут чинить то, что не сломано.

5. Тревога обязана и появляться, и СНИМАТЬСЯ: повод, который не гаснет, через неделю
   перестают читать вместе со всеми остальными.

Сети здесь нет вовсе: названия доноров не спрашиваются (`titles={}`), база — в файле
во временном каталоге, отправка тревог заменена списком.

    .venv/bin/python tests/test_channel_supply.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="channel-supply-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "web"))

from channel_supply import channel_supply, supply_facts       # noqa: E402
from pub_schema import ensure_pub_schema                      # noqa: E402
import pub_alerts as A                                        # noqa: E402
import pub_engine as E                                        # noqa: E402
import pub_tags as T                                          # noqa: E402
import pub_ui as U                                            # noqa: E402

FAILS = []
# Опорный момент: среда, 8 утра по UTC. Час выбран до первого выхода канала, чтобы
# «сегодня» ещё считалось свободным — иначе часть проверок молча меняла бы смысл.
NOW = dt.datetime(2026, 10, 7, 8, 0, tzinfo=dt.timezone.utc).timestamp()


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def near(label, got, want, eps=0.02):
    if got is not None and abs(got - want) <= eps:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: ~{want}\n        получено:  {got!r}")
        FAILS.append(label)


def fresh() -> sqlite3.Connection:
    """Своя чистая база на каждую проверку: соседние строки плана путали бы счёт."""
    path = os.path.join(TMP, f"db{len(os.listdir(TMP))}.sqlite")
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    ensure_pub_schema(c)
    c.executescript("""
      CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
      CREATE TABLE IF NOT EXISTS queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT,
        kind TEXT, status TEXT, created REAL, finished REAL, project_id TEXT, error TEXT);
    """)
    c.commit()
    return c


def channel(c, schedule, horizon=3, tid="t1", name="Канал", uc=None, status="active"):
    uc = uc or ("UC" + str(abs(hash(name)) % 10 ** 22).rjust(22, "0"))
    cur = c.execute(
        "INSERT INTO pub_channels(tenant_id,uc,name,tz,schedule,horizon_days,status,"
        "created,updated) VALUES(?,?,?,'UTC',?,?,?,?,?)",
        (tid, uc, name, json.dumps(schedule), horizon, status, NOW, NOW))
    c.commit()
    return c.execute("SELECT * FROM pub_channels_live WHERE id=?", (cur.lastrowid,)).fetchone()


def links(c, ch, n, now=None, tid="t1"):
    urls = [f"https://youtu.be/{ch['id']}-{i:03d}" for i in range(n)]
    out = T.add_links(c, tid, ch, urls, NOW if now is None else now, titles={})
    c.commit()
    return out


def supply(c, ch, now=None, tid="t1"):
    now = NOW if now is None else now
    return channel_supply(ch, supply_facts(c, tid, now).get(ch["id"]), now)


def slots_of(c, cid):
    return [r["slot_at"] for r in c.execute(
        "SELECT slot_at FROM pub_plan_items WHERE channel_id=? ORDER BY id", (cid,))]


# ─────────────────────────── 1. расчёт запаса ───────────────────────────

def part_calc():
    print("Запас на разных расписаниях")

    c = fresh()
    # Ежедневно в 10:00, горизонт трое суток: три ссылки встанут, три останутся ждать.
    ch = channel(c, {"*": ["10:00"]}, horizon=3, name="ежедневный")
    links(c, ch, 6)
    s = supply(c, ch)
    check("ежедневный: ссылок в запасе", s["items"], 6)
    check("ежедневный: ждут свободного выхода", s["waiting"], 3)
    check("ежедневный: обеспечено выходов подряд", s["covered"], 6)
    # Шестой выход — 12.10 10:00, это 5 суток и 2 часа от 07.10 08:00.
    near("ежедневный: суток вперёд", s["days"], 5 + 2 / 24)
    check("ежедневный: знаем ответ", s["known"], True)
    check("ежедневный: словами", s["text"], "запас на 5 дней · 6 выходов")
    c.close()

    c = fresh()
    # Два выхода в день: те же шесть ссылок кончаются вдвое быстрее. Это и есть смысл
    # «в сутках, а не в штуках» — одно и то же число ссылок стоит разного времени.
    ch = channel(c, {"*": ["10:00", "18:00"]}, horizon=3, name="дважды в день")
    links(c, ch, 6)
    s = supply(c, ch)
    check("дважды в день: обеспечено выходов", s["covered"], 6)
    near("дважды в день: суток вперёд", s["days"], 2 + 10 / 24)
    check("дважды в день: короче, чем у ежедневного", s["days"] < 5, True)
    c.close()

    c = fresh()
    # Только по выходным: две ссылки закрывают почти неделю. Горизонт канала (3 суток)
    # на счёт запаса влиять не должен — он про расстановку, а не про подсчёт.
    ch = channel(c, {"sat": ["12:00"], "sun": ["12:00"]}, horizon=3, name="выходные")
    links(c, ch, 2)
    s = supply(c, ch)
    check("выходные: обеспечено выходов", s["covered"], 2)
    # 07.10.2026 — среда; ближайшие выходы 10.10 и 11.10 в 12:00, последний из них и
    # есть конец запаса: 4 суток и 4 часа от опорного момента.
    near("выходные: суток вперёд", s["days"], 4 + 4 / 24)
    c.close()

    print("Дыра в середине обрывает запас")
    c = fresh()
    ch = channel(c, {"*": ["10:00"]}, horizon=7, name="с дырой")
    links(c, ch, 3)                                    # 07, 08, 09 октября
    # Убираем средний выход: ссылок по-прежнему три, но выйти канал сможет только раз.
    c.execute("UPDATE pub_plan_items SET status='cancelled' WHERE id="
              "(SELECT id FROM pub_plan_items ORDER BY id LIMIT 1 OFFSET 1)")
    c.commit()
    s = supply(c, ch)
    check("с дырой: ссылок осталось", s["items"], 2)
    check("с дырой: обеспечен только ближайший выход", s["covered"], 1)
    near("с дырой: суток вперёд", s["days"], 2 / 24)
    c.close()

    print("Ссылки без даты закрывают дыру — как это сделает движок")
    c = fresh()
    ch = channel(c, {"*": ["10:00"]}, horizon=7, name="дыра и очередь")
    links(c, ch, 8)                       # 7 встанут по датам, 1 останется ждать
    c.execute("UPDATE pub_plan_items SET status='cancelled' WHERE id="
              "(SELECT id FROM pub_plan_items ORDER BY id LIMIT 1 OFFSET 1)")
    c.commit()
    s = supply(c, ch)
    check("очередь закрыла дыру", s["covered"], 7)
    check("ждёт ровно одна", s["waiting"], 1)
    # И тот же ответ должен дать сам движок, иначе показ разойдётся с делом.
    E.reflow_slots(c, NOW)
    c.commit()
    after = supply(c, ch)
    check("после тика ответ тот же", after["covered"], 7)
    check("после тика никто не ждёт", after["waiting"], 0)
    c.close()

    print("Ноль и «не знаем» — разные ответы")
    c = fresh()
    ch = channel(c, {}, name="без расписания")
    s = supply(c, ch)
    check("без расписания: не знаем", s["known"], False)
    check("без расписания: суток НЕ ноль", s["days"], None)
    check("без расписания: так и сказано", "расписание не задано" in s["text"], True)

    ch2 = channel(c, {"*": ["10:00"]}, name="пустой")
    s2 = supply(c, ch2)
    check("выходы есть, материала нет: знаем", s2["known"], True)
    check("и это именно ноль", s2["days"], 0.0)
    check("обеспеченных выходов ноль", s2["covered"], 0)
    check("сказано человеку", s2["text"], "материала нет — вставьте ссылки")

    ch3 = channel(c, {"*": ["10:00"]}, horizon=7, name="материал дальше")
    links(c, ch3, 1, now=NOW)
    c.execute("UPDATE pub_plan_items SET slot_at=slot_at+3*86400 WHERE channel_id=?",
              (ch3["id"],))
    c.commit()
    s3 = supply(c, ch3)
    check("материал стоит дальше: ссылка цела", s3["items"], 1)
    check("но ближайший выход пустой", s3["covered"], 0)
    check("и запас ноль, а не «есть ссылка»", s3["days"], 0.0)
    c.close()

    print("Уже вышедший ролик запасом не считается")
    # Вчерашний выход — это сделанная работа, а не запас на завтра. Если считать его
    # обеспеченным, канал с одной опубликованной ссылкой выглядел бы сытым вечно.
    c = fresh()
    ch = channel(c, {"*": ["10:00"]}, horizon=7, name="вчерашний")
    links(c, ch, 1)
    c.execute("UPDATE pub_plan_items SET slot_at=slot_at-2*86400, status='done' "
              "WHERE channel_id=?", (ch["id"],))
    c.commit()
    s = supply(c, ch)
    check("прошедший выход не в запасе", s["items"], 0)
    check("обеспеченных выходов нет", s["covered"], 0)
    check("и запас ноль", s["days"], 0.0)
    c.close()

    print("Кривое расписание — это «не знаем», а не «пусто»")
    c = fresh()
    ch = channel(c, {"*": ["10:00"]}, name="кривой")
    c.execute("UPDATE pub_channels SET schedule='{\"вторник\": [\"10:00\"]}' WHERE id=?",
              (ch["id"],))
    c.commit()
    ch = c.execute("SELECT * FROM pub_channels_live WHERE id=?", (ch["id"],)).fetchone()
    s = supply(c, ch)
    check("кривое расписание: не знаем", s["known"], False)
    check("и суток не выдумано", s["days"], None)
    c.close()

    print("Чужой арендатор в запас не попадает")
    c = fresh()
    mine = channel(c, {"*": ["10:00"]}, horizon=7, name="мой", tid="t1")
    alien = channel(c, {"*": ["10:00"]}, horizon=7, name="чужой", tid="t2")
    links(c, alien, 5, tid="t2")
    facts = supply_facts(c, "t1", NOW)
    check("чужие строки плана не видны", facts.get(alien["id"]), None)
    check("а свой канал пуст честно", supply(c, mine)["covered"], 0)
    c.close()


# ────────── 2. задание не теряется за горизонтом расписания ──────────

def part_reflow():
    print("Ссылка без даты встаёт, когда выход освободился")

    c = fresh()
    ch = channel(c, {"*": ["10:00"]}, horizon=2, name="горизонт двое суток")
    out = links(c, ch, 4)
    check("в горизонт влезли двое", len(out["placed"]), 2)
    check("две остались без даты", out["queued"], 2)
    check("но сохранены, а не выброшены", len(slots_of(c, ch["id"])), 4)

    # Сдвиг времени на трое суток: прежние выходы прошли, впереди новые свободные.
    later = NOW + 3 * 86400
    placed = E.reflow_slots(c, later)
    c.commit()
    check("тик раздал освободившиеся выходы", placed, 2)
    check("ни одной ссылки без даты не осталось",
          [s for s in slots_of(c, ch["id"]) if s is None], [])
    сроки = sorted(s for s in slots_of(c, ch["id"]) if s)
    check("новые выходы — в будущем", all(s > later for s in сроки[-2:]), True)
    check("и это разные выходы", len(set(сроки)), 4)
    c.close()

    print("Отменили строку — её выход достаётся ожидающей")
    c = fresh()
    ch = channel(c, {"*": ["10:00"]}, horizon=3, name="отмена")
    links(c, ch, 5)
    свободный = c.execute("SELECT slot_at FROM pub_plan_items WHERE channel_id=? "
                          "ORDER BY id LIMIT 1 OFFSET 1", (ch["id"],)).fetchone()[0]
    c.execute("UPDATE pub_plan_items SET status='cancelled' WHERE channel_id=? AND slot_at=?",
              (ch["id"], свободный))
    c.commit()
    check("отмена раздаёт выход", E.reflow_slots(c, NOW), 1)
    c.commit()
    занято = [r["id"] for r in c.execute(
        "SELECT id FROM pub_plan_items WHERE channel_id=? AND slot_at=? AND status='planned'",
        (ch["id"], свободный))]
    check("освободившийся выход занят ровно одной строкой", len(занято), 1)
    # Без `if` сломанный движок ронял бы тест в IndexError вместо строки FAIL, и
    # причину пришлось бы искать в трассировке, а не читать в выводе.
    check("и это бывшая безымянная очередь", занято[0] if занято else None, 4)
    c.close()

    print("Канал на паузе ждёт, включённый — забирает")
    c = fresh()
    ch = channel(c, {"*": ["10:00"]}, horizon=1, name="пауза")
    links(c, ch, 2)
    c.execute("UPDATE pub_channels SET status='paused' WHERE id=?", (ch["id"],))
    c.commit()
    check("на паузе выходы не раздаются", E.reflow_slots(c, NOW + 86400), 0)
    c.execute("UPDATE pub_channels SET status='active' WHERE id=?", (ch["id"],))
    c.commit()
    check("включили — ссылка встала", E.reflow_slots(c, NOW + 86400), 1)
    c.commit()
    check("и без даты не осталось никого",
          [s for s in slots_of(c, ch["id"]) if s is None], [])
    c.close()

    print("Расписание задали позже вставки — ссылка не пропала")
    c = fresh()
    ch = channel(c, {}, name="расписание потом")
    links(c, ch, 2)
    check("без расписания дат нет", slots_of(c, ch["id"]), [None, None])
    c.execute("UPDATE pub_channels SET schedule=? WHERE id=?",
              (json.dumps({"*": ["10:00"]}), ch["id"]))
    c.commit()
    check("расписание появилось — выходы розданы", E.reflow_slots(c, NOW), 2)
    c.commit()
    check("обе строки с датой", [s for s in slots_of(c, ch["id"]) if s is None], [])
    c.close()

    print("Два выхода подряд одной строке не достаются")
    c = fresh()
    ch = channel(c, {"*": ["10:00"]}, horizon=1, name="по одному")
    links(c, ch, 3)
    E.reflow_slots(c, NOW + 86400)
    c.commit()
    сроки = [s for s in slots_of(c, ch["id"]) if s]
    check("у каждой строки свой выход", len(сроки), len(set(сроки)))
    c.close()


# ─────────────────────────── 3. тревога ───────────────────────────

def part_alert():
    print("Тревога появляется и снимается")
    c = fresh()
    A.load_limits(c)
    A.SUPPLY_MIN_DAYS = 3.0
    ch = channel(c, {"*": ["10:00"]}, horizon=7, name="Дым отечества")
    links(c, ch, 1)                       # запаса — на два часа

    found = A.check_channel_supply(c, NOW, tenant="t1")
    check("повод найден", len(found), 1)
    check("ключ один на все каналы", found[0].key, "channel_supply")
    check("канал назван поимённо", "Дым отечества" in found[0].text, True)
    check("счёт каналов без запаса в тексте", "Каналов без запаса: 1" in found[0].text, True)
    check("порог назван", "порог — 3 суток" in found[0].text, True)
    check("про деньги сказано прямо", "Денег это не стоит" in found[0].money, True)

    links(c, ch, 7, now=NOW + 1)          # вставили пачку — запаса стало на неделю
    check("запас вырос", supply(c, ch)["covered"] >= 7, True)
    check("тревога снялась", A.check_channel_supply(c, NOW, tenant="t1"), [])
    c.close()

    print("Канал без расписания в тревогу не попадает")
    c = fresh()
    A.SUPPLY_MIN_DAYS = 3.0
    channel(c, {}, name="без выходов")
    check("«не знаем» не повод", A.check_channel_supply(c, NOW, tenant="t1"), [])
    c.close()

    print("Канал на паузе и в архиве не считается")
    c = fresh()
    A.SUPPLY_MIN_DAYS = 3.0
    channel(c, {"*": ["10:00"]}, name="пауза", status="paused")
    channel(c, {"*": ["10:00"]}, name="архив", status="archived")
    check("выключенный канал молчит", A.check_channel_supply(c, NOW, tenant="t1"), [])
    c.close()

    print("Каналов больше, чем называем поимённо")
    c = fresh()
    A.SUPPLY_MIN_DAYS = 3.0
    A.FAILED_MAX = 5
    for i in range(7):
        channel(c, {"*": ["10:00"]}, name=f"канал {i}")
    got = A.check_channel_supply(c, NOW, tenant="t1")
    check("повод всё равно один", len(got), 1)
    check("сказано, сколько всего", "Каналов без запаса: 7" in got[0].text, True)
    check("поимённо — только пятеро", got[0].text.count("· канал "), 5)
    check("про остальных сказано", "и ещё 2" in got[0].text, True)
    c.close()

    print("Повтор — по общему правилу, а не по редкому «ключевому»")
    # Это повод про конвейер: запас пополняется за минуты, и напомнить через час
    # осмысленно. Час — это `REPEAT_SEC`; сутки `KEY_REPEAT_SEC` тут были бы вредны.
    check("повтор через общий срок", A.repeat_after("channel_supply"), A.REPEAT_SEC)
    check("а не через срок ключей", A.repeat_after("channel_supply") != A.KEY_REPEAT_SEC
          or A.REPEAT_SEC == A.KEY_REPEAT_SEC, True)
    check("повод считается тянущимся", "channel_supply" in A.NO_REPEAT, False)
    check("и «починилось» про него говорится", "channel_supply" in A.NO_FIXED, False)

    print("Проход тревог не падает на базе без схемы публикаций")
    bare = sqlite3.connect(os.path.join(TMP, "bare.sqlite"))
    bare.row_factory = sqlite3.Row
    check("пустая база — пустой ответ", A.check_channel_supply(bare, NOW), [])
    bare.close()


# ─────────────────────────── 4. порог и экран ───────────────────────────

def part_limit_and_ui():
    print("Порог правится с экрана «Админ»")
    c = fresh()
    check("порог заведён в списке", "SUPPLY_MIN_DAYS" in A.LIMITS, True)
    title, unit, lo, hi = A.LIMITS["SUPPLY_MIN_DAYS"]
    check("измеряется в сутках", unit, "days")
    check("границы разумны", (lo, hi), (1, 30))
    check("порог не шире окна счёта запаса", hi < 45, True)
    check("названо по-человечески", "запас" in title, True)

    A.set_limit(c, "SUPPLY_MIN_DAYS", 7, now=NOW)
    check("правка применилась к модулю", A.SUPPLY_MIN_DAYS, 7.0)
    state = {x["key"]: x for x in A.limits_state(c)["limits"]}["SUPPLY_MIN_DAYS"]
    check("экран показывает своё значение", (state["value"], state["own"]), (7.0, True))

    # Тот самый порог, по которому судит тревога: канал с запасом на 5 суток при
    # пороге 7 — повод, при пороге 3 — нет.
    ch = channel(c, {"*": ["10:00"]}, horizon=7, name="пятидневный")
    links(c, ch, 5)
    check("при пороге 7 суток — тревога", len(A.check_channel_supply(c, NOW, "t1")), 1)
    A.set_limit(c, "SUPPLY_MIN_DAYS", 3, now=NOW)
    check("при пороге 3 суток — тихо", A.check_channel_supply(c, NOW, "t1"), [])

    A.set_limit(c, "SUPPLY_MIN_DAYS", None, now=NOW)
    check("возврат к значению машины", A.SUPPLY_MIN_DAYS, 3.0)
    try:
        A.set_limit(c, "SUPPLY_MIN_DAYS", 90, now=NOW)
        check("порог вне границ отклонён", "прошло", "отказ")
    except ValueError as exc:
        check("порог вне границ отклонён", "допустимо" in str(exc), True)
    c.close()

    print("Запас виден на плитке канала и на самом канале")
    page = open(os.path.join(BASE, "server", "pub_ui.py"), encoding="utf-8").read()
    check("карточка канала показывает запас", "{supply_html}" in page, True)

    c = fresh()
    ch = channel(c, {"*": ["10:00"]}, horizon=7, name="показ")
    links(c, ch, 4)
    s = supply(c, ch)
    html = U.supply_line(s)
    check("написан срок", "запас на 3 дня" in html, True)
    check("и число выходов", "4 выхода" in html, True)
    # Плитка собирается целиком: проверка «строка есть в исходнике» пропустила бы
    # правку, после которой запас считается, но на плитку не попадает.
    state = T.channel_state(ch, set(), "", now=None, work=None)
    tile = U.channel_tile(ch, state, "/channels", s)
    check("плитка списка показывает запас", "запас на 3 дня" in tile, True)
    check("без запаса плитка всё равно собирается",
          "запас" in U.channel_tile(ch, state, "/channels", None), False)

    ch2 = channel(c, {}, name="показ без расписания")
    unknown = U.supply_line(supply(c, ch2))
    check("«не знаем» показано серым", "muted" in unknown, True)
    check("и не красным", "pub-err" not in unknown, True)

    ch3 = channel(c, {"*": ["10:00"]}, name="показ пустой")
    empty = U.supply_line(supply(c, ch3))
    check("пустой канал показан красным", "pub-err" in empty, True)
    check("ноль и «не знаем» написаны разными словами", empty == unknown, False)
    c.close()


def main() -> int:
    part_calc()
    print()
    part_reflow()
    print()
    part_alert()
    print()
    part_limit_and_ui()
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
