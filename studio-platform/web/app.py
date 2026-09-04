#!/usr/bin/env python3
"""Оболочка единого пульта: экран выпуска и старт по ссылке.

Временная. В боевой системе эти роуты станут роутером внутри кабинета; отдельным
приложением они живут только чтобы экраны можно было проверять без ключей, без данных
и без соседних сервисов на 8899/8900.

Порядок фаз, сроки и состав этапов производства импортируются из боевых модулей
кабинета — не копируются. Поменяется там, поменяется здесь.

    .venv/bin/python dev/make_db.py
    .venv/bin/python -m uvicorn web.app:app --port 8010
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
SERVER = os.path.join(BASE, "server")
sys.path.insert(0, SERVER)

from fastapi import FastAPI, HTTPException, Request, Response    # noqa: E402
from fastapi.responses import FileResponse, JSONResponse          # noqa: E402
from pydantic import BaseModel                                   # noqa: E402

sys.path.insert(0, HERE)
import pult_auth as auth                                       # noqa: E402

from channel_slots import (ANY_DAY, DAY_KEYS, _localize, _zone,  # noqa: E402
                           normalize_schedule, upcoming_slots)
from pub_engine import PHASES, PHASE_DEADLINES                   # noqa: E402
from pub_remote import COVER_BASE, REWRITE_BASE, get_key         # noqa: E402
from pub_services import MEDIA_STAGES, OPTIONAL_STAGES           # noqa: E402
from pub_tags import add_links, parse_links, plan_rows           # noqa: E402

import pub_alerts                                                # noqa: E402
import pub_alerts_tg                                             # noqa: E402
import pub_retention                                             # noqa: E402
import pub_verify                                                # noqa: E402
from pub_env import load_env, mask                                # noqa: E402

load_env()

DB = os.environ.get("VS_DEV_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
TENANT = os.environ.get("VS_DEV_TENANT", "dev")
CABINET_BASE = os.environ.get("VS_CABINET_BASE") or "http://127.0.0.1:8800"
# Тот же путь, что читает кабинет (main.py TENANTS_DIR): уборка и производство должны
# смотреть в один каталог, иначе экран покажет пустой диск при полном.
TENANTS_DIR = (os.environ.get("VS_TENANTS_DIR")
               or os.path.join(BASE, "dev", "data", "tenants"))

PHASE_TITLE = {
    "created": "заведён",
    "source_ready": "сценарий",
    "project_ready": "проект",
    "media_ready": "производство",
    "cover_ready": "обложка",
    "upload_pending": "передан на заливку",
    "upload_running": "загрузка идёт",
    "scheduled": "запланирован",
    "published": "опубликован",
}

# Семь стадий раздела 14 ТЗ. Третье поле говорит, откуда берётся строка.
STAGES = (
    ("script", "Сценарий", "remote:rewrite"),
    ("tts", "Озвучка", "queue:tts"),
    ("images", "Кадры", "queue:images"),
    ("video", "Клипы", "queue:video"),
    ("render", "Сборка", "queue:render"),
    ("cover", "Обложка", "remote:cover"),
    ("upload", "Публикация", "upload"),
)

TERMINAL_OK = ("done",)
TERMINAL_BAD = ("error", "cancelled", "failed")

app = FastAPI(title="Studio Platform — оболочка разработки", docs_url="/docs")

# Открыто без входа ровно то, без чего в него не войти.
PUBLIC_PATHS = ("/", "/ui/login", "/ui/me", "/docs", "/openapi.json", "/redoc")


@app.middleware("http")
async def require_session(request: Request, call_next):
    """Ни один экран и ни одно действие не работают без входа.

    Раньше пульт не спрашивал вообще ничего: он слушает 127.0.0.1, и казалось, что
    этого довольно. Не довольно — на той же машине живут и соседние сервисы, и любой
    процесс пользователя. Данные здесь настоящие: каналы, выпуски, состояние ключей.
    """
    if request.url.path in PUBLIC_PATHS or not request.url.path.startswith("/ui/"):
        return await call_next(request)
    sid = request.cookies.get(auth.COOKIE) or ""
    c = db()
    try:
        user = auth.who(c, sid)
    finally:
        c.close()
    if user is None:
        return JSONResponse({"detail": "нужен вход"}, status_code=401)
    request.state.sid = sid
    request.state.user = user
    return await call_next(request)


@app.post("/ui/login")
async def ui_login(request: Request, response: Response):
    """Пароль проверяет кабинет, не мы. Своего списка пользователей у пульта нет."""
    body = await request.json()
    try:
        sid = auth.login(CABINET_BASE, str(body.get("login") or ""),
                         str(body.get("password") or ""))
    except auth.LoginFailed as exc:
        raise HTTPException(401, str(exc))
    c = db()
    try:
        user = auth.who(c, sid)
    finally:
        c.close()
    if user is None:
        # Кабинет выдал сессию, а мы её не видим — значит смотрим в разные базы.
        # Сказать об этом прямо дешевле, чем оставить «неверный пароль» при верном.
        raise HTTPException(500, "кабинет выдал сессию, но её нет в базе пульта — "
                                 "проверьте, что VS_SERVER_DB и VS_DEV_DB это один файл")
    response.set_cookie(auth.COOKIE, sid, httponly=True, samesite="lax",
                        secure=not auth.INSECURE, max_age=30 * 86400)
    return {"name": user["name"], "tenant": user["tenant_id"] or str(user["id"])}


@app.post("/ui/logout")
def ui_logout(request: Request, response: Response):
    auth.logout(CABINET_BASE, getattr(request.state, "sid", ""))
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


@app.get("/ui/me")
def ui_me(request: Request):
    """Кто вошёл. Единственный экран-независимый ответ: 401 здесь — не ошибка,
    а нормальный ответ «ещё не вошёл», по которому фронт рисует форму входа."""
    c = db()
    try:
        user = auth.who(c, request.cookies.get(auth.COOKIE) or "")
    finally:
        c.close()
    if user is None:
        raise HTTPException(401, "нужен вход")
    return {"name": user["name"], "tenant": user["tenant_id"] or str(user["id"]),
            "role": user["role"] if "role" in user.keys() else ""}


# ───────────────────────────────── данные ─────────────────────────────────

def db() -> sqlite3.Connection:
    if not os.path.exists(DB):
        raise HTTPException(503, f"нет базы {DB} — сначала: python3 dev/make_db.py")
    c = sqlite3.connect(DB, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def _state(status) -> str:
    s = (status or "").lower()
    if s in TERMINAL_OK:
        return "done"
    if s in TERMINAL_BAD:
        return "error"
    if s in ("running", "claimed", "in_progress"):
        return "running"
    return "queued" if s else "idle"


def _releases(c, channel=None, phase=None, status=None) -> list:
    sql = ("SELECT r.release_id, r.phase, r.status, r.slot_at, r.attempts, r.updated,"
           "       r.blocked_reason, r.video_id, r.project_id,"
           "       ch.id ch_id, ch.name ch_name, ch.lang lang,"
           "       p.donor_url, p.donor_title "
           "FROM pub_releases r "
           "JOIN pub_channels ch ON ch.id = r.channel_id "
           "LEFT JOIN pub_plan_items p ON p.id = r.plan_item_id "
           "WHERE r.tenant_id = ?")
    args = [TENANT]
    if channel:
        sql += " AND ch.id = ?"
        args.append(int(channel))
    if phase:
        sql += " AND r.phase = ?"
        args.append(phase)
    if status:
        sql += " AND r.status = ?"
        args.append(status)
    sql += " ORDER BY r.slot_at"

    now = time.time()
    out = []
    for r in c.execute(sql, args):
        ph = r["phase"] or "created"
        out.append({
            "release_id": r["release_id"],
            "channel_id": r["ch_id"],
            "channel": r["ch_name"],
            "lang": (r["lang"] or "").upper(),
            "donor_title": r["donor_title"] or "",
            "donor_url": r["donor_url"] or "",
            "slot_at": r["slot_at"],
            "phase": ph,
            "phase_title": PHASE_TITLE.get(ph, ph),
            "phase_index": PHASES.index(ph) if ph in PHASES else 0,
            "phase_total": len(PHASES),
            "phase_deadline": PHASE_DEADLINES.get(ph),
            "status": r["status"],
            "attempts": r["attempts"] or 0,
            "blocked_reason": r["blocked_reason"] or "",
            "video_id": r["video_id"] or "",
            "project_id": r["project_id"] or "",
            "idle_sec": max(0, int(now - (r["updated"] or now))),
        })
    return out


def _stages(c, rel: dict) -> list:
    """Семь строк экрана выпуска. Ничего не выдумываем: нет данных — так и пишем."""
    rid = rel["release_id"]
    remote = {(j["service"], j["kind"]): j for j in c.execute(
        "SELECT service, kind, remote_job_id, status, error FROM pub_release_jobs "
        "WHERE release_id = ?", (rid,))}
    queue = {}
    if rel["project_id"]:
        for q in c.execute("SELECT kind, status, progress, stage, error, provider, job_id "
                           "FROM queue_tasks WHERE project_id = ? ORDER BY job_id",
                           (rel["project_id"],)):
            queue[q["kind"]] = q          # последняя задача этого вида — она и актуальна
    upload = c.execute("SELECT * FROM pub_upload_jobs WHERE release_id = ?", (rid,)).fetchone()

    out = []
    for key, title, source in STAGES:
        row = {"key": key, "title": title, "state": "idle", "facts": [], "note": "",
               "job": None}

        if source.startswith("remote:"):
            service = source.split(":", 1)[1]
            job = next((j for (s, _k), j in remote.items() if s == service), None)
            if job is None:
                row["note"] = "не начато"
            else:
                row["state"] = _state(job["status"])
                if job["remote_job_id"]:
                    row["facts"].append(f'задача {job["remote_job_id"]}')
                    # Этап делает сосед — значит и отменять/повторять его должен он.
                    row["job"] = {"service": service, "id": job["remote_job_id"],
                                  "status": job["status"] or ""}
                if job["error"]:
                    row["note"] = job["error"]

        elif source.startswith("queue:"):
            kind = source.split(":", 1)[1]
            if kind not in MEDIA_STAGES:
                # Клипы: kind='video' в очереди кабинета есть, но автоконвейер их не
                # запускает — MEDIA_STAGES их не содержит. Писать «не начато» значило бы
                # обещать, что они начнутся.
                row["state"] = "off"
                row["note"] = "в автоконвейере не участвует"
                out.append(row)
                continue
            job = remote.get(("vsgen", kind))
            q = queue.get(kind)
            if job is None and q is None:
                row["note"] = "не начато"
            else:
                row["state"] = _state((q["status"] if q else None)
                                      or (job["status"] if job else None))
                if q is not None:
                    # На чём этап держится — нужно кнопкам «отменить» и «повторить»:
                    # они не выполняют работу, а пересылают её тому, чья это задача.
                    row["job"] = {"service": "vsgen", "id": q["job_id"],
                                  "status": q["status"] or ""}
                    if q["stage"]:
                        row["facts"].append(q["stage"])
                    if q["progress"] is not None and row["state"] == "running":
                        row["facts"].append(f'{round(float(q["progress"]) * 100)}%')
                    if q["provider"]:
                        row["facts"].append(q["provider"])
                    if q["error"]:
                        row["note"] = q["error"]
                elif job is not None and job["remote_job_id"]:
                    row["facts"].append(f'задача {job["remote_job_id"]}')
            if kind in OPTIONAL_STAGES:
                row["facts"].append("необязательный этап")
            if kind == "render":
                ch = remote.get(("vsgen", "chapters"))
                if ch is not None:
                    row["facts"].append(f'главы: {ch["status"]}')

        else:                                                        # публикация
            if upload is None:
                row["note"] = "не начато"
            else:
                row["state"] = _state(upload["status"])
                for label, value in (("канал", upload["uc"]),
                                     ("профиль", upload["profile_id"]),
                                     ("машина", upload["host"]),
                                     ("шаг", upload["phase"]),
                                     ("ролик", upload["video_id"]),
                                     ("вердикт", upload["verdict"])):
                    if value:
                        row["facts"].append(f"{label} {value}")
                if upload["error"]:
                    row["note"] = upload["error"]
        out.append(row)
    return out


# ───────────────────────────────── роуты ─────────────────────────────────

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(HERE, "index.html"))


@app.get("/ui/releases")
def ui_releases(channel: int | None = None, phase: str | None = None,
                status: str | None = None):
    c = db()
    try:
        return {"releases": _releases(c, channel, phase, status),
                "phases": [{"key": p, "title": PHASE_TITLE.get(p, p)} for p in PHASES]}
    finally:
        c.close()


@app.get("/ui/releases/{release_id}")
def ui_release(release_id: str):
    c = db()
    try:
        found = [r for r in _releases(c) if r["release_id"] == release_id]
        if not found:
            raise HTTPException(404, "выпуск не найден")
        rel = found[0]
        events = [{"seq": e["seq"], "phase": e["phase"],
                   "phase_title": PHASE_TITLE.get(e["phase"], e["phase"]),
                   "video_id": e["video_id"] or "", "created": e["created"]}
                  for e in c.execute("SELECT * FROM pub_release_events WHERE release_id=? "
                                     "ORDER BY seq", (release_id,))]
        log = [{"ts": l["ts"], "level": l["level"], "step": l["step"],
                "message": l["message"]}
               for l in c.execute("SELECT * FROM pub_release_log WHERE release_id=? "
                                  "ORDER BY id", (release_id,))]
        return {"release": rel, "stages": _stages(c, rel), "events": events, "log": log}
    finally:
        c.close()


@app.get("/ui/channels")
def ui_channels():
    c = db()
    try:
        out = []
        for ch in c.execute("SELECT * FROM pub_channels WHERE tenant_id=? ORDER BY name",
                            (TENANT,)):
            try:
                schedule = json.loads(ch["schedule"] or "{}")
            except ValueError:
                schedule = {}
            out.append({
                "id": ch["id"], "name": ch["name"], "handle": ch["handle"] or "",
                "lang": (ch["lang"] or "").upper(), "status": ch["status"],
                "tz": ch["tz"], "horizon_days": ch["horizon_days"],
                "lead_days": ch["lead_days"],
                "per_day": sum(len(v or []) for v in schedule.values()),
                "has_profile": bool(ch["profile_id"]),
            })
        return {"channels": out}
    finally:
        c.close()


# ─────────────────────────── календарь канала ───────────────────────────

def past_slots(schedule: dict, tz_name: str, days: int, now=None) -> list:
    """Слоты канала за прошедшие `days` суток, по возрастанию.

    Зеркало `channel_slots.upcoming_slots()`, которое умеет только вперёд:
    «уже прошедшие сегодня времена не возвращаются — занять их нельзя». Для плана это
    верно, для истории — нет: именно прошедший пустой слот и есть пропуск, который
    требует показывать ТЗ §13, и сегодня его не видно нигде.

    Разбор расписания, часовой пояс и обработка несуществующего времени в ночь перевода
    часов берутся из тех же функций, что и у прямого прохода: расписание не должно
    читаться двумя способами.
    """
    plan = normalize_schedule(schedule)
    zone = _zone(tz_name)
    moment = (now.astimezone(zone) if now is not None else dt.datetime.now(zone))
    out = []
    for offset in range(days, -1, -1):
        day = (moment - dt.timedelta(days=offset)).date()
        key = ANY_DAY if ANY_DAY in plan else DAY_KEYS[day.weekday()]
        for hh, mm in plan.get(key, []):
            point = _localize(day, hh, mm, zone)
            if point is None:
                continue                      # этого времени в эту дату не существует
            if point <= moment:
                out.append(point)
    return sorted(out)


def _calendar(c, ch, history_days: int) -> list:
    """Прошлое и будущее канала одной лентой.

    Состояния слота:
      done      выпуск дошёл до конца
      running   выпуск в работе
      blocked   выпуск остановлен
      planned   ссылка стоит, работа ещё не начиналась
      missed    время прошло, а ролика не было — ПРОПУСК, его и требует показывать ТЗ
      free      будущий слот свободен, сюда и ложится следующая ссылка
    """
    try:
        schedule = json.loads(ch["schedule"] or "{}")
    except ValueError:
        schedule = {}
    if not schedule:
        return []

    # Строки плана по округлённому моменту — так же, как это делает pub_tags.coverage().
    by_slot = {}
    for r in plan_rows(c, ch["id"]):
        if r["slot_at"] and round(r["slot_at"]) not in by_slot:
            by_slot[round(r["slot_at"])] = r
    rels = {r["plan_item_id"]: r for r in c.execute(
        "SELECT * FROM pub_releases WHERE channel_id=? ORDER BY id", (ch["id"],))}

    horizon = int(ch["horizon_days"] or 3)
    tz = ch["tz"] or "Europe/Warsaw"
    moments = ([(m, True) for m in past_slots(schedule, tz, history_days)]
               + [(m, False) for m in upcoming_slots(schedule, tz, horizon)])

    def row_for(moment, item, is_past, beyond=False):
        rel = rels.get(item["id"]) if item else None
        if rel is not None:
            state = {"done": "done", "blocked": "blocked"}.get(rel["status"] or "", "running")
        elif item is not None:
            state = "planned"
        else:
            state = "missed" if is_past else "free"
        return {
            "at": moment.timestamp() if hasattr(moment, "timestamp") else float(moment),
            "past": is_past,
            "beyond_horizon": beyond,
            "state": state,
            "donor_title": (item["donor_title"] if item else "") or "",
            "donor_url": (item["donor_url"] if item else "") or "",
            "release_id": (rel["release_id"] if rel is not None else ""),
            "phase": (rel["phase"] if rel is not None else ""),
            "phase_title": PHASE_TITLE.get(rel["phase"], "") if rel is not None else "",
            "project_id": (rel["project_id"] if rel is not None else "") or "",
            "blocked_reason": (rel["blocked_reason"] if rel is not None else "") or "",
        }

    out = [row_for(m, by_slot.get(round(m.timestamp())), past) for m, past in moments]

    # Строки плана ЗА горизонтом. Горизонт — это «куда класть следующую ссылку», а не
    # «что показывать»: у канала с двумя выходами в неделю и горизонтом в трое суток
    # ближайший выход часто оказывается за его пределами. Без этого блока выпуск,
    # который прямо сейчас в работе, не виден в календаре вообще — интерфейс молчал бы
    # о происходящем.
    shown = {round(r["at"]) for r in out}
    for ts, item in sorted(by_slot.items()):
        if ts in shown:
            continue
        moment = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
        out.append(row_for(moment, item, is_past=(ts <= time.time()), beyond=True))
    out.sort(key=lambda r: r["at"])
    return out


# ─────────────────────────── сквозная очередь ───────────────────────────

# Оба соседа отдают одинаковую форму: {items, total, limit, offset}, элемент —
# ApiJobListItem(id, status, created_at, progress_percent, results_count).
# Ключевая авторизация у обоих живёт под /api/v1 (см. pub_remote.RewriteClient._url).
NEIGHBOURS = (("rewrite", "Текст", REWRITE_BASE), ("cover", "Обложки", COVER_BASE))
NEIGHBOUR_TIMEOUT = 6


def _neighbour_jobs(name: str, base: str, key: str) -> tuple:
    """Задачи соседа. Возвращает (строки, ошибка). Недоступный сосед не роняет экран."""
    if not key:
        return [], "ключ доступа не сохранён"
    req = urllib.request.Request(f"{base}/api/v1/jobs?limit=50")
    req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=NEIGHBOUR_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return [], f"ответил {exc.code}"
    except Exception as exc:                                     # noqa: BLE001
        return [], f"не отвечает: {type(exc).__name__}"

    items = body.get("items", body) if isinstance(body, dict) else body
    rows = []
    for it in items if isinstance(items, list) else []:
        pct = it.get("progress_percent")
        rows.append({
            "service": name,
            "id": str(it.get("id") or ""),
            "title": str(it.get("id") or ""),
            "kind": "",
            "status": str(it.get("status") or ""),
            "state": _state(it.get("status")),
            "progress": (float(pct) / 100 if isinstance(pct, (int, float)) else None),
            "stage": (f'готово {it["results_count"]}'
                      if it.get("results_count") else ""),
            "provider": "",
            "created": it.get("created_at"),
            "error": "",
        })
    return rows, ""


@app.get("/ui/queue")
def ui_queue():
    """Одна очередь на три сервиса. Свои задачи из базы, чужие — по HTTP.

    Сосед, который не отвечает, показывается отдельной строкой состояния, а не роняет
    экран и не притворяется пустой очередью: «нет задач» и «не смогли спросить» — разное.
    """
    c = db()
    try:
        rows = []
        for q in c.execute(
                "SELECT job_id, kind, status, created, progress, stage, provider, error, "
                "       project_id "
                "FROM queue_tasks WHERE tenant_id=? ORDER BY job_id DESC LIMIT 100",
                (TENANT,)):
            rows.append({
                "service": "vsgen", "id": str(q["job_id"]),
                "title": q["project_id"] or str(q["job_id"]),
                "kind": q["kind"] or "",
                "status": q["status"] or "",
                "state": _state(q["status"]),
                "progress": (float(q["progress"]) if q["progress"] is not None else None),
                "stage": q["stage"] or "",
                "provider": q["provider"] or "",
                "created": q["created"],
                "error": q["error"] or "",
            })
        services = [{"key": "vsgen", "title": "Видео", "ok": True, "error": "",
                     "count": len(rows)}]
        for name, title, base in NEIGHBOURS:
            got, err = _neighbour_jobs(name, base, get_key(c, name))
            rows.extend(got)
            services.append({"key": name, "title": title, "ok": not err,
                             "error": err, "count": len(got)})
    finally:
        c.close()
    rows.sort(key=lambda r: (r["created"] or 0), reverse=True)
    return {"rows": rows, "services": services}


@app.get("/ui/keys")
def ui_keys():
    """Честная картина по ключам вместо «свободных слотов» (ТЗ §12).

    Что видно отсюда и откуда берётся:
      очередь и ожидание  — queue_tasks: длина по провайдеру и возраст самой старой
      отдыхающие ключи    — admin_state.credential_cooldown: до какого времени и почему
      свои ключи          — users.keys, только количество и маска, значения наружу не идут
      курсор раздачи      — provider_rr_cursor: кому выдали последним

    Чего отсюда НЕ видно и почему: текущая занятость слотов живёт в памяти кабинета
    (SERVER_CREDENTIAL_POOL._loads), а не в базе. Показывать её выдуманной нельзя —
    это ровно то враньё интерфейса, которое мы весь день убираем. Нужен эндпоинт на
    стороне кабинета; до тех пор поле честно помечено как недоступное.
    """
    now = time.time()
    c = db()
    try:
        # Очередь по провайдерам: длина и возраст самой старой ожидающей задачи.
        providers = {}
        for q in c.execute(
                "SELECT COALESCE(provider,'') provider, status, COUNT(*) n, MIN(created) oldest "
                "FROM queue_tasks WHERE tenant_id=? AND status IN ('queued','running') "
                "GROUP BY provider, status", (TENANT,)):
            p = providers.setdefault(q["provider"] or "—",
                                     {"provider": q["provider"] or "—", "queued": 0,
                                      "running": 0, "oldest_wait_sec": 0})
            p[q["status"]] = q["n"]
            if q["status"] == "queued" and q["oldest"]:
                p["oldest_wait_sec"] = max(p["oldest_wait_sec"], int(now - q["oldest"]))

        # Отдыхающие ключи. Значение ключа наружу не отдаётся — только маска, как в кабинете.
        resting = []
        row = c.execute("SELECT v FROM admin_state WHERE k='credential_cooldown'").fetchone()
        if row and row["v"]:
            try:
                for _sid, rec in (json.loads(row["v"]) or {}).items():
                    until = float(rec.get("until") or 0)
                    if until <= now:
                        continue          # срок вышел, ключ уже проснулся
                    resting.append({
                        "family": rec.get("family") or "",
                        "mask": rec.get("mask") or "—",
                        "until": until,
                        "left_sec": int(until - now),
                        "why": (rec.get("why") or "")[:180],
                        "noticed_by_neighbour": bool(rec.get("shared")),
                    })
            except ValueError:
                pass
        resting.sort(key=lambda r: r["until"])

        # Свои ключи пользователей: сколько их у кого. Значения не читаем и не отдаём.
        own = []
        for u in c.execute("SELECT id, name, keys FROM users WHERE tenant_id=?", (TENANT,)):
            try:
                data = json.loads(u["keys"] or "{}")
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            for family, val in data.items():
                items = val if isinstance(val, (list, tuple)) else ([val] if val else [])
                uniq = [str(v) for v in items if v]
                if not uniq:
                    continue
                own.append({"user": u["name"], "family": family, "count": len(set(uniq)),
                            "pooled": len(set(uniq)) > 1})

        cursor = [{"provider": r["provider"], "last_uid": r["last_uid"],
                   "updated": r["updated"]}
                  for r in c.execute("SELECT * FROM provider_rr_cursor ORDER BY provider")]
    finally:
        c.close()
    return {
        "providers": sorted(providers.values(), key=lambda p: -p["queued"]),
        "resting": resting,
        "own_keys": sorted(own, key=lambda o: (o["user"], o["family"])),
        "rr_cursor": cursor,
        "unavailable": [
            {"what": "занято слотов из лимита",
             "why": "живёт в памяти кабинета (SERVER_CREDENTIAL_POOL), в базе этого нет; "
                    "нужен эндпоинт на стороне кабинета"},
        ],
    }


@app.get("/ui/channels/{channel_id}")
def ui_channel(channel_id: int, history: int = 14):
    """Канал: карточка, календарь с историей и пропусками, сводка.

    `history` — сколько суток прошлого показывать. По умолчанию две недели: этого
    хватает, чтобы увидеть пропуски, и мало, чтобы лента осталась читаемой.
    """
    history = max(0, min(90, int(history)))
    c = db()
    try:
        ch = c.execute("SELECT * FROM pub_channels WHERE id=? AND tenant_id=?",
                       (channel_id, TENANT)).fetchone()
        if ch is None:
            raise HTTPException(404, "канал не найден")
        cal = _calendar(c, ch, history)
        waiting = c.execute(
            "SELECT COUNT(*) n FROM pub_plan_items WHERE channel_id=? AND slot_at IS NULL "
            "AND status NOT IN ('cancelled','done')", (channel_id,)).fetchone()["n"]
        host = c.execute("SELECT code, title FROM pub_hosts WHERE id=?",
                         (ch["host_id"],)).fetchone() if ch["host_id"] else None
        prof = c.execute("SELECT serial, title FROM pub_profiles WHERE id=?",
                         (ch["profile_id"],)).fetchone() if ch["profile_id"] else None
        try:
            schedule = json.loads(ch["schedule"] or "{}")
        except ValueError:
            schedule = {}
        counts = {}
        for row in cal:
            counts[row["state"]] = counts.get(row["state"], 0) + 1
        return {
            "channel": {
                "id": ch["id"], "name": ch["name"], "handle": ch["handle"] or "",
                "uc": ch["uc"], "lang": (ch["lang"] or "").upper(),
                "niche": ch["niche"] or "", "status": ch["status"],
                "tz": ch["tz"], "horizon_days": ch["horizon_days"],
                "lead_days": ch["lead_days"],
                "deferred": bool(ch["deferred"]), "deferred_limit": ch["deferred_limit"],
                "schedule": schedule,
                "per_week": sum(len(v or []) for v in schedule.values()),
                "host": (f'{host["code"]} · {host["title"]}' if host else ""),
                "profile": (f'{prof["serial"]} · {prof["title"]}' if prof else ""),
                "presets": {"rewrite": ch["preset_rewrite"] or "",
                            "vsgen": ch["preset_vsgen_id"],
                            "cover": ch["preset_cover"] or ""},
            },
            "calendar": cal,
            "counts": counts,
            "waiting_without_slot": waiting,
            "history_days": history,
        }
    finally:
        c.close()


class LinksIn(BaseModel):
    channel_id: int
    links: str
    slot: float | None = None


@app.post("/ui/links")
def ui_links(body: LinksIn):
    """Старт по ссылке.

    В боевой системе форма отправляет на существующий `POST /api/channels/{cid}/links`
    — семантика та же. Здесь свой адрес только потому, что кабинет целиком поднять
    без ключей и соседей нельзя.

    `titles` передаём обязательно: без него боевая `add_links` пойдёт в YouTube за
    названиями доноров, а сетевые вызовы в этой оболочке запрещены.
    """
    urls = parse_links(body.links or "")
    if not urls:
        raise HTTPException(400, "в поле нет ни одной ссылки")
    c = db()
    try:
        ch = c.execute("SELECT * FROM pub_channels WHERE id=? AND tenant_id=?",
                       (body.channel_id, TENANT)).fetchone()
        if not ch:
            raise HTTPException(404, "канал не найден")
        res = add_links(c, TENANT, ch, urls, time.time(), slot=body.slot,
                        titles={u: "" for u in urls})
        c.commit()
    finally:
        c.close()
    return {"placed": [str(p) for p in res.get("placed", [])],
            "queued": res.get("queued", 0),
            "skipped": list(res.get("skipped", []))}


# ─────────────────────────── админ: тревоги и диск ───────────────────────────

@app.get("/ui/alerts")
def ui_alerts():
    """Что сейчас горит и сказали ли мы об этом (ТЗ §13).

    Экран отвечает на три разных вопроса, и их важно не смешивать: что происходит,
    что из этого уже отправлено в телеграм, и настроен ли телеграм вообще. Тревога,
    которая честно определилась, но не ушла, потому что нет токена, — не то же самое,
    что тишина.
    """
    now = time.time()
    c = db()
    try:
        found = pub_alerts.detect(c, now, TENANT)
        state = pub_alerts.load_state(c)
        active = []
        for key, a in sorted(found.items()):
            st = state.get(key) or {}
            active.append({
                "key": key, "title": a.title, "text": a.text, "money": a.money,
                "said_at": st.get("sent") or 0, "times": st.get("n") or 0,
                "since": st.get("since") or 0,
            })
        # Поводы, которые уже сняты, но про которые мы обязаны сказать «починилось».
        owed = [{"key": k, "title": (state[k] or {}).get("title") or k}
                for k in sorted(state) if k not in found and (state[k] or {}).get("sent")]
    finally:
        c.close()
    tg = pub_alerts_tg.Telegram(log=lambda *_a: None)
    return {
        "active": active,
        "owed_recovered": owed,
        "transport": {"configured": tg.enabled, "token": mask(tg.token),
                      "chat": tg.chat or "",
                      "how": "токен от @BotFather в .env → dev/alerts.py --whoami "
                             "покажет chat_id → dev/alerts.py --test"},
        "thresholds": [
            {"what": "очередь не пустая, но ничего не завершается",
             "value": pub_alerts.STALL_SEC},
            {"what": "провайдер на паузе", "value": pub_alerts.RESTING_SEC},
            {"what": "есть готовые ролики, но заливки нет", "value": pub_alerts.UPLOAD_SEC},
            {"what": "самая старая задача ждёт", "value": pub_alerts.OLDEST_SEC},
            {"what": "повтор одного и того же повода", "value": pub_alerts.REPEAT_SEC},
        ],
        "disk_min_gb": pub_alerts.DISK_MIN_GB,
    }


@app.get("/ui/disk")
def ui_disk():
    """Место, проекты и цена уборки (ТЗ §03/12).

    Ничего не удаляет и не может: удаление живёт в `dev/retention.py --apply`.
    Здесь видно ровно то, на основании чего принимается решение, включая то, сколько
    оплаченных вызовов исчезнет вместе с файлами.
    """
    now = time.time()
    c = db()
    try:
        pol = pub_retention.policy(c)
        projects = pub_retention.scan(c, TENANTS_DIR, TENANT)
        acts = pub_retention.plan(projects, pol, now)
    finally:
        c.close()
    rows, freeable = [], 0
    for p, action, why, freed in acts:
        price = pub_retention.price_of(p, action) if action != "keep" else {
            "calls": 0, "cost": 0.0, "detail": ""}
        freeable += freed
        rows.append({
            "pid": p.pid, "name": p.name, "phase": p.phase or "",
            "release_id": p.release_id or "", "video_id": p.video_id or "",
            "total": p.total, "strippable": p.strippable, "sizes": p.sizes,
            "published_at": p.published_at or 0, "busy": p.busy,
            "action": action, "why": why, "frees": freed,
            "paid_calls": price["calls"], "paid_cost": price["cost"],
        })
    rows.sort(key=lambda r: -r["total"])
    return {
        "space": pub_retention.free_space(TENANTS_DIR),
        "policy": pol,
        "policy_set": bool(pol),
        "projects": rows,
        "total": sum(r["total"] for r in rows),
        "freeable": freeable,
        "categories": [{"code": c_, "name": n, "fate": f}
                       for c_, n, f, _pat in pub_retention.CATEGORIES],
        "how": "срок хранения задаёт владелец: dev/retention.py --policy strip=14 drop=90",
    }


# ──────────────────────────────── пресеты ────────────────────────────────

def _neighbour_get(base: str, path: str, key: str) -> tuple:
    """(тело, ошибка). Недоступный сосед не роняет экран, но и не притворяется пустым."""
    if not key:
        return None, "ключ доступа не сохранён"
    req = urllib.request.Request(f"{base}{path}")
    req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=NEIGHBOUR_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}"), ""
    except urllib.error.HTTPError as exc:
        return None, f"ответил {exc.code}"
    except Exception as exc:                                     # noqa: BLE001
        return None, f"не отвечает: {type(exc).__name__}"


@app.get("/ui/presets")
def ui_presets():
    """Один список пресетов вместо трёх (ТЗ §04: пресеты 3 → 1).

    Все три набора берутся **по HTTP с ключом**, а не чтением чужих файлов. Сегодня
    кабинет получает пресеты соседей через `pub_neighbors.py`, который открывает их
    SQLite и JSON напрямую и сопоставляет пользователей по логину. Это анти-контракт:
    он делает три сервиса неразделимыми и ломается от любой смены схемы. Ради этого
    экрана в Rewrite добавлен `GET /api/v1/presets` — теперь честный путь существует
    для обоих соседей.

    Отдельно считается то, ради чего экран и нужен: какие каналы на какой пресет
    ссылаются и есть ли ссылки в никуда. Канал, указывающий на удалённый пресет,
    сегодня узнаёт об этом в момент генерации.
    """
    c = db()
    try:
        used = {"rewrite": {}, "vsgen": {}, "cover": {}}
        for ch in c.execute("SELECT id, name, preset_rewrite, preset_vsgen_id, "
                            "preset_cover FROM pub_channels WHERE tenant_id=? "
                            "ORDER BY name", (TENANT,)):
            for kind, val in (("rewrite", ch["preset_rewrite"]),
                              ("vsgen", ch["preset_vsgen_id"]),
                              ("cover", ch["preset_cover"])):
                if val in (None, ""):
                    continue
                used[kind].setdefault(str(val), []).append(
                    {"id": ch["id"], "name": ch["name"]})
        vsgen = [{"key": str(r["id"]), "name": r["name"] or f"пресет {r['id']}",
                  "facts": _preset_facts(r["settings"])}
                 for r in c.execute("SELECT id, name, settings FROM presets "
                                    "WHERE tenant_id=? ORDER BY name COLLATE NOCASE",
                                    (TENANT,))]
        keys = {n: get_key(c, n) for n in ("rewrite", "cover")}
    finally:
        c.close()

    groups = [{"service": "vsgen", "title": "Производство (кабинет)", "items": vsgen,
               "error": "", "note": "ключ пресета — число, а не имя"}]

    body, err = _neighbour_get(REWRITE_BASE, "/api/v1/presets", keys["rewrite"])
    rew = (body or {}).get("rewrite") or {}
    groups.append({
        "service": "rewrite", "title": "Рерайт по ссылке", "error": err,
        "note": "канал ссылается именно на этот набор, а не на брифы генерации",
        "items": [{"key": name, "name": name, "facts": _preset_facts(cfg)}
                  for name, cfg in sorted(rew.items(), key=lambda kv: kv[0].lower())]})
    gen = (body or {}).get("generation") or {}
    if gen:
        groups.append({
            "service": "rewrite_gen", "title": "Брифы генерации с нуля", "error": "",
            "note": "к рерайту по ссылке отношения не имеют — отдельное хранилище",
            "items": [{"key": name, "name": name, "facts": _preset_facts(cfg)}
                      for name, cfg in sorted(gen.items(), key=lambda kv: kv[0].lower())]})

    body, err = _neighbour_get(COVER_BASE, "/presets", keys["cover"])
    cov = body if isinstance(body, dict) else {}
    groups.append({
        "service": "cover", "title": "Обложки", "error": err, "note": "",
        "items": [{"key": name, "name": name, "facts": _preset_facts(cfg)}
                  for name, cfg in sorted(cov.items(), key=lambda kv: kv[0].lower())]})

    # Кто кем пользуется — и куда ссылок нет.
    have = {g["service"]: {i["key"] for i in g["items"]} for g in groups}
    for g in groups:
        for item in g["items"]:
            item["channels"] = used.get(g["service"], {}).get(item["key"], [])
    broken = []
    for kind, title in (("rewrite", "рерайта"), ("vsgen", "производства"),
                        ("cover", "обложек")):
        if have.get(kind) is None or (kind != "vsgen" and not have.get(kind)):
            continue          # сосед не ответил: молчим, а не объявляем всё сломанным
        for key, chans in sorted(used[kind].items()):
            if key in have.get(kind, set()):
                continue
            broken.append({"kind": kind, "key": key,
                           "why": f"пресет {title} «{key}» не найден в сервисе",
                           "channels": chans})
    return {"groups": groups, "broken": broken,
            "unavailable": [] if all(not g["error"] for g in groups) else
            [{"what": g["title"], "why": g["error"]} for g in groups if g["error"]]}


def _preset_facts(settings) -> list:
    """Две-три вещи, по которым пресет узнают. Секреты сюда не попадают по построению:
    имена, похожие на ключ, отбрасываются — как на стороне Rewrite, так и здесь."""
    data = settings
    if isinstance(settings, str):
        try:
            data = json.loads(settings or "{}")
        except ValueError:
            return []
    if not isinstance(data, dict):
        return []
    facts = []
    for k, v in data.items():
        low = str(k).lower()
        if any(m in low for m in ("key", "token", "secret", "password", "auth")):
            continue
        if isinstance(v, (dict, list)):
            v = f"{len(v)} шт."
        if v in (None, "", False):
            continue
        facts.append(f"{k}: {v}")
        if len(facts) >= 4:
            break
    return facts


@app.get("/ui/public")
def ui_public():
    """Вышел ли ролик на самом деле (ТЗ §11).

    Отдельно от всего остального, потому что это единственное место, где видно
    разницу между «расписание подтверждено» и «ролик вышел». Сегодня интерфейс
    называет публикацией первое.

    Проверка ничего не отменяет и не перезаливает — она только повышает запись
    `scheduled` → `published`. Поэтому здесь нет ни одной кнопки, меняющей выпуск.
    """
    now = time.time()
    c = db()
    try:
        key = pub_verify.api_key(c)
        due = [{"release_id": r["release_id"], "video_id": r["video_id"],
                "slot_at": r["slot_at"],
                "late_hours": round((now - float(r["slot_at"] or now)) / 3600, 1)}
               for r in pub_verify.due(c, TENANT, now)]
        checked = []
        for e in c.execute(
                "SELECT e.release_id, e.payload, e.created, r.phase, r.blocked_reason "
                "FROM pub_release_events e JOIN pub_releases r "
                "  ON r.release_id = e.release_id "
                "WHERE e.phase='public_check' AND r.tenant_id=? "
                "ORDER BY e.created DESC LIMIT 40", (TENANT,)):
            try:
                payload = json.loads(e["payload"] or "{}")
            except ValueError:
                payload = {}
            checked.append({"release_id": e["release_id"], "at": e["created"],
                            "state": payload.get("state") or "",
                            "privacy": payload.get("privacy") or "",
                            "error": payload.get("error") or "",
                            "phase": e["phase"], "blocked": e["blocked_reason"] or ""})
        confirmed = c.execute(
            "SELECT COUNT(*) FROM pub_releases WHERE tenant_id=? AND phase='published'",
            (TENANT,)).fetchone()[0]
        scheduled = c.execute(
            "SELECT COUNT(*) FROM pub_releases WHERE tenant_id=? AND phase='scheduled'",
            (TENANT,)).fetchone()[0]
    finally:
        c.close()
    return {
        "key_set": bool(key), "key": mask(key),
        "due": due, "checked": checked,
        "confirmed": confirmed, "scheduled": scheduled,
        "grace_min": pub_verify.GRACE_SEC // 60,
        "how": "ключ YouTube Data API v3: dev/verify_public.py --save-key ... "
               "или VS_YT_API_KEY в .env. Проход: dev/verify_public.py --run",
        "blind_spot": "Ключ API видит только публичные и доступные по ссылке ролики. "
                      "Запланированный (ещё приватный) и удалённый выглядят одинаково — "
                      "пустым ответом. Различить их может только OAuth-токен канала.",
    }


# ─────────────────────── действия: отменить и повторить ───────────────────────

# Пульт эти действия НЕ выполняет — он их пересылает. У кабинета и соседей они уже
# написаны, и написаны со всеми гвардами: доступность провайдера, конфликт с уже идущей
# правкой картинок, отложенный рендер, запрет повтора незавершённой задачи. Повторить
# эти проверки здесь значило бы завести вторую реализацию, которая разойдётся с первой
# в первый же месяц. Именно ради этого пульт и хранит сессию кабинета, а не свою.
ACTIONS = {
    # сервис: действие → (метод, путь, как авторизуемся)
    "vsgen": {"cancel": ("POST", "/api/jobs/{id}/cancel", "session"),
              "retry": ("POST", "/api/jobs/{id}/retry", "session")},
    "rewrite": {"cancel": ("DELETE", "/api/v1/jobs/{id}", "key"),
                "retry": ("POST", "/api/v1/jobs/{id}/retry", "key")},
    "cover": {"cancel": ("POST", "/jobs/{id}/cancel", "key"),
              "retry": ("POST", "/jobs/{id}/retry", "key")},
}
BASES = {"vsgen": None, "rewrite": REWRITE_BASE, "cover": COVER_BASE}


@app.post("/ui/actions/{service}/{job_id}/{action}")
def ui_action(service: str, job_id: str, action: str, request: Request):
    """Отменить или повторить задачу — в том сервисе, которому она принадлежит.

    Отказ соседа пересказывается как есть: «повтор доступен только для ошибочной или
    отменённой задачи» — это ответ, а не сбой, и подменять его своим текстом нельзя.
    """
    plan = ACTIONS.get(service, {}).get(action)
    if not plan:
        raise HTTPException(404, f"нет действия «{action}» для сервиса «{service}»")
    method, path, how = plan
    base = BASES[service] or CABINET_BASE
    headers = {"Content-Type": "application/json"}
    if how == "session":
        headers["Cookie"] = f"{auth.COOKIE}={getattr(request.state, 'sid', '')}"
        # Кабинет отвечает JSON только на пометку «это fetch», иначе шлёт редирект
        # на свою же страницу очереди — её нам показывать нечем.
        headers["X-Requested-With"] = "fetch"
    else:
        c = db()
        try:
            key = get_key(c, service)
        finally:
            c.close()
        if not key:
            raise HTTPException(503, f"нет ключа доступа к сервису «{service}»")
        headers["Authorization"] = f"Bearer {key}"

    req = urllib.request.Request(base + path.format(id=urllib.parse.quote(str(job_id))),
                                 data=b"" if method == "POST" else None, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=NEIGHBOUR_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = (json.loads(raw or "{}") or {}).get("detail") or raw[:200]
        except ValueError:
            detail = raw[:200]
        raise HTTPException(exc.code if exc.code in (400, 401, 403, 404, 409) else 502,
                            detail or f"сервис ответил {exc.code}")
    except Exception as exc:                                     # noqa: BLE001
        raise HTTPException(502, f"сервис «{service}» не отвечает: {type(exc).__name__}")
    return {"ok": True, "service": service, "job_id": job_id, "action": action,
            "answer": body}


@app.post("/ui/links/preview")
def ui_links_preview(body: LinksIn):
    """Что произойдёт, если поставить эти ссылки. Ничего не записывает.

    Раскладку считает **та же** `add_links`, что и настоящая постановка, — в открытой
    транзакции, которая затем откатывается. Написать отдельный «предпросчёт» значило бы
    завести вторую раскладку, и она разошлась бы с первой на первом же граничном случае:
    дубль, занятый слот, конец горизонта.

    Пресеты и язык здесь **показываются, а не выбираются**. Они берутся из канала, и
    выбор на этой форме был бы настройкой, которую никто не читает: колонка
    `pub_plan_items.overrides` в схеме есть, но за неё во всём кабинете не держится
    ни одна строка кода. Настройка либо работает, либо её нет.
    """
    urls = parse_links(body.links or "")
    if not urls:
        raise HTTPException(400, "в поле нет ни одной ссылки")
    c = db()
    try:
        ch = c.execute("SELECT * FROM pub_channels WHERE id=? AND tenant_id=?",
                       (body.channel_id, TENANT)).fetchone()
        if not ch:
            raise HTTPException(404, "канал не найден")
        try:
            res = add_links(c, TENANT, ch, urls, time.time(), slot=body.slot,
                            titles={u: "" for u in urls})
        finally:
            c.rollback()          # предпросмотр не оставляет следов ни при каком исходе
        presets = _channel_presets(c, ch)
    finally:
        c.close()
    return {
        "channel": {"id": ch["id"], "name": ch["name"], "lang": ch["lang"] or "",
                    "tz": ch["tz"], "horizon_days": ch["horizon_days"],
                    "lead_days": ch["lead_days"]},
        "presets": presets,
        "placed": [str(p) for p in res.get("placed", [])],
        "queued": res.get("queued", 0),
        "skipped": list(res.get("skipped", [])),
        "note": "пресеты и язык берутся из канала — поменять их можно в самом канале",
    }


def _channel_presets(c, ch) -> list:
    """Чем именно будет сделан ролик — и существуют ли эти пресеты на самом деле.

    Ссылка на удалённый пресет сегодня выясняется в момент генерации, то есть после
    того, как конвейер встал. Здесь она видна до нажатия кнопки.
    """
    out = []
    row = c.execute("SELECT name FROM presets WHERE id=? AND tenant_id=?",
                    (ch["preset_vsgen_id"], TENANT)).fetchone() \
        if ch["preset_vsgen_id"] else None
    out.append({"what": "производство", "name": (row["name"] if row else
                                                 str(ch["preset_vsgen_id"] or "—")),
                "exists": bool(row), "where": "кабинет"})
    keys = {n: get_key(c, n) for n in ("rewrite", "cover")}
    body, _err = _neighbour_get(REWRITE_BASE, "/api/v1/presets", keys["rewrite"])
    have_rew = set((body or {}).get("rewrite") or {})
    out.append({"what": "рерайт", "name": ch["preset_rewrite"] or "—",
                "exists": (None if body is None
                           else bool(ch["preset_rewrite"] in have_rew)),
                "where": "rewrite"})
    body, _err = _neighbour_get(COVER_BASE, "/presets", keys["cover"])
    have_cov = set(body or {}) if isinstance(body, dict) else None
    out.append({"what": "обложка", "name": ch["preset_cover"] or "—",
                "exists": (None if have_cov is None
                           else bool(ch["preset_cover"] in have_cov)),
                "where": "cover"})
    return out
