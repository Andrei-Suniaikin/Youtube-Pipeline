#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Опись профилей AdsPower, Фаза 1 (task-spec.md, profiles-2026-09-23): миграция схемы
`pub_profiles`.

Проверяется здесь:
  1) на базе с ОСТРЫМ старым ограничением `UNIQUE(tenant_id,serial)` (плюс существующие
     строки с разными id, включая «дыру» — как в `_migrate_channel_uc_optional`) миграция
     переводит ограничение на `UNIQUE(tenant_id,default_host_id,serial)`, не теряя строк,
     не перенумеровывая id и не откатывая `sqlite_sequence` назад;
  2) семь новых колонок §6.1 (`adspower_user_id`, `group_name`, `remark`,
     `last_open_time`, `active_now`, `last_seen`, `missing_since`) появляются на старой
     базе и уже объявлены на новой;
  3) после миграции два профиля с одинаковым `serial`, но разными `default_host_id`,
     заводятся без конфликта (то, ради чего затевалась миграция);
  4) повторный вызов `ensure_pub_schema` — no-op: колонки, строки, индексы, счётчик не
     меняются во второй раз;
  5) мутационная проверка: если восстановление `sqlite_sequence` отключить, тест должен
     упасть — проверяется прямым вызовом «сломанной» пересборки без восстановления
     счётчика (не трогая сам pub_schema.py, а через ручную имитацию того же сценария).

Сеть не требуется, база — в памяти:

    VS_TEST_NO_NET=1 .venv/bin/python tests/test_pub_profiles_host_serial_migration.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

from pub_schema import ensure_pub_schema, _migrate_profiles_host_serial  # noqa: E402

FAILS = []

NEW_COLUMNS = ("adspower_user_id", "group_name", "remark", "last_open_time",
               "active_now", "last_seen", "missing_since")

# Колонки исходной («старой») схемы — до ALTER TABLE ADD COLUMN и до пересборки.
# Ревьюер (Issue 1b, итерация 1): пересборка не должна тихо потерять NOT NULL/DEFAULT/PK
# ни у одной из них (например, tenant_id/serial), даже если состав NEW_COLUMNS не менялся.
ORIGINAL_COLUMNS = ("id", "tenant_id", "serial", "title", "default_host_id",
                     "status", "note", "created")


def pragma_full(c):
    """Тип/notnull/dflt_value/pk для каждой колонки pub_profiles — позиционно из PRAGMA
    (см. комментарий про row_factory в `_migrate_channel_uc_optional`), не только имена."""
    return {r[1]: {"type": r[2], "notnull": r[3], "dflt_value": r[4], "pk": r[5]}
            for r in c.execute("PRAGMA table_info(pub_profiles)").fetchall()}


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


OLD_SCHEMA_SQL = """
  CREATE TABLE pub_hosts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
    code TEXT NOT NULL, title TEXT, adspower_url TEXT,
    status TEXT NOT NULL DEFAULT 'active', note TEXT, created REAL NOT NULL,
    UNIQUE(tenant_id,code)
  );
  CREATE TABLE pub_profiles(
    id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
    serial TEXT NOT NULL, title TEXT, default_host_id INTEGER,
    status TEXT NOT NULL DEFAULT 'active', note TEXT, created REAL NOT NULL,
    UNIQUE(tenant_id,serial)
  );
  CREATE INDEX idx_pub_profiles_default_host_id ON pub_profiles(default_host_id);
"""

NOW = 1_800_000_000.0


def old_style_db() -> sqlite3.Connection:
    """База «до описи AdsPower»: pub_profiles с острым старым UNIQUE(tenant_id,serial),
    как заводит сегодняшний `dev/make_db.py`. Три профиля с разными id, включая «дыру» в
    id (профиль 2 удалён вручную) — тот же приём, что и в `test_channel_without_id.py`."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(OLD_SCHEMA_SQL)
    c.execute("INSERT INTO pub_hosts(tenant_id,code,title,adspower_url,created) "
              "VALUES('dev','minsk','Минская машина','http://127.0.0.1:50325',?)", (NOW,))
    c.execute("INSERT INTO pub_hosts(tenant_id,code,title,adspower_url,created) "
              "VALUES('dev','gdansk','Гданьская машина','http://127.0.0.1:50325',?)", (NOW,))
    minsk = c.execute("SELECT id FROM pub_hosts WHERE code='minsk'").fetchone()["id"]
    c.execute("INSERT INTO pub_profiles(tenant_id,serial,title,default_host_id,created) "
              "VALUES('dev','46','Профиль 46',?,?)", (minsk, NOW))
    c.execute("INSERT INTO pub_profiles(tenant_id,serial,title,default_host_id,created) "
              "VALUES('dev','47','Профиль 47',?,?)", (minsk, NOW))
    c.execute("INSERT INTO pub_profiles(tenant_id,serial,title,default_host_id,created) "
              "VALUES('dev','48','Профиль 48',?,?)", (minsk, NOW))
    c.commit()
    return c


def snapshot(c):
    cols = {r["name"]: dict(r) for r in c.execute("PRAGMA table_info(pub_profiles)")}
    rows = [dict(r) for r in c.execute("SELECT * FROM pub_profiles ORDER BY id")]
    idx = sorted(r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='pub_profiles'"))
    return cols, rows, idx


def unique_constraint_sql(c) -> str:
    row = c.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND tbl_name='pub_profiles'"
    ).fetchone()
    return row[0] if row else ""


def check_migration_constraint():
    print("Миграция: заменить UNIQUE(tenant_id,serial) на "
          "UNIQUE(tenant_id,default_host_id,serial)")
    c = old_style_db()
    before_cols, before_rows, before_idx = snapshot(c)
    pragma_before = pragma_full(c)   # снято ДО ALTER ADD COLUMN и ДО пересборки
    check("до миграции — старое ограничение",
          "UNIQUE(tenant_id,serial)" in unique_constraint_sql(c), True)
    check("три профиля заведены до миграции", len(before_rows), 3)
    check("до миграции — именованный индекс на месте (idx_pub_profiles_default_host_id)",
          "idx_pub_profiles_default_host_id" in before_idx, True)

    # Дыра в id: профиль 47 удалён вручную, максимальный id (профиль 48) остаётся.
    id_47 = next(r["id"] for r in before_rows if r["serial"] == "47")
    c.execute("DELETE FROM pub_profiles WHERE id=?", (id_47,))
    c.commit()
    ids_before = [r["id"] for r in c.execute(
        "SELECT id FROM pub_profiles ORDER BY id").fetchall()]
    max_id_before = max(ids_before)

    for _col, _sql in (
        ("adspower_user_id", "TEXT"), ("group_name", "TEXT"), ("remark", "TEXT"),
        ("last_open_time", "TEXT"), ("active_now", "INTEGER NOT NULL DEFAULT 0"),
        ("last_seen", "REAL"), ("missing_since", "REAL"),
    ):
        c.execute(f"ALTER TABLE pub_profiles ADD COLUMN {_col} {_sql}")
    c.commit()

    _migrate_profiles_host_serial(c)
    c.commit()

    after_cols, after_rows, after_idx = snapshot(c)
    check("после миграции — новое составное ограничение",
          "UNIQUE(tenant_id,default_host_id,serial)" in unique_constraint_sql(c), True)
    # Ревьюер (Issue 2, итерация 1): было тавтологией (`check(..., True, True)` всегда
    # печатает OK). Настоящая проверка — текст CREATE TABLE больше не содержит старую
    # форму ограничения как отдельную подстроку. "UNIQUE(tenant_id,serial)" НЕ является
    # подстрокой "UNIQUE(tenant_id,default_host_id,serial)" (между запятыми стоит
    # "default_host_id", а не "serial"), поэтому проверка реальна и падает, если старое
    # ограничение осталось рядом с новым или вместо него.
    check("старого ограничения UNIQUE(tenant_id,serial) в тексте CREATE TABLE больше нет",
          "UNIQUE(tenant_id,serial)" in unique_constraint_sql(c), False)
    check("id строк не изменились", [r["id"] for r in after_rows], ids_before)
    check("значения строк (кроме новых NULL-колонок) совпали",
          [{k: v for k, v in r.items() if k not in NEW_COLUMNS} for r in after_rows],
          [{k: v for k, v in r.items() if k not in NEW_COLUMNS} for r in before_rows
           if r["id"] != id_47])
    check("новые семь колонок на месте", set(NEW_COLUMNS) <= set(after_cols), True)
    check("именованные индексы не потерялись", set(before_idx) <= set(after_idx), True)

    # Ревьюер (Issue 1b, итерация 1): полное сравнение PRAGMA table_info для ИСХОДНЫХ
    # колонок (не только присутствие имени) — тип, NOT NULL, DEFAULT, PRIMARY KEY. Раньше
    # snapshot() сравнивался только между повторными вызовами уже ПОСЛЕ миграции, поэтому
    # потеря NOT NULL у tenant_id/serial в col_def пересборки не ловилась ничем.
    pragma_after = pragma_full(c)
    for _name in ORIGINAL_COLUMNS:
        check(f"колонка «{_name}»: тип/NOT NULL/DEFAULT/PK не изменились пересборкой",
              pragma_after.get(_name), pragma_before.get(_name))

    print("\n  два профиля с одинаковым serial на разных машинах — не конфликт")
    gdansk = c.execute("SELECT id FROM pub_hosts WHERE code='gdansk'").fetchone()["id"]
    c.execute("INSERT INTO pub_profiles(tenant_id,serial,title,default_host_id,created) "
              "VALUES('dev','46','Профиль 46 на другой машине',?,?)", (gdansk, NOW))
    c.commit()
    rows_46 = c.execute("SELECT default_host_id FROM pub_profiles WHERE serial='46' "
                        "ORDER BY default_host_id").fetchall()
    check("оба профиля 46 существуют, каждый на своей машине", len(rows_46), 2)

    print("\n  а вот повтор (машина, serial) — по-прежнему дубль")
    try:
        c.execute("INSERT INTO pub_profiles(tenant_id,serial,title,default_host_id,created) "
                  "VALUES('dev','46','Дубль профиля 46 на минской машине',?,?)",
                  (minsk_id(c), NOW))
        c.commit()
        FAILS.append("повтор (машина, serial) должен был упасть на UNIQUE")
        print("  FAIL повтор (машина, serial) должен был упасть на UNIQUE")
    except sqlite3.IntegrityError as exc:
        check("конфликт составного ключа пойман", "UNIQUE" in str(exc), True)

    print("\n  максимальный id не переиспользован после пересборки (дыра в id профиля 47)")
    new_id = c.execute(
        "INSERT INTO pub_profiles(tenant_id,serial,title,default_host_id,created) "
        "VALUES('dev','49','Профиль 49',?,?)", (minsk_id(c), NOW)).lastrowid
    check("новый профиль получил id больше прежнего максимума", new_id > max_id_before, True)

    print("\n  повторный вызов миграции — no-op (идемпотентность)")
    _migrate_profiles_host_serial(c)
    c.commit()
    cols2, rows2, idx2 = snapshot(c)
    cols3, rows3, idx3 = cols2, rows2, idx2
    _migrate_profiles_host_serial(c)   # третий раз подряд — тоже ничего не меняет
    c.commit()
    cols4, rows4, idx4 = snapshot(c)
    check("колонки не изменились", cols4, cols3)
    check("строки не изменились", rows4, rows3)
    check("индексы не изменились", idx4, idx3)

    print("\n  повторный вызов реально пропускает пересборку (короткое замыкание по "
          "sqlite_master), а не просто выдаёт тот же результат другим путём")
    executed = []
    c.set_trace_callback(executed.append)
    try:
        _migrate_profiles_host_serial(c)
        c.commit()
    finally:
        c.set_trace_callback(None)
    check("пересборка не запускалась повторно (нет CREATE TABLE pub_profiles_new)",
          any("pub_profiles_new" in sql for sql in executed), False)


def minsk_id(c):
    return c.execute("SELECT id FROM pub_hosts WHERE code='minsk'").fetchone()["id"]


def check_ensure_pub_schema_full_path():
    """Полный штатный путь: не приватный помощник, а ensure_pub_schema целиком, — и на
    старой базе, и повторный вызов на уже новой."""
    print("\nensure_pub_schema целиком: старая база мигрирует, повтор — no-op")
    c = old_style_db()
    ensure_pub_schema(c)
    c.commit()
    check("после ensure_pub_schema — новое ограничение",
          "UNIQUE(tenant_id,default_host_id,serial)" in unique_constraint_sql(c), True)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(pub_profiles)")}
    check("все семь новых колонок присутствуют", set(NEW_COLUMNS) <= cols, True)
    rows = [dict(r) for r in c.execute("SELECT * FROM pub_profiles ORDER BY id")]
    check("три исходных профиля на месте", len(rows), 3)
    check("id профилей не перенумерованы (1,2,3)", [r["id"] for r in rows], [1, 2, 3])

    before_cols, before_rows, before_idx = snapshot(c)
    ensure_pub_schema(c)   # второй вызов на уже новой базе
    c.commit()
    after_cols, after_rows, after_idx = snapshot(c)
    check("повторный ensure_pub_schema не меняет колонки", after_cols, before_cols)
    check("повторный ensure_pub_schema не меняет строки", after_rows, before_rows)
    check("повторный ensure_pub_schema не меняет индексы", after_idx, before_idx)


def check_fresh_db_already_new_form():
    """Новая база (через fresh()) уже объявлена с новым составным UNIQUE — пересборка
    ей не нужна, ensure_pub_schema это не путает с миграцией."""
    print("\nНовая база уже в новой форме — ensure_pub_schema её не трогает")
    c = fresh()
    check("новое ограничение уже объявлено в DDL",
          "UNIQUE(tenant_id,default_host_id,serial)" in unique_constraint_sql(c), True)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(pub_profiles)")}
    check("семь новых колонок объявлены сразу", set(NEW_COLUMNS) <= cols, True)
    ensure_pub_schema(c)   # второй вызов — идемпотентен, не падает
    c.commit()
    check("после повторного вызова — та же форма",
          "UNIQUE(tenant_id,default_host_id,serial)" in unique_constraint_sql(c), True)

    hid = c.execute(
        "INSERT INTO pub_hosts(tenant_id,code,title,adspower_url,created) "
        "VALUES('dev','minsk','Минская машина','http://127.0.0.1:50325',?)", (NOW,)
    ).lastrowid
    hid2 = c.execute(
        "INSERT INTO pub_hosts(tenant_id,code,title,adspower_url,created) "
        "VALUES('dev','gdansk','Гданьская машина','http://127.0.0.1:50325',?)", (NOW,)
    ).lastrowid
    c.execute("INSERT INTO pub_profiles(tenant_id,serial,default_host_id,created) "
              "VALUES('dev','46',?,?)", (hid, NOW))
    c.execute("INSERT INTO pub_profiles(tenant_id,serial,default_host_id,created) "
              "VALUES('dev','46',?,?)", (hid2, NOW))
    c.commit()
    n = c.execute("SELECT COUNT(*) n FROM pub_profiles WHERE serial='46'").fetchone()["n"]
    check("одинаковый serial на двух машинах — обе строки заведены", n, 2)


def check_mutation_sequence_restore():
    """Мутационная проверка (обязательна по task-spec.md §11): если восстановление
    sqlite_sequence не выполнить, тест обязан это поймать. Здесь имитируется «сломанная»
    пересборка без восстановления счётчика — тем же способом, каким это могло бы
    случиться при регрессии в _migrate_profiles_host_serial — и проверяется, что
    сравнение с ожидаемым поведением падает, если починку отключить."""
    print("\nМутация: без восстановления sqlite_sequence новый id переиспользовался бы")
    c = old_style_db()
    id_48 = c.execute("SELECT id FROM pub_profiles WHERE serial='48'").fetchone()["id"]
    c.execute("DELETE FROM pub_profiles WHERE id=?", (id_48,))
    c.commit()

    # «Сломанная» пересборка: то же самое, что делает _migrate_profiles_host_serial, но
    # БЕЗ восстановления sqlite_sequence — так вела бы себя реализация без починки.
    c.execute("CREATE TABLE pub_profiles_broken AS SELECT * FROM pub_profiles")
    c.execute("DROP TABLE pub_profiles")
    c.execute("ALTER TABLE pub_profiles_broken RENAME TO pub_profiles")
    c.commit()
    broken_next_id = c.execute(
        "INSERT INTO pub_profiles(tenant_id,serial,created) "
        "VALUES('dev','zz',?)", (NOW,)).lastrowid
    check("без починки счётчика id действительно переиспользовался бы (демонстрация дефекта)",
          broken_next_id <= id_48, True)

    print("  а настоящая _migrate_profiles_host_serial чинит счётчик — id не переиспользуется")
    c2 = old_style_db()
    c2.execute("DELETE FROM pub_profiles WHERE id=?", (
        c2.execute("SELECT id FROM pub_profiles WHERE serial='48'").fetchone()["id"],))
    c2.commit()
    for _col, _sql in (
        ("adspower_user_id", "TEXT"), ("group_name", "TEXT"), ("remark", "TEXT"),
        ("last_open_time", "TEXT"), ("active_now", "INTEGER NOT NULL DEFAULT 0"),
        ("last_seen", "REAL"), ("missing_since", "REAL"),
    ):
        c2.execute(f"ALTER TABLE pub_profiles ADD COLUMN {_col} {_sql}")
    c2.commit()
    _migrate_profiles_host_serial(c2)
    c2.commit()
    fixed_next_id = c2.execute(
        "INSERT INTO pub_profiles(tenant_id,serial,created) "
        "VALUES('dev','zz',?)", (NOW,)).lastrowid
    check("с починкой id профиля 48 не переиспользован", fixed_next_id > id_48, True)


def main() -> int:
    check_migration_constraint()
    check_ensure_pub_schema_full_path()
    check_fresh_db_already_new_form()
    check_mutation_sequence_restore()

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
