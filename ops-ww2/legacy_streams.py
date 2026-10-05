#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ВВ2 и манхва из старого кабинета — строками в утреннюю сводку новой студии.

Решение владельца А10 (тест решений, 04.10.2026): «Да, одна сводка, ВВ2 и манхва из
старого кабинета». Маршрут заказчика требует три потока в одном отчёте, а сводка новой
студии (`studio-platform/server/pub_digest.py`) видит только свою базу.

Почему файл, а не чтение чужой базы из студии. Студия, которая сама лезет в SQLite
соседа, — это тот самый анти-контракт `pub_neighbors.py`, который при переписывании
переносить нельзя. Здесь шов один и видимый: этот скрипт раз в час читает базу старого
кабинета ТОЛЬКО на чтение и кладёт маленький JSON с версией схемы; сводка читает файл и,
если его нет или он устарел, так и пишет. Остановится выгрузка — сводка скажет «устарели»,
а не покажет вчерашние числа как сегодняшние.

Числа — по тем же определениям, что у сводки (`pub_digest.collect`), иначе строки трёх
потоков в одном отчёте значили бы разное:

  · каналов — не в архиве; отдельно сколько на паузе;
  · в плане — живые строки плана (LIVE_PLAN), у которых выход впереди или не назначен;
  · принято за сутки — событие `scheduled` в журнале фаз внутри календарных суток;
  · блокеры — выпуски в разборе сейчас, плюс три самые частые причины;
  · сегодня: ожидалось (выход в сутках и время наступило), подтверждено вышедшим (фаза
    `published`), дошло до канала (фаза `scheduled` и дальше), задержано (до канала не
    дошёл), не знаем (дошёл, но подтверждать нечем);
  · залито за сутки — задания заливки `done`, закончившиеся в сутках;
  · последний новый выпуск — когда заводился самый свежий.

Подтверждения выхода в старом кабинете нет: событий `public_check` в его журнале фаз нет
ни одного (05.10.2026). Поэтому «подтверждено» здесь честно ноль только для фазы
`published`, а всё дошедшее до канала идёт в «не знаем» со своей причиной — не в ноль и
не в «вышло».

Запуск на сервере (раз в час, по таймеру):
    python3 /root/ops/pub_bridge/legacy_streams.py
    python3 legacy_streams.py --db /path/vsgen.db --out /tmp/x.json --print   # проба
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import datetime as dt
import json
import os
import sqlite3
import tempfile
import time

try:
    from zoneinfo import ZoneInfo
except ImportError:                      # pragma: no cover
    ZoneInfo = None                      # type: ignore

SCHEMA_VERSION = 1
DB_PATH = os.environ.get("LEGACY_VSGEN_DB", "/root/VideoStudio/server/vsgen.db")
OUT_PATH = os.environ.get("VS_LEGACY_STREAMS_FILE", "/srv/studio/data/feeds/legacy_streams.json")
TZ = (os.environ.get("VS_DIGEST_TZ") or "Europe/Warsaw").strip()

# Потоки сводки: ниша канала в старом кабинете → имя в отчёте. Других ниш (аннуаки,
# тайга) в решении А10 нет — добавляются сюда строкой, если владелец скажет.
STREAMS = (("ww2", "ВВ2"), ("манхва", "Манхва"))

# Копия `studio-platform/server/pub_engine.PHASES` (и того же кортежа в старом кабинете,
# /root/VideoStudio/server/pub_engine.py, сверено 05.10.2026). Копия, а не импорт: на
# сервере скрипт живёт рядом с мостом и от кода студии не зависит. Расхождение ловит тест.
PHASES = (
    "created", "source_ready", "project_ready", "media_ready", "cover_ready",
    "upload_pending", "upload_running", "scheduled", "published",
)
PHASE_INDEX = {p: i for i, p in enumerate(PHASES)}
ACCEPTED_INDEX = PHASE_INDEX["scheduled"]
# Копия `pub_digest.LIVE_PLAN`.
LIVE_PLAN = ("planned", "frozen", "running", "done", "error")
TOP_REASONS = 3


def _zone(name: str):
    if ZoneInfo is None:
        return dt.timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:                                              # noqa: BLE001
        return dt.timezone.utc


def day_bounds(now: float, tz_name: str = TZ) -> tuple:
    """Календарные сутки (как `pub_digest.day_bounds`): конец — настоящее начало
    следующего дня, чтобы в день перевода часов не потерять и не удвоить час."""
    zone = _zone(tz_name)
    local = dt.datetime.fromtimestamp(float(now), zone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    nxt = (start + dt.timedelta(days=1, hours=12)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp(), nxt.timestamp()


def connect_ro(path: str) -> sqlite3.Connection:
    """Только чтение: режим `ro` у самого файла и `query_only` у соединения. Старый
    кабинет — чужой production, писать в него этому скрипту нечего и нельзя."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"базы старой студии нет: {path}")
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("PRAGMA query_only=1")
    return c


def _marks(n: int) -> str:
    return ",".join("?" * n)


def _chunks(items, size=500):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _short_reason(text: str) -> str:
    """Причина блокировки для отчёта: первая строка, без хвоста. Номера и детали после
    двоеточия у одной беды разные, а в отчёте их надо сложить."""
    line = " ".join(str(text or "").split())
    if not line:
        return "причина не записана"
    for sep in (" (", ": "):
        if sep not in line:
            continue
        head = line.split(sep, 1)[0]
        if len(head) >= 12:
            line = head
            break
    return line[:90]


def stream(c: sqlite3.Connection, niche: str, now: float, since: float, until: float) -> dict:
    channels = c.execute(
        "SELECT id, uc, status FROM pub_channels WHERE niche=? AND status!='archived'",
        (niche,)).fetchall()
    ids = [row["id"] for row in channels]
    ucs = [row["uc"] for row in channels]
    paused = sum(1 for row in channels if (row["status"] or "active") == "paused")
    out = {"channels": len(channels), "paused": paused, "plan": 0, "accepted": 0,
           "blocked": 0, "reasons": [],
           "today": {"expected": 0, "ahead": 0, "confirmed": 0, "reached": 0,
                     "delayed": 0, "unknown": 0, "why": []},
           "uploaded": 0, "last_new_release": None}
    if not ids:
        return out

    plan_rows, releases = [], {}
    for part in _chunks(ids):
        plan_rows += c.execute(
            f"SELECT id, channel_id, slot_at, status FROM pub_plan_items "
            f"WHERE channel_id IN ({_marks(len(part))}) "
            f"AND status IN ({_marks(len(LIVE_PLAN))})", (*part, *LIVE_PLAN)).fetchall()
        # Как в сводке: на строку плана — последний по id выпуск.
        for rel in c.execute(
                f"SELECT * FROM pub_releases WHERE channel_id IN ({_marks(len(part))}) "
                "ORDER BY id", part):
            releases[rel["plan_item_id"]] = rel

    today_items = []
    for row in plan_rows:
        slot = row["slot_at"]
        if slot is None or float(slot) > now:
            out["plan"] += 1
        if slot is None:
            continue
        slot = float(slot)
        if since <= slot < until:
            if slot <= now:
                today_items.append(row)
            else:
                out["today"]["ahead"] += 1

    all_rels = list(releases.values())

    # Журнал фаз у старого кабинета огромный: 11 млн строк `resource_wait` на 05.10.2026,
    # и почти все они — у тех же выпусков, так что даже индекс по release_id проходит их
    # все (первая проба на сервере шла дольше пяти минут). Поэтому в журнал ходим только
    # за кандидатами. Принят сегодня может быть лишь выпуск, который дошёл до `scheduled`
    # и менялся сегодня: `pub_releases.updated` переписывается каждым касанием выпуска,
    # в том числе сменой фазы, — значит, это надмножество, и никто не потеряется.
    candidates = [r["release_id"] for r in all_rels
                  if PHASE_INDEX.get(r["phase"] or "", 0) >= ACCEPTED_INDEX
                  and float(r["updated"] or 0) >= since]
    accepted_ids = set()
    for rid in candidates:
        if c.execute("SELECT 1 FROM pub_release_events WHERE release_id=? AND phase='scheduled' "
                     "AND created>=? AND created<? LIMIT 1", (rid, since, until)).fetchone():
            accepted_ids.add(rid)
    out["accepted"] = len(accepted_ids)

    today = out["today"]
    today["expected"] = len(today_items)
    for item in today_items:
        rel = releases.get(item["id"])
        if rel is None:
            today["delayed"] += 1
            continue
        phase = rel["phase"] or "created"
        if phase == "published":
            today["confirmed"] += 1
            today["reached"] += 1
        elif PHASE_INDEX.get(phase, 0) < ACCEPTED_INDEX:
            today["delayed"] += 1
        else:
            today["reached"] += 1
            today["unknown"] += 1
            checked = c.execute("SELECT 1 FROM pub_release_events WHERE release_id=? "
                                "AND phase='public_check' LIMIT 1",
                                (rel["release_id"],)).fetchone()
            why = ("проверка выхода ответила, но вышедшим ролик не подтверждён" if checked
                   else "в старой студии выход ролика не проверяется — только расписание")
            if why not in today["why"]:
                today["why"].append(why)

    reasons: dict = {}
    for rel in all_rels:
        if rel["status"] == "blocked":
            out["blocked"] += 1
            key = _short_reason(rel["blocked_reason"])
            reasons[key] = reasons.get(key, 0) + 1
    out["reasons"] = [{"why": k, "count": v} for k, v in
                      sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_REASONS]]

    for part in _chunks(ucs):
        out["uploaded"] += c.execute(
            f"SELECT COUNT(*) FROM pub_upload_jobs WHERE uc IN ({_marks(len(part))}) "
            "AND status='done' AND finished>=? AND finished<?", (*part, since, until)).fetchone()[0]
    newest = max((float(r["created"]) for r in all_rels if r["created"] is not None), default=None)
    out["last_new_release"] = newest
    return out


def collect(c: sqlite3.Connection, now: float = None, tz_name: str = TZ) -> dict:
    now = time.time() if now is None else float(now)
    since, until = day_bounds(now, tz_name)
    streams = []
    for key, name in STREAMS:
        row = {"key": key, "name": name}
        row.update(stream(c, key, now, since, until))
        streams.append(row)
    return {"schema_version": SCHEMA_VERSION, "at": now, "tz": tz_name,
            "day": dt.datetime.fromtimestamp(now, _zone(tz_name)).strftime("%Y-%m-%d"),
            "source": "старый кабинет (vsgen)", "streams": streams}


def write_atomic(path: str, data: dict) -> None:
    """Сводка может прочитать файл в любой момент: пишем рядом и подменяем целиком."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".legacy_streams.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--now", type=float, default=None, help="для проверок: момент отчёта")
    ap.add_argument("--print", action="store_true", help="напечатать выгрузку")
    args = ap.parse_args(argv)
    c = connect_ro(args.db)
    try:
        data = collect(c, now=args.now)
    finally:
        c.close()
    write_atomic(args.out, data)
    if args.print:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        print(f"выгрузка записана: {args.out} — "
              + ", ".join(f"{s['name']}: каналов {s['channels']}, блокеры {s['blocked']}"
                          for s in data["streams"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
