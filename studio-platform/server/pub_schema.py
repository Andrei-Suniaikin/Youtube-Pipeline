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
    c.execute("CREATE TABLE IF NOT EXISTS pub_profiles("
              "id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, "
              "serial TEXT NOT NULL, title TEXT, default_host_id INTEGER, "
              "status TEXT NOT NULL DEFAULT 'active', note TEXT, created REAL NOT NULL, "
              "UNIQUE(tenant_id,serial))")
    # Канал. UC уникален во всей системе и не меняется после создания: неверный UC
    # означает ролик на чужом канале (прецедент был). Формат UC проверяется в Python,
    # уникальность — здесь. Расписание и переопределения — JSON, как projects.settings.
    c.execute("CREATE TABLE IF NOT EXISTS pub_channels("
              "id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, "
              "uc TEXT NOT NULL UNIQUE, name TEXT NOT NULL, handle TEXT, lang TEXT, niche TEXT, "
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
    c.execute("CREATE INDEX IF NOT EXISTS idx_pub_releases_live "
              "ON pub_releases(tenant_id,status,slot_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pub_releases_channel "
              "ON pub_releases(channel_id,slot_at)")
    # Журнал фаз выпуска: он же защита от повторной заливки. Пишется до необратимого
    # действия и подтверждается после; seq растёт монотонно.
    c.execute("CREATE TABLE IF NOT EXISTS pub_release_events("
              "id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL, "
              "seq INTEGER NOT NULL, phase TEXT NOT NULL, video_id TEXT, "
              "payload TEXT, created REAL NOT NULL, UNIQUE(release_id,seq))")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pub_release_events_rid "
              "ON pub_release_events(release_id,seq)")
    # Журнал выпуска: единственное место, где после сбоя видно, ЧТО именно произошло.
    # Фазы отвечают на вопрос «где остановилось», а этот лог — «почему»: что спросили
    # у соседа, что он ответил, какое решение принял движок, что печатал заливщик.
    c.execute("CREATE TABLE IF NOT EXISTS pub_release_log("
              "id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT NOT NULL, "
              "ts REAL NOT NULL, level TEXT NOT NULL DEFAULT 'info', "
              "step TEXT NOT NULL, message TEXT NOT NULL)")
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
              "finished REAL, created REAL NOT NULL, updated REAL NOT NULL)")
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
