#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Убрать со стенда тестовые проекты и каналы — вместе со всем, что на них ссылается.

Чем это НЕ является. Это не уборка по сроку хранения (`dev/retention.py`): та трогает
только опубликованное и старше заданного срока, и работает в бою. Здесь наоборот —
разовая чистка стенда от того, что мы сами наделали, пока проверяли конвейер.

Почему отдельным инструментом, а не кнопкой в интерфейсе. Удалить канал через интерфейс
нельзя намеренно: у канала есть история выпусков, и молчаливое исчезновение строки в
календаре — худший способ узнать, что кто-то навёл порядок. На стенде это, наоборот,
ежедневная нужда.

По умолчанию НИЧЕГО не удаляет — печатает, что нашёл и что бы снёс. Удаляет только
с `--apply`.

    dev/cleanup.py                    что есть на стенде
    dev/cleanup.py --demo             фикстуры стенда (proj-dev-*, «Демо-канал»)
    dev/cleanup.py --all              все проекты и каналы
    dev/cleanup.py --project p_ab12   один проект
    dev/cleanup.py --channel 3        один канал
    dev/cleanup.py --demo --apply     то же, но по-настоящему

Люди, ключи, пресеты и голоса не трогаются никогда: чистим работу, а не настройки.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

DB = os.environ.get("VS_DEV_DB") or os.path.join(HERE, "vsgen.dev.db")

# Боевая база сюда попасть не должна. Инструмент разовый и разрушительный, а разница
# между стендом и production — одна переменная окружения; ошибиться легко, отменить
# нечем. Совпадение с этими путями требует явного --force.
PROD_HINTS = ("/root/VideoStudio", "/root/rewrite-server", "/root/cover_tool")

# Что удаляем и по какому признаку. Одна таблица — одна строка здесь, и она же
# управляет и подсчётом, и удалением: два списка разошлись бы в первый же месяц, и
# отчёт начал бы обещать не то, что делает.
#
# Порядок важен: сначала то, что ссылается, потом то, на что ссылаются. Иначе останутся
# строки, указывающие в пустоту, и экраны покажут выпуски без каналов.
#
# `provider_calls` привязан и к проекту, и к выпуску — поэтому у него два ключа, а не
# две записи в списке: иначе одна и та же строка считалась бы дважды.
PLAN = (
    ("provider_calls",     ("project_id", "release_id")),
    ("card_jobs",          ("project_id",)),
    ("job_logs",           ("job_id",)),
    ("job_files",          ("job_id",)),
    ("gen_jobs",           ("id@job",)),
    ("queue_tasks",        ("project_id",)),
    ("pub_release_events", ("release_id",)),
    ("pub_release_log",    ("release_id",)),
    ("pub_release_jobs",   ("release_id",)),
    ("pub_upload_jobs",    ("release_id",)),
    ("pub_releases",       ("release_id",)),
    ("pub_channel_tags",   ("channel_id",)),
    ("pub_plan_items",     ("channel_id",)),
    ("projects",           ("id@project",)),
    ("pub_channels",       ("id@channel",)),
)

DEMO_PROJECT_PREFIX = "proj-dev-"
DEMO_CHANNEL_NAMES = ("Демо-канал",)


def db(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _tables(c) -> set:
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _count(c, table: str, where: str, args) -> int:
    try:
        return c.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]
    except sqlite3.Error:
        return 0


def _inside(root: str, pid: str) -> bool:
    """Каталог проекта выглядит как .../projects/<pid> — иначе не наш, и мы его не трогаем.

    Тот же предохранитель, что в уборке по сроку хранения: `dir_path` приходит из базы,
    а `shutil.rmtree` по кривому пути — это не «ошибка теста», это чужие файлы.
    """
    parts = os.path.abspath(root).rstrip(os.sep).split(os.sep)
    return len(parts) >= 2 and parts[-1] == pid and parts[-2] == "projects"


def _clauses(sets: dict, keys) -> tuple:
    """Ключи таблицы → один WHERE и его аргументы. Пустой набор = таблицу не трогаем."""
    parts, args = [], []
    for key in keys:
        col, _, kind = key.partition("@")
        values = sets[kind or col]
        if not values:
            continue
        parts.append(f"{col} IN ({','.join('?' * len(values))})")
        args += list(values)
    return (" OR ".join(parts), args)


def _sets(c, projects: list, channels: list) -> dict:
    """Что попадает под удаление, по каждому виду ключа."""
    releases, jobs = [], []
    if channels:
        marks = ",".join("?" * len(channels))
        releases = [r[0] for r in c.execute(
            f"SELECT release_id FROM pub_releases WHERE channel_id IN ({marks})", channels)]
    if projects:
        marks = ",".join("?" * len(projects))
        jobs = [r[0] for r in c.execute(
            f"SELECT job_id FROM queue_tasks WHERE project_id IN ({marks})", projects)]
    return {"project": projects, "project_id": projects,
            "channel": channels, "channel_id": channels,
            "release_id": releases, "job": jobs, "job_id": jobs}


def plan(c, projects: list, channels: list) -> dict:
    """Что именно будет удалено. Ничего не меняет."""
    tabs = _tables(c)
    sets = _sets(c, projects, channels)
    rows = {}
    for table, keys in PLAN:
        if table not in tabs:
            continue
        where, args = _clauses(sets, keys)
        if where:
            n = _count(c, table, where, args)
            if n:
                rows[table] = n

    files, kept = [], []
    for pid in projects:
        p = c.execute("SELECT dir_path FROM projects WHERE id=?", (pid,)).fetchone()
        d = (p["dir_path"] if p else "") or ""
        if not d or not os.path.isdir(d):
            continue
        if not _inside(d, pid):
            kept.append(d)          # строку уберём, каталог — нет. И скажем об этом.
            continue
        size = sum(os.path.getsize(os.path.join(r, f))
                   for r, _, fs in os.walk(d) for f in fs
                   if os.path.exists(os.path.join(r, f)))
        files.append((d, size))

    return {"rows": rows, "files": files, "kept": kept, "sets": sets,
            "bytes": sum(sz for _d, sz in files),
            "projects": projects, "channels": channels,
            "releases": sets["release_id"]}


def apply(c, pl: dict, log=print) -> None:
    tabs = _tables(c)
    for table, keys in PLAN:
        if table not in tabs:
            continue
        where, args = _clauses(pl["sets"], keys)
        if where:
            c.execute(f"DELETE FROM {table} WHERE {where}", args)
    c.commit()
    # Файлы — ПОСЛЕ коммита: упавший rmtree оставит мусор на диске, но база уже
    # согласована. Обратный порядок оставил бы строки на удалённые каталоги.
    for d, _sz in pl["files"]:
        shutil.rmtree(d, ignore_errors=True)
        log(f"  удалён каталог {d}")


def human(n: int) -> str:
    for unit, size in (("ГБ", 1 << 30), ("МБ", 1 << 20), ("КБ", 1 << 10)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n} Б"


def show(c) -> None:
    print("\nПроекты")
    for r in c.execute("SELECT id, name, status FROM projects ORDER BY created"):
        print(f"  {r['id']:<18} {(r['name'] or '—')[:34]:<36} {r['status'] or ''}")
    print("\nКаналы")
    for r in c.execute("SELECT id, name, status FROM pub_channels ORDER BY id"):
        print(f"  #{r['id']:<4} {(r['name'] or '—')[:34]:<36} {r['status'] or ''}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="фикстуры стенда")
    ap.add_argument("--all", action="store_true", help="все проекты и каналы")
    ap.add_argument("--project", action="append", default=[], help="один проект (можно повторять)")
    ap.add_argument("--channel", action="append", default=[], type=int, help="один канал")
    ap.add_argument("--apply", action="store_true", help="удалить по-настоящему")
    ap.add_argument("--force", action="store_true", help="разрешить работу с боевой базой")
    ap.add_argument("--db", default=DB)
    a = ap.parse_args()

    # Проверка «это боевая база» идёт ПЕРВОЙ: иначе на машине, где такого файла нет,
    # предохранитель молчит и выглядит рабочим, а на той, где есть, — срабатывает
    # впервые в жизни. Предохранители должны отвечать одинаково везде.
    if any(h in os.path.abspath(a.db) for h in PROD_HINTS) and not a.force:
        print(f"это похоже на боевую базу: {a.db}\n"
              "Инструмент для стенда. Если правда надо — добавьте --force.")
        return 1
    if not os.path.exists(a.db):
        print(f"нет базы: {a.db}")
        return 1

    c = db(a.db)
    if not (a.demo or a.all or a.project or a.channel):
        show(c)
        print("\nЧто убрать: --demo (фикстуры стенда) · --all (всё) · --project ID · --channel N")
        return 0

    projects = list(a.project)
    channels = list(a.channel)
    if a.all:
        projects = [r[0] for r in c.execute("SELECT id FROM projects")]
        channels = [r[0] for r in c.execute("SELECT id FROM pub_channels")]
    elif a.demo:
        projects += [r[0] for r in c.execute(
            "SELECT id FROM projects WHERE id LIKE ?", (DEMO_PROJECT_PREFIX + "%",))]
        marks = ",".join("?" * len(DEMO_CHANNEL_NAMES))
        channels += [r[0] for r in c.execute(
            f"SELECT id FROM pub_channels WHERE name IN ({marks})", DEMO_CHANNEL_NAMES)]
    projects = sorted(set(projects))
    channels = sorted(set(channels))
    if not projects and not channels:
        print("нечего убирать: под условие ничего не попало")
        return 0

    pl = plan(c, projects, channels)
    print(f"\nПроектов: {len(projects)}   каналов: {len(channels)}   "
          f"выпусков: {len(pl['releases'])}")
    if projects:
        print("  проекты: " + ", ".join(projects))
    if channels:
        print("  каналы:  " + ", ".join(f"#{x}" for x in channels))
    print("\nСтроки")
    for t, n in sorted(pl["rows"].items(), key=lambda kv: -kv[1]):
        if n:
            print(f"  {t:<20} {n}")
    print(f"\nНа диске освободится: {human(pl['bytes'])} ({len(pl['files'])} каталогов)")
    for d in pl["kept"]:
        # Молча оставить каталог и отчитаться «убрано» — это как раз тот случай, когда
        # интерфейс врёт: строки нет, файлы на диске есть, места не прибавилось.
        print(f"  каталог НЕ трогаем (не похож на проектный): {d}")

    if not a.apply:
        print("\nНичего не тронуто. Повторите с --apply.")
        return 0
    apply(c, pl)
    print("\nУбрано.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
