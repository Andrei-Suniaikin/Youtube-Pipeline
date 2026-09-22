#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Чтение чужого журнала ВВ2: лист «Очередь» и правила отбора (19.09.2026).

Дефект. Начало конвейера живёт вне платформы — в Google-таблице ВВ2, и «что какому
каналу взято» записано только там. У нас этого не видно вовсе: строка очереди
владельца и наш план — два журнала, которые никто не сверял, а пороги отбора (длина
оригинала, пороги корзин, размер пула) зашиты у него в книге и правятся без релиза.

Проверяем четыре вещи, и три из них — про границу, а не про удобство:

1. **Разбор.** Sheets не возвращает хвостовые пустые ячейки (строка приедет длиной 4
   вместо 12), лист заведён на 1000 строк с ARRAYFORMULA — пустого мусора приходит
   больше, чем данных, а даты владелец пишет тремя разными форматами. Каждая из этих
   мелочей в отдельности роняет экран целиком.
2. **Сопоставление.** Один и тот же ролик приходит ссылкой в разных написаниях;
   сверять надо `video_id`, и ровно тем же законом (`pub_ids.video_id`), которым живёт
   колонка K таблицы и колонка `donor_video_id` плана. Два закона о том, «что такое
   один ролик», разойдутся — и канал получит дубль, оплаченный дважды.
3. **«Не настроено» ≠ «пусто», «отказ» ≠ «строк нет».** Идентификатора книги и ключа у
   нас ещё нет. Пустой экран без объяснения читается как «владелец ничего не
   запланировал»; отказ книги, стерший прошлые строки, — так же.
4. **Минимальность доступа.** В той же книге лежат почты аккаунтов. Скоуп — только
   readonly, диапазон — только «Очередь!A2:L», метаданных книги (то есть перечня
   листов) не спрашиваем вовсе, и ни одного запроса на запись в модуле нет.

Сети здесь нет: транспорт подменяется, как у `pub_verify.check(..., fetch=None)`.

    .venv/bin/python tests/test_ww2_queue.py
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_ids                                                     # noqa: E402
import ww2_queue as W                                              # noqa: E402

NOW = 1_800_000_000.0
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def check_true(label, got):
    check(label, bool(got), True)


# ------------------------------------------------------------------ окружение

ENV_KEYS = ("VS_WW2_SOURCE", "VS_WW2_SHEET_ID", "VS_WW2_SA_FILE", "VS_WW2_RANGE",
            "VS_WW2_FILE", "VS_WW2_TTL_SEC", "VS_WW2_SETTINGS_TTL_SEC",
            "VS_WW2_TIMEOUT", "VS_WW2_FAIL_TTL_SEC", "VS_WW2_RATE_TTL_SEC",
            "VS_WW2_SETTINGS_BUDGET_SEC", "WW2_SHEET_ID", "WW2_SA")


def env(**kw):
    """Выставить настройки модуля с нуля и забыть всё прочитанное раньше."""
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    for k, v in kw.items():
        os.environ[k] = str(v)
    W.forget_cache()


BOOK = "1ww2-book-id-vydumannyj"          # выдуманный: настоящего у нас нет
VALUES = "https://sheets.googleapis.com/v4/spreadsheets/" + BOOK + "/values/"


def book(**kw):
    """Книга настроена: id и ключ есть. Ключ в сеть не уходит — транспорт подменён."""
    env(VS_WW2_SHEET_ID=BOOK, VS_WW2_SA_FILE="/нет/такого/ключа.json", **kw)


# ------------------------------------------------------------------ транспорт

SEEN = []


def answer(values):
    """Ответ values.get. Запоминает каждый запрошенный адрес — по ним судим о доступе."""
    def fetch(url):
        SEEN.append(url)
        return {"range": "Очередь!A2:L", "majorDimension": "ROWS", "values": values}
    return fetch


def http(code, body=b"{}"):
    def fetch(url):
        SEEN.append(url)
        raise urllib.error.HTTPError(url, code, "нет", {}, io.BytesIO(body))
    return fetch


def timeout():
    def fetch(url):
        SEEN.append(url)
        raise TimeoutError("timed out")
    return fetch


ROW = ["2026-09-18", "Stalowy Pył", "Танки под Прохоровкой",
       "https://www.youtube.com/watch?v=AAAAAAAAAAA&t=30", "Запланировано", "Вова",
       "", "", "заметка", "Prokhorovka: the real story", "AAAAAAAAAAA", "Польский"]


# --------------------------------------------------------------------- разбор

def t_parse():
    print("\nРазбор строки листа")
    rows = W.parse_values([ROW], NOW)
    check("строка разобралась", len(rows), 1)
    r = rows[0]
    check("канал", r["channel_sheet"], "Stalowy Pył")
    check("тема", r["topic"], "Танки под Прохоровкой")
    check("статус листа", r["status_sheet"], "Запланировано")
    check("название оригинала", r["donor_title"], "Prokhorovka: the real story")
    check("язык", r["lang_sheet"], "Польский")
    check("id из колонки K", r["video_id"], "AAAAAAAAAAA")
    check("откуда id", r["video_id_from"], "K")
    check("номер строки листа", r["sheet_row"], 2)
    check("дата в iso", r["date_iso"], "2026-09-18")

    # Sheets обрезает хвостовые пустые ячейки: строка приезжает короче двенадцати.
    short = W.parse_values([["2026-09-18", "Канал", "Тема", "донор без ссылки"]], NOW)
    check("короткая строка не роняет разбор", len(short), 1)
    check("недостающие колонки пусты", short[0]["video_id"], "")
    check("и статус пуст", short[0]["status_sheet"], "")

    # 1000 строк листа с ARRAYFORMULA: пустого мусора больше, чем данных.
    junk = W.parse_values([[""] * 12, ["", "", "", "", "", "", "", "", "", "", "", ""],
                           ["2026-09-18", "", "", "", "", "", "", "", "", "", "", ""],
                           ROW], NOW)
    # Строка, где есть только дата, — тоже мусор автозаполнения: сами формулы
    # таблицы молчат ровно по этому признаку (пусты и «Канал», и «Донор»).
    check("пустые строки отброшены", len(junk), 1)
    check("осталась только настоящая", [r["channel_sheet"] for r in junk],
          ["Stalowy Pył"])

    # Строка, где канал пуст, а донор вписан, — это НЕ мусор: её видно.
    only_donor = W.parse_values([["", "", "", "youtu.be/BBBBBBBBBBB"] + [""] * 8], NOW)
    check("строка без канала, но с донором остаётся", len(only_donor), 1)

    print("\nДаты — три формата владельца, четвёртый остаётся строкой")
    for raw, iso in (("2026-09-18", "2026-09-18"), ("18.09.2026", "2026-09-18"),
                     ("18.09.26", "2026-09-18")):
        got = W.parse_values([[raw, "Канал"] + [""] * 10], NOW)[0]
        check(f"дата {raw}", got["date_iso"], iso)
    bad = W.parse_values([["осенью", "Канал"] + [""] * 10], NOW)[0]
    check("неразобранная дата не теряется", (bad["date"], bad["date_iso"], bad["date_ok"]),
          ("осенью", "", False))

    print("\nИдентификатор ролика — тот же закон, что у нас")
    for raw in ("https://youtu.be/CCCCCCCCCCC", "https://www.youtube.com/watch?v=CCCCCCCCCCC&t=30",
                "CCCCCCCCCCC", "https://www.youtube.com/shorts/CCCCCCCCCCC"):
        got = W.parse_values([["", "Канал", "", "", "", "", "", "", "", "", raw, ""]], NOW)[0]
        # Прямое сравнение с нашим законом: два разных ответа о том, «что такое один
        # ролик», развели бы таблицу и план по разным роликам молча.
        check(f"K={raw[:38]}", got["video_id"], pub_ids.video_id(raw))
        check_true(f"id не пуст для {raw[:38]}", got["video_id"])

    named = W.parse_values([["", "Канал", "", "Прохоровка Танки", "", "", "", "", "", "", "", ""]], NOW)[0]
    check("в «Донор» название, а не ссылка — id пуст", named["video_id"], "")
    # Ровно 11 латинских символов в НАЗВАНИИ наш же закон принял бы за
    # идентификатор — и строка приклеилась бы к чужому ролику. Поэтому из колонки
    # «Донор» id берётся, только если там ссылка.
    tricky = W.parse_values([["", "Канал", "", "Kursk_Tanks", "", "", "", "", "", "", "", ""]], NOW)[0]
    check("название длиной ровно в id не становится id", tricky["video_id"], "")
    check("а наш закон, спрошенный в лоб, его бы принял",
          pub_ids.video_id("Kursk_Tanks"), "Kursk_Tanks")
    from_donor = W.parse_values([["", "Канал", "", "https://youtu.be/DDDDDDDDDDD",
                                  "", "", "", "", "", "", "", ""]], NOW)[0]
    check("K пуста — id берём из ссылки донора", from_donor["video_id"], "DDDDDDDDDDD")
    check("и помечаем, откуда взяли", from_donor["video_id_from"], "донор")

    dbl = W.parse_values([ROW, ROW], NOW)
    check("две одинаковые строки — обе видны", len(dbl), 2)
    check("номера строк листа разные", [r["sheet_row"] for r in dbl], [2, 3])


# ------------------------------------------------------- чтение и его отказы

def t_read():
    print("\nЧтение: что уходит наружу")
    SEEN.clear()
    book()
    got = W.read_rows(fetch=answer([ROW]), now=NOW)
    check("строки прочитаны", len(got["rows"]), 1)
    check("ошибки нет", got["error"], "")
    check("источник", got["source"], "sheets")
    check("запрос ровно один", len(SEEN), 1)
    url = SEEN[0]
    check_true("адрес — values.get нашей книги", url.startswith(VALUES))
    check_true("в адресе ровно наш диапазон", "%D0%9E%D1%87%D0%B5%D1%80%D0%B5%D0%B4%D1%8C%21A2%3AL" in url)
    check_true("значения просим как их видит владелец", "FORMATTED_VALUE" in url)
    check("скоуп один и только на чтение", list(W.SCOPES),
          ["https://www.googleapis.com/auth/spreadsheets.readonly"])
    # Метаданные книги — это перечень её листов, то есть «Почты» и «Профили».
    for forbidden in ("?fields=", "includeGridData", "/values:batchUpdate",
                      "/spreadsheets/" + BOOK + "?", "developerMetadata"):
        check(f"не спрашиваем {forbidden}", forbidden in url, False)

    print("\nКэш: книгу не дёргаем на каждый показ экрана")
    SEEN.clear()
    again = W.read_rows(fetch=answer([ROW]), now=NOW + 10)
    check("второе чтение в пределах TTL — из памяти", again["cached"], True)
    check("в сеть не ходили", len(SEEN), 0)
    check("строки те же", len(again["rows"]), 1)
    SEEN.clear()
    W.read_rows(fetch=answer([ROW, ROW]), now=NOW + 10, fresh=True)
    check("«обновить» кэш не спрашивает", len(SEEN), 1)

    print("\nСменили сервисный аккаунт — прочитанное прежним доступом не наше")
    book()
    W.read_rows(fetch=answer([ROW]), now=NOW)
    os.environ["VS_WW2_SA_FILE"] = "/другой/ключ.json"   # кэш при этом НЕ сбрасываем
    SEEN.clear()
    other = W.read_rows(fetch=answer([ROW, ROW]), now=NOW + 1)
    check("книгу спросили заново", len(SEEN), 1)
    check("и показали прочитанное новым доступом", len(other["rows"]), 2)
    check("а не старое из памяти", other["cached"], False)

    print("\nОтказ источника ≠ «строк нет»")
    for kind, fetch, code in (("forbidden", http(403), 403), ("not_found", http(404), 404),
                              ("rate", http(429), 429), ("server", http(503), 503),
                              ("timeout", timeout(), 0)):
        book()
        W.read_rows(fetch=answer([ROW]), now=NOW)          # сначала успешное чтение
        bad = W.read_rows(fetch=fetch, now=NOW + 3600, fresh=True)
        check(f"{code or 'таймаут'}: вид отказа", bad["error"], kind)
        check(f"{code or 'таймаут'}: прошлые строки на месте", len(bad["rows"]), 1)
        check(f"{code or 'таймаут'}: видно возраст", bad["stale_sec"], 3600.0)
        check_true(f"{code or 'таймаут'}: сказано, что показываем прочитанное",
                   "показываю прочитанное" in bad["text"])
        check_true(f"{code or 'таймаут'}: текст свой", len(bad["text"]) > 20)

    texts = {}
    for kind, fetch in (("forbidden", http(403)), ("not_found", http(404))):
        book()
        texts[kind] = W.read_rows(fetch=fetch, now=NOW)["text"]
    check("403 и 404 объясняются по-разному", texts["forbidden"] == texts["not_found"], False)
    check_true("403 говорит про доступ", "доступ" in texts["forbidden"])
    check_true("404 говорит про id", "id" in texts["not_found"])

    book()
    renamed = W.read_rows(fetch=http(400, "Unable to parse range: Очередь!A2:L".encode()),
                          now=NOW)
    check("лист переименован — свой вид отказа", renamed["error"], "bad_range")
    check_true("и назван VS_WW2_RANGE", "VS_WW2_RANGE" in renamed["text"])

    print("\nПуть к ключу не уезжает на экран вместе с ответом")
    # Модуль сам объявляет правило «целиком печатать нельзя даже путь» — и обязан
    # исполнять его там, где ответ уходит наружу, а не только в тексте одной ошибки.
    book()
    shown = W.read_rows(fetch=answer([ROW]), now=NOW)["settings"]
    check("наружу — только имя файла ключа", shown["sa_file"], "…/ключа.json")
    check("и отдельно признак «файл на месте»", shown["sa_present"], False)
    check("полного пути нет во всём ответе",
          "/нет/такого" in json.dumps(shown, ensure_ascii=False), False)
    check("а внутри модуль по-прежнему знает настоящий путь",
          W.settings()["sa_file"], "/нет/такого/ключа.json")


def t_off():
    print("\nНе настроено — это состояние, а не пустая очередь")
    env()
    got = W.read_rows(fetch=answer([ROW]), now=NOW)
    check("источник выключен", got["source"], "off")
    check("вид состояния", got["error"], "not_configured")
    check("строк нет", got["rows"], [])
    check("в сеть не ходили", got["url"], "")
    check_true("сказано, что не подключена", "не подключена" in got["text"])
    check_true("сказано, что именно положить", len(got["need"]) >= 3)
    joined = " ".join(got["need"])
    for must in ("VS_WW2_SHEET_ID", "VS_WW2_SA_FILE", "/etc/studio/ww2_sa.json",
                 "Просмотр", "Очередь!A2:L"):
        check_true(f"в списке есть {must}", must in joined)

    # Книгу попросили, а реквизитов нет — это тоже «не настроено», а не отказ сети.
    env(VS_WW2_SOURCE="sheets")
    check("без id и ключа источник не включается", W.settings()["source"], "off")
    env(VS_WW2_SHEET_ID="КНИГА")
    check("id без ключа — тоже не включается", W.settings()["source"], "off")

    print("\nЗапасные имена из .env владельца и порядок «окружение старше базы»")
    env(WW2_SHEET_ID="ЕГО-КНИГА", WW2_SA="/его/ключ.json")
    st = W.settings()
    check("WW2_SHEET_ID подхвачен", st["sheet_id"], "ЕГО-КНИГА")
    check("WW2_SA подхвачен", st["sa_file"], "/его/ключ.json")
    check("источник определился сам", st["source"], "sheets")
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL)")
    c.execute("INSERT INTO admin_state VALUES('ww2_sheet_id','из-базы',0)")
    env()
    check("книга из базы переживает рестарт", W.settings(c)["sheet_id"], "из-базы")
    env(VS_WW2_SHEET_ID="из-окружения")
    check("окружение старше базы", W.settings(c)["sheet_id"], "из-окружения")
    env()
    check("путь к ключу в лог целиком не уходит",
          W.hide_path("/etc/studio/ww2_sa.json"), "…/ww2_sa.json")

    print("\nИсточник выключили — прошлых строк на экране не остаётся")
    # Единственное исключение из правила «отказ не стирает прочитанное»: настройку
    # сняли — значит смотреть на чужой журнал мы больше не имеем права.
    book()
    check("сначала прочитали", len(W.read_rows(fetch=answer([ROW]), now=NOW)["rows"]), 1)
    os.environ["VS_WW2_SOURCE"] = "off"        # кэш при этом НЕ сбрасываем
    after = W.read_rows(fetch=answer([ROW]), now=NOW + 1)
    check("строк не осталось", after["rows"], [])
    check("и состояние названо", after["error"], "not_configured")
    env()


def t_file():
    print("\nИсточник «файл»: стенд без доступа к книге и без google-auth")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "ww2_queue.sample.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"values": [ROW], "pool": {"min_len": 25}}, fh, ensure_ascii=False)
        env(VS_WW2_FILE=path)
        check("источник определился", W.settings()["source"], "file")
        got = W.read_rows(now=NOW)                 # fetch не передан: сети нет вовсе
        check("строки прочитаны из снимка", len(got["rows"]), 1)
        check("ошибки нет", got["error"], "")
        check("наружу не ходили", got["url"], "")
        env(VS_WW2_FILE=os.path.join(tmp, "нет.json"))
        gone = W.read_rows(now=NOW)
        check("снимка нет — свой вид отказа", gone["error"], "no_file")

    # Снимок, который лежит в репозитории: на нём поднимается стенд и его же можно
    # показать владельцу ДО того, как он даст ключ. Проверяем и то, что он обезличен.
    sample = os.path.join(HERE, "data", "ww2_queue.sample.json")
    env(VS_WW2_FILE=sample)
    got = W.read_rows(now=NOW)
    check("снимок из репозитория читается", got["error"], "")
    check("в нём восемь непустых строк", len(got["rows"]), 8)
    check("короткая строка в снимке разобралась",
          [r["sheet_row"] for r in got["rows"] if not r["status_sheet"]], [7])
    raw = open(sample, encoding="utf-8").read()
    check("почт в снимке нет", "@" in raw, False)
    pool = W.read_pool_settings(now=NOW)
    check("пороги из снимка помечены снимком", pool["source"]["min_len"], "снимок")


# ---------------------------------------------------- сопоставление с планом

def base():
    c = sqlite3.connect(":memory:")
    c.executescript("""
      CREATE TABLE pub_channels(id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT);
      CREATE TABLE pub_plan_items(id INTEGER PRIMARY KEY, tenant_id TEXT,
                                  channel_id INTEGER, donor_url TEXT,
                                  donor_video_id TEXT, status TEXT);
      CREATE TABLE pub_releases(id INTEGER PRIMARY KEY, release_id TEXT,
                                tenant_id TEXT, plan_item_id INTEGER,
                                phase TEXT, status TEXT);
    """)
    c.execute("INSERT INTO pub_channels VALUES(7,'dev','Stalowy  Pył')")
    c.execute("INSERT INTO pub_channels VALUES(8,'dev','Тёмный Фронт')")
    c.execute("INSERT INTO pub_plan_items VALUES(1,'dev',7,"
              "'https://youtu.be/AAAAAAAAAAA','AAAAAAAAAAA','planned')")
    c.execute("INSERT INTO pub_releases VALUES(1,'r-1','dev',1,'text','running')")
    c.commit()
    return c


DEV = "dev"                               # арендатор, от имени которого смотрим


def t_match():
    print("\nСопоставление строки очереди с нашим выпуском")
    c = base()
    rows = W.parse_values([
        ROW,                                                     # есть у нас
        ["2026-09-18", "stalowy pył", "Тема", "https://youtu.be/EEEEEEEEEEE",
         "Запланировано", "", "", "", "", "", "EEEEEEEEEEE", "Польский"],
        ["2026-09-18", "Темный фронт", "Тема", "название без ссылки",
         "Запланировано", "", "", "", "", "", "", "Польский"],
        ["2026-09-18", "Новый канал 16.09", "Тема", "https://youtu.be/FFFFFFFFFFF",
         "Запланировано", "", "", "", "", "", "FFFFFFFFFFF", "Чешский"],
    ], NOW)
    got = W.match(c, rows, DEV)
    check("наш выпуск найден", got[0]["have"], W.OURS)
    check("и это наш канал", got[0]["channel_id"], 7)
    check("строка плана", got[0]["plan_item_id"], 1)
    check("и её выпуск", (got[0]["release_id"], got[0]["release_phase"]), ("r-1", "text"))
    check("донора нет в плане", got[1]["have"], W.MISSING)
    check("регистр и двойные пробелы имени не мешают", got[1]["channel_id"], 7)
    check("в «Донор» название — нет id", got[2]["have"], W.NO_ID)
    check("«ё» и «е» — один канал", got[2]["channel_id"], 8)
    check("нового канала ВВ2 у нас нет", got[3]["have"], W.NO_CHANNEL)
    check("и наш id не выдуман", got[3]["channel_id"], 0)
    check("сводка", W.summary(got),
          {"rows": 4, W.OURS: 1, W.MISSING: 1, W.NO_CHANNEL: 1, W.NO_ID: 1, "unknown": 0})

    print("\nЧто сопоставление не должно делать")
    c.execute("UPDATE pub_plan_items SET status='cancelled' WHERE id=1")
    check("отменённая строка плана не считается нашей",
          W.match(c, rows, DEV)[0]["have"], W.MISSING)
    c.execute("UPDATE pub_plan_items SET status='planned' WHERE id=1")
    # Ссылка в плане записана как youtu.be, в таблице — как watch?v=…&t=30.
    check("разные написания ссылки — один ролик", W.match(c, rows, DEV)[0]["plan_item_id"], 1)

    dbl = W.match(c, W.parse_values([ROW, ROW], NOW), DEV)
    check("дубль в листе не схлопывается", len(dbl), 2)
    check("и обе строки сопоставлены", [r["have"] for r in dbl], [W.OURS, W.OURS])

    c.execute("INSERT INTO pub_channels VALUES(9,'dev','stalowy pyl')")
    check("похожие имена не путаются", W.match(c, rows, DEV)[1]["channel_id"], 7)
    c.execute("INSERT INTO pub_channels VALUES(10,'dev','Stalowy Pył')")
    twins = W.match(c, rows, DEV)[0]
    check("два наших канала с одним именем — не выбираем молча", twins["have"], W.NO_CHANNEL)
    check_true("и говорим почему", "вручную" in twins["match_error"])
    W.remember_channel(c, DEV, "Stalowy  Pył", 7, now=NOW)
    fixed = W.match(c, rows, DEV)[0]
    check("ручное исправление связи помогает", (fixed["channel_id"], fixed["have"]),
          (7, W.OURS))

    print("\nПлан не прочитался — это не «у нас ничего нет»")
    broken = sqlite3.connect(":memory:")
    blind = W.match(broken, rows, DEV)
    check("состояние не выдумано", [r["have"] for r in blind], ["", "", "", ""])
    check_true("и сказано почему", "не прочитался" in blind[0]["match_error"])


def t_key():
    print("\nКлюч сопоставления — (канал, ролик), а не один только ролик")
    c = base()
    # Тот же донор запланирован, но на ДРУГОМ нашем канале. Это не «у нас есть»:
    # ролик на канале А и тот же ролик на канале Б — две разные работы, и обе
    # оплачиваются отдельно. Сказать «есть» здесь — значит потерять строку очереди.
    c.execute("INSERT INTO pub_plan_items VALUES(2,'dev',8,"
              "'https://youtu.be/GGGGGGGGGGG','GGGGGGGGGGG','planned')")
    c.execute("INSERT INTO pub_releases VALUES(2,'r-2','dev',2,'text','running')")
    alien = ["2026-09-18", "Stalowy Pył", "Тема", "https://youtu.be/GGGGGGGGGGG",
             "Запланировано", "", "", "", "", "", "GGGGGGGGGGG", "Польский"]
    got = W.match(c, W.parse_values([alien], NOW), DEV)[0]
    check("ролик запланирован на другом канале — это «у нас нет»", got["have"], W.MISSING)
    check("чужая строка плана не приклеилась", got["plan_item_id"], 0)
    check("и чужой выпуск тоже", got["release_id"], "")

    # Строка плана на НАШЕМ канале, но с чужим арендатором: так выглядит запись,
    # оставшаяся от переноса канала между арендаторами. Показывать её как нашу
    # нельзя — чужой план и чужой выпуск попадут на наш экран.
    c.execute("INSERT INTO pub_plan_items VALUES(3,'other',7,"
              "'https://youtu.be/IIIIIIIIIII','IIIIIIIIIII','planned')")
    moved = ["2026-09-18", "Stalowy Pył", "Тема", "https://youtu.be/IIIIIIIIIII",
             "Запланировано", "", "", "", "", "", "IIIIIIIIIII", "Польский"]
    check("строка плана чужого арендатора на нашем канале — не наша",
          W.match(c, W.parse_values([moved], NOW), DEV)[0]["have"], W.MISSING)

    mine = list(alien)
    mine[1] = "Тёмный Фронт"                       # тот же ролик, но свой канал
    found = W.match(c, W.parse_values([mine], NOW), DEV)[0]
    check("а на своём канале тот же ролик находится",
          (found["have"], found["plan_item_id"], found["release_id"]),
          (W.OURS, 2, "r-2"))


def t_tenant():
    print("\nАрендатор: чужой журнал не клеится к каналам чужого арендатора")
    c = base()
    # Канал другого арендатора с тем же именем и тот же донор в ЕГО плане: ровно то,
    # что экран ВВ2 показал бы как «наша строка» вместе с чужим планом и выпуском.
    c.execute("INSERT INTO pub_channels VALUES(21,'other','Stalowy Pył')")
    c.execute("INSERT INTO pub_plan_items VALUES(21,'other',21,"
              "'https://youtu.be/AAAAAAAAAAA','AAAAAAAAAAA','planned')")
    c.execute("INSERT INTO pub_releases VALUES(21,'REL-CHUZHOY','other',21,"
              "'upload','running')")
    rows = W.parse_values([ROW], NOW)

    ours = W.match(c, rows, DEV)[0]
    check("наш канал остаётся нашим", ours["channel_id"], 7)
    check("и выпуск наш", ours["release_id"], "r-1")
    theirs = W.match(c, rows, "other")[0]
    check("у другого арендатора — его канал", theirs["channel_id"], 21)
    check("и его выпуск", theirs["release_id"], "REL-CHUZHOY")
    nobody = W.match(c, rows, "nikto")[0]
    check("у арендатора без каналов строка не сопоставлена",
          (nobody["have"], nobody["channel_id"]), (W.NO_CHANNEL, 0))
    whole = W.match(c, rows, W.ALL_TENANTS)[0]
    check("служебный просмотр всей базы не выбирает молча из двух арендаторов",
          whole["have"], W.NO_CHANNEL)

    print("\nРучная связка тоже принадлежит арендатору")
    W.remember_channel(c, "other", "Stalowy  Pył", 21, now=NOW)
    check("связка соседа на нас не действует", W.match(c, rows, DEV)[0]["channel_id"], 7)
    check("а у него действует", W.match(c, rows, "other")[0]["channel_id"], 21)
    refused = ""
    try:
        W.remember_channel(c, DEV, "Stalowy  Pył", 21, now=NOW)
    except W.MapError as exc:
        refused = str(exc)
    check_true("связать со своим листом чужой канал нельзя", "нет" in refused)
    check("и чужая связка от этого не появилась",
          c.execute("SELECT COUNT(*) FROM ww2_channel_map WHERE tenant_id='dev'"
                    ).fetchone()[0], 0)
    no_tenant = ""
    try:
        W.remember_channel(c, "", "Stalowy  Pył", 7, now=NOW)
    except W.MapError as exc:
        no_tenant = str(exc)
    check_true("и без арендатора связка не делается", no_tenant)

    print("\nБез арендатора не сопоставляем вовсе")
    blind = W.match(c, rows)
    check("состояние не выдумано", blind[0]["have"], "")
    check("и канал тоже", blind[0]["channel_id"], 0)
    check_true("и сказано почему", "арендатор" in blind[0]["match_error"])

    print("\nСтарая таблица связок без арендатора не раздаётся всем")
    old = base()
    old.execute("CREATE TABLE ww2_channel_map(sheet_name TEXT PRIMARY KEY, "
                "channel_id INTEGER NOT NULL, updated REAL NOT NULL DEFAULT 0)")
    old.execute("INSERT INTO ww2_channel_map VALUES('Stalowy  Pył',7,0)")
    W.ensure_ww2_schema(old)
    cols = [r[1] for r in old.execute("PRAGMA table_info(ww2_channel_map)").fetchall()]
    check_true("у связок появился арендатор", "tenant_id" in cols)
    check("глобальные связки больше не действуют",
          old.execute("SELECT COUNT(*) FROM ww2_channel_map").fetchone()[0], 0)
    check("но и не потеряны",
          old.execute("SELECT sheet_name FROM ww2_channel_map_bez_arendatora"
                      ).fetchone()[0], "Stalowy  Pył")


def t_dead_link():
    print("\nСвязка на канал, которого больше нет, — не «у нас нет»")
    c = base()
    W.remember_channel(c, DEV, "Старое имя канала", 8, now=NOW)
    c.execute("DELETE FROM pub_channels WHERE id=8")     # канал удалили
    row = ["2026-09-18", "Старое имя канала", "Тема", "https://youtu.be/HHHHHHHHHHH",
           "Запланировано", "", "", "", "", "", "HHHHHHHHHHH", "Польский"]
    got = W.match(c, W.parse_values([row], NOW), DEV)[0]
    check("строка не выглядит сопоставленной", got["have"], W.NO_CHANNEL)
    check("и наш id не выдуман", got["channel_id"], 0)
    check("и имени у канала нет", got["channel_name"], "")
    check_true("и сказано, что связка протухла", "больше нет" in got["match_error"])


# ------------------------------------------------- C5: пороги отбора как данные

def pool_answer(values):
    def fetch(url):
        SEEN.append(url)
        name = url.split("/values/")[1].split("?")[0]
        if name in values:
            return {"values": [[values[name]]]}
        raise urllib.error.HTTPError(
            url, 400, "нет", {},
            io.BytesIO(f"Unable to parse range: {name}".encode()))
    return fetch


def t_pool():
    print("\nПороги отбора читаются из книги, а не зашиты числами")
    SEEN.clear()
    book()
    got = W.read_pool_settings(fetch=pool_answer(
        {"pool_min_len": 25, "pool_mid": 0.6, "pool_top": 3, "pool_min_views": 400000,
         "pool_size": 40, "pool_warmup": 12}), now=NOW)
    check("длина оригинала", got["min_len"], 25.0)
    check("порог «средне»", got["mid"], 0.6)
    check("порог «много»", got["top"], 3.0)
    check("минимум просмотров", got["min_views"], 400000.0)
    check("размер пула", got["size"], 40.0)
    check("разгон", got["warmup"], 12.0)
    check("рядом со значением — откуда", got["source"]["min_len"], "таблица")
    check("спросили ровно шесть именованных диапазонов", len(SEEN), 6)
    check("и ни одного лишнего",
          sorted(u.split("/values/")[1].split("?")[0] for u in SEEN),
          ["pool_mid", "pool_min_len", "pool_min_views", "pool_size", "pool_top",
           "pool_warmup"])
    check_true("числа просим неформатированными",
               all("UNFORMATTED_VALUE" in u for u in SEEN))
    check_true("всё те же values.get нашей книги",
               all(u.startswith(VALUES) for u in SEEN))

    print("\nДиапазона нет — берём умолчание и ГОВОРИМ об этом")
    book()
    part = W.read_pool_settings(fetch=pool_answer({"pool_min_len": 25}), now=NOW)
    check("прочитанное взято", part["min_len"], 25.0)
    check("остальное — умолчания владельца", (part["mid"], part["top"], part["size"]),
          (0.5, 2.0, 60.0))
    check("и помечено как умолчание", part["source"]["size"], "умолчание")
    check("молчать об этом нельзя", len(part["notes"]), 5)
    check_true("в строке названо имя диапазона",
               any("pool_size" in n for n in part["notes"]))

    book()
    zero = W.read_pool_settings(fetch=pool_answer({"pool_min_len": 0, "pool_mid": -1}),
                                now=NOW)
    check("ноль — не настройка", (zero["min_len"], zero["source"]["min_len"]),
          (20.0, "умолчание"))
    check("отрицательное — тоже", zero["mid"], 0.5)
    book()
    as_text = W.read_pool_settings(fetch=pool_answer({"pool_min_len": "25,5"}), now=NOW)
    check("число строкой из зеркала не теряется", as_text["min_len"], 25.5)
    book()
    # Тот же закон «положительное число» и для строк: в зеркале и в выгрузке порог
    # приезжает текстом, и «0» текстом — это такая же не-настройка, как ноль числом.
    text_zero = W.read_pool_settings(
        fetch=pool_answer({"pool_min_len": "0", "pool_mid": "-5"}), now=NOW)
    check("ноль строкой — не настройка",
          (text_zero["min_len"], text_zero["source"]["min_len"]), (20.0, "умолчание"))
    check("минус строкой — тоже",
          (text_zero["mid"], text_zero["source"]["mid"]), (0.5, "умолчание"))

    print("\nКнига не подключена — пороги показываем, но честно")
    env()
    off = W.read_pool_settings(fetch=pool_answer({"pool_min_len": 25}), now=NOW)
    check("значения — умолчания", off["min_len"], 20.0)
    check("источник — умолчание", off["source"]["min_len"], "умолчание")
    check("состояние названо", off["error"], "not_configured")

    print("\nОдно число, которое у нас решает: длина донора")
    book()
    check("из таблицы", W.min_len_minutes(fetch=pool_answer({"pool_min_len": 25})),
          (25.0, "таблица"))
    book()
    check("таблица молчит — наша сохранённая",
          W.min_len_minutes(fetch=pool_answer({}), saved=30.0), (30.0, "наша настройка"))
    book()
    check("нет и её — константа первого запуска",
          W.min_len_minutes(fetch=pool_answer({})), (20.0, "умолчание"))


def t_backoff():
    print("\nОтказ помним так же, как прочитанное: после 429 не ломимся в книгу")
    SEEN.clear()
    book()
    W.read_rows(fetch=answer([ROW]), now=NOW)
    first = W.read_rows(fetch=http(429), now=NOW + 100, fresh=True)
    check("вид отказа", first["error"], "rate")
    was = len(SEEN)
    again = W.read_rows(fetch=http(429), now=NOW + 101, fresh=True)
    check("повторный показ экрана наружу не пошёл", len(SEEN), was)
    check("отказ на экране тот же", again["error"], "rate")
    check("прошлые строки на месте", len(again["rows"]), 1)
    check_true("сказано, когда можно снова", again["retry_in"] > 0)
    check_true("и это видно человеком", "не раньше" in again["text"])
    # 429 — просьба провайдера, пауза для него своя и длиннее обычной (30 с).
    still = W.read_rows(fetch=http(429), now=NOW + 100 + 60, fresh=True)
    check("через минуту всё ещё ждём", len(SEEN), was)
    check("и это по-прежнему отказ, а не пустая очередь", still["error"], "rate")
    W.read_rows(fetch=http(429), now=NOW + 100 + 121, fresh=True)
    check("после паузы спрашиваем снова", len(SEEN), was + 1)

    print("\nДлина паузы — настройка, а не число в коде")
    SEEN.clear()
    book(VS_WW2_FAIL_TTL_SEC=5)
    W.read_rows(fetch=answer([ROW]), now=NOW)
    W.read_rows(fetch=timeout(), now=NOW + 10, fresh=True)
    was = len(SEEN)
    W.read_rows(fetch=timeout(), now=NOW + 12, fresh=True)
    check("внутри паузы наружу не ходим", len(SEEN), was)
    W.read_rows(fetch=timeout(), now=NOW + 16, fresh=True)
    check("за паузой — ходим", len(SEEN), was + 1)

    print("\nОтказ, который чинит владелец, паузой не накрываем")
    # 403 и «книги нет» — это ответ, а не перегрузка: владелец правит доступ и жмёт
    # «обновить». Заставлять его ждать полминуты после каждой правки нельзя.
    SEEN.clear()
    book()
    W.read_rows(fetch=http(403), now=NOW)
    W.read_rows(fetch=http(403), now=NOW + 1, fresh=True)
    check("«нет доступа» переспрашиваем сразу", len(SEEN), 2)


def t_parallel():
    print("\nДва одновременных показа экрана — один запрос в чужую книгу")
    # Ручки кабинета синхронные, uvicorn исполняет их в пуле потоков: без замка
    # «ровно один запрос наружу» на практике оказывается двумя, а токен обновляется
    # дважды. Времени здесь настоящее: проверяем именно одновременность.
    SEEN.clear()
    book()
    in_flight = threading.Event()

    def slow(url):
        SEEN.append(url)
        in_flight.set()
        time.sleep(0.2)                    # запрос «висит», пока стучится второй
        return {"values": [ROW]}

    first = {}
    worker = threading.Thread(target=lambda: first.update(W.read_rows(fetch=slow)))
    worker.start()
    check_true("первый запрос ушёл", in_flight.wait(5.0))
    second = W.read_rows(fetch=slow)
    worker.join(5.0)
    check("наружу сходили один раз", len(SEEN), 1)
    check("строки увидел и первый", len(first.get("rows") or []), 1)
    check("и второй — прочитанное первым", len(second["rows"]), 1)


def t_pool_stale():
    print("\nОтказ не опускает пороги отбора до захардкоженных")
    book()
    ok = W.read_pool_settings(fetch=pool_answer({"pool_min_len": 42}), now=NOW)
    check("прочитали из книги", (ok["min_len"], ok["source"]["min_len"]), (42.0, "таблица"))
    lost = W.read_pool_settings(fetch=timeout(), now=NOW + 1, fresh=True)
    check("после таймаута порог владельца остался", lost["min_len"], 42.0)
    check("и помечен, что прочитан раньше", lost["source"]["min_len"], "прочитано раньше")
    check_true("со временем чтения", any("показываю прочитанное" in n
                                         for n in lost["notes"]))
    # Ровно то место, где это стоит денег: C4 подключит нижнюю границу длины донора
    # к проверке, и одна сетевая икота пропустила бы коротких доноров в оплату.
    check("нижняя граница длины донора не упала до умолчания",
          W.min_len_minutes(fetch=timeout()), (42.0, "прочитано раньше"))

    print("\nНи один порог не прочитался — это состояние, а не молчание")
    book()
    nothing = W.read_pool_settings(fetch=timeout(), now=NOW)
    check("вид отказа назван", nothing["error"], "timeout")
    check("значения — умолчания", nothing["min_len"], 20.0)
    book()
    mirror = W.read_pool_settings(fetch=pool_answer({}), now=NOW)
    check("зеркало без именованных диапазонов — своё состояние",
          mirror["error"], "no_settings")
    book()
    part = W.read_pool_settings(fetch=pool_answer({"pool_min_len": 25}), now=NOW)
    check("хоть один прочитан — отказа нет", part["error"], "")

    print("\nНеудача не кэшируется на все десять минут")
    book()
    SEEN.clear()
    W.read_pool_settings(fetch=timeout(), now=NOW)
    was = len(SEEN)
    W.read_pool_settings(fetch=timeout(), now=NOW + 10)
    check("внутри паузы не переспрашиваем", len(SEEN), was)
    after = W.read_pool_settings(fetch=pool_answer({"pool_min_len": 42}), now=NOW + 40)
    check("а за паузой — перечитываем", after["min_len"], 42.0)
    check("и это не из кэша", after["cached"], False)
    # «Диапазона нет» — ответ книги, а не перегрузка: у зеркала имён нет никогда, и
    # переспрашивать их каждые полминуты значит шесть запросов на каждый показ.
    book()
    SEEN.clear()
    W.read_pool_settings(fetch=pool_answer({"pool_min_len": 25}), now=NOW)
    was = len(SEEN)
    W.read_pool_settings(fetch=pool_answer({"pool_min_len": 25}), now=NOW + 120)
    check("ответ книги держим полный срок", len(SEEN), was)


def t_budget():
    print("\nШесть порогов — один общий бюджет времени")
    SEEN.clear()
    book()
    tick = {"t": 0.0}

    def slow(url):
        SEEN.append(url)
        tick["t"] += 8.0                   # каждый запрос «висит» восемь секунд
        raise TimeoutError("timed out")

    got = W.read_pool_settings(fetch=slow, now=NOW, clock=lambda: tick["t"])
    # Без общего бюджета недоступная книга стоила бы 6 × VS_WW2_TIMEOUT = 90 с
    # занятого потока uvicorn на ОДИН показ экрана.
    check("после исчерпания бюджета наружу не ходим", len(SEEN), 2)
    check_true("и говорим, что кончился именно бюджет",
               any("бюджет" in n for n in got["notes"]))
    check("про каждый из шести порогов сказано отдельно",
          len([n for n in got["notes"] if n.startswith("пул: настройка")]), 6)
    check("и состояние названо целиком", got["error"], "timeout")
    check("значения — умолчания", got["min_len"], 20.0)

    SEEN.clear()
    book(VS_WW2_SETTINGS_BUDGET_SEC=30)
    tick["t"] = 0.0
    W.read_pool_settings(fetch=slow, now=NOW, clock=lambda: tick["t"])
    check("бюджет — настройка, а не число в коде", len(SEEN), 4)


# ------------------------------------------------------------------ запись

def t_readonly():
    print("\nВ книгу не пишем ничего и никогда")
    src = open(os.path.join(BASE, "server", "ww2_queue.py"), encoding="utf-8").read()
    body = "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith("#"))
    # Метод запроса нигде не назначается: urlopen без data — это GET.
    for danger in ("batchUpdate", "values:append", "values:clear", "method=\"POST\"",
                   "method='POST'", "data=", "spreadsheets.currentonly",
                   "auth/drive", "auth/spreadsheets\""):
        check(f"в модуле нет {danger}", danger in body, False)
    check("скоуп ровно один", len(W.SCOPES), 1)
    check_true("и он readonly", W.SCOPES[0].endswith(".readonly"))
    # Единственная запись модуля — в НАШУ базу: ручное исправление связи каналов.
    c = base()
    W.remember_channel(c, DEV, "Имя из листа", 7, now=NOW)
    check("исправление связи сохраняется у нас",
          c.execute("SELECT channel_id FROM ww2_channel_map").fetchone()[0], 7)
    W.remember_channel(c, DEV, "Имя из листа", 8, now=NOW + 1)
    check("и переписывается, а не двоится",
          c.execute("SELECT COUNT(*), MAX(channel_id) FROM ww2_channel_map").fetchone(),
          (1, 8))


def main():
    try:
        t_parse()
        t_read()
        t_off()
        t_file()
        t_match()
        t_key()
        t_tenant()
        t_dead_link()
        t_backoff()
        t_parallel()
        t_pool()
        t_pool_stale()
        t_budget()
        t_readonly()
    finally:
        env()
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
