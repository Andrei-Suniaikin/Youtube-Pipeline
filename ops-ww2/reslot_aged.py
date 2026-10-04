#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Старый кабинет (vsgen): готовые ролики, чьё задание заливки «состарилось», — на новый выход.

04.10.2026. 17 готовых роликов ВВ2 стоят в разборе с «к времени выхода не успели —
задание состарилось в очереди». Файл в YouTube ни разу не уходил (номера ролика нет, фаза
задания — до начала передачи). Ворота выдачи (`pub_upload_api`, ~строка 146) отсекают
задание, если его выход ближе 30 минут, а перенести выход старый движок не умеет: вернуть
задание в очередь (`/root/ops/requeue.py`) бесполезно — оно возвращается с тем же прошедшим
временем и через минуту умирает снова (так и вышло 04.10 в 09:14–09:34 UTC).

Что делает один запуск:
  1. находит такие выпуски: в разборе с этой причиной, задание `error`/`repair`, номера
     ролика нет, фаза задания «до передачи» ("", preflight, profile_opened), канал
     активен, выпуск не подтверждён руками;
  2. каждому подбирает ближайший СВОБОДНЫЙ выход канала по его же расписанию — не ближе
     LEAD_H часов (по умолчанию 4: пока до задания дойдёт очередь из двух мест, выход не
     должен снова состариться) и в окне max(горизонт канала, 30 дней);
  3. с --apply в ОДНОЙ транзакции на ролик: новый выход в строке плана, в выпуске и в
     задании (`slot_at` + `schedule_iso` в поясе канала), задание — снова `queued`,
     выпуск — `running`, строка в журнал выпуска.

По умолчанию ничего не пишет. Выход защищён уникальным индексом плана
(idx_pub_plan_slot_busy_v2): занятый тем временем выход — откат и пропуск.

Запуск на сервере (то же окружение, что у моста):
    /root/ops/ww2_sheet/venv/bin/python reslot_aged.py            # только показать
    /root/ops/ww2_sheet/venv/bin/python reslot_aged.py --apply    # записать
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import datetime as dt
import json
import os
import sqlite3
import time

SERVER_DIR = os.environ.get("RESLOT_SERVER_DIR", "/root/VideoStudio/server")
sys.path.insert(0, SERVER_DIR)

from channel_slots import ScheduleError, upcoming_slots   # noqa: E402
from pub_publish import schedule_iso                        # noqa: E402

DB_PATH = os.environ.get("VS_SERVER_DB", os.path.join(SERVER_DIR, "vsgen.db"))
AGED = "к времени выхода не успели%"
SAFE_PHASES = ("", "preflight", "profile_opened")     # как у /root/ops/requeue.py
LEAD_H = float(os.environ.get("RESLOT_LEAD_H", "4"))
MIN_WINDOW_DAYS = 30
UPLOAD_DEADLINE = 1800       # PHASE_DEADLINES["upload_running"] старого движка


def job_phase(raw: str) -> str:
    raw = raw or ""
    if raw.startswith("{"):
        try:
            return str((json.loads(raw) or {}).get("phase") or "")
        except ValueError:
            return "?"
    return raw


def taken(c, channel_id: int) -> set:
    """Занятые выходы канала — тем же правилом, что pub_tags.taken_slots."""
    return {round(r[0]) for r in c.execute(
        "SELECT slot_at FROM pub_plan_items WHERE channel_id=? AND slot_at IS NOT NULL "
        "AND status IN ('planned','frozen','running','done','error')", (channel_id,))}


def pick_slot(ch, busy: set, now: float, lead_h: float = None):
    lead = (LEAD_H if lead_h is None else lead_h) * 3600
    days = max(int(ch["horizon_days"] or 3), MIN_WINDOW_DAYS)
    schedule = json.loads(ch["schedule"] or "{}")
    for p in upcoming_slots(schedule, ch["tz"], days,
                            dt.datetime.fromtimestamp(now, dt.timezone.utc)):
        stamp = round(p.timestamp())
        if stamp in busy or stamp <= now + lead:
            continue
        return stamp
    return None


def candidates(c) -> list:
    rows = c.execute(
        "SELECT r.release_id, r.plan_item_id, r.channel_id, r.slot_at old_slot, "
        "u.id job, u.status jst, COALESCE(u.video_id,'') vid, COALESCE(u.phase,'') jphase, "
        "ch.name, ch.tz, ch.schedule, ch.horizon_days, COALESCE(ch.status,'?') chst "
        "FROM pub_releases r JOIN pub_upload_jobs u ON u.release_id=r.release_id "
        "LEFT JOIN pub_channels ch ON ch.id=r.channel_id "
        "WHERE r.status='blocked' AND r.blocked_reason LIKE ? ORDER BY r.slot_at, r.id",
        (AGED,)).fetchall()
    out = []
    for r in rows:
        why = None
        if r["vid"]:
            why = f"есть номер ролика {r['vid']} — файл уже уходил"
        elif job_phase(r["jphase"]) not in SAFE_PHASES:
            why = f"фаза задания «{job_phase(r['jphase'])}» — передача могла начаться"
        elif r["jst"] not in ("error", "repair"):
            why = f"задание в статусе «{r['jst']}»"
        elif r["chst"] != "active":
            why = f"канал {r['chst']}"
        elif _hand_confirmed(c, r["release_id"]):
            why = "подтверждён руками"
        out.append((r, why))
    return out


def _hand_confirmed(c, rid) -> bool:
    try:
        row = c.execute("SELECT hand_confirmed FROM pub_releases WHERE release_id=?",
                        (rid,)).fetchone()
    except sqlite3.OperationalError:
        return False                 # у старой базы такой колонки нет
    return bool(row and row[0])


def apply_one(c, r, slot: int, iso: str, now: float) -> str:
    """Одна транзакция на ролик. Возвращает "" или причину отказа."""
    c.execute("BEGIN IMMEDIATE")
    try:
        cur = c.execute("SELECT status, blocked_reason FROM pub_releases WHERE release_id=?",
                        (r["release_id"],)).fetchone()
        if not cur or cur["status"] != "blocked" or not str(cur["blocked_reason"] or "") \
                .startswith("к времени выхода не успели"):
            c.execute("ROLLBACK")
            return "выпуск уже изменился — не трогаю"
        job = c.execute("SELECT status, COALESCE(video_id,'') vid, COALESCE(phase,'') ph "
                        "FROM pub_upload_jobs WHERE id=?", (r["job"],)).fetchone()
        if not job or job["vid"] or job_phase(job["ph"]) not in SAFE_PHASES \
                or job["status"] not in ("error", "repair"):
            c.execute("ROLLBACK")
            return "задание уже изменилось — не трогаю"
        c.execute("UPDATE pub_plan_items SET slot_at=?, status='running', updated=? WHERE id=?",
                  (slot, now, r["plan_item_id"]))
        c.execute("UPDATE pub_releases SET slot_at=?, status='running', blocked_reason=NULL, "
                  "deadline=?, updated=? WHERE release_id=?",
                  (slot, now + UPLOAD_DEADLINE + 7200, now, r["release_id"]))
        c.execute("UPDATE pub_upload_jobs SET slot_at=?, schedule_iso=?, status='queued', "
                  "error=NULL, verdict=NULL, claim_token=NULL, claimed=NULL, lease_until=NULL, "
                  "heartbeat=NULL, finished=NULL, updated=? WHERE id=?",
                  (slot, iso, now, r["job"]))
        c.execute("INSERT INTO pub_release_log(release_id,ts,level,step,message) "
                  "VALUES(?,?,?,?,?)",
                  (r["release_id"], now, "warn", "заливка",
                   f"прежний выход прошёл, пока задание ждало очереди, — перенёс выход на "
                   f"{iso} и вернул задание в очередь (файл в YouTube не уходил)"))
        c.execute("COMMIT")
        return ""
    except sqlite3.IntegrityError as exc:
        c.execute("ROLLBACK")
        return f"выход занят тем временем ({exc})"
    except Exception:
        c.execute("ROLLBACK")
        raise


def run(c, now: float, apply: bool, lead_h: float = None, say=print) -> dict:
    report = {"moved": [], "skipped": []}
    busy_by_ch = {}
    for r, why in candidates(c):
        name = (r["name"] or "—")[:30]
        if why:
            report["skipped"].append((r["release_id"], why))
            say(f"  · {r['release_id']} {name}: {why}")
            continue
        busy = busy_by_ch.setdefault(r["channel_id"], taken(c, r["channel_id"]))
        try:
            slot = pick_slot(r, busy, now, lead_h)
        except (ScheduleError, ValueError) as exc:
            report["skipped"].append((r["release_id"], f"расписание не читается: {exc}"))
            say(f"  · {r['release_id']} {name}: расписание не читается: {exc}")
            continue
        if slot is None:
            report["skipped"].append((r["release_id"], "нет свободного выхода в окне"))
            say(f"  · {r['release_id']} {name}: нет свободного выхода в окне")
            continue
        iso = schedule_iso(slot, r["tz"])
        if apply:
            err = apply_one(c, r, slot, iso, now)
            if err:
                report["skipped"].append((r["release_id"], err))
                say(f"  · {r['release_id']} {name}: {err}")
                continue
        busy.add(slot)
        report["moved"].append((r["release_id"], r["channel_id"], iso))
        old = dt.datetime.fromtimestamp(r["old_slot"], dt.timezone.utc).strftime("%d.%m")
        say(f"  {'↻' if apply else '→'} {r['release_id']} {name}: был {old}, новый выход {iso}")
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="записать (без флага — только показать)")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--lead-h", type=float, default=None)
    a = ap.parse_args(argv)
    uri = a.db if a.apply else f"file:{a.db}?mode=ro"
    c = sqlite3.connect(uri, uri=not a.apply, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    print(("ЗАПИСЬ" if a.apply else "ПРОБА (ничего не пишу)") + f" · база {a.db}")
    rep = run(c, time.time(), a.apply, a.lead_h)
    print(f"перенесено: {len(rep['moved'])} · пропущено: {len(rep['skipped'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
