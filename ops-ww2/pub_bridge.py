#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Мост «Очередь» (Google-таблица ВВ2) → движок публикаций кабинета (vsgen).

Что делает один запуск:
  1. читает лист «Очередь»: строки со статусом «Готово к заливке», в «Заметке» которых
     ровно один номер проекта p_xxxxxxxxxx и заметка новой волны («волна ДД.ММ · …»);
  2. канал строки → лист «Каналы» → UC → pub_channels (по UC, другого ключа нет);
  3. проверяет проект: последний готовый рендер, mp4 на месте и того же размера, кадры
     googler/* не правили после рендера, по проекту не идут задачи, главы готовы или
     срок ожидания глав вышел;
  4. заводит в ОДНОЙ транзакции строку плана (frozen) на ближайший свободный выход канала
     и выпуск с project_id на шаге media_ready — следующим движок сам сделает обложку,
     название, описание и задание заливки;
  5. по умолчанию ничего не пишет (--dry). Пишет только с --apply.

Идемпотентность: выпуск с тем же project_id уже есть → строка пропускается. Проверка
повторяется внутри транзакции записи (BEGIN IMMEDIATE), поэтому два запуска подряд
второго выпуска не заведут. Слот защищён уникальным индексом движка
idx_pub_plan_slot_busy_v2: занятый тем временем выход — откат и повтор в следующий раз.

Почему выпуск заводится сразу на шаге media_ready, а не строкой плана 'planned':
строку 'planned' движок сам превращает в выпуск с шага 'created' — то есть заказывает
рерайт и делает НОВЫЙ ролик. Готовый проект так провести нельзя, поэтому строка плана
рождается уже замороженной (frozen): ни reflow_slots, ни materialize её не трогают.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True        # не писать .pyc в боевую папку сервера

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import sqlite3
import time

SERVER_DIR = os.environ.get("PUB_BRIDGE_SERVER_DIR", "/root/VideoStudio/server")
sys.path.insert(0, SERVER_DIR)

from channel_slots import ScheduleError, upcoming_slots   # noqa: E402
from pub_engine import PHASE_DEADLINES, log_phase, new_release_id, reached_irreversible   # noqa: E402
from pub_log import add as log_line   # noqa: E402
from pub_publish import chapters_enabled, find_video   # noqa: E402
from pub_tags import taken_slots   # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_link   # noqa: E402  — проект из кабинета Гринда для канала кабинета testpartner

# ─────────────────────────── настройки ───────────────────────────

SHEET_ID = os.environ.get("WW2_SHEET_ID", "1wio2te9wkWkIJ1n-caPlzpzSuYM8ajlOHvuzNvEwjPE")
SA_FILE = os.environ.get("WW2_SA", "/root/ops/ww2_sheet/google_service_account.json")
DB_PATH = os.environ.get("VS_SERVER_DB", os.path.join(SERVER_DIR, "vsgen.db"))
TENANTS_DIR = os.environ.get("PUB_BRIDGE_TENANTS_DIR", "/data/tenants")   # как _project_dir в main.py
STATE_DIR = os.environ.get("PUB_BRIDGE_STATE_DIR", "/root/ops/pub_bridge")

# Какие строки «Очереди» мост вообще берёт. По умолчанию — только заметки новой волны
# («волна 18.09 · проект p_… — рендер готов»). Старые «восстановлено 17.09» (340 строк)
# сюда НЕ попадают: среди них есть начатые заливки и строки с двумя проектами.
NOTE_RE = os.environ.get("PUB_BRIDGE_NOTE_RE", r"^\s*волна \d{1,2}\.\d{2}\s*·")
READY_STATUS = os.environ.get("PUB_BRIDGE_STATUS", "Готово к заливке")
CHAPTERS_WAIT_H = float(os.environ.get("PUB_BRIDGE_CHAPTERS_WAIT_H", "6"))
MIN_LEAD_H = float(os.environ.get("PUB_BRIDGE_MIN_LEAD_H", "2"))
MAX_NEW = int(os.environ.get("PUB_BRIDGE_MAX_NEW", "50"))
# Профиль канала в движке обязан совпадать с «Профилем» в «Каналах»: задание заливки уходит
# на профиль из движка. Расхождение = кто-то из двух ошибся, лить нельзя, пока не сверят.
CHECK_PROFILE = os.environ.get("PUB_BRIDGE_CHECK_PROFILE", "1") != "0"
# Отсечка по времени рендера: рендеры, начатые РАНЬШЕ, не считаются готовыми (например,
# сделаны до стирания значка). ISO («2026-09-19T18:00+02:00») или epoch. Пусто — без отсечки.
MIN_RENDER_AT = os.environ.get("PUB_BRIDGE_MIN_RENDER_AT", "").strip()


def _parse_moment(text: str) -> float:
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    point = dt.datetime.fromisoformat(text)
    if point.tzinfo is None:
        raise SystemExit(f"PUB_BRIDGE_MIN_RENDER_AT без часового пояса: {text!r}")
    return point.timestamp()

PID_RE = re.compile(r"\bp_[0-9a-f]{10}\b")
UC_RE = re.compile(r"^UC[0-9A-Za-z_-]{22}$")
YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([0-9A-Za-z_-]{11})")
CODE_IN_PROJECT_RE = re.compile(r"^[KК](\d{2,4})_")          # «K031_1_wp — …»
CODE_IN_SHEET_RE = re.compile(r"(\d{2,4})")                  # «К-031»
FRAME_EXT = (".jpg", ".jpeg", ".png", ".webp", ".mp4")
ACTIVE_TASK = ("queued", "running")

# Заголовки ищем по имени, а не по букве колонки: колонки в таблице переставляют.
Q_COLS = {"channel": "Канал", "donor": "Донор (ссылка или название)", "status": "Статус",
          "check": "Проверка", "note": "Заметка", "title": "Название оригинала",
          "topic": "Тема по-русски", "vid": "ID оригинала"}
CH_COLS = {"code": "Код", "name": "Название канала", "uc": "UC канала", "lang": "Язык",
           "profile": "Профиль"}

# Только для --what-if: пояс по языку канала (расписание владельца — 10:00 местного).
LANG_TZ = {"Польский": "Europe/Warsaw", "Чешский": "Europe/Prague", "Румынский": "Europe/Bucharest",
           "Венгерский": "Europe/Budapest", "Украинский": "Europe/Kyiv", "Испанский": "Europe/Madrid",
           "Греческий": "Europe/Athens", "Хорватский": "Europe/Zagreb", "Словацкий": "Europe/Bratislava",
           "Болгарский": "Europe/Sofia", "Немецкий": "Europe/Berlin", "Французский": "Europe/Paris",
           "Итальянский": "Europe/Rome", "Португальский": "Europe/Lisbon"}


def say(*a):
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] мост:", *a, flush=True)


def fmt_local(stamp: float, tz: str) -> str:
    from zoneinfo import ZoneInfo
    try:
        z = ZoneInfo(tz or "Europe/Warsaw")
    except Exception:
        z = dt.timezone.utc
    return dt.datetime.fromtimestamp(float(stamp), dt.timezone.utc).astimezone(z).strftime("%d.%m %H:%M")


def yt_id(url: str) -> str:
    m = YT_ID_RE.search(url or "")
    return m.group(1) if m else ""


# ─────────────────────────── таблица ───────────────────────────

def read_sheet(snapshot: str = ""):
    """Вернуть (строки «Очереди», строки «Каналов») как списки словарей. Только чтение."""
    if snapshot:
        data = json.load(open(snapshot, encoding="utf-8"))
        q, ch = data["queue"], data["channels"]
    else:
        import gspread
        gc = gspread.service_account(filename=SA_FILE)
        sh = gc.open_by_key(SHEET_ID)
        q = sh.worksheet("Очередь").get_all_values()
        ch = sh.worksheet("Каналы").get_all_values()
    return _rows(q, Q_COLS, "Очередь"), _rows(ch, CH_COLS, "Каналы")


def _rows(values, cols, title):
    if not values:
        raise SystemExit(f"лист «{title}» пуст — ничего не делаю")
    hdr = [h.strip() for h in values[0]]
    missing = [v for v in cols.values() if v not in hdr]
    if missing:
        # Колонку переименовали или удалили: данные лягут не туда — лучше не делать ничего.
        raise SystemExit(f"в листе «{title}» нет колонок {missing} — ничего не делаю")
    ix = {k: hdr.index(v) for k, v in cols.items()}
    out = []
    for n, r in enumerate(values[1:], start=2):
        r = list(r) + [""] * (len(hdr) - len(r))
        d = {k: (r[i] or "").strip() for k, i in ix.items()}
        d["_row"] = n
        out.append(d)
    return out


# ─────────────────────────── база ───────────────────────────

def connect(path: str, write: bool):
    if write:
        c = sqlite3.connect(path, timeout=30, isolation_level=None)
    else:
        # Сухой прогон открывает базу ТОЛЬКО на чтение: запись физически невозможна.
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    return c


def project_dir(tid, pid) -> str:
    return os.path.join(TENANTS_DIR, str(tid), "projects", pid)


def release_for_project(c, pid):
    return c.execute("SELECT release_id, status, phase, channel_id FROM pub_releases "
                     "WHERE project_id=? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()


def tasks_of(c, pid) -> list:
    return [dict(r) for r in c.execute(
        "SELECT job_id, kind, status, created, started, finished, output_rel, render_size "
        "FROM queue_tasks WHERE project_id=? ORDER BY job_id", (pid,)).fetchall()]


def preset_settings(c, preset_id) -> dict:
    if not preset_id:
        return {}
    try:
        row = c.execute("SELECT settings FROM presets WHERE id=?", (preset_id,)).fetchone()
        v = json.loads(row["settings"]) if row and row["settings"] else {}
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


# ─────────────────────────── проверка строки ───────────────────────────

class Skip(Exception):
    def __init__(self, code: str, text: str, wait: bool = False):
        super().__init__(text)
        self.code, self.text, self.wait = code, text, wait


def check_project(c, pid, tid, now, chapters_on: bool, min_render_at: float = 0.0):
    """Готов ли проект к выпуску. Возвращает словарь с рендером и главами или Skip."""
    proj = project_dir(tid, pid)
    if not os.path.isdir(proj):
        raise Skip("no_dir", f"папки проекта нет: {proj}")
    tasks = tasks_of(c, pid)
    busy = [t for t in tasks if t["status"] in ACTIVE_TASK and t["kind"] != "chapters"]
    if busy:
        raise Skip("busy", "по проекту идут задачи: " + ", ".join(
            f"{t['kind']} {t['job_id']} {t['status']}" for t in busy), wait=True)
    renders = [t for t in tasks if t["kind"] == "render" and t["status"] == "done"]
    if not renders:
        raise Skip("no_render", "готового рендера нет")
    render = max(renders, key=lambda t: (t["finished"] or 0, t["job_id"]))
    # Звезду Gemini убрал star_clean.py (А6 владельца, 05.10.2026): старый рендер годится,
    # пока на месте лежит ровно очищенный файл (отметка сверяется по размеру и времени).
    star = bridge_link.star_cleaned(os.path.join(proj, render["output_rel"] or "")) \
        if render["output_rel"] else {}
    if min_render_at and float(render["started"] or 0) < min_render_at and not star:
        raise Skip("render_old", f"последний рендер {render['job_id']} начат "
                                 f"{fmt_local(render['started'] or 0, 'Europe/Berlin')}, раньше отсечки "
                                 f"{fmt_local(min_render_at, 'Europe/Berlin')} — жду перерендер", wait=True)
    rel = render["output_rel"] or ""
    mp4 = os.path.join(proj, rel) if rel else ""
    if not rel or not os.path.isfile(mp4):
        raise Skip("no_mp4", f"рендер {render['job_id']} done, но файла {rel or '(пусто)'} нет")
    size = os.path.getsize(mp4)
    if render["render_size"] and int(render["render_size"]) != size and not star:
        raise Skip("size", f"размер {rel} {size} ≠ записанного {render['render_size']} — файл подменён")
    # Движок при заливке берёт файл сам (find_video): если в render/ лежит другой, более
    # свежий mp4, уедет не тот ролик. Сверяем, что он выберет ровно наш.
    engine_pick = find_video(proj, render["job_id"])
    if os.path.realpath(engine_pick or "") != os.path.realpath(mp4):
        raise Skip("other_mp4", f"движок взял бы {os.path.basename(engine_pick or '') or 'ничего'}, "
                                f"а готовый рендер — {os.path.basename(mp4)}")
    # Кадры правили после рендера (очистка значка и т.п.) — ролик устарел, ждём перерендер.
    # Смотрим только файлы кадров верхнего уровня googler/: служебные json и карантин не в счёт.
    started = float(render["started"] or render["finished"] or 0)
    newest, newest_name = 0.0, ""
    gdir = os.path.join(proj, "googler")
    try:
        with os.scandir(gdir) as it:
            for e in it:
                if e.is_file(follow_symlinks=False) and e.name.lower().endswith(FRAME_EXT):
                    m = e.stat(follow_symlinks=False).st_mtime
                    if m > newest:
                        newest, newest_name = m, e.name
    except FileNotFoundError:
        pass
    if newest > started:
        raise Skip("frames_newer", f"кадр googler/{newest_name} правили "
                                   f"{fmt_local(newest, 'Europe/Berlin')}, после старта рендера "
                                   f"{render['job_id']} ({fmt_local(started, 'Europe/Berlin')}) — жду перерендер",
                   wait=True)
    # Главы: готовы — хорошо; нет/упали/идут — ждём не дольше CHAPTERS_WAIT_H от конца рендера.
    ch = [t for t in tasks if t["kind"] == "chapters"]
    ch_path = os.path.join(proj, "chapters", "description.txt")
    ch_file = os.path.isfile(ch_path) and os.path.getsize(ch_path) > 0
    if ch_file:
        chapters = "есть"                     # движок берёт именно этот файл
    elif not chapters_on:
        chapters = "в пресете канала выключены"
    else:
        # Срок ждём от конца рендера, а если главы как раз перезапущены — от их постановки:
        # иначе давно упавшие главы, поставленные заново, не дождались бы своей очереди.
        base = float(render["finished"] or now)
        if ch and ch[-1]["status"] in ACTIVE_TASK:
            base = max(base, float(ch[-1]["created"] or 0))
        until = base + CHAPTERS_WAIT_H * 3600
        state = ch[-1]["status"] if ch else "нет задачи"
        if now < until:
            raise Skip("chapters_wait", f"главы: {state} — жду до {fmt_local(until, 'Europe/Berlin')}",
                       wait=True)
        chapters = f"нет ({state}), срок ожидания {CHAPTERS_WAIT_H:g} ч вышел"
    images = [t for t in tasks if t["kind"] == "images"]
    warn = ""
    if images and images[-1]["status"] != "done":
        warn = f"последняя задача картинок {images[-1]['job_id']} — {images[-1]['status']}"
    return {"render": render, "mp4": mp4, "size": size, "chapters": chapters, "tasks": tasks,
            "warn": warn}


def pick_slot(ch, busy: set, now: float):
    """Ближайший свободный выход канала не раньше, чем через MIN_LEAD_H часов."""
    schedule = json.loads(ch["schedule"] or "{}")
    slots = upcoming_slots(schedule, ch["tz"], int(ch["horizon_days"] or 3),
                           dt.datetime.fromtimestamp(now, dt.timezone.utc))
    for p in slots:
        stamp = round(p.timestamp())
        if stamp in busy or p.timestamp() <= now + MIN_LEAD_H * 3600:
            continue
        return stamp
    return None


def channel_issues(ch) -> list:
    out = []
    if ch["status"] != "active":
        out.append(f"канал в движке «{ch['status']}», а не active")
    try:
        sched = json.loads(ch["schedule"] or "{}")
    except Exception:
        sched = None
    if not sched:
        out.append("у канала нет расписания")
    if not (ch["preset_cover"] or "").strip():
        out.append("нет пресета обложек (движок уведёт выпуск в разбор)")
    if not ch["profile_id"]:
        out.append("не указан профиль AdsPower")
    if not (ch["lang"] or "").strip():
        out.append("не указан язык канала")
    return out


def plan(c, qrows, chrows, now, all_notes=False, what_if=False, retry=()) -> list:
    """Решение по каждой подходящей строке. Ничего не пишет."""
    note_re = re.compile(NOTE_RE)
    by_name = {}
    for r in chrows:
        if r["name"]:
            by_name.setdefault(r["name"], []).append(r)
    busy_cache: dict = {}
    virt_ids: dict = {}
    out = []
    for r in qrows:
        if r["status"] != READY_STATUS:
            continue
        pids = PID_RE.findall(r["note"])
        item = {"row": r["_row"], "channel": r["channel"], "pid": ",".join(pids),
                "donor": r["donor"], "decision": "skip"}
        out.append(item)
        try:
            if not pids:
                raise Skip("no_pid", "в «Заметке» нет номера проекта")
            if not all_notes and not note_re.search(r["note"]):
                raise Skip("scope", "не новая волна (заметка старого формата) — мост такие не берёт")
            if len(set(pids)) != 1:
                raise Skip("multi_pid", "в заметке несколько проектов — какой лить, неясно")
            # Хвост « · авто-заливка: …» дописывает сама таблица по итогам движка — это не
            # «ручная» заливка, по нему строку не блокируем (её держит проверка «уже заведён»).
            core = re.sub(r"\s*·\s*авто-заливка:.*$", "", r["note"])
            if re.search(r"залив|reconcile|id ролика", core, re.I):
                raise Skip("upload_mention", "в заметке упомянута начатая заливка — нужен разбор человеком")
            if "повтор" in r["check"].lower():
                raise Skip("sheet_dup", f"«Проверка» пишет: {r['check'][:80]}")
            pid = pids[0]
            vid = r["vid"] or yt_id(r["donor"])
            if not r["donor"].startswith("http") or not vid:
                raise Skip("bad_donor", "в «Доноре» нет ссылки на ролик YouTube")
            title = r["title"] or r["topic"]
            if not title:
                raise Skip("no_title", "нет названия оригинала — движок не сможет сделать заголовок")
            cands = by_name.get(r["channel"], [])
            if len(cands) != 1:
                raise Skip("sheet_channel", "канала нет в «Каналах»" if not cands
                           else f"в «Каналах» {len(cands)} строки с таким названием")
            srow = cands[0]
            uc = srow["uc"]
            item["uc"] = uc
            if not UC_RE.match(uc):
                raise Skip("bad_uc", f"в «Каналах» у канала нет верного UC ({uc or 'пусто'})")
            prj = c.execute("SELECT id, tenant_id, name FROM projects WHERE id=?", (pid,)).fetchone()
            if not prj:
                raise Skip("no_project", "проекта нет в кабинете")
            item["project_name"] = prj["name"]
            done = release_for_project(c, pid)
            if done and not (pid in retry and done["status"] in ("cancelled", "blocked")
                             and not reached_irreversible(c, done["release_id"])):
                # Отменённый владельцем или ушедший в разбор выпуск сам НЕ воскрешаем:
                # иначе «убрать из очереди» в кабинете отменялось бы мостом через 10 минут.
                # Повтор — только явно: --retry p_… (и только если заливка не начиналась).
                item["decision"] = "already"
                raise Skip("already", f"уже заведён: выпуск {done['release_id']} "
                                      f"({done['status']}, шаг {done['phase']})")
            # Проект сделан для этого канала? Код в имени проекта (K031_…) против кода строки «Каналов».
            m1, m2 = CODE_IN_PROJECT_RE.match(prj["name"] or ""), CODE_IN_SHEET_RE.search(srow["code"])
            if m1 and m2 and int(m1.group(1)) != int(m2.group(1)):
                raise Skip("code_mismatch", f"проект сделан для K{m1.group(1)}, а строка — "
                                            f"для {srow['code']} «{r['channel']}»")
            ch = c.execute("SELECT * FROM pub_channels WHERE uc=?", (uc,)).fetchone()
            virtual = False
            if ch is None:
                if not what_if:
                    raise Skip("engine_channel", f"канал не заведён в движке (UC {uc})", wait=True)
                virtual = True
                issues = ["канал не заведён в движке"]
            else:
                issues = channel_issues(ch)
                if ch["tenant_id"] != prj["tenant_id"]:
                    # А5 владельца 04.10: готовые ролики Гринда (свой кабинет) идут на каналы
                    # кабинета testpartner — через ссылку на папку проекта (bridge_link).
                    if not bridge_link.foreign_allowed(prj["tenant_id"], ch["tenant_id"]):
                        raise Skip("tenant", f"канал в кабинете {ch['tenant_id']}, проект — в {prj['tenant_id']}")
                    item["link_from"] = str(prj["tenant_id"])
                sheet_prof = re.sub(r"\D", "", srow["profile"])
                if CHECK_PROFILE and sheet_prof and ch["profile_id"] and str(ch["profile_id"]) != sheet_prof:
                    issues.append(f"профиль в движке {ch['profile_id']}, а в «Каналах» {srow['profile']} — "
                                  "сверьте, какой верный")
                if issues and not what_if:
                    raise Skip("engine_channel", "; ".join(issues), wait=True)
                virtual = bool(issues)
            if virtual:
                # Только для прогноза: так канал выглядел бы по решению владельца (1 ролик
                # в 10:00 по времени страны). В базу это не пишется никогда.
                vid_key = virt_ids.setdefault(uc, -(len(virt_ids) + 1))
                ch = {"id": ch["id"] if ch is not None else vid_key, "status": "active",
                      "schedule": json.dumps({"*": ["10:00"]}),
                      "tz": LANG_TZ.get(srow["lang"], "Europe/Warsaw"), "horizon_days": 14,
                      "preset_cover": (ch["preset_cover"] if ch is not None else "") or "?",
                      "profile_id": ch["profile_id"] if ch is not None else None,
                      "preset_vsgen_id": ch["preset_vsgen_id"] if ch is not None else None,
                      "tenant_id": prj["tenant_id"]}
                item["what_if"] = "; ".join(issues)
            # Этот донор уже стоит в плане канала — второй раз не ставим (дубль на канале).
            if ch["id"] > 0:
                for p in c.execute("SELECT id, donor_url, status FROM pub_plan_items WHERE channel_id=? "
                                   "AND status IN ('planned','frozen','running','done')", (ch["id"],)):
                    if yt_id(p["donor_url"]) == vid:
                        raise Skip("dup_donor", f"этот донор уже в плане канала (строка {p['id']}, {p['status']})")
            chk = check_project(c, pid, prj["tenant_id"], now,
                                chapters_enabled(preset_settings(c, ch["preset_vsgen_id"])),
                                min_render_at=_parse_moment(MIN_RENDER_AT))
            if ch["id"] not in busy_cache:
                busy_cache[ch["id"]] = ({round(x.timestamp()) for x in taken_slots(c, ch["id"])}
                                        if ch["id"] > 0 else set())
            try:
                slot = pick_slot(ch, busy_cache[ch["id"]], now)
            except (ScheduleError, ValueError) as exc:
                raise Skip("schedule", f"расписание канала не читается: {exc}", wait=True)
            if slot is None:
                raise Skip("no_slot", f"нет свободного выхода в горизонте {ch['horizon_days']} дн — возьму позже",
                           wait=True)
            busy_cache[ch["id"]].add(slot)
            item.update({"decision": "create", "code": "ok", "reason": "", "wait": False,
                         # Выпуск — в кабинете КАНАЛА: движок ведёт его вместе с каналом.
                         "tenant_id": ch["tenant_id"], "channel_id": ch["id"], "slot_at": slot,
                         "slot_local": fmt_local(slot, ch["tz"]) + " " + ch["tz"],
                         "donor_title": title, "render_job": chk["render"]["job_id"],
                         "render_finished": chk["render"]["finished"],
                         "mp4": os.path.relpath(chk["mp4"], project_dir(prj["tenant_id"], pid)),
                         "mp4_mb": round(chk["size"] / 1048576, 1), "chapters": chk["chapters"],
                         "warn": chk["warn"], "tasks": chk["tasks"], "virtual": virtual})
        except Skip as s:
            item.update({"code": s.code, "reason": s.text, "wait": s.wait})
    return out


# ─────────────────────────── запись ───────────────────────────

def _latest(tasks, kind, prefer_done=True):
    ts = [t for t in tasks if t["kind"] == kind]
    if prefer_done and any(t["status"] == "done" for t in ts):
        ts = [t for t in ts if t["status"] == "done"]
    return ts[-1] if ts else None


def create_release(c, it, now: float, retry=()) -> str:
    """Строка плана (frozen) + выпуск на шаге media_ready + зеркало задач + журнал. Одна транзакция."""
    c.execute("BEGIN IMMEDIATE")
    try:
        # Повтор проверки под замком: пока мы думали, выпуск мог завести другой запуск.
        prev = release_for_project(c, it["pid"])
        if prev and not (it["pid"] in retry and prev["status"] in ("cancelled", "blocked")
                         and not reached_irreversible(c, prev["release_id"])):
            c.execute("ROLLBACK")
            return ""
        ch = c.execute("SELECT * FROM pub_channels WHERE id=?", (it["channel_id"],)).fetchone()
        if not ch or channel_issues(ch) or ch["uc"] != it["uc"]:
            c.execute("ROLLBACK")
            return ""
        if it.get("link_from"):
            why = bridge_link.ensure_project_link(TENANTS_DIR, it["link_from"], ch["tenant_id"],
                                                  it["pid"])
            if why:
                c.execute("ROLLBACK")
                it["link_error"] = why
                return ""
        note = f"мост «Очередь»: строка {it['row']}, проект {it['pid']}"
        cur = c.execute(
            "INSERT INTO pub_plan_items(tenant_id,channel_id,version,donor_url,donor_title,position,"
            "slot_at,slot_mode,overrides,note,status,frozen,created,updated) "
            "VALUES(?,?,1,?,?,?,?,'auto','{}',?,'frozen',1,?,?)",
            (it["tenant_id"], it["channel_id"], it["donor"], it["donor_title"][:300], now,
             it["slot_at"], note, now, now))
        item_id = cur.lastrowid
        rid = new_release_id()
        c.execute(
            "INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,plan_item_version,channel_id,uc,"
            "slot_at,project_id,status,phase,heartbeat,deadline,attempts,created,updated) "
            "VALUES(?,?,?,1,?,?,?,?,'running','media_ready',?,?,0,?,?)",
            (rid, it["tenant_id"], item_id, it["channel_id"], it["uc"], it["slot_at"], it["pid"],
             now, now + PHASE_DEADLINES["media_ready"], now, now))
        # Зеркало этапов кабинета — то же, что пишет do_media_ready: по нему интерфейс
        # рисует полоски, а заливка находит задачу рендера.
        tasks = it["tasks"]
        mirror = {"tts": _latest(tasks, "tts"), "images": _latest(tasks, "images"),
                  "render": next(t for t in tasks if t["job_id"] == it["render_job"]),
                  "chapters": _latest(tasks, "chapters")}
        for kind, t in mirror.items():
            if not t:
                continue
            c.execute(
                "INSERT OR IGNORE INTO pub_release_jobs(release_id,service,kind,remote_job_id,status,"
                "payload,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                (rid, "vsgen", kind, str(t["job_id"]), t["status"],
                 json.dumps({"bridge": True}), now, now))
        log_phase(c, rid, "created", now, payload={"source": "bridge", "sheet_row": it["row"]})
        log_phase(c, rid, "media_ready", now, payload={
            "source": "bridge", "project_id": it["pid"], "render_job": it["render_job"],
            "mp4": it["mp4"], "chapters": it["chapters"]})
        if it.get("link_from"):
            log_line(c, rid, "мост", f"проект из кабинета {it['link_from']} — движок видит его "
                     f"через ссылку в кабинете канала {ch['tenant_id']}", now=now)
        log_line(c, rid, "мост",
                 f"взял готовый проект {it['pid']} из «Очереди» (строка {it['row']}): рендер "
                 f"{it['render_job']}, {it['mp4_mb']} МБ, главы — {it['chapters']}; выход {it['slot_local']}",
                 now=now)
        if it.get("warn"):
            log_line(c, rid, "мост", it["warn"], level="warn", now=now)
        c.execute("COMMIT")
        return rid
    except sqlite3.IntegrityError as exc:
        c.execute("ROLLBACK")
        if "UNIQUE" in str(exc):
            return ""           # выход заняли в ту же секунду — возьмём в следующий запуск
        raise
    except Exception:
        c.execute("ROLLBACK")
        raise


# ─────────────────────────── запуск ───────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description="Мост «Очередь» → движок публикаций")
    ap.add_argument("--apply", action="store_true", help="записать в базу (без флага — только показ)")
    ap.add_argument("--db", default=DB_PATH, help="путь к vsgen.db (для сухого прогона — копия)")
    ap.add_argument("--snapshot", default="", help="JSON-снимок таблицы вместо чтения Google")
    ap.add_argument("--all-notes", action="store_true", help="брать и строки старого формата заметки")
    ap.add_argument("--what-if", action="store_true",
                    help="только показ: считать незаведённые каналы заведёнными (10:00, пояс по языку)")
    ap.add_argument("--max-new", type=int, default=MAX_NEW)
    ap.add_argument("--retry", action="append", default=[], metavar="p_…",
                    help="завести заново проект, чей выпуск отменён или ушёл в разбор до заливки")
    ap.add_argument("--report", default="", help="куда положить JSON-отчёт")
    ap.add_argument("--now", type=float, default=0.0, help="для проверки: подменить «сейчас»")
    a = ap.parse_args(argv)
    if a.apply and a.what_if:
        raise SystemExit("--what-if только для показа, с --apply нельзя")
    now = a.now or time.time()

    lock = None
    if a.apply:
        if os.path.exists(os.path.join(STATE_DIR, "PAUSE")):
            say(f"стоит {STATE_DIR}/PAUSE — ничего не делаю")
            return 0
        os.makedirs(STATE_DIR, exist_ok=True)
        lock = open(os.path.join(STATE_DIR, ".lock"), "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            say("другой запуск ещё идёт — выхожу")
            return 0

    qrows, chrows = read_sheet(a.snapshot)
    c = connect(a.db, write=a.apply)
    items = plan(c, qrows, chrows, now, all_notes=a.all_notes, what_if=a.what_if, retry=set(a.retry))
    made = []
    todo = [x for x in items if x["decision"] == "create"]
    for it in todo[max(0, a.max_new):]:
        it.update({"decision": "skip", "code": "max_new", "wait": True,
                   "reason": f"за запуск завожу не больше {a.max_new} — остальные в следующий раз"})
    if a.apply:
        for it in todo[:max(0, a.max_new)]:
            try:
                rid = create_release(c, it, now, retry=set(a.retry))
            except Exception as exc:          # одна строка не валит остальные
                it.update({"decision": "error", "reason": f"ошибка записи: {exc}"})
                continue
            if rid:
                it.update({"decision": "created", "release_id": rid})
                made.append(rid)
            elif it.get("link_error"):
                it.update({"decision": "skip", "code": "link",
                           "reason": "ссылка на проект из чужого кабинета не легла: "
                                     + it["link_error"]})
            else:
                it.update({"decision": "skip", "code": "race",
                           "reason": "выпуск/выход заняли в эту же секунду — повторю в следующий запуск"})
    c.close()

    # Сводка — человеческим языком, для журнала службы.
    verb = "заведено" if a.apply else "заведу"
    creates = [x for x in items if x["decision"] in ("create", "created")]
    for x in items:
        if x.get("code") == "scope":
            continue
        head = f"стр {x['row']:>4} | {x['channel'][:26]:<26} | {x['pid'] or '—':<13}"
        if x["decision"] in ("create", "created"):
            tail = (f"{verb.upper()}: выход {x['slot_local']}, рендер {x['render_job']} "
                    f"({x['mp4_mb']} МБ), главы: {x['chapters']}")
            if x.get("release_id"):
                tail += f", выпуск {x['release_id']}"
            if x.get("what_if"):
                tail += f"  [если заведут канал: {x['what_if']}]"
            if x.get("warn"):
                tail += f"  [!] {x['warn']}"
        else:
            tail = ("ЖДЁТ: " if x.get("wait") else "ПРОПУСК: ") + x.get("reason", "")
        print(head, "|", tail)
    scope = sum(1 for x in items if x.get("code") == "scope")
    by = {}
    for x in items:
        if x["decision"] not in ("create", "created"):
            by[x.get("code")] = by.get(x.get("code"), 0) + 1
    say(f"строк «{READY_STATUS}»: {len(items)}; вне новой волны: {scope}; "
        f"{verb}: {len(creates) if not a.apply else len(made)}; пропуски по причинам: "
        + ", ".join(f"{k}={v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])))
    if a.report:
        for x in items:
            x.pop("tasks", None)
        tmp = a.report + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"at": now, "apply": a.apply, "what_if": a.what_if, "items": items},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, a.report)
    if lock:
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
