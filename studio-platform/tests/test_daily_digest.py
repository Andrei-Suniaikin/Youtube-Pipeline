#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ежедневная сводка в формате заказчика: счётчики, группы, честность чисел (29.09.2026).

Что здесь опасно и потому проверяется.

1. **«Подтверждено» — самое дорогое число сводки.** Заказчик читает его как «ролик
   вышел на YouTube». У нас же `done` означает подтверждённое расписание — это закон
   проекта и его известная ловушка. Если сводка возьмёт `done`, заказчику каждый день
   будут рапортовать о выходах, которых могло не быть. Поэтому подтверждением считается
   только фаза `published`, и это проверяется прямо: выпуск, доведённый до `scheduled`,
   в «подтверждено» не попадает НИКОГДА.

2. **Ноль вместо «не знаем» — прямая ложь, и она незаметна.** Подтверждать бывает
   нечем: нет ключа YouTube Data API, кончилась квота, проверка ещё не ходила. Ноль в
   этот момент читается как «сегодня не вышло ничего» — то есть как авария конвейера,
   которой нет. Проверяется, что два разных состояния дают два разных текста и что при
   отсутствии ключа слов «подтверждено 0» в сводке не появляется вовсе.

3. **Задержку нельзя объявлять по незнанию — и незнанием нельзя прятать задержку.**
   Выпуск, про который мы не успели спросить, не «задержан». Но выпуск, не дошедший до
   канала, строка плана без выпуска вовсе и уже полученный ответ проверки задержаны и
   без ключа: убранный ключ и кончившаяся квота — свойства НАШЕЙ настройки, ответа
   YouTube они не отменяют. Проверяется, что ни то, ни другое не превращает задержку в
   «не знаем», и что сам ответ читается по сроку: «ролика ещё не видно» — это ожидание,
   пока не кончилось терпение (`pub_verify.GIVE_UP_SEC`), и задержка после.

4. **Канал без тега не должен потеряться.** Группы — это теги, а тег у нас ничего не
   решает. Канал без тегов обязан попасть в «прочие»: сводка, из которой канал тихо
   выпал, хуже отсутствия сводки — по ней решат, что у канала всё хорошо.

5. **Рассылка по умолчанию выключена.** Владелец 28.09.2026 просил уменьшить поток
   сообщений; включить новую суточную рассылку за него нельзя. Проверяется, что без
   явного включения не уходит ничего, что при включении сводка уходит один раз в сутки,
   и что непринятое телеграмом сообщение не считается отправленным.

Сети здесь нет вовсе: наличие ключа подменяется параметром, отправка — списком.

    .venv/bin/python tests/test_daily_digest.py
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
TMP = tempfile.mkdtemp(prefix="daily-digest-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
# Пояс отчёта в тесте — UTC: границы суток должны быть предсказуемы, иначе проверка
# «ожидалось сегодня» сама зависела бы от того, в каком поясе её запустили.
os.environ["VS_DIGEST_TZ"] = "UTC"
# Сроки поздней проверки закрепляем умолчаниями: проверка «ждём ≠ задержано» ставит слоты
# по обе стороны терпения, и чужое значение в окружении машины увело бы их за границы
# суток — тест краснел бы от настройки, а не от дефекта. Сами сроки он всё равно читает
# из `pub_verify`, а не переписывает числами.
os.environ["VS_PUBLIC_GRACE_SEC"] = "1800"
os.environ["VS_PUBLIC_GIVE_UP_SEC"] = str(6 * 3600)
sys.path.insert(0, os.path.join(BASE, "server"))

from pub_schema import ensure_pub_schema                          # noqa: E402
import pub_calls as PC                                            # noqa: E402
import pub_digest as D                                            # noqa: E402
import pub_verify as V                                            # noqa: E402

FAILS = []

# Опорный момент: среда 07.10.2026, 20:00 UTC. Вечер выбран намеренно — часть выходов
# дня уже прошла, часть ещё впереди, и обе половины обязаны считаться по-разному.
NOW = dt.datetime(2026, 10, 7, 20, 0, tzinfo=dt.timezone.utc).timestamp()
HOUR = 3600
TID = "t1"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def fresh() -> sqlite3.Connection:
    """Своя чистая база на каждую часть: чужие выпуски сбили бы счёт."""
    path = os.path.join(TMP, f"db{len(os.listdir(TMP))}.sqlite")
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    ensure_pub_schema(c)
    c.executescript("""
      CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
      CREATE TABLE IF NOT EXISTS queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT,
        kind TEXT, status TEXT, created REAL, finished REAL, project_id TEXT, error TEXT);
      CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT);
    """)
    c.commit()
    return c


def channel(c, name, schedule=None, tags=(), status="active"):
    """Канал с тегами. Теги пишем прямой вставкой: их путь проверяет свой тест."""
    uc = "UC" + str(abs(hash(name)) % 10 ** 22).rjust(22, "0")
    cid = c.execute(
        "INSERT INTO pub_channels(tenant_id,uc,name,tz,schedule,horizon_days,status,"
        "created,updated) VALUES(?,?,?,'UTC',?,3,?,?,?)",
        (TID, uc, name, json.dumps(schedule or {}), status, NOW, NOW)).lastrowid
    for tag in tags:
        row = c.execute("SELECT id FROM pub_tags WHERE tenant_id=? AND name=?",
                        (TID, tag)).fetchone()
        tid_ = row[0] if row else c.execute(
            "INSERT INTO pub_tags(tenant_id,name,created) VALUES(?,?,?)",
            (TID, tag, NOW)).lastrowid
        c.execute("INSERT OR IGNORE INTO pub_channel_tags(channel_id,tag_id) VALUES(?,?)",
                  (cid, tid_))
    c.commit()
    return cid


def plan(c, cid, slot):
    return c.execute(
        "INSERT INTO pub_plan_items(tenant_id,channel_id,donor_url,slot_at,status,"
        "created,updated) VALUES(?,?,?,?,'planned',?,?)",
        (TID, cid, f"https://youtu.be/x{cid}-{int(slot)}", slot, NOW, NOW)).lastrowid


def release(c, cid, item, slot, phase, status="running", blocked=None,
            accepted_at=None, check_state=None):
    """Выпуск плюс, по желанию, событие приёмки и последний ответ поздней проверки."""
    rid = f"r{item}"
    c.execute(
        "INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,"
        "channel_id,uc,slot_at,phase,status,video_id,blocked_reason,created,updated) "
        "VALUES(?,?,?,1,?,?,?,?,?,?,?,?,?)",
        (rid, TID, item, cid, f"UC{cid}", slot, phase, status,
         f"v{item}", blocked, NOW - 86400, NOW))
    seq = 0
    if accepted_at is not None:
        seq += 1
        c.execute("INSERT INTO pub_release_events(release_id,seq,phase,created,repeats,"
                  "last_seen) VALUES(?,?,'scheduled',?,1,?)",
                  (rid, seq, accepted_at, accepted_at))
    if check_state is not None:
        seq += 1
        c.execute("INSERT INTO pub_release_events(release_id,seq,phase,payload,created,"
                  "repeats,last_seen) VALUES(?,?,'public_check',?,?,1,?)",
                  (rid, seq, json.dumps({"state": check_state}), NOW - HOUR, NOW - HOUR))
    c.commit()
    return rid


def day_at(hour, day=7):
    return dt.datetime(2026, 10, day, hour, 0, tzinfo=dt.timezone.utc).timestamp()


# ─────────────────────── 1. счётчики на подготовленной базе ───────────────────────

def base():
    """Четыре канала, восемь строк плана, семь выпусков. Одна и та же для трёх частей."""
    c = fresh()
    ch1 = channel(c, "Первый", {"*": ["10:00"]}, tags=("ww2",))
    ch2 = channel(c, "Второй", {"*": ["10:00"]}, tags=("ww2", "тест"))
    ch3 = channel(c, "Третий", {"*": ["10:00"]})
    ch4 = channel(c, "Четвёртый")                 # без расписания: запас неизвестен
    # Сегодня, время уже прошло.
    release(c, ch1, plan(c, ch1, day_at(8)), day_at(8), "published",
            accepted_at=NOW - 9 * HOUR)
    release(c, ch1, plan(c, ch1, day_at(12)), day_at(12), "scheduled",
            accepted_at=day_at(12, day=6))        # приняли ВЧЕРА — в сутки не считается
    release(c, ch2, plan(c, ch2, day_at(9)), day_at(9), "scheduled",
            accepted_at=NOW - 8 * HOUR, check_state="not_public")
    release(c, ch3, plan(c, ch3, day_at(11)), day_at(11), "media_ready",
            status="blocked", blocked="ключ FastGen кончился")
    # Сегодня, время ещё не пришло, и завтра.
    plan(c, ch2, day_at(23))
    plan(c, ch1, day_at(10, day=8))
    plan(c, ch2, day_at(10, day=8))
    return c, (ch1, ch2, ch3, ch4)


def part_counters():
    print("Счётчики на подготовленной базе")
    c, _ = base()
    d = D.collect(c, TID, now=NOW, have_key=True)

    t = d["today"]
    check("ожидалось выходов сегодня", t["expected"], 4)
    check("ещё впереди сегодня", t["ahead"], 1)
    check("подтверждено — только фаза published", t["confirmed"], 1)
    check("задержано", t["delayed"], 2)
    check("про сколько не знаем", t["unknown"], 1)
    check("причина незнания названа",
          any("проверка выхода ещё не проходила" in w for w in t["why"]), True)

    p = d["production"]
    check("принято новых роликов за сутки", p["accepted"], 2)
    check("каналов без запаса", p["no_supply"], 3)
    check("каналов с неизвестным запасом", p["supply_unknown"], 1)

    names = [g["tag"] for g in d["groups"]]
    check("группы по тегам, «прочие» последними", names, ["ww2", "тест", "прочие"])
    ww2 = next(g for g in d["groups"] if g["tag"] == "ww2")
    check("ww2: в плане", ww2["plan"], 3)
    check("ww2: принято", ww2["accepted"], 2)
    check("ww2: блокеры", ww2["blocked"], 0)
    тест = next(g for g in d["groups"] if g["tag"] == "тест")
    check("тест: в плане (тот же канал считается и здесь)", тест["plan"], 2)
    check("тест: принято", тест["accepted"], 1)

    problems = {p["channel"]: p for p in d["problems"]}
    check("проблемные каналы", sorted(problems), ["Второй", "Третий"])
    check("причина блокера — своими словами", problems["Третий"]["why"],
          "ключ FastGen кончился")
    check("у канала без будущих выходов результат не назначен",
          "не назначен" in problems["Третий"]["next"], True)
    # У «Второго» два будущих выхода — сегодня в 23:00 и завтра в 10:00. Назван обязан
    # быть БЛИЖАЙШИЙ: отчёт отвечает на вопрос «когда будет результат», и дальняя дата
    # там означала бы простой, которого нет.
    check("у канала с будущим выходом назван ближайший момент",
          problems["Второй"]["next"], "07.10 23:00")
    check("канал с неизвестным запасом в проблемы не попал",
          "Четвёртый" in problems, False)

    # Канал в архиве работу принимать может (его выпуск дожили до расписания), но в
    # сводку он не входит — и его приёмка не имеет права раздувать «принято новых
    # роликов»: число тогда не сойдётся с суммой по группам, а спорить с ним нечем.
    архив = channel(c, "Архивный", {"*": ["10:00"]}, status="archived")
    release(c, архив, plan(c, архив, day_at(7)), day_at(7), "scheduled",
            accepted_at=NOW - 7 * HOUR)
    сарх = D.collect(c, TID, now=NOW, have_key=True)
    check("приёмка архивного канала в производство не попала",
          сарх["production"]["accepted"], 2)
    check("и его выход не стал ожидавшимся", сарх["today"]["expected"], 4)

    # Общий проход (боевой `dev/alerts.py` ходит без VS_DEV_TENANT) обязан видеть то же
    # самое. Молчаливая фильтрация по пустому арендатору дала бы пустой отчёт, а он
    # читается как остановившийся конвейер, а не как незаданная настройка.
    общий = D.collect(c, None, now=NOW, have_key=True)
    check("проход без арендатора видит те же выходы",
          общий["today"]["expected"], t["expected"])
    check("и те же группы", [g["tag"] for g in общий["groups"]],
          [g["tag"] for g in d["groups"]])
    check("и то же принято", общий["production"]["accepted"], p["accepted"])
    c.close()


def part_no_tag():
    print("\nКанал без тега не теряется")
    c, (_ch1, _ch2, ch3, _ch4) = base()
    d = D.collect(c, TID, now=NOW, have_key=True)
    прочие = next((g for g in d["groups"] if g["tag"] == "прочие"), None)
    check("группа «прочие» есть", прочие is not None, True)
    check("в неё попали оба канала без тегов", прочие["channels"], 2)
    check("и её блокер виден", прочие["blocked"], 1)
    text = D.render(d)
    check("«прочие» названы в тексте сводки", "· прочие:" in text, True)
    check("канал без тега попал в проблемы текстом", "Третий →" in text, True)

    # Тот же канал, но с тегом: он обязан уйти из «прочих», а не удвоиться.
    c.execute("INSERT INTO pub_tags(tenant_id,name,created) VALUES(?,?,?)",
              (TID, "сон", NOW))
    tag_id = c.execute("SELECT id FROM pub_tags WHERE name='сон'").fetchone()[0]
    c.execute("INSERT INTO pub_channel_tags(channel_id,tag_id) VALUES(?,?)",
              (ch3, tag_id))
    c.commit()
    d2 = D.collect(c, TID, now=NOW, have_key=True)
    прочие2 = next(g for g in d2["groups"] if g["tag"] == "прочие")
    check("после тега канал ушёл из «прочих»", прочие2["channels"], 1)
    check("и блокер переехал в свою группу",
          next(g for g in d2["groups"] if g["tag"] == "сон")["blocked"], 1)
    c.close()


# ───────────── 1б. про ключ — только когда есть что подтверждать ─────────────

def part_key_note_only_when_relevant():
    print("\nПро ключ говорим только когда есть что подтверждать")
    # 03.10.2026: сводка новой студии — «ключа нет, подтверждать нечем» при 11 задержках,
    # из которых ни одна до канала не дошла (все стояли на рерайте и озвучке).
    c = fresh()
    cid = channel(c, "Nocne Biografie")
    for k in range(3):
        item = plan(c, cid, NOW - 3600 * (k + 1))
        release(c, cid, item, NOW - 3600 * (k + 1), "project_ready", status="blocked",
                blocked=f"этап «tts» завершился со статусом error")
    d = D.collect(c, TID, now=NOW, have_key=False)
    check("все три — задержаны", d["today"]["delayed"], 3)
    check("ключ не обвиняется", any("нет ключа" in w or "ключа YouTube" in w
                                    for w in d["today"]["why"]), False)
    check("сказана правда: до канала не дошли",
          any("до канала не дошёл" in w for w in d["today"]["why"]), True)


# ─────────────────────── 2. «не знаем» не равно нулю ───────────────────────

def part_unknown_vs_zero():
    print("\n«Нет данных» и ноль — разные ответы")
    c, _ = base()
    known = D.collect(c, TID, now=NOW, have_key=True)
    blind = D.collect(c, TID, now=NOW, have_key=False)

    check("без ключа подтверждённое остаётся фактом", blind["today"]["confirmed"], 1)
    # Ключ решает только то, что мы собирались СПРОСИТЬ. Всё, что уже известно —
    # незалитая заливка и уже сохранённый ответ проверки, — от него не зависит, иначе
    # утро без ключа выглядело бы спокойнее утра с ключом.
    check("без ключа задержки не убавилось",
          blind["today"]["delayed"], known["today"]["delayed"])
    check("и невыясненных не прибавилось",
          blind["today"]["unknown"], known["today"]["unknown"])
    check("но причина незнания стала другой",
          any("нет ключа YouTube Data API" in w for w in blind["today"]["why"]), True)
    check("а с ключом про ключ речи нет",
          any("нет ключа" in w for w in known["today"]["why"]), False)
    check("проблемные каналы те же",
          sorted(p["channel"] for p in blind["problems"]),
          sorted(p["channel"] for p in known["problems"]))
    c.close()

    # Крайний случай: подтвердить нельзя НИ ОДИН выход. Ноль здесь запрещён.
    c = fresh()
    ch = channel(c, "Слепой", {"*": ["10:00"]})
    release(c, ch, plan(c, ch, day_at(8)), day_at(8), "scheduled")
    release(c, ch, plan(c, ch, day_at(9)), day_at(9), "scheduled")
    blind = D.collect(c, TID, now=NOW, have_key=False)
    check("ожидалось два выхода", blind["today"]["expected"], 2)
    check("про оба не знаем", blind["today"]["unknown"], 2)
    check("подтверждённых ноль по факту", blind["today"]["confirmed"], 0)
    check("но словами это не ноль", D._confirmed_text(blind["today"]),
          "подтверждено: не знаем")
    text = D.render(blind)
    check("в тексте нет слов «подтверждено 0»", "подтверждено 0" in text, False)
    check("в тексте сказано, чего не хватает",
          "нет ключа YouTube Data API" in text, True)
    c.close()

    # Тот же расклад, но проверка ходила и ответила: ролика не видно дольше терпения.
    # Здесь ноль — правда, и он обязан быть написан числом.
    c = fresh()
    ch = channel(c, "Проверенный", {"*": ["10:00"]})
    release(c, ch, plan(c, ch, day_at(8)), day_at(8), "scheduled",
            check_state="not_public")
    release(c, ch, plan(c, ch, day_at(9)), day_at(9), "scheduled",
            check_state="not_public")
    seen = D.collect(c, TID, now=NOW, have_key=True)
    check("оба выхода задержаны", seen["today"]["delayed"], 2)
    check("незнания нет", seen["today"]["unknown"], 0)
    check("честный ноль пишется числом", D._confirmed_text(seen["today"]),
          "подтверждено 0")
    check("«подтверждено 0» и «не знаем» — разный текст",
          D._confirmed_text(seen["today"]) == "подтверждено: не знаем", False)
    c.close()

    # Выход, время которого прошло совсем недавно: спрашивать рано, и это не задержка.
    c = fresh()
    ch = channel(c, "Свежий", {"*": ["10:00"]})
    release(c, ch, plan(c, ch, NOW - 60), NOW - 60, "scheduled")
    fresh_d = D.collect(c, TID, now=NOW, have_key=True)
    check("свежий выход не объявлен задержанным", fresh_d["today"]["delayed"], 0)
    check("он в «не знаем»", fresh_d["today"]["unknown"], 1)
    check("и сказано, что рано",
          any("рано спрашивать" in w for w in fresh_d["today"]["why"]), True)
    c.close()


def part_saved_answer():
    """Уже полученный ответ проверки — знание, и настройками оно не отменяется."""
    print("\nСохранённый ответ проверки не подменяется на «не знаем»")
    c = fresh()
    ch = channel(c, "Поссылке", {"*": ["10:00"]})
    release(c, ch, plan(c, ch, day_at(9)), day_at(9), "scheduled",
            check_state="unlisted")
    # Запас каналу даём с избытком: без него канал попал бы в «Проблемы» по кончившемуся
    # материалу, и проверка «строка на месте» ничего бы не доказывала.
    for д in range(1, 6):
        plan(c, ch, day_at(10, day=7 + д))
    PC.ensure_quota_schema(c)

    d = None
    for label, key, без_квоты in (("ключ на месте", True, False),
                                  ("ключ убрали", False, False),
                                  ("квота кончилась", True, True)):
        if без_квоты:
            PC.quota_block(c, "единицы суточной квоты кончились", now=NOW)
            check("ожидание квоты действительно записано",
                  bool(PC.quota_wait(c, now=NOW)), True)
        d = D.collect(c, TID, now=NOW, have_key=key)
        check(f"{label}: «только по ссылке» — задержка", d["today"]["delayed"], 1)
        check(f"{label}: и это не «не знаем»", d["today"]["unknown"], 0)
        check(f"{label}: причина названа ответом проверки",
              [p["why"] for p in d["problems"]],
              ["ролик доступен только по ссылке, а должен быть публичным"])
        check(f"{label}: «задержано 1» в тексте", "задержано 1" in D.render(d), True)

    # Почему строка «задержано» — единственная защита такого выпуска: на ответе «только
    # по ссылке» поздняя проверка ставит blocked_reason, но НЕ status='blocked'. В
    # блокеры канал не попадает, и подмени мы задержку незнанием — он исчезнет из
    # отчёта целиком, вместе с уже оплаченным ответом YouTube.
    в_разборе = c.execute(
        "SELECT COUNT(*) FROM pub_releases WHERE status='blocked'").fetchone()[0]
    check("такой выпуск в разборе не числится", в_разборе, 0)
    check("и в блокерах групп его нет", sum(g["blocked"] for g in d["groups"]), 0)
    c.close()


def part_plan_without_release():
    """Слот наступил, выпуска нет вовсе: конвейер встал, и сказать это надо прямо."""
    print("\nСтрока плана без выпуска — задержка, а не незнание")
    c = fresh()
    ch = channel(c, "Забытый", {"*": ["10:00"]})
    plan(c, ch, day_at(9))                    # время слота прошло, выпуска не заводили
    for д in range(1, 6):
        plan(c, ch, day_at(10, day=7 + д))    # материал есть — дело не в нём
    for label, key in (("с ключом", True), ("без ключа", False)):
        d = D.collect(c, TID, now=NOW, have_key=key)
        check(f"{label}: ожидался один выход", d["today"]["expected"], 1)
        check(f"{label}: он задержан", d["today"]["delayed"], 1)
        check(f"{label}: и это не «не знаем»", d["today"]["unknown"], 0)
        check(f"{label}: сказано, что выпуск не заводился",
              [p["why"] for p in d["problems"]],
              ["выпуск не заводился: строка плана осталась без работы"])
        check(f"{label}: «задержано 1» в тексте", "задержано 1" in D.render(d), True)
    c.close()


def part_patience():
    """«Ждём» и «задержано» разделяет терпение поздней проверки, а не факт слота."""
    print("\nТерпение проверки: ждём — не то же, что задержано")
    c = fresh()
    ch = channel(c, "Терпеливый", {"*": ["10:00"]})
    # Два выпуска с одним и тем же ответом «ключ ролика не видит» и разными слотами:
    # у первого терпение вышло ровно сейчас, у второго не вышло на минуту. Границу
    # берём из самой `pub_verify`, чтобы тест не разъехался с ней при смене env.
    вышло = NOW - V.GIVE_UP_SEC
    не_вышло = NOW - V.GIVE_UP_SEC + 60
    release(c, ch, plan(c, ch, вышло), вышло, "scheduled", check_state="not_public")
    release(c, ch, plan(c, ch, не_вышло), не_вышло, "scheduled",
            check_state="not_public")
    d = D.collect(c, TID, now=NOW, have_key=True)
    check("ожидалось два выхода", d["today"]["expected"], 2)
    check("задержан только тот, у кого терпение вышло", d["today"]["delayed"], 1)
    check("второй — «не знаем», а не задержка", d["today"]["unknown"], 1)
    check("и про него сказано, что ждём",
          any("ждём" in w for w in d["today"]["why"]), True)
    check("grace при этом давно прошёл",
          any("рано спрашивать" in w for w in d["today"]["why"]), False)

    # Тот же выпуск до конца grace: спрашивать рано, ответа проверки быть не может, и
    # задержкой это не становится ни на секунду раньше срока.
    c2 = fresh()
    ch2 = channel(c2, "Свежайший", {"*": ["10:00"]})
    рано = NOW - max(1.0, V.GRACE_SEC / 2)
    release(c2, ch2, plan(c2, ch2, рано), рано, "scheduled")
    d2 = D.collect(c2, TID, now=NOW, have_key=True)
    check("до конца grace задержки нет", d2["today"]["delayed"], 0)
    check("и это «не знаем»", d2["today"]["unknown"], 1)
    check("с причиной «рано спрашивать»",
          any("рано спрашивать" in w for w in d2["today"]["why"]), True)
    c2.close()
    c.close()


# ─────────────────────── 3. формат текста ───────────────────────

def part_alerts_guard():
    print("\nТревоги: «не прочиталось» не выдаётся за «их нет»")
    c, _ = base()
    d = D.collect(c, TID, now=NOW, have_key=True)
    check("на целой базе тревоги прочитаны", bool(d["alerts"]), True)
    check("и беды нет", d["alerts_why"], "")

    # База, до которой очередная миграция не доехала: проход тревог падает на
    # отсутствующей таблице. Остальная сводка обязана уцелеть, а пустой список тревог
    # не имеет права выглядеть как «всё спокойно».
    c.execute("DROP TABLE projects")
    c.commit()
    d = D.collect(c, TID, now=NOW, have_key=True)
    check("сводка всё равно посчитана", d["today"]["expected"], 4)
    check("список тревог пуст", d["alerts"], [])
    check("но сказано, что он не прочитался",
          "не читается" in (d["alerts_why"] or ""), True)
    check("и это написано в тексте", "Тревоги: " in D.render(d), True)
    check("слов «действующие тревоги» при этом нет",
          "Действующие тревоги" in D.render(d), False)
    c.close()


def part_text():
    print("\nТекст сводки в формате заказчика")
    c, _ = base()
    text = D.render(D.collect(c, TID, now=NOW, have_key=True))
    for piece in ("Группы каналов:", "в плане ", "принято ", "блокеры ",
                  "Сегодня: ожидалось ", "задержано ", "Производство: ",
                  "каналов без запаса ", "Проблемы:", "следующий результат:"):
        check(f"строка формата «{piece.strip()}» на месте", piece in text, True)
    check("сумма по группам не складывается — про это сказано",
          "строки не складываются" in text, True)
    check("день отчёта назван", "Сводка за 07.10.2026" in text, True)
    long_lines = [ln for ln in text.splitlines() if len(ln) > 160]
    check("ни одна строка сводки не превращается в простыню", long_lines, [])
    c.close()


# ─────────────────────── 3б. срывы заливки за сутки ───────────────────────

def part_hand_confirmed():
    """Подтверждённый человеком выход — выполнение плана, но названное отдельно.

    Решение владельца 01.10.2026, п. 6. Два требования разом, и они тянут в разные
    стороны: такой выход НЕ должен висеть в «не знаем» до конца суток (план выполнен,
    ждать нечего), и он НЕ должен притворяться машинным подтверждением — за `published`
    стоит ответ YouTube Data API, а здесь человек, посмотревший канал.
    """
    print("\nПодтверждённое человеком — выполнение плана, но своё слово")
    c = fresh()
    ch = channel(c, "Первый", {"*": ["10:00"]})
    # Выход сегодня, время прошло, машинного подтверждения нет и не будет: фаза
    # `upload_running` и ролик на канале, который увидел человек.
    rid = release(c, ch, plan(c, ch, day_at(8)), day_at(8), "upload_running",
                  status="done")
    c.execute("UPDATE pub_releases SET hand_confirmed=?, hand_by='хозяин', "
              "hand_video_id='v1' WHERE release_id=?", (NOW - HOUR, rid))
    c.commit()

    d = D.collect(c, TID, now=NOW, have_key=True)
    check("ожидался один выход", d["today"]["expected"], 1)
    check("он посчитан как подтверждённый человеком", d["today"]["by_hand"], 1)
    check("машинным подтверждением он НЕ считается", d["today"]["confirmed"], 0)
    check("и в «не знаем» не висит", d["today"]["unknown"], 0)
    check("и задержанным не считается", d["today"]["delayed"], 0)

    text = D.render(d)
    check("в тексте выход назван выполненным", "подтверждено 1" in text, True)
    check("и отдельно сказано, что руками", "в том числе руками 1" in text, True)
    c.close()


def part_uploads():
    """Числа переписи срывов заливки попадают в сводку (решение владельца 01.10.2026, п. 7).

    Автоповтор заливки решается по числам, а числа надо собрать и ПОКАЗАТЬ. Сводка —
    то место, куда владелец смотрит каждый день; отдельный отчёт никто не откроет.
    Главное здесь — чтобы «нода молчала» не читалось как «можно повторить»: это
    единственная категория, в которой мы не знаем, ушёл ли файл.
    """
    print("\nСрывы заливки за сутки")
    import pub_upload_census as census                             # noqa: PLC0415

    c, _ = base()
    пусто = D.collect(c, TID, now=NOW, have_key=True)
    check("без срывов числа всё равно названы",
          [пусто["uploads"][k] for k in census.KINDS], [0, 0, 0, 0])
    # Ноль в текст не пишем: строка «срывов нет» каждое утро приучает её не читать.
    check("и в текст строка не попала", "Заливка:" in D.render(пусто), False)

    census.note(c, "rel-x", "браузер умер", now=NOW - HOUR)        # safe: фаз нет
    census.note(c, "rel-y", "нода молчит", now=NOW - HOUR, silent=True)
    census.note(c, "rel-z", "ролик пересобран", now=NOW - HOUR, stale=True)
    census.note(c, "rel-w", "вчерашний", now=NOW - 30 * HOUR, silent=True)
    c.commit()

    d = D.collect(c, TID, now=NOW, have_key=True)
    check("за сутки посчитаны только сегодняшние", d["uploads"]["всего"], 3)
    check("безопасных", d["uploads"]["safe"], 1)
    check("молчания", d["uploads"]["silent"], 1)
    check("нечего повторять", d["uploads"]["stale"], 1)

    text = D.render(d)
    check("строка про заливку в тексте есть", "Заливка: срывов заливки 3" in text, True)
    check("сказано, сколько безопасно повторить", "безопасен повтор 1" in text, True)
    # Без этой оговорки «нода молчала 1» читается как «один безопасный повтор».
    check("и что молчание — это не ноль и не разрешение",
          "могла продолжать загрузку молча" in text, True)
    c.close()


# ─────────────────────── 4. рассылка по умолчанию выключена ───────────────────────

def part_screen():
    """Экран «Сводка» в пульте. Статически по `web/index.html` — тем же приёмом, что в
    `tests/test_channel_settings_layout.py`: стенд для этого поднимать не за что."""
    print("\nЭкран «Сводка» в пульте")
    src = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
    check("вкладка «Сводка» есть в шапке", 'href="#/digest" id="tab-digest"' in src, True)
    check("адрес #/digest разбирается роутером", "h === '#/digest'" in src, True)
    # С 03.10.2026 между ними стоит «Выпуски» — проверяем, что «Сводка» в списке подсветки.
    check("вкладка подсвечивается", "for (const id of ['digest', " in src, True)
    check("экран нарисован своей функцией", "async function viewDigest(){" in src, True)
    check("экран берёт данные с сервера", "api('/ui/digest' + (chosen" in src, True)
    # Текст отчёта копируется ТОТ ЖЕ, что уходит в телеграм: если экран соберёт свой,
    # два отчёта разойдутся молча, и обнаружится это у заказчика.
    check("копируется текст с сервера, а не разметка",
          "writeText(d.text)" in src, True)
    # «Не знаем» вместо нуля — на экране тоже. С 01.10.2026 в число входит и
    # подтверждённое человеком (решение владельца, п. 6), поэтому слагаемое не одно;
    # что складывается именно так, как в тексте отчёта, проверяет уже браузер
    # (tests/test_digest_screen_browser.py). Здесь — сторож на само «не знаем».
    check("«не знаем» вместо нуля живёт и на экране",
          "(t.unknown && known <= 0) ? 'не знаем'" in src, True)
    check("и подтверждённое человеком входит в число",
          "String((t.confirmed || 0) + руками)" in src, True)
    check("переключатель рассылки есть", 'id="digest-tg"' in src, True)
    check("и он для администратора", "ME.role === 'admin'" in src, True)

    app_src = open(os.path.join(BASE, "web", "app.py"), encoding="utf-8").read()
    check("маршрут сводки поднят", '@app.get("/ui/digest")' in app_src, True)
    check("настройка рассылки — своим маршрутом",
          '@app.post("/ui/digest/settings")' in app_src, True)
    # Проверка роли стоит на сервере, а не на спрятанной галочке: запрос может
    # прислать кто угодно. И отказ говорит именно про рассылку сводки — общий текст
    # про учётные записи отправил бы человека искать беду не туда.
    guard = app_src.split("def ui_digest_settings")[1][:1200]
    check("включать рассылку может только администратор",
          'role != "admin"' in guard, True)
    check("и отказ написан про рассылку",
          "рассылку сводки включает и выключает администратор" in guard, True)

    alerts_src = open(os.path.join(BASE, "dev", "alerts.py"), encoding="utf-8").read()
    check("суточная отправка висит на общем таймере тревог",
          "D.daily_tick(" in alerts_src, True)


def part_telegram():
    print("\nРассылка в телеграм")
    c, _ = base()
    conf = D.settings(c)
    check("по умолчанию рассылка выключена", conf["telegram"], False)
    check("час отправки по умолчанию", conf["hour"], 9)

    sent = []
    out = D.daily_tick(c, lambda t: sent.append(t) or True, TID, now=NOW)
    check("выключенная рассылка ничего не отправляет", out["sent"], False)
    check("и говорит почему", "выключена" in out["why"], True)
    check("сообщений не было", sent, [])

    D.set_settings(c, telegram=True, hour=9)
    check("настройка сохранилась", D.settings(c)["telegram"], True)

    утро = day_at(8, day=8)
    out = D.daily_tick(c, lambda t: sent.append(t) or True, TID, now=утро)
    check("до назначенного часа не отправляет", out["sent"], False)
    check("и объясняет, что рано", "рано" in out["why"], True)
    check("сообщений по-прежнему нет", sent, [])

    девять = day_at(9, day=8)
    out = D.daily_tick(c, lambda t: sent.append(t) or True, TID, now=девять)
    check("в назначенный час отправляет", out["sent"], True)
    check("сообщение ушло одно", len(sent), 1)
    check("в сообщении есть сводка", "Группы каналов:" in sent[0], True)

    out = D.daily_tick(c, lambda t: sent.append(t) or True, TID, now=девять + HOUR)
    check("второй раз за сутки не отправляет", out["sent"], False)
    check("и говорит, что уже уходила", "уже уходила" in out["why"], True)
    check("сообщение так и одно", len(sent), 1)

    # Следующие сутки, телеграм молчит: обязанность отправить остаётся.
    завтра = day_at(9, day=9)
    out = D.daily_tick(c, lambda _t: False, TID, now=завтра)
    check("непринятое сообщение не считается отправленным", out["sent"], False)
    check("и причина названа", "не принял" in out["why"], True)
    out = D.daily_tick(c, lambda t: sent.append(t) or True, TID, now=завтра + HOUR)
    check("на следующем проходе сводка уходит", out["sent"], True)
    check("всего ушло два сообщения", len(sent), 2)

    D.set_settings(c, telegram=False)
    out = D.daily_tick(c, lambda t: sent.append(t) or True, TID, now=day_at(9, day=10))
    check("выключение снова затыкает рассылку", out["sent"], False)
    check("сообщений по-прежнему два", len(sent), 2)
    c.close()


# ─────────────────────── 5. границы суток ───────────────────────

def part_day_bounds():
    print("\nГраницы суток")
    since, until = D.day_bounds(NOW, "UTC")
    check("сутки начинаются в полночь", since, day_at(0))
    check("и кончаются следующей полночью", until, day_at(0, day=8))
    check("сутки длиной в сутки", until - since, 86400)

    # Переход на зимнее время в Европе: 25.10.2026 в сутках 25 часов. Замена времени
    # на 23:59:59 вместо честного начала следующего дня потеряла бы час выходов.
    осень = dt.datetime(2026, 10, 25, 12, 0, tzinfo=dt.timezone.utc).timestamp()
    since, until = D.day_bounds(осень, "Europe/Warsaw")
    check("в день перехода сутки длиннее обычных", until - since, 25 * 3600)

    c, _ = base()
    # Выход ровно в полночь следующих суток — это уже не сегодня.
    ch = channel(c, "Полуночный", {"*": ["10:00"]})
    plan(c, ch, day_at(0, day=8))
    d = D.collect(c, TID, now=NOW, have_key=True)
    check("полуночный выход следующих суток в сегодня не попал",
          d["today"]["expected"] + d["today"]["ahead"], 5)
    c.close()


# ─────────────── период экрана и блокеры по смыслу (04.10.2026) ───────────────

def part_period_and_blockers():
    """Владелец 04.10.2026: «не понятно, за какой период — календарь, по умолчанию
    текущий месяц»; «24 блокера — что за блокеры, разгребём, что наше, что не наше»."""
    print("Период экрана: даты «с» и «по» включительно, по умолчанию — текущий месяц")
    check("месяц по умолчанию", D.month_bounds(NOW, "UTC"), ("2026-10-01", "2026-10-31"))
    feb = dt.datetime(2028, 2, 10, 12, tzinfo=dt.timezone.utc).timestamp()
    check("високосный февраль", D.month_bounds(feb, "UTC"), ("2028-02-01", "2028-02-29"))
    a, b = D.period_bounds("2026-10-06", "2026-10-07", "UTC")
    check("начало — полночь первого дня", a, day_at(0, day=6))
    check("конец — полночь дня после последнего", b, day_at(0, day=8))
    a, b = D.period_bounds("2026-10-25", "2026-10-25", "Europe/Warsaw")
    check("сутки перевода часов — 25 часов, а не 24", (b - a) / HOUR, 25.0)
    for bad, why in ((("2026-10-07", "2026-10-06"), "раньше"), (("7.10.2026", "8.10.2026"),
                     "ГГГГ-ММ-ДД"), (("2025-01-01", "2026-10-01"), "400")):
        try:
            D.period_bounds(*bad, "UTC")
            got = "принято"
        except ValueError as exc:
            got = why in str(exc)
        check(f"отказ: {why}", got, True)

    print("\nПринято за период — по всем дням периода, а не за сутки")
    c, _ = base()
    day = D.collect(c, TID, now=NOW, have_key=False)
    a, b = D.period_bounds("2026-10-06", "2026-10-07", "UTC")
    two = D.collect(c, TID, now=NOW, have_key=False, since=a, until=b)
    a, b = D.period_bounds("2026-10-01", "2026-10-05", "UTC")
    early = D.collect(c, TID, now=NOW, have_key=False, since=a, until=b)
    check("сутки — как раньше", day["production"]["accepted"], 2)
    check("два дня — и вчерашняя приёмка", two["production"]["accepted"], 3)
    check("дни без приёмки — ноль", early["production"]["accepted"], 0)
    check("период назван в ответе", two["period"], {"from": "2026-10-06", "to": "2026-10-07"})
    check("сутки без периода — те же сутки", day["period"],
          {"from": "2026-10-07", "to": "2026-10-07"})
    check("ожидалось за два дня больше, чем за сутки",
          two["today"]["expected"] >= day["today"]["expected"], True)
    c.close()

    print("\n«Проблемы» — про сегодня, даже когда числа за период")
    c = fresh()
    x = channel(c, "Вчерашний", {"*": ["10:00"]})
    plan(c, x, day_at(10, day=6))                 # вчерашний выход, ролика не было
    a, b = D.period_bounds("2026-10-06", "2026-10-07", "UTC")
    per = D.collect(c, TID, now=NOW, have_key=False, since=a, until=b)
    check("в числах периода вчерашний выход задержан", per["today"]["delayed"], 1)
    # Канал может быть в проблемах по другой причине (кончился запас) — это правда;
    # проверяем, что его не держит там ВЧЕРАШНИЙ пропущенный выход.
    check("а вчерашний пропуск в проблемы не попал",
          [p_["why"] for p_ in per["problems"]
           if p_["channel"] == "Вчерашний" and "не заводился" in p_["why"]], [])
    check("пояснение не про «сегодняшний»",
          any("сегодняшний" in w for w in per["today"]["why"]), False)
    c.close()

    print("\nБлокеры — по смыслу причины и по тому, кто чинит")
    c = fresh()
    a1 = channel(c, "Альфа", {"*": ["10:00"]})
    b1 = channel(c, "Бета", {"*": ["10:00"]})
    n = 100
    for cid, why in ((a1, "у ролика нет субтитров (VideoUnplayable)"),
                     (b1, "у ролика нет субтитров (VideoUnplayable)"),
                     (a1, "account recovery needs the owner: no exact account binding "
                          "for this profile+channel"),
                     (a1, "задача рерайта прервалась — рерайт перезапускали"),
                     (b1, "задача рерайта прервалась — рерайт перезапускали"),
                     (b1, "задача рерайта прервалась — рерайт перезапускали"),
                     (a1, "обложка деградировала дважды: текст наложен поверх картинки"),
                     (b1, "что-то совсем новое")):
        n += 1
        release(c, cid, plan(c, cid, day_at(10, day=n % 20 + 1)), day_at(10), "created",
                status="blocked", blocked=why)
    out = D.collect(c, TID, now=NOW, have_key=False)
    bl = out["blockers"]
    check("сначала наша беда, потом канал, обложка, донор, незнакомое",
          [x["kind"] for x in bl], ["studio", "channel", "cover", "donor", ""])
    check("одинаковые причины сложены", [x["count"] for x in bl], [3, 1, 1, 2, 1])
    check("каналы перечислены без повторов", bl[0]["channels"], ["Бета", "Альфа"]
          if bl[0]["channels"][0] == "Бета" else ["Альфа", "Бета"])
    check("у нашей беды — что делать", "Вернуть в работу" in bl[0]["fix"], True)
    check("незнакомое — как есть, под «Прочее»", (bl[-1]["tag"], bl[-1]["title"]),
          ("Прочее", "что-то совсем новое"))
    check("сумма блокеров — все выпуски в разборе",
          sum(x["count"] for x in bl), 8)
    c.close()


def main() -> int:
    part_key_note_only_when_relevant()
    part_counters()
    part_no_tag()
    part_unknown_vs_zero()
    part_saved_answer()
    part_plan_without_release()
    part_patience()
    part_alerts_guard()
    part_text()
    part_hand_confirmed()
    part_uploads()
    part_screen()
    part_telegram()
    part_day_bounds()
    part_period_and_blockers()
    print("Про отсутствие ключа сводка говорит даже когда все ответы уже есть")
    # Перестановка порядка (сохранённый ответ выше отсутствия ключа) закрыла ложь «не
    # знаем» вместо полученного ответа — но вместе с ней пропало и честное «подтверждать
    # нечем»: если каждый сегодняшний выход закрыт сохранённым ответом, сводка выглядела
    # полной, хотя новые выходы подтверждать больше нечем.
    c, _ = base()
    out = D.collect(c, TID, now=NOW, have_key=False)
    c.close()
    said = " ".join(out["today"]["why"]) + " " + D.render(out)
    check("сказано, что ключа нет", "ключа YouTube Data API нет" in said, True)
    check("и что это значит для новых выходов", "подтверждать нечем" in said, True)

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
