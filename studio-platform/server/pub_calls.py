#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Журнал платных вызовов провайдеров — уровнем ниже журнала выпусков.

Зачем отдельная таблица. Существующий журнал (`pub_release_jobs`) хранит строку на ЭТАП:
`UNIQUE(release_id, service, kind)`. Внутри одного этапа «картинки» при медиане 155 кадров
происходит 155 отдельных платных вызовов, про которые этот журнал не знает ничего.

Что защищает их сегодня — пять независимых файловых кэшей внутри движков (по подсчёту
разбора: картинки 54 места, рендер 18, Lumean 7, Voicer 23, VGC 2) плюс правило «повтор
только на явный 429» в озвучке. Файловый кэш закрывает случай «файл уже скачан» и не
закрывает окно между «провайдер принял заказ» и «файл скачан»: рестарт в этот момент
платит второй раз. Симптом виден в живом коде — 03.09 все таймауты картинок подняты ×10
с комментарием, что оплаченные кадры снимались собственным таймером.

Правило №4 ТЗ: платный вызов повторяется ТОЛЬКО когда доказано, что он не состоялся.
Доказательство — строка в этой таблице.

Порядок работы (важен именно такой):

    1. reserve()          — ДО обращения к провайдеру. Строка появляется раньше денег.
    2. mark_ordered()     — как только известен идентификатор заказа.
    3. mark_delivered()   — когда результат лежит на диске и открывается.

Если между 1 и 2 процесс умер, при следующем reserve() вернётся состояние `reserved`:
это НЕ разрешение повторить, а требование сходить к провайдеру и выяснить судьбу заказа.
Единственное состояние, разрешающее новую попытку, — `failed`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

# Состояния строки. Порядок отражает жизненный цикл.
NEW = "new"                 # строки не было, вызов можно делать
RESERVED = "reserved"       # заявка записана, судьба заказа неизвестна
ORDERED = "ordered"         # заказ у провайдера создан — деньги, возможно, потрачены
DELIVERED = "delivered"     # результат получен и лежит на диске
FAILED = "failed"           # провайдер отказал ДО создания заказа — повтор безопасен

# Что делать вызывающему, получив существующую строку.
SKIP = "skip"               # ничего не делать, результат уже есть
VERIFY = "verify"           # спросить провайдера про существующий заказ, НЕ создавать новый
RETRY = "retry"             # прошлая попытка не дошла до заказа — можно пробовать снова

_ACTION = {DELIVERED: SKIP, ORDERED: VERIFY, RESERVED: VERIFY, FAILED: RETRY}


def ensure_calls_schema(c: sqlite3.Connection) -> None:
    """Создать таблицу журнала. Идемпотентно."""
    c.execute(
        "CREATE TABLE IF NOT EXISTS provider_calls("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "tenant_id TEXT NOT NULL, "
        # Проект, а не выпуск: выпуск есть только у автоконвейера, проект — всегда.
        "project_id TEXT NOT NULL, "
        "release_id TEXT NOT NULL DEFAULT '', "
        "kind TEXT NOT NULL, "              # images | clip | tts | cover
        "unit TEXT NOT NULL, "              # номер сцены, кусок текста, язык обложки
        "fingerprint TEXT NOT NULL, "       # хэш входа: модель + промпт + параметры
        "provider TEXT NOT NULL DEFAULT '', "
        "model TEXT NOT NULL DEFAULT '', "
        "remote_order_id TEXT NOT NULL DEFAULT '', "
        "status TEXT NOT NULL, "
        "result_path TEXT NOT NULL DEFAULT '', "
        "cost REAL, "
        "attempts INTEGER NOT NULL DEFAULT 0, "
        "error TEXT NOT NULL DEFAULT '', "
        "created REAL NOT NULL, updated REAL NOT NULL, "
        # Ядро защиты: тот же вход в том же проекте нельзя заказать дважды.
        "UNIQUE(project_id, kind, unit, fingerprint))"
    )
    # Экран выпуска и подсчёт стоимости ходят по проекту; журнал — по времени.
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_project "
              "ON provider_calls(project_id, kind, updated)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_release "
              "ON provider_calls(release_id, status)")
    # Поиск незавершённых при восстановлении после рестарта.
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_open "
              "ON provider_calls(status, updated)")


def fingerprint(**parts) -> str:
    """Отпечаток входа вызова.

    Всё, что влияет на РЕЗУЛЬТАТ и на ЦЕНУ, должно попасть сюда: модель, промпт,
    seed, соотношение сторон, референсы. Что не влияет — не должно, иначе один и тот же
    кадр будет считаться разным и оплачиваться заново. Пустые значения отбрасываются,
    чтобы добавление необязательного параметра не обесценивало старые строки.
    """
    clean = {k: v for k, v in parts.items() if v not in (None, "", [], {})}
    blob = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def reserve(c: sqlite3.Connection, *, tenant_id: str, project_id: str, kind: str,
            unit: str, fp: str, provider: str = "", model: str = "",
            release_id: str = "", now: float | None = None) -> dict:
    """Заявить вызов ДО обращения к провайдеру.

    Возвращает {"action": …, "status": …, "call_id": …, "row": …}:

      action=new     строки не было — вызов делать можно
      action=skip    результат уже получен — вызов делать НЕЛЬЗЯ, взять result_path
      action=verify  заказ уже создавался (или судьба неизвестна) — спросить провайдера
                     про remote_order_id, НЕ создавать второй
      action=retry   прошлая попытка отвалилась до создания заказа — можно пробовать

    Гонка двух воркеров разрешается уникальным индексом: проигравший получит `verify`,
    а не вторую заявку.
    """
    now = time.time() if now is None else now
    cur = c.execute(
        "INSERT OR IGNORE INTO provider_calls(tenant_id,project_id,release_id,kind,unit,"
        "fingerprint,provider,model,status,attempts,created,updated) "
        "VALUES(?,?,?,?,?,?,?,?,?,1,?,?)",
        (tenant_id, project_id, release_id, kind, unit, fp, provider, model,
         RESERVED, now, now))
    row = c.execute(
        "SELECT * FROM provider_calls WHERE project_id=? AND kind=? AND unit=? "
        "AND fingerprint=?", (project_id, kind, unit, fp)).fetchone()
    if cur.rowcount == 1:
        return {"action": "new", "status": RESERVED, "call_id": row["id"], "row": row}

    status = row["status"]
    action = _ACTION.get(status, VERIFY)
    if action == RETRY:
        # Прошлая попытка не дошла до заказа — открываем строку заново, считая попытку.
        c.execute("UPDATE provider_calls SET status=?, error='', attempts=attempts+1, "
                  "updated=? WHERE id=?", (RESERVED, now, row["id"]))
        row = c.execute("SELECT * FROM provider_calls WHERE id=?", (row["id"],)).fetchone()
        return {"action": "new", "status": RESERVED, "call_id": row["id"], "row": row}
    return {"action": action, "status": status, "call_id": row["id"], "row": row}


def mark_ordered(c: sqlite3.Connection, call_id: int, remote_order_id: str,
                 now: float | None = None) -> None:
    """Заказ создан. С этого момента повтор запрещён — только проверка существующего."""
    now = time.time() if now is None else now
    c.execute("UPDATE provider_calls SET status=?, remote_order_id=?, updated=? "
              "WHERE id=?", (ORDERED, str(remote_order_id or ""), now, call_id))


def mark_delivered(c: sqlite3.Connection, call_id: int, result_path: str,
                   cost: float | None = None, now: float | None = None) -> None:
    """Результат получен. `result_path` обязателен: «готово» без файла — не готово."""
    if not result_path:
        raise ValueError("mark_delivered без result_path: «готово» доказывается файлом")
    now = time.time() if now is None else now
    c.execute("UPDATE provider_calls SET status=?, result_path=?, cost=?, error='', "
              "updated=? WHERE id=?", (DELIVERED, result_path, cost, now, call_id))


def mark_failed(c: sqlite3.Connection, call_id: int, error: str,
                ordered: bool = False, now: float | None = None) -> None:
    """Вызов не удался.

    `ordered=False` — провайдер отказал ДО создания заказа (документированный 429,
    отказ валидации): денег нет, повтор безопасен, строка уходит в `failed`.

    `ordered=True` — заказ создан, но результат не получен: обрыв, таймаут, 5xx.
    Это НЕОДНОЗНАЧНО, и строка остаётся в `ordered`, чтобы следующая попытка пошла
    проверять заказ, а не создавать второй. Ровно та разница, из-за которой в озвучке
    повтор разрешён только на явный 429.
    """
    now = time.time() if now is None else now
    if ordered:
        c.execute("UPDATE provider_calls SET error=?, updated=? WHERE id=?",
                  (str(error or "")[:500], now, call_id))
        return
    c.execute("UPDATE provider_calls SET status=?, error=?, updated=? WHERE id=?",
              (FAILED, str(error or "")[:500], now, call_id))


def open_calls(c: sqlite3.Connection, project_id: str) -> list:
    """Вызовы проекта, судьба которых не выяснена. Это и есть список «сходить спросить»."""
    return c.execute(
        "SELECT * FROM provider_calls WHERE project_id=? AND status IN (?,?) "
        "ORDER BY id", (project_id, RESERVED, ORDERED)).fetchall()


def project_summary(c: sqlite3.Connection, project_id: str) -> dict:
    """Сводка по проекту: сколько вызовов в каком состоянии и сколько потрачено.

    Отдаёт то, чего просит ТЗ §12 — «до запуска видно примерную стоимость»: колонка
    `cost` заполняется по факту, и сумма по проекту становится наблюдаемой величиной,
    а не оценкой.
    """
    out = {"by_status": {}, "calls": 0, "cost": 0.0}
    for r in c.execute(
            "SELECT status, COUNT(*) n, COALESCE(SUM(cost),0) c FROM provider_calls "
            "WHERE project_id=? GROUP BY status", (project_id,)):
        out["by_status"][r["status"]] = r["n"]
        out["calls"] += r["n"]
        out["cost"] += float(r["c"] or 0)
    return out


# ─────────────────────────── свободное место на нодах ───────────────────────────

# Тревога про диск смотрела только на сервер. Рендер и заливка идут на другой машине,
# и про её место сервер не знал ничего — а «мало места» без указания машины посылает
# человека проверять не тот компьютер.
NODE_DISK_KEY = "node_disk"
# Через сколько молчания сведения о машине считаются протухшими. Ноды опрашивают
# очередь раз в двадцать секунд, так что четверть часа тишины — это уже тишина.
NODE_DISK_STALE = 900


def note_node_disk(c: sqlite3.Connection, host: str, role: str, free_gb, total_gb,
                   now: float | None = None) -> None:
    """Запомнить, сколько места на машине. Молча игнорирует неполные сведения:
    старый пуллер их не шлёт, и это не повод падать."""
    if not host or free_gb is None:
        return
    now = time.time() if now is None else now
    try:
        data = json.loads(_state_raw(c, NODE_DISK_KEY) or "{}")
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        data = {}
    data[str(host)[:40]] = {"role": str(role or "")[:20], "at": now,
                            "free_gb": float(free_gb),
                            "total_gb": float(total_gb or 0)}
    c.execute("INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES(?,?,?)",
              (NODE_DISK_KEY, json.dumps(data, ensure_ascii=False), now))


def node_disk(c: sqlite3.Connection, now: float | None = None) -> list:
    """Что известно про машины: [{host, role, free_gb, total_gb, age_sec, stale}]."""
    now = time.time() if now is None else now
    try:
        data = json.loads(_state_raw(c, NODE_DISK_KEY) or "{}")
    except ValueError:
        return []
    out = []
    for host, rec in sorted((data or {}).items()):
        if not isinstance(rec, dict):
            continue
        age = now - float(rec.get("at") or 0)
        out.append({"host": host, "role": rec.get("role") or "",
                    "free_gb": rec.get("free_gb"), "total_gb": rec.get("total_gb"),
                    "age_sec": int(age), "stale": age > NODE_DISK_STALE})
    return out


def _state_raw(c: sqlite3.Connection, key: str):
    try:
        row = c.execute("SELECT v FROM admin_state WHERE k=?", (key,)).fetchone()
    except Exception:                                              # noqa: BLE001
        return None
    return row[0] if row else None
