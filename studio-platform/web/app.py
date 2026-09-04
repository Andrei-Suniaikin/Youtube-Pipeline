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
import secrets
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
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402
from pydantic import BaseModel                                   # noqa: E402

sys.path.insert(0, HERE)
import pult_auth as auth                                       # noqa: E402

from channel_slots import (ANY_DAY, DAY_KEYS, _localize, _zone,  # noqa: E402
                           normalize_schedule, upcoming_slots)
from pub_engine import PHASES, PHASE_DEADLINES                   # noqa: E402
from pub_remote import (COVER_BASE, REWRITE_BASE, donor_text,     # noqa: E402
                        get_key, video_id)
from pub_services import MEDIA_STAGES, OPTIONAL_STAGES           # noqa: E402
from pub_neighbors import (cover_presets, rewrite_presets,        # noqa: E402
                           vsgen_presets)
from pub_tags import add_links, parse_links, plan_rows           # noqa: E402
import pub_ui                                                    # noqa: E402

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


class LoginIn(BaseModel):
    login: str = ""
    password: str = ""


@app.post("/ui/login")
def ui_login(body: LoginIn, response: Response):
    """Пароль проверяет кабинет, не мы. Своего списка пользователей у пульта нет.

    Обработчик синхронный сознательно: при отдельном запуске пульта вход уходит
    к кабинету по HTTP, и на потоке событий такой вызов повесил бы процесс.
    """
    try:
        sid = auth.login(CABINET_BASE, body.login, body.password)
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
    """Оболочка пульта.

    `no-cache` — не «не кэшировать», а «перед показом спроси, не изменилось ли».
    Без этого заголовка браузер кэширует страницу по своему усмотрению (эвристика
    от Last-Modified), и человек продолжает работать со вчерашней разметкой, пока
    не догадается про Cmd+Shift+R. Здесь это стоило одного полного тупика: страницу
    починили, а вход по-прежнему падал с 404, потому что в браузере лежала прежняя.

    Цена — 74 КБ на открытие страницы: ETag сервер выставляет, но условный запрос
    (`If-None-Match`) FileResponse в этой версии Starlette не обрабатывает и всё
    равно отдаёт тело. Проверено, а не предположено. Для оболочки, которая меняется
    несколько раз в час, это несопоставимо дешевле, чем работать со вчерашней.
    """
    return FileResponse(os.path.join(HERE, "index.html"),
                        headers={"Cache-Control": "no-cache"})


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
        for u in c.execute("SELECT id, name, keys, tenant_id FROM users WHERE tenant_id=?",
                           (TENANT,)):
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
                seen_vals = list(dict.fromkeys(uniq))
                own.append({
                    "user": u["name"], "user_id": u["id"], "family": family,
                    "count": len(seen_vals), "pooled": len(seen_vals) > 1,
                    "mine": str(u["tenant_id"] or u["id"]) == TENANT,
                    # Позиция нужна, чтобы ключ можно было снять поимённо, не отдавая
                    # его значения: снаружи видно «второй ключ googler», а не сам ключ.
                    "items": [{"index": i, "mask": _mask_key(v)}
                              for i, v in enumerate(seen_vals)]})

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


# {channel_id:int} — не украшение: без указания типа этот путь перехватывает и
# «/ui/channels/new», объявленный ниже, и форма заведения канала отвечает 422.
@app.get("/ui/channels/{channel_id:int}")
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
                "lang_raw": ch["lang"] or "",
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
                # Сырые значения — для формы настройки: она подставляет их в поля
                # и отдаёт обратно теми же именами, что у формы кабинета.
                "note": ch["note"] or "",
                "profile_id": ch["profile_id"], "host_id": ch["host_id"],
                "preset_vsgen_id": ch["preset_vsgen_id"],
                "preset_rewrite": ch["preset_rewrite"] or "",
                "preset_cover": ch["preset_cover"] or "",
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


def _mask_key(value: str) -> str:
    """Показать, что ключ есть, не показав ключа. Тот же приём, что в кабинете."""
    v = str(value or "")
    return f"{v[:4]}…{v[-4:]}" if len(v) > 12 else "…" * 3


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


# ──────────────────────────────── текст ────────────────────────────────

# Экран рерайта и генерации — один на оба (ТЗ §05). Пульт здесь ничего не делает сам:
# он пересылает запрос Rewrite'у ОТ ИМЕНИ вошедшего человека, его же кукой. Ключ
# доступа не годится: ключ принадлежит одному человеку, и партнёр создавал бы задачи
# от чужого имени, а видел бы чужие результаты.
TEXT_MODES = (
    ("link", "Ссылка на YouTube", "текст берётся из субтитров ролика"),
    ("text", "Вставленный текст", "свой текст, без донора"),
    ("topic", "С нуля по теме", "текста нет, есть тема и бриф"),
)


def _pass_code(code: int) -> int:
    """Какой код вернуть наружу, получив его от соседа.

    Отказ соседа — это ответ, а не наш сбой: 4xx и «не настроено» (503) проходят
    как есть. Прятать «не настроен ключ» под 502 «сосед не отвечает» значит послать
    человека чинить сеть вместо ключа.
    """
    return code if (400 <= code < 500 or code == 503) else 502


def _rw(path: str, sid: str, payload=None, method=None, raw=None, ctype=None):
    """Запрос к Rewrite от имени вошедшего. Возвращает (код, тело)."""
    data = raw
    headers = {"Cookie": f"{auth.COOKIE}={sid}"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif ctype:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(REWRITE_BASE + path, data=data,
                                 method=method or ("POST" if data is not None else "GET"))
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=NEIGHBOUR_TIMEOUT * 5) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body.startswith(("{", "[")) else body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body or "{}")
            detail = parsed.get("detail") or parsed.get("error") or body[:200]
        except ValueError:
            detail = body[:200]
        return exc.code, detail
    except Exception as exc:                                     # noqa: BLE001
        return 502, f"рерайт не отвечает: {type(exc).__name__}"


@app.get("/ui/text")
def ui_text(request: Request):
    """Экран текста: что уже сделано и чем можно сделать ещё.

    Модели спрашиваем у Rewrite, а не перечисляем сами: он показывает только те, под
    которые у этого человека есть рабочий ключ или подписка. Список, в котором половина
    пунктов отвечает «503», — это ровно та ложь интерфейса, которую мы убираем.
    """
    sid = getattr(request.state, "sid", "")
    code, jobs = _rw("/jobs", sid)
    if code != 200:
        jobs = []
    _c, models = _rw("/models", sid)
    # Пресеты берём сессионными путями, а не /api/v1/presets: тот требует ключ или
    # заголовок X-Session-Id, а у нас кука — по ней Rewrite узнаёт человека только
    # в middleware, куда /api/v1/* не заходит. Сессионные пути отдают ровно то же.
    _c, settings = _rw("/settings/keys", sid)
    _c, gen = _rw("/presets", sid)
    # Список языков не зависит от человека — это справочник, и его можно спросить общим
    # ключом. Разница принципиальная: данные человека берём его сессией, справочники —
    # ключом сервиса. Смешивать эти два способа и есть тот дефект, из-за которого
    # партнёр однажды увидел бы чужие пресеты.
    c0 = db()
    try:
        langs_body, _err = _neighbour_get(REWRITE_BASE, "/api/v1/languages",
                                          get_key(c0, "rewrite"))
    finally:
        c0.close()
    languages = [{"code": x.get("short_code") or x.get("canonical"),
                  "title": x.get("display_ru") or x.get("canonical")}
                 for x in ((langs_body or {}).get("languages") or [])
                 if x.get("canonical")]
    rows = []
    for j in (jobs if isinstance(jobs, list) else []):
        pct = j.get("progress")
        rows.append({
            "id": str(j.get("id") or ""),
            "kind": str(j.get("type") or ""),
            "title": j.get("name") or j.get("preset_name") or str(j.get("id") or ""),
            "status": str(j.get("status") or ""),
            "state": _state(j.get("status")),
            "progress": (float(pct) / 100 if isinstance(pct, (int, float)) else None),
            "detail": j.get("detail") or "",
            "langs": j.get("langs") or [],
            "created": j.get("created_at"),
        })
    rows.sort(key=lambda r: -(r["created"] or 0))
    c = db()
    try:
        channels = [{"id": r["id"], "name": r["name"], "lang": r["lang"] or "",
                     "preset_rewrite": r["preset_rewrite"] or ""}
                    for r in c.execute("SELECT id, name, lang, preset_rewrite "
                                       "FROM pub_channels WHERE tenant_id=? ORDER BY name",
                                       (TENANT,))]
    finally:
        c.close()
    return {
        "jobs": rows,
        "modes": [{"key": k, "title": t, "note": n} for k, t, n in TEXT_MODES],
        "models": models if isinstance(models, (list, dict)) else [],
        "presets": {
            "rewrite": sorted((settings or {}).get("rew_presets") or {})
            if isinstance(settings, dict) else [],
            "generation": sorted(gen or {}) if isinstance(gen, dict) else [],
        },
        "channels": channels,
        "languages": languages,
        "note": "задачи и результаты — ваши: пульт обращается к рерайту вашей же сессией",
    }


class TextIn(BaseModel):
    mode: str
    source: str = ""                 # ссылка, текст или тема — по режиму
    languages: list = []
    model: str = ""
    preset: str = ""
    channel_id: int | None = None


@app.post("/ui/text")
def ui_text_create(body: TextIn, request: Request):
    """Создать задачу текста. Три источника, один экран.

    Ссылка превращается в текст здесь, кабинетом: `donor_text` уже умеет доставать
    субтитры, ходит по нескольким выходам при исчерпанной квоте и кэширует результат
    для соседних каналов. Дублировать это в интерфейсе незачем.
    """
    sid = getattr(request.state, "sid", "")
    mode = (body.mode or "").strip()
    if mode not in {k for k, _t, _n in TEXT_MODES}:
        raise HTTPException(400, f"неизвестный режим «{mode}»")
    langs = [str(x).strip() for x in (body.languages or []) if str(x).strip()]
    if not langs:
        raise HTTPException(400, "не выбран ни один язык")

    if mode == "topic":
        if not body.preset:
            raise HTTPException(400, "для генерации с нуля нужен бриф (пресет)")
        code, out = _rw("/jobs/generation", sid, {
            "preset_name": body.preset, "topic": body.source,
            "languages": [{"code": x, "display": x} for x in langs],
            "model": body.model or "gpt-4.1-nano"})
        if code >= 400:
            raise HTTPException(_pass_code(code), out)
        return {"ok": True, "job": out}

    if mode == "link":
        try:
            text = donor_text(body.source)
        except Exception as exc:                                 # noqa: BLE001
            raise HTTPException(400, f"текст ролика не получен: {exc}")
        name = (video_id(body.source) or "донор") + ".txt"
    else:
        text = body.source or ""
        name = "вставленный.txt"
    if not text.strip():
        raise HTTPException(400, "текста нет — рерайтить нечего")

    boundary = "----studio" + secrets.token_hex(8)
    part = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n").encode("utf-8")
    raw = part + text.encode("utf-8") + f"\r\n--{boundary}--\r\n".encode("utf-8")
    code, out = _rw("/upload", sid, raw=raw,
                    ctype=f"multipart/form-data; boundary={boundary}")
    if code >= 400:
        raise HTTPException(_pass_code(code), out)
    stored = (out or {}).get("filename") or (out or {}).get("file") or name

    code, out = _rw("/jobs/rewrite", sid, {
        "files": [stored],
        "languages": [{"code": x, "display": x} for x in langs],
        "model": body.model or "gpt-4.1-nano"})
    if code >= 400:
        raise HTTPException(_pass_code(code), out)
    return {"ok": True, "job": out, "chars": len(text)}


# ──────────────────────────────── обложки ────────────────────────────────

# Стратегия выбирается ДО показа полей (ТЗ §10): каждая берёт свой источник, и поля
# у них разные. Раньше это была одна простыня, где половина полей молча не работала.
COVER_MODES = (
    ("url", "По превью донора", "ссылка на ролик — берём его обложку и переделываем"),
    ("title", "По заголовку", "обложки нет, есть текст заголовка на каждый язык"),
    ("seed", "По семени", "шаблон ниши плюс семя — несколько вариантов на язык"),
)

# Что перестаёт действовать при каком выборе. Каждая строка проверена по коду, а не
# выведена из названий полей: интерфейс гасит поле только там, где движок его не читает.
COVER_RULES = (
    {"when": "edit",
     "kills": ["style_prompt", "use_auto_style", "analysis_provider", "analysis_model"],
     "why": "превью уходит в модель на редактирование целиком: vision-анализ и сборка "
            "промпта пропускаются (cover_core.py:1879)"},
    {"when": "not_url",
     "kills": ["reference_image", "simple_mode"],
     "why": "редактирование превью возможно только когда превью есть — то есть в режиме "
            "«по превью донора» (cover_core.py:2107)"},
)


def _cover(path: str, sid: str, payload=None, method=None):
    """Запрос к Cover от имени вошедшего — его же кукой, а не общим ключом."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(COVER_BASE + path, data=data,
                                 method=method or ("POST" if data is not None else "GET"))
    req.add_header("Cookie", f"{auth.COOKIE}={sid}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=NEIGHBOUR_TIMEOUT * 5) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body.startswith(("{", "[")) else body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body or "{}")
            detail = parsed.get("detail") or parsed.get("error") or body[:200]
        except ValueError:
            detail = body[:200]
        return exc.code, detail
    except Exception as exc:                                     # noqa: BLE001
        return 502, f"обложки не отвечают: {type(exc).__name__}"


def _cover_options(is_admin: bool) -> list:
    """Варианты генерации — из самого Cover, а не переписанные у нас.

    `GENERATION_OPTIONS` там и есть источник правды: по нему же Cover проверяет пару
    provider+model на входе. Список, разъехавшийся с проверкой, обещал бы то, что
    сервер отвергнет.
    """
    mod = sys.modules.get("cover_server")
    core = sys.modules.get("cover_core")
    if core is None or not hasattr(core, "GENERATION_OPTIONS"):
        return []
    admin_only = getattr(mod, "ADMIN_ONLY_PROVIDERS", set()) if mod else set()
    out = []
    for label, provider, model in core.GENERATION_OPTIONS:
        if provider in admin_only and not is_admin:
            continue          # платит с личного счёта владельца — не показываем чужим
        out.append({"label": label, "provider": provider, "model": model,
                    "admin_only": provider in admin_only})
    return out


@app.get("/ui/covers")
def ui_covers(request: Request):
    """Экран обложек: стратегия, поля этой стратегии и что в ней не действует."""
    sid = getattr(request.state, "sid", "")
    user = getattr(request.state, "user", None)
    is_admin = bool(user is not None and (user["role"] if "role" in user.keys() else "")
                    == "admin")
    code, jobs = _cover("/jobs?page=1&page_size=50", sid)
    items = (jobs.get("items") if isinstance(jobs, dict) else jobs) or []
    rows = []
    for j in (items if isinstance(items, list) else []):
        pct = j.get("progress") or j.get("progress_percent")
        rows.append({
            "id": str(j.get("id") or ""),
            "title": j.get("name") or j.get("preset_name") or str(j.get("id") or ""),
            "status": str(j.get("status") or ""),
            "state": _state(j.get("status")),
            "progress": (float(pct) / 100 if isinstance(pct, (int, float)) else None),
            "detail": j.get("detail") or j.get("progress_detail") or "",
            "results": j.get("results_count") or 0,
            "created": j.get("created_at"),
        })
    _c, presets = _cover("/presets", sid)
    # Языки берём у Cover его же справочником: он ждёт ОТОБРАЖАЕМОЕ имя («Русский»),
    # а не код. Подставить сюда список Rewrite значило бы слать ему «ru» и получать
    # отказ на каждой второй задаче.
    core = sys.modules.get("cover_core")
    languages = sorted(getattr(core, "LANG_DISPLAY_TO_CODE", {}) or {}) if core else []
    return {
        "jobs": rows,
        "languages": languages,
        "modes": [{"key": k, "title": t, "note": n} for k, t, n in COVER_MODES],
        "presets": sorted(presets) if isinstance(presets, dict) else [],
        "options": _cover_options(is_admin),
        "rules": list(COVER_RULES),
        "aspect": "16:9",
        "note": "задачи и результаты — ваши: пульт обращается к обложкам вашей же сессией",
    }


class CoverIn(BaseModel):
    mode: str
    preset: str = ""
    languages: list = []
    provider: str = ""
    model: str = ""
    urls: list = []
    titles: dict = {}
    seed: str = ""
    n_variants: int = 3
    extra_prompt: str = ""
    edit_preview: bool = False       # явный выбор, а не побочный эффект приложенного файла


@app.post("/ui/covers")
def ui_covers_create(body: CoverIn, request: Request):
    """Создать задачу обложек.

    Соотношение сторон не спрашиваем и не даём выбрать: у обложки YouTube оно одно.
    Оно уходит в пресет явно, а не «по умолчанию, если никто не переопределил».
    """
    sid = getattr(request.state, "sid", "")
    mode = (body.mode or "").strip()
    if mode not in {k for k, _t, _n in COVER_MODES}:
        raise HTTPException(400, f"неизвестная стратегия «{mode}»")
    langs = [str(x).strip() for x in (body.languages or []) if str(x).strip()]
    if not langs:
        raise HTTPException(400, "не выбран ни один язык")

    preset_data = {"aspect_ratio": "16:9"}
    if body.provider:
        preset_data["generation_provider"] = body.provider
        preset_data["generation_model"] = body.model or ""
    # Редактирование превью — отдельная галочка, а не следствие приложенного файла.
    # Приложенный референс сам по себе режим не меняет: это требование ТЗ §10, и до
    # сих пор оно нарушалось — reference_image молча уводил в другую ветку движка.
    if mode == "url":
        preset_data["simple_mode"] = bool(body.edit_preview)

    payload = {"languages": langs, "mode": mode,
               "preset_name": body.preset or "", "preset_data": preset_data}
    if mode == "url":
        urls = [u.strip() for u in (body.urls or []) if str(u).strip()]
        if not urls:
            raise HTTPException(400, "нет ни одной ссылки на донора")
        payload["urls"] = urls
    elif mode == "title":
        titles = {k: v for k, v in (body.titles or {}).items() if str(v).strip()}
        if not titles:
            raise HTTPException(400, "не задан заголовок ни для одного языка")
        payload["titles_per_lang"] = titles
        payload["extra_prompt"] = body.extra_prompt or ""
    else:
        if not (body.seed or "").strip():
            raise HTTPException(400, "семя пустое — генерировать не из чего")
        payload["seeds_per_lang"] = {lang: body.seed for lang in langs}
        payload["n_variants"] = max(1, min(8, int(body.n_variants or 3)))

    code, out = _cover("/jobs/cover", sid, payload)
    if code >= 400:
        raise HTTPException(_pass_code(code), out)
    return {"ok": True, "job": out}


# ──────────────────────────── результаты задач ────────────────────────────

# Один путь на оба сервиса: у Rewrite и Cover эндпоинты результатов совпадают по
# форме — `/jobs/{id}/files` отдаёт список имён, `/jobs/{id}/files/{имя}` сам файл.
# Проверку владельца делают они сами, каждый по своей задаче; пульт её не повторяет
# и не может ослабить — он ходит кукой человека, а не общим ключом.
RESULT_BASES = {"rewrite": REWRITE_BASE, "cover": COVER_BASE}

_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".webp": "image/webp", ".gif": "image/gif", ".txt": "text/plain; charset=utf-8",
         ".md": "text/markdown; charset=utf-8", ".json": "application/json",
         ".srt": "text/plain; charset=utf-8", ".zip": "application/zip"}


@app.get("/ui/files/{service}/{job_id}")
def ui_files(service: str, job_id: str, request: Request):
    """Что получилось у задачи. Пустой список — это «ничего не сделано», а не ошибка."""
    base = RESULT_BASES.get(service)
    if not base:
        raise HTTPException(404, f"нет сервиса «{service}»")
    code, body = _proxy_get(base, f"/jobs/{urllib.parse.quote(job_id)}/files",
                            getattr(request.state, "sid", ""))
    if code >= 400:
        raise HTTPException(_pass_code(code), body)
    names = [str(x) for x in body] if isinstance(body, list) else []
    return {"service": service, "job_id": job_id,
            "files": [{"name": n, "kind": _file_kind(n)} for n in names]}


@app.get("/ui/files/{service}/{job_id}/{name:path}")
def ui_file(service: str, job_id: str, name: str, request: Request):
    """Сам файл. Пульт его только передаёт: ни имени, ни пути он не конструирует."""
    base = RESULT_BASES.get(service)
    if not base:
        raise HTTPException(404, f"нет сервиса «{service}»")
    url = (f"{base}/jobs/{urllib.parse.quote(job_id)}/files/"
           f"{urllib.parse.quote(name)}")
    req = urllib.request.Request(url)
    req.add_header("Cookie", f"{auth.COOKIE}={getattr(request.state, 'sid', '')}")
    try:
        with urllib.request.urlopen(req, timeout=NEIGHBOUR_TIMEOUT * 5) as resp:
            data = resp.read()
            ctype = resp.headers.get("Content-Type")
    except urllib.error.HTTPError as exc:
        raise HTTPException(_pass_code(exc.code), exc.read().decode("utf-8", "replace")[:200])
    except Exception as exc:                                     # noqa: BLE001
        raise HTTPException(502, f"сервис «{service}» не отвечает: {type(exc).__name__}")
    ext = os.path.splitext(name)[1].lower()
    return Response(content=data, media_type=(_MIME.get(ext) or ctype
                                              or "application/octet-stream"))


def _proxy_get(base: str, path: str, sid: str) -> tuple:
    req = urllib.request.Request(base + path)
    req.add_header("Cookie", f"{auth.COOKIE}={sid}")
    try:
        with urllib.request.urlopen(req, timeout=NEIGHBOUR_TIMEOUT * 5) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.startswith(("{", "[")) else raw)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:200]
    except Exception as exc:                                     # noqa: BLE001
        return 502, f"не отвечает: {type(exc).__name__}"


def _file_kind(name: str) -> str:
    """Картинку показываем, текст даём прочитать, остальное — только скачать."""
    ext = os.path.splitext(name)[1].lower()
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        return "image"
    if ext in (".txt", ".md", ".srt", ".json"):
        return "text"
    return "file"


# ────────────────────────── пресеты: копия и удаление ──────────────────────────

# Копирование и удаление — целые операции: пресет либо появился целиком, либо нет.
# Редактора полей здесь сознательно НЕТ. У пресета обложек их больше двадцати,
# и половинчатый редактор — это ровно та ложь интерфейса, которую мы убираем:
# поле показано, а движок его не читает. Правка полей остаётся в самом сервисе,
# ссылка на него рядом. Копия закрывает то, что нужно каждый день, — «такой же,
# но для другого канала».

@app.post("/ui/presets/{service}/copy")
def ui_preset_copy(service: str, body: dict, request: Request):
    """Сделать копию пресета под новым именем. Существующее имя не затирается."""
    src = str(body.get("from") or "").strip()
    dst = str(body.get("to") or "").strip()
    if not src or not dst:
        raise HTTPException(400, "нужны имя источника и имя копии")
    if src == dst:
        raise HTTPException(400, "имя копии совпадает с исходным")
    sid = getattr(request.state, "sid", "")
    store, put = _preset_store(service, sid, request)
    if src not in store:
        raise HTTPException(404, f"пресета «{src}» нет")
    if dst in store:
        raise HTTPException(409, f"пресет «{dst}» уже есть — выберите другое имя")
    store[dst] = _renamed(store[src], dst)
    put(store)
    return {"ok": True, "name": dst}


@app.delete("/ui/presets/{service}/{name}")
def ui_preset_delete(service: str, name: str, request: Request):
    """Удалить пресет. Если на него ссылается канал — сначала сказать, какой."""
    sid = getattr(request.state, "sid", "")
    c = db()
    try:
        column = {"vsgen": "preset_vsgen_id", "rewrite": "preset_rewrite",
                  "cover": "preset_cover"}.get(service)
        used = []
        if column:
            used = [r["name"] for r in c.execute(
                f"SELECT name FROM pub_channels WHERE tenant_id=? AND {column}=?",
                (TENANT, name))]
    finally:
        c.close()
    if used:
        raise HTTPException(409, "на него ссылаются каналы: " + ", ".join(used)
                            + ". Сначала переключите их — иначе они встанут в момент "
                              "генерации, а не сейчас.")
    store, put = _preset_store(service, sid, request)
    if name not in store:
        raise HTTPException(404, f"пресета «{name}» нет")
    store.pop(name)
    put(store)
    return {"ok": True}


def _renamed(preset, name: str):
    """Копия с новым именем. Имя лежит и внутри значения — иначе копия зовётся старым."""
    if isinstance(preset, dict):
        out = dict(preset)
        if "name" in out:
            out["name"] = name
        out.pop("id", None)          # id пересоздаст сам сервис: копия — не тот же пресет
        return out
    return preset


def _preset_store(service: str, sid: str, request):
    """(словарь пресетов, как его записать обратно) для одного из трёх сервисов.

    Запись у соседей идёт полной заменой — так же, как это делает их собственный
    интерфейс. Поэтому читаем весь набор, меняем и отдаём целиком: частичная запись
    поверх чужой правки потеряла бы её молча.
    """
    if service == "vsgen":
        c = db()
        try:
            rows = {r["name"]: {"name": r["name"], "settings": r["settings"],
                                "id": r["id"]}
                    for r in c.execute("SELECT id, name, settings FROM presets "
                                       "WHERE tenant_id=?", (TENANT,))}
        finally:
            c.close()

        def put(store):
            now = time.time()
            c2 = db()
            try:
                have = {r["name"]: r["id"] for r in c2.execute(
                    "SELECT id, name FROM presets WHERE tenant_id=?", (TENANT,))}
                for name, val in store.items():
                    if name not in have:
                        c2.execute("INSERT INTO presets(tenant_id,name,settings,created) "
                                   "VALUES(?,?,?,?)",
                                   (TENANT, name, (val or {}).get("settings") or "{}", now))
                for name, pid in have.items():
                    if name not in store:
                        c2.execute("DELETE FROM presets WHERE id=? AND tenant_id=?",
                                   (pid, TENANT))
                c2.commit()
            finally:
                c2.close()
        return rows, put

    if service == "cover":
        code, body = _cover("/presets", sid)
        if code >= 400 or not isinstance(body, dict):
            raise HTTPException(_pass_code(code), body or "список пресетов не получен")

        def put(store):
            code2, out = _cover("/presets/sync", sid, {"presets": store})
            if code2 >= 400:
                raise HTTPException(_pass_code(code2), out)
        return dict(body), put

    if service == "rewrite":
        code, body = _rw("/settings/keys", sid)
        if code >= 400 or not isinstance(body, dict):
            raise HTTPException(_pass_code(code), body or "настройки не получены")
        store = dict(body.get("rew_presets") or {})

        def put(s):
            code2, out = _rw("/settings/presets", sid, {"rew_presets": s})
            if code2 >= 400:
                raise HTTPException(_pass_code(code2), out)
        return store, put

    if service == "rewrite_gen":
        code, body = _rw("/presets", sid)
        if code >= 400 or not isinstance(body, dict):
            raise HTTPException(_pass_code(code), body or "брифы не получены")

        def put(s):
            code2, out = _rw("/presets/sync", sid, {"presets": s})
            if code2 >= 400:
                raise HTTPException(_pass_code(code2), out)
        return dict(body), put

    raise HTTPException(404, f"нет сервиса «{service}»")


# ─────────────────────────── каналы: завести и настроить ───────────────────────────

# Проверку полей и запись делает кабинет своими же функциями (`pub_ui.read_channel_form`,
# `create_channel`, `update_channel`): расписание, часовой пояс, горизонт и лимиты
# разбираются там по одним правилам для обоих интерфейсов. Вторая копия этих правил
# разошлась бы с первой на первом же новом поле.

@app.get("/ui/channels/new")
def ui_channel_new():
    """Из чего выбирать при заведении канала: пресеты, машины, профили, часовые пояса."""
    c = db()
    try:
        profiles = [{"id": r["id"], "title": r["title"] or r["serial"],
                     "serial": r["serial"]}
                    for r in c.execute("SELECT id, serial, title FROM pub_profiles "
                                       "WHERE tenant_id=? AND status='active' "
                                       "ORDER BY serial", (TENANT,))]
        hosts = [{"id": r["id"], "title": r["title"] or r["code"], "code": r["code"]}
                 for r in c.execute("SELECT id, code, title FROM pub_hosts "
                                    "WHERE tenant_id=? AND status='active' "
                                    "ORDER BY code", (TENANT,))]
        vsgen = [{"id": pid, "name": name} for pid, name in vsgen_presets(c, TENANT)]
        rew = rewrite_presets(TENANT)
        cov = cover_presets(TENANT)
    finally:
        c.close()
    return {
        "profiles": profiles, "hosts": hosts,
        "presets": {"vsgen": vsgen, "rewrite": rew, "cover": cov},
        "days": list(DAY_KEYS), "any_day": ANY_DAY,
        "statuses": [{"key": k, "title": v} for k, v in pub_ui.STATUS_TITLES.items()],
        "note": "остальное настраивается в карточке канала — она откроется сразу",
    }


class ChannelIn(BaseModel):
    uc: str = ""
    name: str = ""
    lang: str = ""
    niche: str = ""


@app.post("/ui/channels")
def ui_channel_create(body: ChannelIn):
    """Завести канал. Полей четыре — остальное в карточке, как и в кабинете."""
    c = db()
    try:
        cid = pub_ui.create_channel(c, TENANT, body.uc, body.name, body.lang, body.niche)
    except pub_ui.ChannelError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:                                     # noqa: BLE001
        if "UNIQUE" in str(exc):
            raise HTTPException(409, "такой канал уже заведён")
        raise
    finally:
        c.close()
    return {"ok": True, "id": cid}


@app.post("/ui/channels/{cid}")
def ui_channel_edit(cid: int, body: dict):
    """Сохранить настройки канала.

    Тело — те же имена полей, что у формы кабинета: их разбирает его же
    `read_channel_form`, вместе со всеми проверками и нормализацией расписания.
    """
    form = {k: (v if isinstance(v, list) else [v]) for k, v in (body or {}).items()}
    c = db()
    try:
        row = c.execute("SELECT id FROM pub_channels WHERE id=? AND tenant_id=?",
                        (cid, TENANT)).fetchone()
        if not row:
            raise HTTPException(404, "канал не найден")
        try:
            data = pub_ui.read_channel_form(form)
        except (pub_ui.ChannelError, pub_ui.ScheduleError) as exc:
            raise HTTPException(400, str(exc))
        pub_ui.update_channel(c, TENANT, cid, data)
        c.commit()
    finally:
        c.close()
    return {"ok": True}


@app.post("/ui/channels/{cid}/status")
def ui_channel_status(cid: int, body: dict):
    """Включить, поставить на паузу, убрать в архив."""
    c = db()
    try:
        try:
            pub_ui.set_channel_status(c, TENANT, cid, str((body or {}).get("status") or ""))
        except pub_ui.ChannelError as exc:
            raise HTTPException(400, str(exc))
        c.commit()
    finally:
        c.close()
    return {"ok": True}


# ─────────────────────────── свои ключи: добавить и убрать ───────────────────────────

class KeysIn(BaseModel):
    add: dict = {}
    drop: dict = {}


# ВАЖНО, и уже дважды укусило: обработчик, который ходит по HTTP в свой же процесс,
# обязан быть `def`, а не `async def`. FastAPI выполняет синхронный обработчик в
# отдельном потоке, и поток событий остаётся свободен обслужить внутренний запрос.
# Асинхронный делает блокирующий вызов прямо на потоке событий — запрос уходит,
# ответить некому, и всё висит до таймаута. Первый раз так завис вход, второй —
# сохранение ключей: запись при этом проходила, а ответ не возвращался.
@app.post("/ui/keys")
def ui_keys_save(body: KeysIn, request: Request):
    """Добавить или убрать свой ключ провайдера.

    Разбор формы делает кабинет своим же `/api/my/keys/web` — тем самым, что и его
    страница. Логика там нетривиальная: ключи правятся по одному, каждый несёт свой
    лимит, ёмкость дорожки пересчитывается сразу, а негодный codex-auth не должен
    стать credential'ом. Вторая её реализация здесь была бы вторым набором правил
    для тех же денег.

    Тело: {"add": {"googler": ["ключ", …]}, "drop": {"googler": [0, 2]}}.
    Индексы в `drop` — позиции в списке ключей провайдера, как их отдаёт `/ui/keys`.
    """
    fields = []
    for family, values in (body.add or {}).items():
        for v in (values if isinstance(values, list) else [values]):
            if str(v).strip():
                fields.append((f"add_{family}", str(v).strip()))
    for family, idx in (body.drop or {}).items():
        for i in (idx if isinstance(idx, list) else [idx]):
            fields.append((f"drop_{family}", str(int(i))))
    if not fields:
        raise HTTPException(400, "нечего сохранять: ни одного ключа не добавлено и не снято")

    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(CABINET_BASE + "/api/my/keys/web", data=data,
                                 method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Cookie", f"{auth.COOKIE}={getattr(request.state, 'sid', '')}")
    req.add_header("X-Requested-With", "fetch")
    try:
        with urllib.request.urlopen(req, timeout=NEIGHBOUR_TIMEOUT * 3) as resp:
            out = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raise HTTPException(_pass_code(exc.code),
                            exc.read().decode("utf-8", "replace")[:200])
    except Exception as exc:                                     # noqa: BLE001
        raise HTTPException(502, f"кабинет не отвечает: {type(exc).__name__}")
    if out.get("bad_codex_auth"):
        raise HTTPException(400, "путь к codex-auth не похож на файл авторизации — "
                                 "ключ не сохранён")
    if out.get("bad_prompt_channel"):
        raise HTTPException(400, "выбранный канал промптов недоступен — не сохранён")
    return {"ok": True}


@app.get("/ui/keys/families")
def ui_key_families():
    """Каким провайдерам можно задать свой ключ. Список — из кабинета, не переписан."""
    mod = sys.modules.get("main")
    families = getattr(mod, "_KEY_PROVIDERS", []) if mod else []
    return {"families": [{"key": k, "title": title, "group": group}
                         for k, title, group in families],
            "note": "значения ключей наружу не отдаются — ни здесь, ни на экране: "
                    "только количество и маска"}
