#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Теги каналов и очередь донорских ссылок.

Теги — единственный способ группировать каналы. Язык и ниша тоже показываются тегами,
но хранятся своими колонками: от языка зависит часовой пояс и флажок, от ниши — ничего,
кроме показа. Свои теги владелец добавляет сам, их список ничем не ограничен.

Тег НИКОГДА не определяет, какой ролик куда пойдёт: он только ищет и фильтрует.
"""

from __future__ import annotations

import datetime as dt
import json
import re

from channel_slots import next_free_slot, upcoming_slots

OEMBED = "https://www.youtube.com/oembed"
YT_HOSTS = ("youtube.com", "youtu.be")


def donor_title(url: str) -> str:
    """Оригинальное название донорского ролика.

    Через открытый oembed YouTube — без ключей и квот. Не ответил или ролик закрыт —
    возвращаем пусто: показывать вместо названия ссылку хуже, но терять строку нельзя.
    """
    if not any(h in (url or "") for h in YT_HOSTS):
        return ""          # не YouTube — спрашивать не у кого, в сеть не ходим
    try:
        import requests
        r = requests.get(OEMBED, params={"url": url, "format": "json"}, timeout=6)
        if r.status_code != 200:
            return ""
        return (r.json().get("title") or "").strip()[:300]
    except Exception:
        return ""

URL_RE = re.compile(r"https?://\S+")


# ─────────────────────────── теги ───────────────────────────

def tag_names(c, channel_id: int) -> list:
    rows = c.execute(
        "SELECT t.name FROM pub_tags t JOIN pub_channel_tags ct ON ct.tag_id=t.id "
        "WHERE ct.channel_id=? ORDER BY t.name COLLATE NOCASE", (channel_id,)).fetchall()
    return [r[0] for r in rows]


def all_tags(c, tenant_id: str) -> list:
    """Все теги пользователя со счётчиком каналов — для полосы фильтров."""
    rows = c.execute(
        "SELECT t.name, COUNT(ct.channel_id) n FROM pub_tags t "
        "LEFT JOIN pub_channel_tags ct ON ct.tag_id=t.id "
        "LEFT JOIN pub_channels ch ON ch.id=ct.channel_id AND ch.status!='archived' "
        "WHERE t.tenant_id=? GROUP BY t.id HAVING n>0 ORDER BY n DESC, t.name COLLATE NOCASE",
        (tenant_id,)).fetchall()
    return [(r[0], r[1]) for r in rows]


def set_tags(c, tenant_id: str, channel_id: int, names: list, now: float) -> None:
    """Переписать теги канала. Неизвестные заводятся на лету."""
    clean = []
    for raw in names:
        name = " ".join(str(raw).split()).strip().lower()[:40]
        if name and name not in clean:
            clean.append(name)
    c.execute("DELETE FROM pub_channel_tags WHERE channel_id=?", (channel_id,))
    for name in clean:
        row = c.execute("SELECT id FROM pub_tags WHERE tenant_id=? AND name=?",
                        (tenant_id, name)).fetchone()
        if row:
            tag_id = row[0]
        else:
            tag_id = c.execute("INSERT INTO pub_tags(tenant_id,name,created) VALUES(?,?,?)",
                               (tenant_id, name, now)).lastrowid
        c.execute("INSERT OR IGNORE INTO pub_channel_tags(channel_id,tag_id) VALUES(?,?)",
                  (channel_id, tag_id))


def parse_tags(raw: str) -> list:
    """«ww2, сон, тест» → ['ww2','сон','тест']."""
    return [x for x in re.split(r"[,\n;]+", raw or "") if x.strip()]


# ─────────────────────── очередь ссылок ───────────────────────

def parse_links(raw: str) -> list:
    """Текст из поля → список ссылок. По одной в строке либо просто вперемешку."""
    out, seen = [], set()
    for line in (raw or "").splitlines():
        for url in URL_RE.findall(line) or ([line.strip()] if line.strip() else []):
            url = url.strip().rstrip(",;")
            if url.startswith("http") and url not in seen:
                seen.add(url)
                out.append(url)
    return out


def taken_slots(c, channel_id: int) -> list:
    """Уже занятые моменты этого канала — чтобы не поставить два ролика в один слот."""
    rows = c.execute(
        "SELECT slot_at FROM pub_plan_items WHERE channel_id=? AND slot_at IS NOT NULL "
        "AND status IN ('planned','frozen','running','done','error')", (channel_id,)).fetchall()
    return [dt.datetime.fromtimestamp(r[0], dt.timezone.utc) for r in rows if r[0]]


def add_links(c, tenant_id: str, channel, urls: list, now: float, slot=None,
              titles: dict = None) -> dict:
    """Разложить ссылки по свободным выходам канала.

    `slot` задан — ставим ИМЕННО в этот выход (так работает поле в пустой строке
    таблицы: куда вставил, туда и встало). Без него — по ближайшим свободным подряд.

    Ссылка, которой не хватило места в горизонте, всё равно сохраняется, просто без
    даты. Она займёт выход, как только он освободится: терять вставленное нельзя.
    """
    schedule = json.loads(channel["schedule"] or "{}")
    placed, queued, skipped = [], 0, []
    busy = taken_slots(c, channel["id"])
    horizon = int(channel["horizon_days"] or 3)
    # «Сейчас» берём из аргумента, а не из системных часов: иначе одна и та же вставка
    # даёт разный результат в зависимости от времени суток, и поведение нельзя проверить.
    moment = dt.datetime.fromtimestamp(float(now), dt.timezone.utc)
    # Названия доноров тянем ДО первой записи: сетевые вызовы под открытым write-замком
    # держали бы всю базу кабинета до шести секунд на ссылку.
    if titles is None:
        titles = {u: donor_title(u) for u in urls}

    for url in urls:
        dup = c.execute(
            "SELECT id FROM pub_plan_items WHERE channel_id=? AND donor_url=? "
            "AND status IN ('planned','frozen','running','done')",
            (channel["id"], url)).fetchone()
        if dup:
            skipped.append(url)
            continue
        if slot is not None and not placed:
            stamp = float(slot)                     # точный выход, выбранный владельцем
            mode = "exact"
            placed.append(dt.datetime.fromtimestamp(stamp, dt.timezone.utc))
        else:
            point = (next_free_slot(schedule, channel["tz"], busy, horizon, moment)
                     if schedule else None)
            mode = "auto"
            if point is not None:
                busy.append(point)
                stamp = point.timestamp()
                placed.append(point)
            else:
                stamp = None
                queued += 1
        try:
            c.execute(
                "INSERT INTO pub_plan_items(tenant_id,channel_id,donor_url,donor_title,slot_at,"
                "slot_mode,position,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                (tenant_id, channel["id"], url, (titles or {}).get(url, ""), stamp, mode,
                 now, now, now))
        except Exception as exc:
            if "UNIQUE" in str(exc):
                skipped.append(url)      # двойной сабмит формы: выход уже занят — не роняем пачку
                continue
            raise
    return {"placed": placed, "queued": queued, "skipped": skipped}


def plan_rows(c, channel_id: int) -> list:
    return c.execute(
        "SELECT * FROM pub_plan_items WHERE channel_id=? AND status!='cancelled' "
        "ORDER BY CASE WHEN slot_at IS NULL THEN 1 ELSE 0 END, slot_at, id",
        (channel_id,)).fetchall()


def coverage(c, channel, now=None) -> list:
    """Слоты канала на горизонт с привязанной ссылкой — строка для «Календаря».

    Возвращает [(момент, строка плана или None)] по возрастанию: пустая позиция и есть
    то место, куда нужно вставить ссылку.
    """
    schedule = json.loads(channel["schedule"] or "{}")
    if not schedule:
        return []
    slots = upcoming_slots(schedule, channel["tz"], int(channel["horizon_days"] or 3), now)
    rows = {}
    for r in plan_rows(c, channel["id"]):
        if r["slot_at"] and round(r["slot_at"]) not in rows:
            rows[round(r["slot_at"])] = r      # первая (живая) строка слота не затирается
    return [(s, rows.get(round(s.timestamp()))) for s in slots]


def releases_by_item(c, channel_id: int) -> dict:
    """Выпуски канала, разложенные по строкам плана: {plan_item_id: строка выпуска}."""
    rows = c.execute(
        "SELECT * FROM pub_releases WHERE channel_id=? ORDER BY id", (channel_id,)).fetchall()
    return {r["plan_item_id"]: r for r in rows}


def plan_by_channel(c, tenant_id: str, since: float, until: float) -> dict:
    """Строки плана всех каналов в окне — ОДНИМ запросом.

    Отдельный запрос на канал (70 каналов = 70 запросов) на пятистах превратит
    список в мёртвый экран, поэтому берём всё разом и раскладываем в память.
    """
    rows = c.execute(
        "SELECT channel_id, slot_at, status FROM pub_plan_items "
        "WHERE tenant_id=? AND status!='cancelled' AND slot_at IS NOT NULL "
        "AND slot_at BETWEEN ? AND ?", (tenant_id, since, until)).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["channel_id"], set()).add(round(r["slot_at"]))
    return out


def troubles_by_channel(c, tenant_id: str) -> dict:
    """Каналы, где есть застрявший выпуск: {channel_id: причина}. Тоже одним запросом."""
    rows = c.execute(
        "SELECT r.channel_id, r.blocked_reason, MAX(r.updated) FROM pub_releases r "
        "JOIN pub_plan_items p ON p.id=r.plan_item_id AND p.status!='cancelled' "
        "WHERE r.tenant_id=? AND r.status='blocked' GROUP BY r.channel_id",
        (tenant_id,)).fetchall()
    return {r["channel_id"]: (r["blocked_reason"] or "нужен разбор") for r in rows}


def work_by_channel(c, tenant_id: str) -> dict:
    """Что реально происходит на каждом канале — одним запросом на весь список.

    Плитка должна отвечать на вопрос «как идут дела», а не «сколько мест в расписании
    занято»: владелец смотрит на список, чтобы увидеть работу, а заполненность выходов
    об этом не говорит ничего — она одинаковая и когда ролики делаются, и когда стоят.
    """
    rows = c.execute(
        "SELECT p.channel_id cid, "
        " COUNT(*) queued, "
        " SUM(CASE WHEN r.status IN ('pending','running') THEN 1 ELSE 0 END) working, "
        " SUM(CASE WHEN r.status='done' THEN 1 ELSE 0 END) ready, "
        " SUM(CASE WHEN r.status='blocked' THEN 1 ELSE 0 END) stuck "
        "FROM pub_plan_items p LEFT JOIN pub_releases r ON r.plan_item_id=p.id "
        "WHERE p.tenant_id=? AND p.status!='cancelled' GROUP BY p.channel_id",
        (tenant_id,)).fetchall()
    return {r["cid"]: {"queued": r["queued"] or 0, "working": r["working"] or 0,
                       "ready": r["ready"] or 0, "stuck": r["stuck"] or 0} for r in rows}


def channel_state(row, taken: set, trouble: str, now=None, work=None) -> dict:
    """Что происходит на канале — человеческой строкой, а не счётом слотов."""
    import json as _json
    from channel_slots import upcoming_slots
    try:
        schedule = _json.loads(row["schedule"] or "{}")
    except Exception:
        schedule = {}
    w = work or {}
    queued = int(w.get("queued") or 0)
    working = int(w.get("working") or 0)
    ready = int(w.get("ready") or 0)
    stuck = int(w.get("stuck") or 0)
    waiting = max(0, queued - working - ready - stuck)

    # Полоса заполнения показывает продвижение работы: сделано из взятого в план.
    filled, total = ready, queued

    if not schedule:
        return {"kind": "none", "text": "расписание не задано", "filled": 0, "total": 0}
    if not queued:
        slots = upcoming_slots(schedule, row["tz"], int(row["horizon_days"] or 3), now)
        return {"kind": "warn" if slots else "none",
                "text": "ссылок нет — вставьте доноров", "filled": 0, "total": 0}

    части = []
    if ready:
        части.append(f"{ready} готово")
    if working:
        части.append(f"{working} делается")
    if waiting:
        части.append(f"{waiting} ждёт")
    if stuck:
        части.append(f"{stuck} в разборе")
    text = " · ".join(части) or f"{queued} в очереди"

    if stuck:
        kind = "err"
    elif working:
        kind = "warn"
    elif ready >= queued:
        kind = "ok"
    else:
        kind = "warn"
    return {"kind": kind, "text": text, "filled": filled, "total": total}


def _links_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "ссылки"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "ссылок"
    return "ссылок"


def move_item(c, item_id: int, target_slot: float, now: float) -> None:
    """Перетащить ссылку в другой выход. Если там уже стоит ссылка — меняются местами."""
    row = c.execute("SELECT * FROM pub_plan_items WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise ValueError("строка не найдена")
    if row["frozen"]:
        raise ValueError("ролик уже взят в работу — переставить нельзя")
    other = c.execute(
        "SELECT * FROM pub_plan_items WHERE channel_id=? AND slot_at=? AND id!=? "
        "AND status IN ('planned','frozen','running','done')",
        (row["channel_id"], target_slot, item_id)).fetchone()
    if other and other["frozen"]:
        raise ValueError("в том выходе ролик уже готовится — менять местами нельзя")
    # Освобождаем целевой слот через промежуточное состояние: иначе уникальный
    # индекс не даст двум строкам одновременно указывать на одно время.
    if other:
        moved = c.execute("UPDATE pub_plan_items SET slot_at=NULL, updated=? "
                          "WHERE id=? AND frozen=0", (now, other["id"]))
        if moved.rowcount != 1:
            raise ValueError("тот ролик уже взят в работу — менять местами поздно")
    moved = c.execute("UPDATE pub_plan_items SET slot_at=?, updated=? "
                      "WHERE id=? AND frozen=0", (target_slot, now, item_id))
    if moved.rowcount != 1:
        # Пока владелец тянул строку, движок успел её заморозить: возвращаем всё как было.
        if other:
            c.execute("UPDATE pub_plan_items SET slot_at=?, updated=? WHERE id=?",
                      (target_slot, now, other["id"]))
        raise ValueError("ролик уже взят в работу — переставить нельзя")
    if other:
        c.execute("UPDATE pub_plan_items SET slot_at=?, updated=? WHERE id=?",
                  (row["slot_at"], now, other["id"]))


def cancel_item(c, item_id: int, now: float) -> str:
    """Убрать ролик из очереди — вместе с уже начатой работой над ним.

    Снять строку и оставить выпуск работать было бы худшим из вариантов: из таблицы
    ролик пропал, а система продолжает его делать и потом зальёт.
    """
    from pub_engine import reached_irreversible
    row = c.execute("SELECT * FROM pub_plan_items WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise ValueError("строка не найдена")
    rel = c.execute("SELECT * FROM pub_releases WHERE plan_item_id=? ORDER BY id DESC LIMIT 1",
                    (item_id,)).fetchone()
    if rel and reached_irreversible(c, rel["release_id"]):
        raise ValueError("ролик уже заливается на канал — из очереди его убирать поздно")
    if rel:
        job = c.execute("SELECT status FROM pub_upload_jobs WHERE release_id=?",
                        (rel["release_id"],)).fetchone()
        if job and job["status"] == "running":
            raise ValueError("машина уже взяла ролик в заливку — убирать поздно")
        # Ждущее задание гасим ОБЯЗАТЕЛЬНО: иначе нода позже заберёт его и зальёт
        # ролик, которого в таблице уже нет.
        c.execute("UPDATE pub_upload_jobs SET status='cancelled', error=?, updated=? "
                  "WHERE release_id=? AND status='queued'",
                  ("убран владельцем из очереди", now, rel["release_id"]))
    if rel and rel["status"] in ("pending", "running", "blocked"):
        c.execute("UPDATE pub_releases SET status='cancelled', updated=? WHERE release_id=?",
                  (now, rel["release_id"]))
    c.execute("UPDATE pub_plan_items SET status='cancelled', updated=? WHERE id=?", (now, item_id))
    return rel["release_id"] if rel else ""


def release_stages(c, channel_id: int) -> dict:
    """Состояние этапов по каждому выпуску канала — ОДНИМ запросом.

    Берём статус прямо из очереди кабинета, а не из своих отметок: там правда о том,
    идёт этап, стоит в очереди или упал.
    """
    rows = c.execute(
        "SELECT j.release_id, j.kind, q.status FROM pub_release_jobs j "
        "LEFT JOIN queue_tasks q ON CAST(q.job_id AS TEXT)=j.remote_job_id "
        "WHERE j.service='vsgen' AND j.release_id IN "
        "(SELECT release_id FROM pub_releases WHERE channel_id=?)", (channel_id,)).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["release_id"], {})[r["kind"]] = r["status"] or "queued"
    return out


def upload_jobs_by_release(c, channel_id: int) -> dict:
    rows = c.execute(
        "SELECT * FROM pub_upload_jobs WHERE channel_id=?", (channel_id,)).fetchall()
    return {r["release_id"]: r for r in rows}
