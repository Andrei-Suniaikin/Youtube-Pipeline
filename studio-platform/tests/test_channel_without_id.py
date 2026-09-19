#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Канал без ID YouTube (пункт 6.1 ТЗ, батч 16.09.2026) + дополнение 16.09 (ответы
владельца на вопросы 1-3 и доработки ревьюера по фазе 3).

Проверяется здесь:
  1) миграция схемы, снимающая NOT NULL с pub_channels.uc, — идемпотентно, без потери
     строк и индексов; отдельно — что она же работает БЕЗ row_factory=sqlite3.Row
     (доработка 1), что переносит тип колонки как объявлен, не подменяя пустой тип
     на TEXT (доработка 2), и что не сбрасывает счётчик AUTOINCREMENT, если максимальный
     id был удалён до миграции (code review, итерация 2, Issue 1);
  2) канал без ID заводится, и два таких канала не конфликтуют друг с другом (несколько
     NULL в UNIQUE-колонке SQLite не дубль, несколько пустых строк были бы дублем);
  3) перенос списком (`import_channels`) строку без uc ОТКЛОНЯЕТ, а не заводит канал
     без ID — так решил владелец (вопрос 2 дополнения): у сопоставления строк переноса
     по uc нет способа отличить повтор одной и той же безымянной строки от новой, и
     канал без ID заводится только формой;
  4) `pub_ui.set_channel_uc` — вписать ID можно только в пустое поле, один раз; уже
     заданный ID не меняется и не стирается, конфликт по уникальности и кривой формат
     по-прежнему отклоняются (вопрос 1 дополнения, вариант «а»); запрет решает сам
     UPDATE (`WHERE uc IS NULL`), а не отдельный SELECT до него (code review, итерация
     2, Issue 2);
  5) движок (`pub_engine.materialize`) не заводит выпуск на канале без ID, не падает и
     не блокирует остальные строки плана; когда ID появляется, следующий тик заводит
     выпуск и стирает устаревшую причину «ID не задан» из заметки строки, не трогая
     чужие заметки (code review, итерация 2, Issue 4);
  6) интерфейс: поле ID в обеих формах заведения канала необязательное (без "6.1" в
     видимом тексте страницы — вопрос 3 дополнения, ссылка на пункт ТЗ только в
     комментариях кода), плашка «ID пустой» — в списке и в шапке карточки, а форма
     настроек канала в пульте показывает ID как редактируемое поле (пусто) или как
     чтение с пояснением (уже задан); кабинет (`register_pub_routes`, форма
     `/api/channels/{id}/edit`) при этом не тронут — своего поля ID там как не было,
     так и нет. Статические проверки — без стенда, плюс сквозная проверка через
     поднятый пульт (`/ui/channels`), которая пропускается с пометкой, если стенда нет;
  7) регрессия (QA, при проверке фазы 3): обычное сохранение настроек канала
     (`POST /ui/channels/{cid}`, `update_channel`/`read_channel_form`) поле "uc" не
     читает и не может ни подменить уже вписанный ID, ни завести его в обход
     `/ui/channels/{cid}/uc` — даже если тело запроса его вдруг содержит.

Части 1-5 сети не требуют: база — в памяти, движок проверяется без единого сетевого
вызова. Часть 6 частично требует стенда (см. `dev/run.sh` или раздел «Проверка
результата» в task-spec.md) и своей копии базы:

    .venv/bin/python tests/test_channel_without_id.py
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

from pub_schema import ensure_pub_schema, _migrate_channel_uc_optional  # noqa: E402
import pub_ui as U                                                # noqa: E402
import pub_engine as E                                            # noqa: E402

PULT = os.environ.get("VS_PULT_URL", "http://127.0.0.1:8080/studio")
DEV_DB = os.environ.get("VS_DEV_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
LOGIN = os.environ.get("VS_DEV_LOGIN", "dev")
PASSWORD = os.environ.get("VS_DEV_PASSWORD", "dev-local-only")

NOW = 1_800_000_000.0
FAILS = []
# Пропущенные из-за отсутствия стенда проверки. Молчаливый пропуск — это зелень, купленная
# за то, что проверка не выполнялась: в прогоне и в CI «проверено» и «не проверялось»
# выглядели одинаково. Теперь код выхода честный (2), а осознанный пропуск объявляется
# переменной, как в tests/test_pult_wave3_browser.py.
SKIPPED = []
NO_STAND_OK = os.environ.get("VS_PULT_TEST_NO_STAND", "") not in ("", "0")


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def fresh() -> sqlite3.Connection:
    """Настоящая схема контура публикаций — та же функция, что и в бою."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_pub_schema(c)
    c.commit()
    return c


# ─────────────────────────── 1. миграция ───────────────────────────

OLD_SCHEMA_SQL = """
  CREATE TABLE pub_channels(
    id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
    uc TEXT NOT NULL UNIQUE, name TEXT NOT NULL, handle TEXT, lang TEXT, niche TEXT,
    profile_id INTEGER, host_id INTEGER, tz TEXT NOT NULL DEFAULT 'Europe/Warsaw',
    schedule TEXT NOT NULL DEFAULT '{}', horizon_days INTEGER NOT NULL DEFAULT 3,
    status TEXT NOT NULL DEFAULT 'active', note TEXT,
    created REAL NOT NULL, updated REAL NOT NULL,
    lead_days INTEGER NOT NULL DEFAULT 2,
    deferred INTEGER NOT NULL DEFAULT 1, deferred_limit INTEGER NOT NULL DEFAULT 5
  );
  CREATE INDEX idx_pub_channels_list ON pub_channels(tenant_id,status,name);
  CREATE INDEX idx_pub_channels_profile ON pub_channels(profile_id);
  CREATE INDEX idx_pub_channels_lang ON pub_channels(lang,niche);
"""


def old_style_db() -> sqlite3.Connection:
    """База «до пункта 6.1»: uc — TEXT NOT NULL UNIQUE, как было исторически."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(OLD_SCHEMA_SQL)
    c.execute("INSERT INTO pub_channels(tenant_id,uc,name,created,updated) "
              "VALUES('dev','UCK8sQmJBp8GCxrOtXWBpyEA','Канал 1',?,?)", (NOW, NOW))
    c.execute("INSERT INTO pub_channels(tenant_id,uc,name,created,updated) "
              "VALUES('dev','UCAAAAAAAAAAAAAAAAAAAAAA','Канал 2',?,?)", (NOW, NOW))
    c.commit()
    return c


def old_style_db_plain() -> sqlite3.Connection:
    """Та же база, но БЕЗ row_factory=sqlite3.Row (доработка 1, дополнение 16.09): в
    бою `ensure_pub_schema` зовут с разными соединениями, не всегда с Row-фабрикой."""
    c = sqlite3.connect(":memory:")
    c.executescript(OLD_SCHEMA_SQL)
    c.execute("INSERT INTO pub_channels(tenant_id,uc,name,created,updated) "
              "VALUES('dev','UCK8sQmJBp8GCxrOtXWBpyEA','Канал без Row',?,?)", (NOW, NOW))
    c.commit()
    return c


def snapshot(c):
    cols = {r["name"]: dict(r) for r in c.execute("PRAGMA table_info(pub_channels)")}
    rows = [dict(r) for r in c.execute("SELECT * FROM pub_channels ORDER BY id")]
    idx = sorted(r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='pub_channels'"))
    return cols, rows, idx


def check_migration():
    print("Миграция: снять NOT NULL с pub_channels.uc")
    c = old_style_db()
    before_cols, before_rows, before_idx = snapshot(c)
    check("до миграции uc NOT NULL", before_cols["uc"]["notnull"], 1)

    _migrate_channel_uc_optional(c)
    c.commit()
    after_cols, after_rows, after_idx = snapshot(c)
    check("после миграции NOT NULL снят", after_cols["uc"]["notnull"], 0)
    check("столбцы не потерялись", set(before_cols) <= set(after_cols), True)
    check("число строк не изменилось", len(after_rows), len(before_rows))
    check("значения строк совпали", after_rows, before_rows)
    check("именованные индексы на месте", after_idx, before_idx)

    print("\n  повторный запуск ничего не меняет (идемпотентность)")
    _migrate_channel_uc_optional(c)
    c.commit()
    cols2, rows2, idx2 = snapshot(c)
    check("колонки те же", cols2, after_cols)
    check("строки те же", rows2, after_rows)
    check("индексы те же", idx2, after_idx)

    print("\n  новая база уже без NOT NULL — ensure_pub_schema его не находит")
    fresh_c = fresh()
    fresh_cols = {r["name"]: dict(r) for r in fresh_c.execute("PRAGMA table_info(pub_channels)")}
    check("uc новой базы допускает NULL", fresh_cols["uc"]["notnull"], 0)
    ensure_pub_schema(fresh_c)   # второй вызов — тоже идемпотентен и не падает
    fresh_c.commit()


def check_migration_without_row_factory():
    """Доработка 1 (дополнение 16.09): ensure_pub_schema не требует row_factory=Row."""
    print("\nМиграция и ensure_pub_schema работают без row_factory=sqlite3.Row")
    c = old_style_db_plain()
    before = c.execute("PRAGMA table_info(pub_channels)").fetchall()
    notnull_before = next(r[3] for r in before if r[1] == "uc")
    check("до ensure_pub_schema uc NOT NULL (кортежи, без Row)", notnull_before, 1)

    ensure_pub_schema(c)   # полный штатный путь — не только приватный помощник
    c.commit()
    after = c.execute("PRAGMA table_info(pub_channels)").fetchall()
    notnull_after = next(r[3] for r in after if r[1] == "uc")
    check("после ensure_pub_schema NOT NULL снят (кортежи, без Row)", notnull_after, 0)
    row = c.execute("SELECT tenant_id, uc, name FROM pub_channels "
                    "WHERE name='Канал без Row'").fetchone()
    check("строка не потерялась и читается кортежем",
          row, ("dev", "UCK8sQmJBp8GCxrOtXWBpyEA", "Канал без Row"))

    print("\n  повторный вызов на уже мигрированной базе — тоже без Row, тоже не падает")
    ensure_pub_schema(c)
    c.commit()
    after2 = c.execute("PRAGMA table_info(pub_channels)").fetchall()
    check("повторный вызов ensure_pub_schema ничего не меняет (без Row)", after2, after)


def check_migration_type_preserved():
    """Доработка 2 (дополнение 16.09): тип колонки переносится ровно как объявлен,
    пустой тип не подменяется на TEXT."""
    print("\nПересборка не подменяет пустой тип колонки на TEXT")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    # untyped_note — колонка без объявленного типа: SQLite это допускает (динамическая
    # типизация), и раньше пересборка дописывала бы ей TEXT от себя.
    c.executescript("""
      CREATE TABLE pub_channels(
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
        uc TEXT NOT NULL UNIQUE, name TEXT NOT NULL, untyped_note
      );
    """)
    c.execute("INSERT INTO pub_channels(tenant_id,uc,name,untyped_note) "
              "VALUES('dev','UCK8sQmJBp8GCxrOtXWBpyEA','Канал',123)")
    c.commit()
    before_type = next(r["type"] for r in c.execute("PRAGMA table_info(pub_channels)")
                       if r["name"] == "untyped_note")
    check("тип untyped_note до миграции пуст", before_type, "")

    _migrate_channel_uc_optional(c)
    c.commit()
    after_type = next(r["type"] for r in c.execute("PRAGMA table_info(pub_channels)")
                      if r["name"] == "untyped_note")
    check("тип untyped_note после миграции остался пустым (не стал TEXT)", after_type, "")
    val = c.execute("SELECT untyped_note FROM pub_channels").fetchone()["untyped_note"]
    check("значение untyped-колонки не потерялось", val, 123)


def check_migration_preserves_autoincrement():
    """Code review (итерация 2, Issue 1): пересборка таблицы не должна возвращать
    переиспользованный id, если максимальный id был когда-то удалён — INSERT...SELECT
    копирует id как обычные значения, а не через счётчик AUTOINCREMENT, и без починки
    sqlite_sequence после миграции падает до максимума СРЕДИ ОСТАВШИХСЯ строк. Внешних
    ключей в базе нет, поэтому старые pub_plan_items/pub_releases/pub_upload_jobs с
    удалённым channel_id молча привязались бы к чужому новому каналу."""
    print("\nПересборка таблицы не сбрасывает счётчик AUTOINCREMENT")
    c = old_style_db()   # каналы с id 1 и 2 уже заведены
    c.execute("INSERT INTO pub_channels(tenant_id,uc,name,created,updated) "
              "VALUES('dev','UCBBBBBBBBBBBBBBBBBBBBBB','Канал 3',?,?)", (NOW, NOW))
    c.commit()
    max_id_before = c.execute("SELECT MAX(id) m FROM pub_channels").fetchone()["m"]
    check("три канала заведены, максимальный id — 3", max_id_before, 3)

    # Канал с максимальным id удалили вручную — типичный случай на боевой базе.
    c.execute("DELETE FROM pub_channels WHERE id=?", (max_id_before,))
    c.commit()

    _migrate_channel_uc_optional(c)
    c.commit()

    rows = c.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='pub_channels'").fetchall()
    # Ровно одна строка: fetchone() прятал дубль, из-за которого SQLite мог однажды
    # прочитать устаревший счётчик.
    check("строка счётчика ровно одна", len(rows), 1)
    seq_after = rows[0]["seq"]
    check("sqlite_sequence после миграции не уменьшился", seq_after >= max_id_before, True)

    cid_new = U.create_channel(c, "dev", "", "Канал после миграции")
    check("новый канал не переиспользует удалённый id", cid_new != max_id_before, True)
    check("новый канал получил id больше прежнего максимума", cid_new > max_id_before, True)

    print("\n  максимальный id на месте — счётчик тоже ровно одной строкой")
    c2 = old_style_db()
    c2.execute("INSERT INTO pub_channels(tenant_id,uc,name,created,updated) "
               "VALUES('dev','UCCCCCCCCCCCCCCCCCCCCCCC','Канал 3',?,?)", (NOW, NOW))
    c2.commit()
    top = c2.execute("SELECT MAX(id) m FROM pub_channels").fetchone()["m"]
    _migrate_channel_uc_optional(c2)
    c2.commit()
    rows2 = c2.execute("SELECT seq FROM sqlite_sequence WHERE name='pub_channels'").fetchall()
    check("без удалений — тоже одна строка", len(rows2), 1)
    check("и значение не меньше максимального id", rows2[0]["seq"] >= top, True)
    check("временное имя не осталось в счётчике",
          c2.execute("SELECT COUNT(*) n FROM sqlite_sequence WHERE name='pub_channels_new'")
            .fetchone()["n"], 0)

    print("\n  повторный запуск миграции на уже мигрированной базе тоже не трогает счётчик")
    seq_before_second_run = c.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='pub_channels'").fetchone()["seq"]
    _migrate_channel_uc_optional(c)
    c.commit()
    seq_after_second_run = c.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='pub_channels'").fetchone()["seq"]
    check("счётчик не изменился (миграция уже не нужна — notnull снят)",
          seq_after_second_run, seq_before_second_run)


# ─────────────────────────── 2. заведение канала ───────────────────────────

def check_create():
    print("\nЗаведение канала без ID")
    check("clean_uc('') -> None", U.clean_uc(""), None)
    check("clean_uc('   ') -> None", U.clean_uc("   "), None)
    try:
        U.clean_uc("не похоже на UC")
        FAILS.append("clean_uc должен был отвергнуть кривой ID")
        print("  FAIL clean_uc должен был отвергнуть кривой ID")
    except U.ChannelError:
        print("  OK   clean_uc по-прежнему отвергает кривой (непустой) ID")

    c = fresh()
    cid1 = U.create_channel(c, "dev", "", "Канал без ID — раз")
    row1 = c.execute("SELECT uc FROM pub_channels WHERE id=?", (cid1,)).fetchone()
    check("канал заведён, uc хранится как NULL", row1["uc"], None)

    cid2 = U.create_channel(c, "dev", "", "Канал без ID — два")
    check("второй канал без ID заведён отдельной строкой", cid2 != cid1, True)
    check("оба канала числятся без ID",
          [r["uc"] for r in c.execute(
              "SELECT uc FROM pub_channels WHERE id IN (?,?)", (cid1, cid2))],
          [None, None])

    cid3 = U.create_channel(c, "dev", "UCK8sQmJBp8GCxrOtXWBpyEA", "Канал с ID")
    check("канал с настоящим ID заводится как раньше",
          c.execute("SELECT uc FROM pub_channels WHERE id=?", (cid3,)).fetchone()["uc"],
          "UCK8sQmJBp8GCxrOtXWBpyEA")
    try:
        U.create_channel(c, "dev", "UCK8sQmJBp8GCxrOtXWBpyEA", "Дубль по ID")
        FAILS.append("повтор настоящего ID должен был упасть на UNIQUE")
        print("  FAIL повтор настоящего ID должен был упасть на UNIQUE")
    except Exception as exc:
        check("а вот повтор настоящего ID — по-прежнему дубль", "UNIQUE" in str(exc), True)


def check_import():
    """Вопрос 2 дополнения 16.09 (вариант «в»): импорт строк без uc отклоняется, как
    было до пункта 6.1. Канал без ID заводится только формой (`check_create` выше)."""
    print("\nПеренос списком (import_channels): строки без ID отклоняются")
    c = fresh()
    out = U.import_channels(c, "dev", json.dumps([
        {"name": "Перенос без ID — раз"},
        {"name": "Перенос без ID — два"},
    ]), NOW)
    check("обе строки без uc отклонены, а не заведены как новые каналы",
          (out["created"], out["updated"], len(out["rejected"])), (0, 0, 2))
    check("причина отказа упоминает ID", all("ID" in r for r in out["rejected"]), True)
    n = c.execute("SELECT COUNT(*) n FROM pub_channels "
                  "WHERE name LIKE 'Перенос без ID%'").fetchone()["n"]
    check("ни один канал без ID из импорта не заведён", n, 0)

    print("\n  повторный перенос той же безымянной по ID строки — тоже отклонён (не дубль)")
    out2 = U.import_channels(c, "dev", json.dumps([{"name": "Перенос без ID — раз"}]), NOW)
    check("вторая попытка тоже отклонена", (out2["created"], len(out2["rejected"])), (0, 1))

    print("\n  перенос с настоящим ID по-прежнему заводит и обновляет как раньше")
    out3 = U.import_channels(c, "dev", json.dumps(
        [{"uc": "UCK8sQmJBp8GCxrOtXWBpyEA", "name": "Перенесённый канал"}]), NOW)
    check("канал с ID заведён", (out3["created"], out3["updated"]), (1, 0))
    out4 = U.import_channels(c, "dev", json.dumps(
        [{"uc": "UCK8sQmJBp8GCxrOtXWBpyEA", "name": "Новое имя"}]), NOW)
    check("повторный перенос с тем же ID обновляет, а не дублирует",
          (out4["created"], out4["updated"]), (0, 1))


# ────────────────── 3. дописать ID уже заведённому каналу (вопрос 1) ──────────────────

def check_set_channel_uc():
    """Вопрос 1 дополнения 16.09, вариант «а»: ID можно вписать только в пустое поле,
    один раз; уже заданный — read-only навсегда, проверка на сервере."""
    print("\npub_ui.set_channel_uc: вписать ID каналу, у которого его не было")
    c = fresh()
    cid = U.create_channel(c, "dev", "", "Канал без ID")
    U.set_channel_uc(c, "dev", cid, "UCK8sQmJBp8GCxrOtXWBpyEA")
    row = c.execute("SELECT uc FROM pub_channels WHERE id=?", (cid,)).fetchone()
    check("ID записан", row["uc"], "UCK8sQmJBp8GCxrOtXWBpyEA")

    try:
        U.set_channel_uc(c, "dev", cid, "UCAAAAAAAAAAAAAAAAAAAAAA")
        FAILS.append("смена уже заданного ID должна была отклониться")
        print("  FAIL смена уже заданного ID должна была отклониться")
    except U.ChannelError:
        print("  OK   уже заданный ID менять нельзя")

    try:
        U.set_channel_uc(c, "dev", cid, "")
        FAILS.append("стереть уже заданный ID через set_channel_uc не должно получиться")
        print("  FAIL стереть уже заданный ID через set_channel_uc не должно получиться")
    except U.ChannelError:
        print("  OK   и стереть уже заданный ID тоже нельзя")

    cid2 = U.create_channel(c, "dev", "", "Второй канал без ID")
    try:
        U.set_channel_uc(c, "dev", cid2, "UCK8sQmJBp8GCxrOtXWBpyEA")   # занят первым
        FAILS.append("ID, занятый другим каналом, должен был упасть на UNIQUE")
        print("  FAIL ID, занятый другим каналом, должен был упасть на UNIQUE")
    except Exception as exc:
        check("чужой занятый ID — по-прежнему дубль", "UNIQUE" in str(exc), True)

    try:
        U.set_channel_uc(c, "dev", cid2, "не похоже на UC")
        FAILS.append("кривой формат ID должен был отклониться")
        print("  FAIL кривой формат ID должен был отклониться")
    except U.ChannelError:
        print("  OK   кривой формат ID по-прежнему отклоняется")

    try:
        U.set_channel_uc(c, "dev", 999999, "UCBBBBBBBBBBBBBBBBBBBBBB")
        FAILS.append("несуществующий канал должен был вернуть понятную ошибку")
        print("  FAIL несуществующий канал должен был вернуть понятную ошибку")
    except U.ChannelError:
        print("  OK   несуществующий канал — понятная ошибка, а не падение")

    print("\n  code review (итерация 2, Issue 2): запись условна на самом UPDATE, "
          "не на предварительном SELECT")
    cid3 = U.create_channel(c, "dev", "", "Третий канал без ID")
    # Имитация гонки: ID оказался вписан в обход предварительной проверки set_channel_uc
    # (например, другим одновременным запросом) — напрямую через SQL, не через саму
    # функцию. Раньше set_channel_uc делала SELECT uc, затем безусловный UPDATE — оба
    # одновременных вызова прошли бы SELECT и второй переписал бы первый. Теперь решает
    # только сам UPDATE (WHERE ... AND uc IS NULL), поэтому даже без обращения к
    # set_channel_uc до этого — попытка записать поверх уже стоящего значения отклоняется.
    c.execute("UPDATE pub_channels SET uc=? WHERE id=?",
              ("UCDDDDDDDDDDDDDDDDDDDDDD", cid3))
    c.commit()
    try:
        U.set_channel_uc(c, "dev", cid3, "UCEEEEEEEEEEEEEEEEEEEEEE")
        FAILS.append("условная запись должна была отклонить попытку перезаписать ID")
        print("  FAIL условная запись должна была отклонить попытку перезаписать ID")
    except U.ChannelError:
        print("  OK   условная запись ничего не поменяла")
    row3 = c.execute("SELECT uc FROM pub_channels WHERE id=?", (cid3,)).fetchone()
    check("значение, вписанное в обход проверки, осталось нетронутым",
          row3["uc"], "UCDDDDDDDDDDDDDDDDDDDDDD")


# ─────────────────────────── 4. движок ───────────────────────────

def add_channel(c, name, uc, status="active", lead_days=0):
    c.execute("INSERT INTO pub_channels(tenant_id,uc,name,status,lead_days,created,updated) "
              "VALUES('dev',?,?,?,?,?,?)", (uc, name, status, lead_days, NOW, NOW))
    return c.execute("SELECT id FROM pub_channels WHERE name=?", (name,)).fetchone()["id"]


def add_plan_item(c, channel_id, slot_at, donor="https://youtu.be/demo"):
    c.execute("INSERT INTO pub_plan_items(tenant_id,channel_id,donor_url,slot_at,created,updated) "
              "VALUES('dev',?,?,?,?,?)", (channel_id, donor, slot_at, NOW, NOW))
    return c.execute("SELECT id FROM pub_plan_items WHERE channel_id=? AND donor_url=?",
                     (channel_id, donor)).fetchone()["id"]


def check_engine():
    print("\nДвижок: выпуск на канале без ID не заводится")
    c = fresh()
    no_id = add_channel(c, "Без ID", None)
    with_id = add_channel(c, "С ID", "UCK8sQmJBp8GCxrOtXWBpyEA")
    item_no_id = add_plan_item(c, no_id, NOW)
    item_with_id = add_plan_item(c, with_id, NOW)
    # Чужая заметка на строке, которой ID не мешал никогда, — контроль для Issue 4:
    # очистка заметки не должна задевать ничего, кроме ровно NO_UC_REASON.
    c.execute("UPDATE pub_plan_items SET note=? WHERE id=?",
             ("личная заметка редактора", item_with_id))
    c.commit()

    made = E.materialize(c, NOW)
    c.commit()
    check("выпуск завёлся только для канала с ID", len(made), 1)
    note_with_id = c.execute("SELECT note FROM pub_plan_items WHERE id=?",
                             (item_with_id,)).fetchone()["note"]
    check("чужая заметка не тронута при заведении обычного выпуска",
          note_with_id, "личная заметка редактора")

    row_no_id = c.execute("SELECT status, note FROM pub_plan_items WHERE id=?",
                          (item_no_id,)).fetchone()
    check("строка канала без ID осталась ждать (status='planned')",
          row_no_id["status"], "planned")
    check("причина записана понятными словами", row_no_id["note"], E.NO_UC_REASON)
    check("выпуск для канала без ID не создан",
          c.execute("SELECT COUNT(*) FROM pub_releases WHERE channel_id=?",
                    (no_id,)).fetchone()[0], 0)

    row_with_id = c.execute("SELECT status FROM pub_plan_items WHERE id=?",
                            (item_with_id,)).fetchone()
    check("а другой канал в это время работает как обычно (не заблокирован)",
          row_with_id["status"], "frozen")
    check("выпуск для канала с ID создан",
          c.execute("SELECT COUNT(*) FROM pub_releases WHERE channel_id=?",
                    (with_id,)).fetchone()[0], 1)

    print("\n  повторный тик не плодит выпуски и не падает")
    made2 = E.materialize(c, NOW + 10)
    c.commit()
    check("новых выпусков нет", made2, [])
    check("движок не упал на канале без ID", True, True)

    print("\n  ID у канала появился — следующий тик заводит выпуск как обычно")
    c.execute("UPDATE pub_channels SET uc=? WHERE id=?",
              ("UCAAAAAAAAAAAAAAAAAAAAAA", no_id))
    c.commit()
    made3 = E.materialize(c, NOW + 20)
    c.commit()
    check("выпуск на канале, у которого только что появился ID, заведён", len(made3), 1)
    row_now_with_id = c.execute("SELECT status, note FROM pub_plan_items WHERE id=?",
                                (item_no_id,)).fetchone()
    check("строка плана взята в работу", row_now_with_id["status"], "frozen")
    # Code review (итерация 2, Issue 4): устаревшая причина «ID не задан» не должна
    # оставаться в заметке строки, когда выпуск уже заведён — иначе будущий показ этой
    # заметки соврёт, что выпуск не заводится, хотя он уже в работе.
    check("устаревшая причина «ID не задан» стёрлась при заведении выпуска",
          row_now_with_id["note"], None)


# ─────────────────────────── 5. интерфейс ───────────────────────────

def check_interface_static():
    """Поле ID необязательное, плашка «ID пустой» на месте, форма настроек канала в
    пульте показывает ID (вопрос 1), кабинет не тронут (вопрос 1), «6.1» нет в тексте
    страницы (вопрос 3) — без стенда, по исходникам."""
    print("\nИнтерфейс (статически, по исходникам)")

    index_html = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
    pub_ui_src = open(os.path.join(BASE, "server", "pub_ui.py"), encoding="utf-8").read()

    uc_field_line = next((ln for ln in index_html.splitlines() if 'id="uc"' in ln), "")
    check("пульт: поле ID канала в форме заведения найдено", bool(uc_field_line), True)
    check("пульт: поле ID канала — без required", "required" in uc_field_line, False)
    check("пульт: рядом есть подсказка про необязательность",
          "Необязательно" in index_html, True)
    check("пульт: список каналов показывает плашку «ID пустой» по has_uc",
          "has_uc" in index_html and "ID пустой</span>" in index_html, True)
    check("пульт: шапка карточки канала тоже показывает «ID пустой»",
          "ID пустой — выпуски на этом канале" in index_html, True)

    # Дополнение 16.09, вопрос 1 (Issue 1 ревью, итерация 3): подсказки обязаны называть
    # реально существующий путь — вкладку «Настройки» канала, а не просто говорить
    # "задайте позже", не уточняя где. Ищем блок подсказки под полем заведения канала
    # (многострочный, поэтому берём диапазон, а не одну строку) и плашку в шапке.
    newch_note = re.search(r'<div class="note muted">Необязательно.*?</div>',
                            index_html, re.S)
    check("пульт: подсказка при заведении канала найдена", bool(newch_note), True)
    if newch_note:
        check("пульт: подсказка при заведении называет вкладку «Настройки»",
              "Настройки" in newch_note.group(0), True)

    header_plate = re.search(r'ID пустой — выпуски на этом канале.*?</span>',
                              index_html, re.S)
    check("пульт: плашка в шапке канала найдена", bool(header_plate), True)
    if header_plate:
        check("пульт: плашка в шапке канала называет вкладку «Настройки»",
              "Настройки" in header_plate.group(0), True)

    # Вопрос 1: форма настроек канала в пульте знает про ID — редактируемое поле для
    # пустого uc, и read-only отображение для уже заданного.
    check("пульт: форма настроек канала — редактируемое поле для пустого ID",
          'id="f-uc"' in index_html, True)
    check("пульт: форма настроек канала сохраняет ID отдельным запросом",
          '/ui/channels/${c.id}/uc' in index_html, True)
    # Обе ветки условного рендера ID (уже задан / ещё пуст) должны реально существовать
    # в разметке, а не только редактируемая: read-only показ обязан объяснять, почему
    # поле нельзя менять, иначе выглядит как забытое ограничение, а не решение владельца.
    check("пульт: уже заданный ID показан read-only, а не полем ввода",
          '<span class="mono">${esc(c.uc)}</span>' in index_html, True)
    check("пульт: read-only ID сопровождён пояснением «не меняется после создания»",
          "Не меняется после создания" in index_html, True)

    # Вопрос 3: ссылка на пункт ТЗ — только в комментариях кода, не в видимом тексте
    # страницы. Комментарии JS начинаются с "//" — их и весь остальной код в тестах
    # видимым текстом не считаем; проверяем ровно то, что сказано в дополнении: явные
    # подсказки/note-блоки рядом с полем ID не содержат "6.1".
    note_lines = [ln for ln in index_html.splitlines()
                  if "note muted" in ln and "необязательн" in ln.lower()]
    check("подсказка про необязательность ID не содержит ссылку на «6.1»",
          any("6.1" in ln for ln in note_lines), False)

    admin_uc_line = next((ln for ln in pub_ui_src.splitlines() if "name=uc" in ln), "")
    check("кабинет: форма заведения канала найдена", bool(admin_uc_line), True)
    check("кабинет: поле ID канала — без required", "required" in admin_uc_line, False)
    check("кабинет: рядом есть подсказка про необязательность",
          "необязательно" in pub_ui_src, True)

    # Вопрос 1: кабинет (форма настроек, /api/channels/{id}/edit) НЕ тронут — своего
    # поля ID там как не было, так и нет.
    edit_form = re.search(r'action="/api/channels/\{row\[.id.\]\}/edit".*?</form>',
                          pub_ui_src, re.S)
    check("кабинет: форма настроек канала найдена в исходнике", bool(edit_form), True)
    if edit_form:
        check("кабинет: форма настроек канала по-прежнему не знает про ID (не тронута)",
              "uc" in re.findall(r'name=(\w+)', edit_form.group(0)), False)

    app_py = open(os.path.join(BASE, "web", "app.py"), encoding="utf-8").read()
    check("список каналов пульта отдаёт has_uc — фронту есть на чём решать про плашку",
          '"has_uc"' in app_py, True)
    check("пульт: роут для вписывания ID заведён",
          '"/ui/channels/{cid:int}/uc"' in app_py, True)


class _Client:
    """Тот же минимальный HTTP-клиент, что и в `test_web_pult.py` / `test_fastgen_usage.py`."""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def go(self, path, payload=None, method=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(PULT + urllib.parse.quote(path, safe="/?&="),
                                     data=data,
                                     method=method or ("POST" if data is not None else "GET"))
        req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw.startswith(("{", "[")) else raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(raw or "{}")
            except ValueError:
                return exc.code, raw[:200]
        except urllib.error.URLError as exc:
            raise SystemExit(f"пульт {PULT} не отвечает: {exc.reason}")


def db_dev():
    c = sqlite3.connect(DEV_DB, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def drop_channel_by_name(name: str) -> None:
    """Убрать за собой тестовый канал, заведённый через живой /ui/channels."""
    c = db_dev()
    try:
        c.execute("DELETE FROM pub_channels WHERE name=?", (name,))
        c.commit()
    finally:
        c.close()


def check_pult_channels():
    """Сквозная проверка через поднятый пульт: то же самое, что выше, но через
    настоящий HTTP-слой (`web/app.py:ChannelIn`, `ui_channels`, `ui_channel_set_uc`),
    а не напрямую в базу.

    Требует стенда (см. docstring модуля). Если стенд не поднят — проверка честно
    пропускается с пометкой, а не выдаёт себя за пройденную."""
    print("\nПульт по HTTP: /ui/channels видит канал без ID (нужен поднятый стенд)")
    me = _Client()
    try:
        code, body = me.go("/ui/login", {"login": LOGIN, "password": PASSWORD})
    except SystemExit as exc:
        SKIPPED.append(f"сквозная часть по HTTP: {exc}")
        print(f"  ПРОПУЩЕНО: {exc}")
        return
    if code != 200:
        SKIPPED.append(f"сквозная часть по HTTP: вход не удался ({code})")
        print(f"  ПРОПУЩЕНО: вход не удался ({code}): {body}")
        return

    mark = f"QA-без-ID-{NOW:.0f}"
    n1, n2 = f"{mark}-раз", f"{mark}-два"
    n3 = f"{mark}-с-ID"
    try:
        code, body = me.go("/ui/channels", {"uc": "", "name": n1})
        check("канал без ID заведён через HTTP (uc пустой строкой)", code, 200)
        cid1 = (body or {}).get("id")

        code, body = me.go("/ui/channels", {"uc": "", "name": n2})
        check("второй канал без ID тоже заведён — не конфликтует с первым", code, 200)
        cid2 = (body or {}).get("id")
        check("это разные каналы", cid2 != cid1, True)

        code, lst = me.go("/ui/channels")
        by_id = {c["id"]: c for c in (lst or {}).get("channels", [])}
        check("оба новых канала видны в списке", {cid1, cid2} <= set(by_id), True)
        if cid1 in by_id:
            check("has_uc=false у канала без ID", by_id[cid1]["has_uc"], False)

        code, card = me.go(f"/ui/channels/{cid1}")
        check("карточка канала отвечает", code, 200)
        check("uc в карточке — null, а не пустая строка",
              (card or {}).get("channel", {}).get("uc"), None)

        # Вопрос 1: вписать ID через новый роут /ui/channels/{cid}/uc.
        code, body = me.go(f"/ui/channels/{cid1}/uc", {"uc": "UCAAAAAAAAAAAAAAAAAAAAAA"})
        check("ID вписан через HTTP", code, 200)
        code, card = me.go(f"/ui/channels/{cid1}")
        check("ID теперь виден в карточке",
              (card or {}).get("channel", {}).get("uc"), "UCAAAAAAAAAAAAAAAAAAAAAA")

        code, body = me.go(f"/ui/channels/{cid1}/uc", {"uc": "UCCCCCCCCCCCCCCCCCCCCCCC"})
        check("а повторная попытка сменить уже заданный ID отклонена (400)", code, 400)

        code, body = me.go(f"/ui/channels/{cid2}/uc", {"uc": "UCAAAAAAAAAAAAAAAAAAAAAA"})
        check("ID, занятый другим каналом, отклонён (409)", code, 409)

        code, body = me.go(f"/ui/channels/{cid2}/uc", {"uc": "не похоже на UC"})
        check("кривой формат ID отклонён (400)", code, 400)

        # Регрессия на §4.2/Дополнение 16.09: у ID свой роут (/ui/channels/{cid}/uc),
        # а обычное сохранение настроек (/ui/channels/{cid}) поле "uc" не читает
        # (`read_channel_form`, `update_channel`) — даже если фронт вдруг его пришлёт.
        code, body = me.go(f"/ui/channels/{cid1}", {"name": n1, "uc": "UCFFFFFFFFFFFFFFFFFFFFFF"})
        check("сохранение настроек канала с уже заданным ID прошло", code, 200)
        code, card = me.go(f"/ui/channels/{cid1}")
        check("сохранение настроек не подменило уже вписанный ID",
              (card or {}).get("channel", {}).get("uc"), "UCAAAAAAAAAAAAAAAAAAAAAA")

        code, body = me.go(f"/ui/channels/{cid2}", {"name": n2, "uc": "UCGGGGGGGGGGGGGGGGGGGGGG"})
        check("сохранение настроек канала без ID прошло", code, 200)
        code, card = me.go(f"/ui/channels/{cid2}")
        check("сохранение настроек не завело ID через обычную форму настроек",
              (card or {}).get("channel", {}).get("uc"), None)

        code, body = me.go("/ui/channels", {"uc": "UCBBBBBBBBBBBBBBBBBBBBBB", "name": n3})
        check("канал с настоящим ID заводится как раньше", code, 200)
        code, body = me.go("/ui/channels", {"uc": "UCBBBBBBBBBBBBBBBBBBBBBB", "name": n3 + "2"})
        check("а повтор такого же ID при заведении отклонён (409)", code, 409)
    finally:
        drop_channel_by_name(n1)
        drop_channel_by_name(n2)
        drop_channel_by_name(n3)


def main() -> int:
    check_migration()
    check_migration_without_row_factory()
    check_migration_type_preserved()
    check_migration_preserves_autoincrement()
    check_create()
    check_import()
    check_set_channel_uc()
    check_engine()
    check_interface_static()
    check_pult_channels()

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    if SKIPPED and not NO_STAND_OK:
        print("СТЕНД НЕ ПОДНЯТ — проверки не выполнены:")
        for s_ in SKIPPED:
            print("  -", s_)
        print("Это не успех: без стенда не проверено главное — что обычная форма настроек"
              " канала не подменяет и не заводит идентификатор в обход. Поднимите стенд"
              " (см. заголовок файла) либо объявите пропуск осознанно:"
              " VS_PULT_TEST_NO_STAND=1.")
        return 2
    print("все проверки прошли" + (" (стенд пропущен осознанно)" if SKIPPED else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
