#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Схема контура публикаций: каналы → строки плана → выпуски.

Вынесена из main.py отдельным модулем по двум причинам: правка в 14-тысячном файле
остаётся в две строки, а сами гарантии (уникальный UC, один ролик в слот, один выпуск
на версию строки) проверяются тестами без запуска кабинета.

Внешних ключей в этой базе нет — PRAGMA foreign_keys нигде не включается, — поэтому
всё, что защищает от дублей, держится на уникальных индексах ниже.
"""

from __future__ import annotations

import sqlite3


# Колонки, которые пресет канала перекрывает. Язык, ID, название, хэндл, профиль
# AdsPower, статус и заметка сюда не входят: они у каждого канала свои (решение
# владельца 18.09.2026).
CHANNEL_PRESET_COLUMNS = ("tz", "schedule", "horizon_days", "lead_days", "deferred",
                          "deferred_limit", "preset_vsgen_id", "host_id", "niche",
                          "collaborator")


def _channel_live_view(c) -> None:
    """Вид «канал с учётом его пресета»: значение пресета сильнее, NULL — берём своё.

    Читатели меняют только имя таблицы: имена и порядок колонок те же, `dict(row)`
    работает как раньше. Это единственный способ разрешить ссылку и там, где колонка
    участвует в отборе прямо в SQL (`pub_engine.due_items` фильтрует по `lead_days`,
    список каналов — по `niche`): питоновская подмена туда не достаёт — строка, не
    прошедшая WHERE, до Python не доедет.

    Запись через вид SQLite запрещает — и это хорошо: настройки по-прежнему пишутся
    только в `pub_channels`, случайная запись падает громко, а не мимо.
    """
    cols = {r[1] for r in c.execute("PRAGMA table_info(pub_channels)")}
    own = [x for x in cols if x not in CHANNEL_PRESET_COLUMNS]
    merged = [f"COALESCE(p.{x}, ch.{x}) {x}" for x in CHANNEL_PRESET_COLUMNS if x in cols]
    fields = ", ".join([f"ch.{x}" for x in sorted(own)] + merged + ["p.name preset_channel_name"])
    c.execute("DROP VIEW IF EXISTS pub_channels_live")
    c.execute(f"CREATE VIEW pub_channels_live AS SELECT {fields} "
              "FROM pub_channels ch "
              "LEFT JOIN pub_channel_presets p "
              # tenant_id в условии — та же защита от чужой ссылки, что и в
              # `preset_links.resolve`: id чужого тенанта не должен ничего перекрывать.
              "ON p.id = ch.preset_channel_id AND p.tenant_id = ch.tenant_id")


def _migrate_channel_uc_optional(c: sqlite3.Connection) -> None:
    """Снять NOT NULL с `pub_channels.uc` на базах, заведённых до пункта 6.1 ТЗ.

    SQLite не умеет менять ограничение колонки через ALTER — единственный путь:
    пересобрать таблицу целиком в одной транзакции. Идемпотентно: если notnull уже
    снят (новая база или повторный запуск), ничего не делаем. Список колонок и
    индексов берём из PRAGMA/sqlite_master, а не хардкодим — таблица уже не раз
    расширялась через ALTER TABLE ADD COLUMN, и жёсткий список их бы потерял.

    Доработка ревьюера (дополнение 16.09, п.1): в бою `ensure_pub_schema` зовут с
    разными соединениями, не всегда с `row_factory=sqlite3.Row`. Поэтому здесь —
    только позиционный доступ к PRAGMA table_info: cid, name, type, notnull,
    dflt_value, pk — порядок столбцов PRAGMA фиксирован SQLite и не зависит от
    row_factory вызывающего.
    """
    cols = c.execute("PRAGMA table_info(pub_channels)").fetchall()
    uc_col = next((r for r in cols if r[1] == "uc"), None)
    if uc_col is None or uc_col[3] == 0:
        return   # колонки ещё нет либо ограничение уже снято — мигрировать нечего

    def col_def(r) -> str:
        cid, name, ctype, notnull, dflt_value, pk = r
        if name == "uc":
            # Ровно то ограничение, ради которого затевается пересборка: без NOT NULL,
            # UNIQUE остаётся — несколько NULL в ней SQLite допускает, дублей не будет.
            return "uc TEXT UNIQUE"
        parts = [f'"{name}"']
        # Доработка ревьюера (дополнение 16.09, п.2): тип переносится ровно как
        # объявлен. SQLite допускает колонку без типа (динамическая типизация) —
        # пустой тип остаётся пустым, а не подменяется на TEXT по умолчанию.
        if ctype:
            parts.append(ctype)
        if pk:
            parts.append("PRIMARY KEY AUTOINCREMENT")
        if notnull:
            parts.append("NOT NULL")
        if dflt_value is not None:
            parts.append(f"DEFAULT {dflt_value}")
        return " ".join(parts)

    col_names = [r[1] for r in cols]
    cols_csv = ",".join(f'"{n}"' for n in col_names)
    create_sql = "CREATE TABLE pub_channels_new(" + ", ".join(col_def(r) for r in cols) + ")"
    # Индексы таблицы — их полный CREATE INDEX уже лежит в sqlite_master, автоиндекс
    # UNIQUE-ограничения (sql IS NULL) сюда не попадает и пересоздастся сам новой
    # колонкой `uc TEXT UNIQUE` выше.
    idx_sql = [r[0] for r in c.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='pub_channels' "
        "AND sql IS NOT NULL").fetchall()]

    # Доработка ревьюера (code review, итерация 2, Issue 1): INSERT...SELECT копирует
    # значения id как обычные числа, а не через счётчик AUTOINCREMENT, поэтому
    # sqlite_sequence после пересборки равен максимальному id СРЕДИ ОСТАВШИХСЯ строк,
    # а не прежнему значению счётчика. Если максимальный id когда-то был удалён (канал
    # убрали вручную), следующий новый канал получает переиспользованный id. Внешних
    # ключей в базе нет, поэтому старые pub_plan_items/pub_releases/pub_upload_jobs с
    # этим channel_id молча привяжутся к чужому новому каналу — ролик на чужом канале
    # (см. CLAUDE.md, «Ловушки»). Поэтому счётчик запоминается до перестройки и
    # восстанавливается после, если он оказался больше того, что получилось.
    old_seq_row = c.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='pub_channels'").fetchone()
    old_seq = old_seq_row[0] if old_seq_row else None

    in_tx = c.in_transaction
    if not in_tx:
        c.execute("BEGIN")
    try:
        c.execute(create_sql)
        c.execute(f"INSERT INTO pub_channels_new({cols_csv}) "
                  f"SELECT {cols_csv} FROM pub_channels")
        # Вид над таблицей мешает её пересобрать (и остался бы висеть на удалённой
        # таблице). Снимаем перед заменой, поднимаем сразу после.
        c.execute("DROP VIEW IF EXISTS pub_channels_live")
        c.execute("DROP TABLE pub_channels")
        c.execute("ALTER TABLE pub_channels_new RENAME TO pub_channels")
        for sql in idx_sql:
            c.execute(sql)
        # Счётчик приводится к ровно одной строке. Раньше здесь был UPDATE ... WHERE
        # seq<?, а при rowcount == 0 — INSERT OR IGNORE. Но rowcount == 0 означает и
        # «строки нет», и «строка есть, значение не меньше» — во втором случае вставлялась
        # вторая строка (у sqlite_sequence нет UNIQUE, IGNORE ничего не отсекает). На
        # копии dev-базы после миграции было две строки pub_channels. Сегодня SQLite
        # читает первую, но какую он прочтёт завтра — не гарантировано, а устаревшая
        # строка означает повтор номеров каналов и привязку старых выпусков к новому
        # каналу. Поэтому: взять максимум из всего, что знаем, стереть все строки,
        # записать одну.
        seqs = [r[0] for r in c.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='pub_channels'").fetchall()]
        max_id = c.execute("SELECT MAX(id) FROM pub_channels").fetchone()[0]
        want = max([v for v in [old_seq, max_id, *seqs] if v is not None], default=None)
        c.execute("DELETE FROM sqlite_sequence WHERE name IN ('pub_channels', 'pub_channels_new')")
        if want is not None:
            c.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('pub_channels', ?)",
                      (want,))
        if not in_tx:
            c.commit()
    except Exception:
        if not in_tx:
            c.rollback()
        raise


def _migrate_profiles_host_serial(c: sqlite3.Connection) -> None:
    """Заменить `UNIQUE(tenant_id,serial)` на `UNIQUE(tenant_id,default_host_id,serial)`
    на базах, заведённых до описи AdsPower по машинам (task-spec.md §6.2).

    Один и тот же серийный номер профиля существует на нескольких машинах отдельными
    профилями AdsPower — старое ограничение это запрещало. Техника пересборки — та же,
    что и `_migrate_channel_uc_optional` выше: SQLite не умеет менять ограничение
    существующей таблицы через ALTER, поэтому таблица пересобирается целиком в одной
    транзакции, список колонок берётся из PRAGMA table_info динамически (колонки §6.1
    уже добавлены к этому моменту через ALTER TABLE ADD COLUMN в ensure_pub_schema,
    поэтому переносятся вместе с остальными без отдельного упоминания здесь), индексы —
    из sqlite_master, счётчик AUTOINCREMENT — восстанавливается, а не пересчитывается
    заново (иначе удалённый когда-то профиль с максимальным id отдал бы свой номер
    новому — тот же прецедент, что и у каналов).

    Идемпотентность: сверяется текст CREATE TABLE из sqlite_master — если новая форма
    ограничения уже там, пересборка пропускается.
    """
    row = c.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND tbl_name='pub_profiles'"
    ).fetchone()
    if row is None or row[0] is None:
        return   # таблицы ещё нет — её создаст CREATE TABLE IF NOT EXISTS уже в новой форме
    if "UNIQUE(tenant_id,default_host_id,serial)" in row[0]:
        return   # уже новая форма — мигрировать нечего

    def col_def(r) -> str:
        cid, name, ctype, notnull, dflt_value, pk = r
        parts = [f'"{name}"']
        if ctype:
            parts.append(ctype)
        if pk:
            parts.append("PRIMARY KEY AUTOINCREMENT")
        if notnull:
            parts.append("NOT NULL")
        if dflt_value is not None:
            parts.append(f"DEFAULT {dflt_value}")
        return " ".join(parts)

    cols = c.execute("PRAGMA table_info(pub_profiles)").fetchall()
    col_names = [r[1] for r in cols]
    cols_csv = ",".join(f'"{n}"' for n in col_names)
    create_sql = ("CREATE TABLE pub_profiles_new(" + ", ".join(col_def(r) for r in cols) +
                  ", UNIQUE(tenant_id,default_host_id,serial))")
    idx_sql = [r[0] for r in c.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='pub_profiles' "
        "AND sql IS NOT NULL").fetchall()]

    # Тот же приём, что и у каналов: INSERT...SELECT копирует id как обычные значения, не
    # через AUTOINCREMENT-счётчик, поэтому sqlite_sequence после пересборки чинится
    # отдельно — иначе удалённый когда-то профиль с максимальным id отдал бы номер новому.
    old_seq_row = c.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='pub_profiles'").fetchone()
    old_seq = old_seq_row[0] if old_seq_row else None

    in_tx = c.in_transaction
    if not in_tx:
        c.execute("BEGIN")
    try:
        c.execute(create_sql)
        c.execute(f"INSERT INTO pub_profiles_new({cols_csv}) "
                  f"SELECT {cols_csv} FROM pub_profiles")
        c.execute("DROP TABLE pub_profiles")
        c.execute("ALTER TABLE pub_profiles_new RENAME TO pub_profiles")
        for sql in idx_sql:
            c.execute(sql)
        seqs = [r[0] for r in c.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='pub_profiles'").fetchall()]
        max_id = c.execute("SELECT MAX(id) FROM pub_profiles").fetchone()[0]
        want = max([v for v in [old_seq, max_id, *seqs] if v is not None], default=None)
        c.execute("DELETE FROM sqlite_sequence WHERE name IN ('pub_profiles', 'pub_profiles_new')")
        if want is not None:
            c.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('pub_profiles', ?)",
                      (want,))
        if not in_tx:
            c.commit()
    except Exception:
        if not in_tx:
            c.rollback()
        raise


def ensure_pub_schema(c: sqlite3.Connection) -> None:
    """Создать таблицы контура. Идемпотентно: повторный вызов ничего не меняет."""
    # === Контур публикаций: каналы → план → выпуски ===
    # Справочники хостов и профилей — таблицами, а не enum в коде: третья машина
    # должна добавляться через интерфейс, без правки main.py.
    c.execute("CREATE TABLE IF NOT EXISTS pub_hosts("
              "id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, "
              "code TEXT NOT NULL, title TEXT, adspower_url TEXT, "
              "status TEXT NOT NULL DEFAULT 'active', note TEXT, created REAL NOT NULL, "
              "UNIQUE(tenant_id,code))")
    # Опись AdsPower (task-spec.md §6.1): серийный номер профиля не уникален глобально —
    # один и тот же serial существует как разные профили на разных машинах, поэтому
    # уникальность — по паре (машина, serial), не по одному serial. Новые семь колонок
    # (adspower_user_id/group_name/remark/last_open_time/active_now/last_seen/
    # missing_since) — только то, что разрешает читать белый список §6.3; username,
    # password и прочие поля ответа AdsPower сюда не попадают никогда.
    c.execute("CREATE TABLE IF NOT EXISTS pub_profiles("
              "id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, "
              "serial TEXT NOT NULL, title TEXT, default_host_id INTEGER, "
              "status TEXT NOT NULL DEFAULT 'active', note TEXT, created REAL NOT NULL, "
              "adspower_user_id TEXT, group_name TEXT, remark TEXT, last_open_time TEXT, "
              "active_now INTEGER NOT NULL DEFAULT 0, active_at REAL, "
              "last_seen REAL, missing_since REAL, "
              "UNIQUE(tenant_id,default_host_id,serial))")
    # Существующие базы: колонки могли не существовать до этой описи. ADD COLUMN на уже
    # актуальной базе падает молча (except) — проверяется живым SELECT в тестах, не
    # молчанием. Порядок важен: колонки добавляются ДО пересборки ограничения ниже —
    # `_migrate_profiles_host_serial` берёт список колонок из PRAGMA table_info
    # динамически, и добавленные здесь колонки попадут в пересобранную таблицу сами,
    # без отдельного упоминания в теле миграции.
    for _col, _sql in (
        ("adspower_user_id", "TEXT"),
        ("group_name", "TEXT"),
        ("remark", "TEXT"),
        ("last_open_time", "TEXT"),
        ("active_now", "INTEGER NOT NULL DEFAULT 0"),
        ("active_at", "REAL"),
        ("last_seen", "REAL"),
        ("missing_since", "REAL"),
    ):
        try:
            c.execute(f"ALTER TABLE pub_profiles ADD COLUMN {_col} {_sql}")
        except Exception:
            pass
    _migrate_profiles_host_serial(c)
    # Канал. UC уникален во всей системе и не меняется после создания: неверный UC
    # означает ролик на чужом канале (прецедент был). Формат UC проверяется в Python,
    # уникальность — здесь. Расписание и переопределения — JSON, как projects.settings.
    # UC может быть пустым (пункт 6.1 ТЗ): канал иногда заводят раньше, чем узнают его
    # реальный ID на YouTube. SQLite допускает несколько NULL в UNIQUE-колонке — этим и
    # пользуемся; несколько пустых строк были бы дублем, поэтому пустое значение хранится
    # как NULL (см. `pub_ui.clean_uc`), а не как "". У новых баз ограничение уже такое;
    # у существующих его снимает `_migrate_channel_uc_optional` ниже.
    c.execute("CREATE TABLE IF NOT EXISTS pub_channels("
              "id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, "
              "uc TEXT UNIQUE, name TEXT NOT NULL, handle TEXT, lang TEXT, niche TEXT, "
              "profile_id INTEGER, host_id INTEGER, tz TEXT NOT NULL DEFAULT 'Europe/Warsaw', "
              "schedule TEXT NOT NULL DEFAULT '{}', horizon_days INTEGER NOT NULL DEFAULT 3, "
              "preset_rewrite TEXT, preset_vsgen_id INTEGER, preset_cover TEXT, "
              # video_mode: МЁРТВАЯ КОЛОНКА (проверено 04.09.2026 — не читает никто).
              # Канал как бы объявляет, каким должен быть ролик, и объявление никуда
              # не доходит: автоконвейер клипов не ставит вовсе, а ручной берёт режим
              # из параметров задачи. Из схемы не убрана намеренно: она есть в боевой
              # базе, и ALTER ради мёртвого поля — риск ради красоты. Оживлять её имеет
              # смысл только вместе с клипами в автовыпусках (docs/КОНВЕЙЕР_граф.md).
              "collaborator TEXT, video_mode TEXT NOT NULL DEFAULT 'slideshow', "
              "t2v_first_n INTEGER NOT NULL DEFAULT 0, "
              "status TEXT NOT NULL DEFAULT 'active', note TEXT, "
              "created REAL NOT NULL, updated REAL NOT NULL)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pub_channels_list ON pub_channels(tenant_id,status,name)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pub_channels_profile ON pub_channels(profile_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pub_channels_lang ON pub_channels(lang,niche)")
    # За сколько дней до выхода начинать готовить ролик. Появилось позже таблицы,
    # поэтому добавляется отдельно: у существующих каналов колонки ещё нет.
    try:
        c.execute("ALTER TABLE pub_channels ADD COLUMN lead_days INTEGER NOT NULL DEFAULT 2")
    except Exception:
        pass   # колонка уже есть — проверяется живым SELECT в тестах, не молчанием
    # Отложенная публикация: на раскрученных каналах ролик встаёт в расписание, на новых
    # публикуется сразу. Лимит — сколько роликов канала может одновременно висеть в отложке;
    # исчерпан — готовый ролик ждёт освободившегося места, а не льётся вне расписания.
    for _col, _sql in (("deferred", "INTEGER NOT NULL DEFAULT 1"),
                       ("deferred_limit", "INTEGER NOT NULL DEFAULT 5")):
        try:
            c.execute(f"ALTER TABLE pub_channels ADD COLUMN {_col} {_sql}")
        except Exception:
            pass
    # Пресет канала (18.09.2026, решение владельца): те же настройки, что у канала, кроме
    # языка — чтобы завести два канала на разных языках по одному набору правил.
    # Привязка ЖИВАЯ: канал ссылается на пресет, правка пресета сразу меняет поведение
    # всех каналов на нём. Поэтому значения не копируются в канал, а разрешаются на
    # чтении — видом `pub_channels_live` ниже.
    #
    # Колонки, а не JSON: те же имена и типы, что у канала, и NULL значит «в пресете не
    # задано, берём своё значение канала». С JSON пришлось бы разбирать его в каждом из
    # двадцати пяти читателей таблицы, а часть из них фильтрует прямо в SQL (lead_days в
    # `pub_engine.due_items`, niche в списке каналов) — там питоновский разбор бессилен.
    c.execute("CREATE TABLE IF NOT EXISTS pub_channel_presets("
              "id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, "
              "name TEXT NOT NULL, note TEXT, "
              "tz TEXT, schedule TEXT, horizon_days INTEGER, lead_days INTEGER, "
              "deferred INTEGER, deferred_limit INTEGER, "
              "preset_vsgen_id INTEGER, host_id INTEGER, niche TEXT, collaborator TEXT, "
              "created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(tenant_id,name))")
    try:
        c.execute("ALTER TABLE pub_channels ADD COLUMN preset_channel_id INTEGER")
    except Exception:
        pass   # колонка уже есть
    # «Монетный канал» (владелец 17.10.2026: «главное чтобы в студии могли видеть
    # монетные каналы, которые и будем помечать монеткой, плюс добавим фильтр»).
    # Отметка руками, не вывод из писем: письмо «можно подавать» — повод, а не факт
    # монетизации. `coin_note` — зачем/когда поставили, словами человека.
    for _col, _sql in (("coin", "INTEGER NOT NULL DEFAULT 0"),
                       ("coin_note", "TEXT")):
        try:
            c.execute(f"ALTER TABLE pub_channels ADD COLUMN {_col} {_sql}")
        except Exception:
            pass   # колонка уже есть — проверяется живым SELECT в тестах
    # Вид создаётся ПОСЛЕ миграции таблицы: та пересобирает `pub_channels` целиком, и
    # вид, созданный раньше, ссылался бы на таблицу, которой на время миграции нет.
    _migrate_channel_uc_optional(c)
    _channel_live_view(c)
    # Теги: связь заводим сразу, интерфейс позже. Маршрут ролика они не определяют никогда.
    c.execute("CREATE TABLE IF NOT EXISTS pub_tags("
              "id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, "
              "name TEXT NOT NULL, created REAL NOT NULL, UNIQUE(tenant_id,name))")
    c.execute("CREATE TABLE IF NOT EXISTS pub_channel_tags("
              "channel_id INTEGER NOT NULL, tag_id INTEGER NOT NULL, "
              "PRIMARY KEY(channel_id,tag_id))")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pub_channel_tags_tag ON pub_channel_tags(tag_id,channel_id)")
    # Строка плана: одна донорская ссылка на один точный канал в один слот.
    # slot_at — абсолютный момент (epoch UTC); таймзона канала нужна только для показа.
    c.execute("CREATE TABLE IF NOT EXISTS pub_plan_items("
              "id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, "
              "channel_id INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1, "
              "donor_url TEXT NOT NULL, position REAL NOT NULL DEFAULT 0, "
              "slot_at REAL, slot_mode TEXT NOT NULL DEFAULT 'auto', "
              "overrides TEXT NOT NULL DEFAULT '{}', note TEXT, "
              "status TEXT NOT NULL DEFAULT 'planned', frozen INTEGER NOT NULL DEFAULT 0, "
              "created REAL NOT NULL, updated REAL NOT NULL)")
    # Оригинальное название донорского ролика: показываем его вместо длинной ссылки.
    try:
        c.execute("ALTER TABLE pub_plan_items ADD COLUMN donor_title TEXT")
    except Exception:
        pass
    # Идентификатор донорского ролика (19.09.2026). Проверка «этот донор уже взят» шла
    # по строке ссылки, а одно и то же видео приходит в разных написаниях:
    # `youtu.be/X`, `watch?v=X`, `watch?v=X&t=30`, ссылка с `si=…` из «Поделиться».
    # Все они проходили как разные ролики — и канал получал дубль, оплаченный дважды
    # (субтитры, рерайт, озвучка, картинки, рендер). Отдельная колонка, а не разбор
    # ссылки при каждом чтении: по ней ищут и по ней же строится индекс.
    try:
        c.execute("ALTER TABLE pub_plan_items ADD COLUMN donor_video_id TEXT")
    except Exception:
        pass   # колонка уже есть
    else:
        # Разовое заполнение уже накопленных строк: без него старые строки не защищены,
        # а новая ссылка на тот же ролик к ним не приклеится.
        from pub_ids import video_id
        for _id, _url in c.execute("SELECT id, donor_url FROM pub_plan_items").fetchall():
            vid = video_id(_url)
            if vid:
                c.execute("UPDATE pub_plan_items SET donor_video_id=? WHERE id=?",
                          (vid, _id))
    c.execute("CREATE INDEX IF NOT EXISTS idx_pub_plan_donor_vid "
              "ON pub_plan_items(channel_id, donor_video_id)")
    # Единственная настоящая защита от двух роликов в один слот: внешних ключей в этой
    # базе нет (PRAGMA foreign_keys не включается), поймать дубль больше нечем.
    # v2: карантинная строка ('error') ДЕРЖИТ слот — её ролик может уже стоять в Studio,
    # и поставить в тот же выход второй было бы дублем на канале.
    c.execute("DROP INDEX IF EXISTS idx_pub_plan_slot_busy")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pub_plan_slot_busy_v2 "
              "ON pub_plan_items(channel_id,slot_at) "
              "WHERE slot_at IS NOT NULL "
              "AND status IN ('planned','frozen','running','done','error')")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pub_plan_queue "
              "ON pub_plan_items(channel_id,status,position,id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pub_plan_window "
              "ON pub_plan_items(tenant_id,slot_at,status)")
    # Выпуск: одна попытка провести конкретную версию строки плана до публикации.
    # UNIQUE(plan_item_id,plan_item_version) — защита от второго выпуска на ту же строку
    # при повторном тике или рестарте сервиса.
    c.execute("CREATE TABLE IF NOT EXISTS pub_releases("
              "id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL UNIQUE, "
              "tenant_id TEXT NOT NULL, plan_item_id INTEGER NOT NULL, "
              "plan_item_version INTEGER NOT NULL, channel_id INTEGER NOT NULL, "
              "uc TEXT NOT NULL, slot_at REAL NOT NULL, project_id TEXT, "
              "status TEXT NOT NULL DEFAULT 'pending', phase TEXT NOT NULL DEFAULT 'created', "
              "video_id TEXT, blocked_reason TEXT, evidence TEXT, "
              "heartbeat REAL, deadline REAL, attempts INTEGER NOT NULL DEFAULT 0, "
              "created REAL NOT NULL, updated REAL NOT NULL, "
              "UNIQUE(plan_item_id,plan_item_version))")
    # Подтверждение человеком (решение владельца 01.10.2026, п. 6). Из выпуска, у
    # которого загрузка уже начиналась, выхода не было ни одного: вернуть в работу
    # нельзя (повтор запрещён навсегда), снять строку плана нельзя, переставить нельзя,
    # а выход канала занят. Единственный честный исход — человек посмотрел канал своими
    # глазами и говорит: ролик там. Это считается выполнением плана, и пометка остаётся
    # НАВСЕГДА: её не снимает ни возврат в работу, ни повторная проверка. Машинным
    # подтверждением она при этом не становится — в сводке такие выходы названы отдельно.
    for _col, _sql in (("hand_confirmed", "REAL"), ("hand_by", "TEXT"),
                       ("hand_video_id", "TEXT"), ("hand_note", "TEXT")):
        try:
            c.execute(f"ALTER TABLE pub_releases ADD COLUMN {_col} {_sql}")
        except Exception:
            pass   # колонка уже есть
    c.execute("CREATE INDEX IF NOT EXISTS idx_pub_releases_live "
              "ON pub_releases(tenant_id,status,slot_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pub_releases_channel "
              "ON pub_releases(channel_id,slot_at)")
    # Журнал фаз выпуска: он же защита от повторной заливки. Пишется до необратимого
    # действия и подтверждается после; seq растёт монотонно.
    # `repeats`/`last_seen` (19.09.2026): одна и та же фаза, повторённая подряд, не
    # заводит новую строку — растёт счётчик и обновляется время последнего раза.
    # Повод из боя: движок пишет `resource_wait` на КАЖДОМ тике (30 с) по каждому
    # стоящему выпуску — 10,5 млн строк и 1,64 ГБ базы, при 187 ждущих выпусках это
    # ~530 тыс. строк в сутки. Журнал при этом не сообщает ничего нового: важны «с
    # какого времени ждём» (created) и «когда проверяли в последний раз» (last_seen).
    c.execute("CREATE TABLE IF NOT EXISTS pub_release_events("
              "id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL, "
              "seq INTEGER NOT NULL, phase TEXT NOT NULL, video_id TEXT, "
              "payload TEXT, created REAL NOT NULL, "
              "repeats INTEGER NOT NULL DEFAULT 1, last_seen REAL, "
              "UNIQUE(release_id,seq))")
    for _col, _sql in (("repeats", "INTEGER NOT NULL DEFAULT 1"), ("last_seen", "REAL")):
        try:
            c.execute(f"ALTER TABLE pub_release_events ADD COLUMN {_col} {_sql}")
        except Exception:
            pass   # колонка уже есть — живой SELECT в тестах это проверяет
    c.execute("CREATE INDEX IF NOT EXISTS idx_pub_release_events_rid "
              "ON pub_release_events(release_id,seq)")
    # Журнал выпуска: единственное место, где после сбоя видно, ЧТО именно произошло.
    # Фазы отвечают на вопрос «где остановилось», а этот лог — «почему»: что спросили
    # у соседа, что он ответил, какое решение принял движок, что печатал заливщик.
    # `repeats`/`last_ts` — по той же причине, что и у фаз: строка «ждём: нет ключей»
    # писалась каждый тик и за час выедала весь лимит KEEP_PER_RELEASE, вытесняя из
    # журнала то единственное, ради чего его открывают — что было ДО ожидания.
    c.execute("CREATE TABLE IF NOT EXISTS pub_release_log("
              "id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL, "
              "ts REAL NOT NULL, level TEXT NOT NULL DEFAULT 'info', "
              "step TEXT NOT NULL, message TEXT NOT NULL, "
              "repeats INTEGER NOT NULL DEFAULT 1, last_ts REAL)")
    for _col, _sql in (("repeats", "INTEGER NOT NULL DEFAULT 1"), ("last_ts", "REAL")):
        try:
            c.execute(f"ALTER TABLE pub_release_log ADD COLUMN {_col} {_sql}")
        except Exception:
            pass
    c.execute("CREATE INDEX IF NOT EXISTS idx_pub_release_log_rid "
              "ON pub_release_log(release_id, id)")
    # Задание на заливку: то, что забирает машина с профилем AdsPower.
    # Отдельно от pub_release_jobs, потому что у него своя аренда, свой клейм и свой вердикт,
    # а истёкшая аренда ведёт в карантин — в отличие от рендера, где задача возвращается.
    c.execute("CREATE TABLE IF NOT EXISTS pub_upload_jobs("
              "id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL UNIQUE, "
              "tenant_id TEXT NOT NULL, channel_id INTEGER NOT NULL, uc TEXT NOT NULL, "
              "profile_id INTEGER, host_id INTEGER, channel_url TEXT NOT NULL, "
              "title TEXT NOT NULL, description TEXT, schedule_iso TEXT, "
              "visibility TEXT NOT NULL DEFAULT 'public', collaborator TEXT, "
              "video_path TEXT NOT NULL, video_sha256 TEXT, cover_path TEXT, "
              "slot_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'queued', "
              "phase TEXT, claim_token TEXT, host TEXT, worker TEXT, claimed REAL, lease_until REAL, "
              "heartbeat REAL, verdict TEXT, video_id TEXT, evidence TEXT, error TEXT, "
              "finished REAL, blocked_since REAL, blocked_why TEXT, "
              "created REAL NOT NULL, updated REAL NOT NULL)")
    # Задание, которое не выдаётся, потому что его профиль открыт ПОСТОРОННИМ (чужой
    # конвейер на той же машине или человек руками): когда придержали впервые и почему.
    # Без `blocked_since` нельзя ответить на вопрос «а долго ли оно так стоит», а
    # именно на нём держится тревога §13 — ждать вечно молча нельзя.
    # `video_passport` — опознание ролика на момент допуска: версия сборки плюс
    # отпечаток файла (`server/pub_artifact.py`). Без него в задании был только ПУТЬ,
    # а путь у рендера постоянный: пересборка подменяла файл, не меняя задания, и на
    # канал уходила версия, которую никто не принимал. Задания, допущенные до этой
    # колонки, живут с пустым значением — пустое значит «не знаем», и сверка по ним
    # молчит, а не объявляет их пересобранными.
    # `finalize_tries` / `finalize_last` — доведение черновика по номеру (18.10.2026):
    # сколько раз машина уже пыталась довести залитый ролик до расписания по его номеру
    # и когда в последний раз. Счёт живёт отдельными колонками, а не меткой в `error`:
    # текст причины принадлежит человеку, и разбирать его на слова нельзя.
    for _col, _sql in (("blocked_since", "REAL"), ("blocked_why", "TEXT"),
                       ("video_passport", "TEXT"),
                       ("finalize_tries", "INTEGER"), ("finalize_last", "REAL")):
        try:
            c.execute(f"ALTER TABLE pub_upload_jobs ADD COLUMN {_col} {_sql}")
        except Exception:
            pass
    c.execute("CREATE INDEX IF NOT EXISTS idx_pub_upload_queue "
              "ON pub_upload_jobs(status, slot_at, id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pub_upload_profile "
              "ON pub_upload_jobs(status, profile_id)")
    # Задачи, отданные соседним сервисам (rewrite/vsgen/cover). Своя копия исхода:
    # штатные чистилки очереди знают только queue_tasks и job_logs, история выпусков
    # не должна исчезать вместе с ними.
    c.execute("CREATE TABLE IF NOT EXISTS pub_release_jobs("
              "id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL, "
              "service TEXT NOT NULL, kind TEXT NOT NULL, remote_job_id TEXT, "
              "status TEXT NOT NULL DEFAULT 'queued', payload TEXT, error TEXT, "
              "created REAL NOT NULL, updated REAL NOT NULL, "
              "UNIQUE(release_id,service,kind))")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pub_release_jobs_rid ON pub_release_jobs(release_id)")
    # Перепись срывов заливки: одна строка на срыв, с разбором «безопасно ли повторять»
    # в тот момент, когда ответ ещё известен. Решение владельца 01.10.2026, п. 7:
    # автоповтор заливки решается по числам, а числа надо сначала собрать — статус
    # задания перетирается следующим исходом и разобранный руками срыв следа не
    # оставляет. Подробности и классы — `server/pub_upload_census.py`.
    import pub_upload_census                                       # noqa: PLC0415
    pub_upload_census.ensure(c)
