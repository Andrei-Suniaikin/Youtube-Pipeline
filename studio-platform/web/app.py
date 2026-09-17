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
import zoneinfo
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
from pydantic import BaseModel, StrictInt                         # noqa: E402

sys.path.insert(0, HERE)
import pult_auth as auth                                       # noqa: E402

from channel_slots import (ANY_DAY, DAY_KEYS, _localize, _zone,  # noqa: E402
                           normalize_schedule, upcoming_slots)
from pub_engine import PHASES, PHASE_DEADLINES                   # noqa: E402
from pub_engine import revive as pub_engine_revive                # noqa: E402
import preset_fields                                             # noqa: E402
import preset_links                                              # noqa: E402
from pub_engine import channel_presets                            # noqa: E402
from pub_remote import (COVER_BASE, REWRITE_BASE, donor_text,     # noqa: E402
                        get_key, video_id)
from pub_services import MEDIA_STAGES, OPTIONAL_STAGES           # noqa: E402
from pub_neighbors import (cover_presets, rewrite_presets,        # noqa: E402
                           vsgen_presets)
from pub_tags import add_links, parse_links, plan_rows           # noqa: E402
import pub_ui                                                    # noqa: E402

import pub_alerts                                                # noqa: E402
import pub_alerts_tg                                             # noqa: E402
import accounts                                                  # noqa: E402
import pub_calls                                                 # noqa: E402
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

# Виды задач по-русски. ТЗ §13: внутренние слова наружу не показываются.
KIND_TITLE = {
    "tts": "озвучка", "images": "кадры", "prompts": "промпты", "regen": "перегенерация",
    "video": "клипы", "regen_video": "перегенерация клипов", "render": "сборка",
    "chapters": "тайм-коды", "cards": "карточка", "pipeline": "весь конвейер",
}

# Состояния по-русски и одинаковые везде. «Готово частично» и «нужно действие» ТЗ
# требует отдельно: первое бывает у рерайта, второе — у выпуска в разборе.
STATE_TITLE = {
    "done": "готово", "running": "в работе", "queued": "в очереди",
    "partial": "готово частично", "error": "ошибка", "cancelled": "остановлено",
    "blocked": "нужно действие", "idle": "не начато", "off": "не участвует",
}

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
        raise HTTPException(503, "хранилище недоступно")
    c = sqlite3.connect(DB, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def _state(status) -> str:
    s = (status or "").lower()
    if s in TERMINAL_OK:
        return "done"
    if s == "partial":
        return "partial"        # рерайт умеет «сделал часть языков» — это не ошибка
    if s == "cancelled":
        return "cancelled"      # остановлено человеком — тоже не ошибка
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
    rows = list(c.execute(sql, args))
    bars = _stage_bars(c, rows)
    out = []
    for r in rows:
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
            # Полоска по этапам: семь отрезков вместо одной шкалы «сколько процентов».
            # Процент по выпуску — выдумка: этапы разной длины и часть из них
            # необязательна. Отрезок честнее: он говорит, что сделано, что идёт
            # и что ещё не начиналось.
            "bar": bars.get(r["release_id"], []),
            "video_id": r["video_id"] or "",
            "project_id": r["project_id"] or "",
            "idle_sec": max(0, int(now - (r["updated"] or now))),
        })
    return out


def _stage_bars(c, rows) -> dict:
    """Состояние семи этапов сразу для всех выпусков списка.

    Одним запросом на источник, а не по три на выпуск: пятьдесят выпусков в списке
    превратились бы в полтораста обращений к базе на каждое открытие экрана.
    """
    ids = [r["release_id"] for r in rows]
    projects = [r["project_id"] for r in rows if r["project_id"]]
    if not ids:
        return {}

    def _chunks(seq, size=400):
        for i in range(0, len(seq), size):
            yield seq[i:i + size]

    remote = {}          # release_id → {service: статус}
    for part in _chunks(ids):
        marks = ",".join("?" * len(part))
        for j in c.execute(
                f"SELECT release_id, service, kind, status FROM pub_release_jobs "
                f"WHERE release_id IN ({marks})", part):
            remote.setdefault(j["release_id"], {})[(j["service"], j["kind"])] = j["status"]

    queue = {}           # project_id → {kind: статус}
    for part in _chunks(projects):
        marks = ",".join("?" * len(part))
        for q in c.execute(
                f"SELECT project_id, kind, status FROM queue_tasks "
                f"WHERE project_id IN ({marks}) ORDER BY job_id", part):
            queue.setdefault(q["project_id"], {})[q["kind"]] = q["status"]

    upload = {}
    for part in _chunks(ids):
        marks = ",".join("?" * len(part))
        for u in c.execute(
                f"SELECT release_id, status FROM pub_upload_jobs "
                f"WHERE release_id IN ({marks})", part):
            upload[u["release_id"]] = u["status"]

    # Какой фазой закрывается каждый этап. Фаза авторитетнее записей задач: строку
    # задачи могли вычистить кнопкой «очистить очередь», а фаза остаётся. Без этого
    # у опубликованного выпуска вся полоска показывала «не начато» — при том, что
    # ролик вышел.
    stage_phase = {"script": "source_ready", "tts": "media_ready",
                   "images": "media_ready", "video": "media_ready",
                   "render": "media_ready", "cover": "cover_ready",
                   # Заливка закрыта, когда Studio подтвердила расписание. «published»
                   # тут не годится: это последняя фаза, и её никогда не «пройти».
                   "upload": "upload_running"}
    order = {p: i for i, p in enumerate(PHASES)}

    out = {}
    for r in rows:
        rid, pid = r["release_id"], r["project_id"]
        mine, qs = remote.get(rid, {}), queue.get(pid or "", {})
        here = order.get(r["phase"] or "created", 0)
        bar = []
        for key, title, source in STAGES:
            if source.startswith("remote:"):
                svc = source.split(":", 1)[1]
                got = next((v for (s_, _k), v in mine.items() if s_ == svc), None)
                state = _state(got) if got is not None else "idle"
            elif source.startswith("queue:"):
                kind = source.split(":", 1)[1]
                if kind not in MEDIA_STAGES:
                    state = "off"            # клипы автоконвейер не ставит
                else:
                    got = qs.get(kind) or mine.get(("vsgen", kind))
                    state = _state(got) if got is not None else "idle"
            else:
                state = _state(upload.get(rid)) if rid in upload else "idle"
            if state in ("idle", "queued") and here > order.get(stage_phase[key], 99):
                state = "done"      # фазу прошли — значит этап позади, чем бы его ни делали
            bar.append({"key": key, "title": title, "state": state})
        out[rid] = bar
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

    # Материалы проекта — по группам, теми же шаблонами, что и скачивание. ТЗ §14
    # требует на каждой стадии не строку статуса, а сам результат: миниатюры кадров,
    # плеер озвучки, готовые файлы сборки. Всё это уже посчитано для карточки проекта;
    # здесь оно раскладывается по стадиям.
    mod = sys.modules.get("main")
    pid = rel.get("project_id") if isinstance(rel, dict) else rel["project_id"]
    groups = {g["code"]: g for g in (_project_files(mod, pid) if pid else [])}
    ready = {}
    if pid and mod is not None and hasattr(mod, "_project_status"):
        try:
            ready = mod._project_status(TENANT, pid) or {}
        except Exception:                                          # noqa: BLE001
            ready = {}
    file_base = f"/p/{urllib.parse.quote(pid)}/file/" if pid else ""
    # Какой этап каким материалом доказывается. «Готово» без файла — не готово.
    STAGE_FILES = {"tts": ("audio",), "images": ("images",), "video": ("clips",),
                   "render": ("renders", "subs", "chapters")}

    out = []
    for key, title, source in STAGES:
        row = {"key": key, "title": title, "state": "idle", "facts": [], "note": "",
               "job": None, "files": [], "file_base": file_base}
        for code in STAGE_FILES.get(key, ()):
            g = groups.get(code)
            if g:
                row["files"].append({"code": code, "title": g["title"],
                                     "kind": g["kind"], "count": g["count"],
                                     "names": g["names"][:12]})
        # Правило 3 ТЗ: «готово» — когда результат создан и открывается. Файл на диске
        # доказывает этап надёжнее записи в очереди: запись могли вычистить кнопкой
        # «очистить очередь», а файл остался. Поэтому материал поднимает состояние,
        # но никогда не понижает — идущий этап остаётся идущим.
        row["_has_files"] = bool(row["files"])

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
            if kind == "images":
                # «Готово 38 из 60» — ровно та формулировка, которую просит ТЗ §14.
                got, want = ready.get("images") or 0, ready.get("images_expected")
                if got or want:
                    row["facts"].insert(0, f"готово {got}"
                                        + (f" из {want}" if want else ""))
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
        # Только из «не начато»: если задача стоит в очереди или идёт, файлы на диске
        # могли остаться от прошлого прогона, и назвать этап готовым было бы враньём
        # в обратную сторону. Поймано на фикстурах: озвучка в очереди показывалась
        # готовой из-за старого mp3.
        if row.pop("_has_files", False) and row["state"] == "idle":
            row["state"] = "done"
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
                # Пункт 6.1 ТЗ: канал можно завести без ID — списку нужно как-то
                # отличить этот случай, чтобы показать плашку «ID пустой».
                "has_uc": bool(ch["uc"]),
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
def ui_queue(service: str | None = None, channel: int | None = None,
             state: str | None = None, since: str | None = None):
    """Одна очередь на три сервиса, с фильтрами (ТЗ §13).

    Имя задачи — название ролика, а не идентификатор проекта: «proj-dev-0001» ничего
    не говорит человеку, который смотрит на очередь из сорока пяти каналов. Рядом
    контекст, который ТЗ требует поимённо: сервис, канал, язык, текущий этап.

    Сосед, который не отвечает, показывается отдельной строкой состояния, а не роняет
    экран и не притворяется пустой очередью: «нет задач» и «не смогли спросить» — разное.
    """
    c = db()
    try:
        # Название и канал берём одним запросом: очередь на сотню строк не должна
        # превращаться в сотню обращений за именем ролика.
        rows = []
        for q in c.execute(
                "SELECT q.job_id, q.kind, q.status, q.created, q.progress, q.stage, "
                "       q.provider, q.error, q.project_id, q.uid, "
                "       r.release_id, ch.id ch_id, ch.name ch_name, ch.lang lang, "
                "       p.donor_title, p.donor_url, u.name user_name "
                "FROM queue_tasks q "
                "LEFT JOIN pub_releases r ON r.project_id = q.project_id "
                "LEFT JOIN pub_channels ch ON ch.id = r.channel_id "
                "LEFT JOIN pub_plan_items p ON p.id = r.plan_item_id "
                "LEFT JOIN users u ON u.id = q.uid "
                "WHERE q.tenant_id=? ORDER BY q.job_id DESC LIMIT 100",
                (TENANT,)):
            rows.append({
                "service": "vsgen", "id": str(q["job_id"]),
                "title": (q["donor_title"] or q["donor_url"] or q["project_id"]
                          or str(q["job_id"])),
                "channel": q["ch_name"] or "", "channel_id": q["ch_id"],
                "lang": (q["lang"] or "").upper(),
                "release_id": q["release_id"] or "",
                "project_id": q["project_id"] or "",
                "user": q["user_name"] or "",
                "kind": q["kind"] or "",
                "kind_title": KIND_TITLE.get(q["kind"] or "", q["kind"] or ""),
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
        channels = [{"id": r["id"], "name": r["name"]} for r in c.execute(
            "SELECT id, name FROM pub_channels WHERE tenant_id=? ORDER BY name",
            (TENANT,))]
    finally:
        c.close()

    total = len(rows)
    if service:
        rows = [r for r in rows if r["service"] == service]
    if channel:
        rows = [r for r in rows if r.get("channel_id") == int(channel)]
    if state:
        rows = [r for r in rows if r["state"] == state]
    if since:
        try:
            edge = time.time() - {"1h": 3600, "24h": 86400, "7d": 7 * 86400}[since]
            rows = [r for r in rows if (r.get("created") or 0) >= edge]
        except KeyError:
            pass

    rows.sort(key=lambda r: (r["created"] or 0), reverse=True)
    return {
        "rows": rows, "services": services, "channels": channels,
        "total": total, "shown": len(rows),
        "states": [{"key": k, "title": v} for k, v in STATE_TITLE.items()],
        "periods": [{"key": "1h", "title": "за час"},
                    {"key": "24h", "title": "за сутки"},
                    {"key": "7d", "title": "за неделю"}],
        # Канал и язык есть только у задач, рождённых выпуском. Ручная задача их не
        # имеет, и это не пробел данных, а другое происхождение — так и показываем.
        "note": "канал и язык видны у задач, созданных выпуском",
    }


@app.get("/ui/keys")
def ui_keys():
    """Честная картина по ключам вместо «свободных слотов» (ТЗ §12).

    Что видно отсюда и откуда берётся:
      очередь и ожидание  — queue_tasks: длина по провайдеру и возраст самой старой
      отдыхающие ключи    — admin_state.credential_cooldown: до какого времени и почему
      свои ключи          — users.keys, только количество и маска, значения наружу не идут
      курсор раздачи      — provider_rr_cursor: кому выдали последним
      FastGen             — admin_state.fastgen_usage: снимок расхода и срока ключа
                            (пункт 8 ТЗ), нет снимка — «данных пока нет»

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
        busy = _pool_load()

        # Ресурс ключа FastGen (пункт 8 ТЗ, батч 16.09.2026): снимок кладёт
        # `main.save_fastgen_usage` — и из `fastgen_status`, и из `dev/alerts.py` перед
        # каждым тиком тревоги. Нет снимка — «данных пока нет», а не выдуманный ноль.
        fastgen = None
        fg_row = c.execute("SELECT v FROM admin_state WHERE k='fastgen_usage'").fetchone()
        if fg_row and fg_row["v"]:
            try:
                snap = json.loads(fg_row["v"])
            except ValueError:
                snap = None
            if isinstance(snap, dict):
                limits = snap.get("limits") if isinstance(snap.get("limits"), dict) else {}
                hourly = snap.get("hourly") if isinstance(snap.get("hourly"), dict) else {}
                threads = snap.get("threads") if isinstance(snap.get("threads"), dict) else {}
                expires = snap.get("expires")
                fastgen = {
                    "checked": snap.get("checked"),
                    "expires": expires,
                    # Оставшееся считаем здесь, а не в браузере: серверные часы —
                    # источник правды, как и у «отдыхающих ключей» выше (left_sec).
                    "expires_in_sec": (int(float(expires) - now) if expires else None),
                    "images_used": hourly.get("image_generation"),
                    "images_limit": limits.get("img_gen_per_hour_limit"),
                    "threads_used": threads.get("image_threads"),
                    "threads_limit": limits.get("img_generation_threads_allowed"),
                }
    finally:
        c.close()
    return {
        "providers": sorted(providers.values(), key=lambda p: -p["queued"]),
        "resting": resting,
        "own_keys": sorted(own, key=lambda o: (o["user"], o["family"])),
        "rr_cursor": cursor,
        "busy": busy,
        "fastgen": fastgen,
        "unavailable": [],
    }


def _pool_load() -> list:
    """Сколько потоков занято прямо сейчас и сколько всего можно.

    Это то самое, чего экран раньше не показывал: занятость жила в памяти сервиса.
    Теперь кабинет и пульт — один процесс, и число можно спросить у самого пула,
    а не выдумывать.
    """
    mod = sys.modules.get("main")
    if mod is None or not hasattr(mod, "SERVER_CREDENTIAL_POOL"):
        return []
    try:
        lanes = mod.SERVER_CREDENTIAL_POOL.lanes()
    except Exception:                                              # noqa: BLE001
        return []
    out = []
    for lane, loads in sorted(lanes.items()):
        provider = lane.split(":", 1)[-1]
        try:
            capacity = int(mod._provider_slot_capacity_units(provider))
        except Exception:                                          # noqa: BLE001
            capacity = 0
        used = sum(int(v or 0) for v in loads.values())
        total = capacity * max(1, len(loads))
        out.append({
            "lane": lane, "provider": provider,
            "own": lane.startswith("byok:"),
            "slots": len(loads), "used": used, "capacity": capacity,
            "total": total,
            "percent": (round(100 * used / total) if total else 0),
        })
    return out


# {channel_id:int} — не украшение: без указания типа этот путь перехватывает и
# Четыре стадии заливки, только вперёд. Начиная с третьей повторная заливка запрещена
# навсегда: файл уже ушёл в форму YouTube, и второй заход создаст дубль на канале
# (`upload_contract.PHASES`, `phase_requires_no_reupload`).
UPLOAD_PHASES = ("preflight", "profile_opened", "upload_started", "video_id_known")
UPLOAD_PHASE_TITLE = {
    "preflight": "проверка профиля и канала",
    "profile_opened": "браузер профиля запущен",
    "upload_started": "файл ушёл в форму",
    "video_id_known": "идентификатор ролика известен",
}
# Вердикт — не слово «успех», а состояние строки в YouTube Studio. Текст на странице
# врёт: 07.08 проверка по тексту объявила провальными тринадцать прошедших заливок.
UPLOAD_VERDICT_TITLE = {
    "scheduled": "запланирован", "published": "опубликован",
    "draft": "остался черновиком", "duplicate": "уже был залит",
    "failed": "не удалось", "already": "уже был вердикт",
}


def _uploads(c, channel_id: int) -> list:
    """Заливки канала: стадия, вердикт и доказательства.

    Кнопки «залить ещё раз» здесь нет и не будет. После стадии `upload_started` файл
    уже у YouTube, и единственное разрешённое действие — найти начатый ролик и довести
    его. Показываем состояние, а не предлагаем действие, которого делать нельзя.
    """
    rows = []
    try:
        got = c.execute(
            "SELECT u.release_id, u.status, u.phase, u.verdict, u.video_id, u.error, "
            "       u.visibility, u.schedule_iso, u.title, u.updated, p.donor_title "
            "FROM pub_upload_jobs u "
            "LEFT JOIN pub_releases r ON r.release_id = u.release_id "
            "LEFT JOIN pub_plan_items p ON p.id = r.plan_item_id "
            "WHERE u.channel_id=? ORDER BY u.slot_at DESC LIMIT 40",
            (channel_id,)).fetchall()
    except sqlite3.Error:
        return rows
    for r in got:
        phase = str(r["phase"] or "preflight")
        rank = UPLOAD_PHASES.index(phase) if phase in UPLOAD_PHASES else 0
        rows.append({
            "release_id": r["release_id"],
            "title": r["donor_title"] or r["title"] or r["release_id"],
            "status": r["status"] or "",
            "phase": phase,
            "phase_title": UPLOAD_PHASE_TITLE.get(phase, phase),
            "phase_rank": rank,
            "phases": [{"key": k, "title": UPLOAD_PHASE_TITLE[k], "done": i <= rank}
                       for i, k in enumerate(UPLOAD_PHASES)],
            # Признак из контракта, а не наша догадка: с этой стадии повтор запрещён.
            "no_reupload": rank >= UPLOAD_PHASES.index("upload_started"),
            "verdict": r["verdict"] or "",
            "verdict_title": UPLOAD_VERDICT_TITLE.get(r["verdict"] or "", r["verdict"] or ""),
            "video_id": r["video_id"] or "",
            "url": (f"https://youtu.be/{r['video_id']}" if r["video_id"] else ""),
            "visibility": r["visibility"] or "",
            "schedule_iso": r["schedule_iso"] or "",
            "error": r["error"] or "",
            "updated": r["updated"],
        })
    return rows


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
        _res_rew, _res_cov = channel_presets(c, ch["preset_vsgen_id"],
                                             ch["preset_rewrite"] or "",
                                             ch["preset_cover"] or "")
        counts = {}
        for row in cal:
            counts[row["state"]] = counts.get(row["state"], 0) + 1
        # Сводка по дням для календаря-сетки: сколько роликов вышло, сколько ещё в
        # работе и сколько пропущено. Считаем здесь, а не в браузере: часовой пояс
        # канала знает сервер, и «вышло 12 сентября» в Варшаве и в Минске — разные дни.
        by_day = {}
        tzname = ch["tz"] or "Europe/Warsaw"
        try:
            zone = zoneinfo.ZoneInfo(tzname)
        except Exception:                                          # noqa: BLE001
            zone = dt.timezone.utc
        for row in cal:
            day = dt.datetime.fromtimestamp(row["at"], zone).strftime("%Y-%m-%d")
            box = by_day.setdefault(day, {"done": 0, "running": 0, "missed": 0,
                                          "planned": 0, "free": 0, "blocked": 0})
            box[row["state"]] = box.get(row["state"], 0) + 1
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
                # Текст и обложка приходят из пресета производства — с 09.09 он
                # единственное место, где они задаются (`pub_engine.channel_presets`).
                # Сырые колонки канала ниже остаются для формы и для отката.
                "presets": {"rewrite": _res_rew, "vsgen": ch["preset_vsgen_id"],
                            "cover": _res_cov},
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
            "uploads": _uploads(c, channel_id),
            "by_day": by_day,
            "tz": tzname,
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
                      "how": "нужны токен бота и адрес чата — их выдаёт @BotFather; "
                             "порядок описан в инструкции к системе"},
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

    Ничего не удаляет и не может: удаление живёт в уборщике (`dev/retention.py`), а
    решение о сроке — в `.env` владельца. Из интерфейса чужие файлы не удаляются
    нажатием кнопки: слишком дёшево для необратимого действия.
    Здесь видно ровно то, на основании чего принимается решение, включая то, сколько
    оплаченных вызовов исчезнет вместе с файлами.
    """
    now = time.time()
    c = db()
    try:
        pol = pub_retention.policy(c)
        projects = pub_retention.scan(c, TENANTS_DIR, TENANT)
        acts = pub_retention.plan(projects, pol, now)
        last = pub_retention.last_sweep(c)
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
    c2 = db()
    try:
        nodes = pub_calls.node_disk(c2, now)
    finally:
        c2.close()
    return {
        "space": pub_retention.free_space(TENANTS_DIR),
        # Рендер и заливка идут на другой машине. Она чистит за собой после каждой
        # задачи, поэтому место там должно быть транзитным — и если кончается,
        # это симптом, а не переполнение.
        "nodes": nodes,
        "policy": pol,
        "policy_set": bool(pol),
        # Кто задал срок и убирает ли система сама. Это разные вопросы: срок может быть
        # задан, а уборка выключена — тогда экран показывает счёт, но ничего не тронуто.
        "source": pol.get("source", ""),
        "auto": pub_retention.auto_enabled(),
        "every_sec": pub_retention.every_sec(),
        "env": {"strip": pub_retention.ENV_STRIP, "drop": pub_retention.ENV_DROP,
                "auto": pub_retention.ENV_AUTO},
        "last": last,
        "projects": rows,
        "total": sum(r["total"] for r in rows),
        "freeable": freeable,
        "categories": [{"code": c_, "name": n, "fate": f}
                       for c_, n, f, _pat in pub_retention.CATEGORIES],
        "how": "сроки хранения задаёт владелец в .env: через сколько дней после "
               "публикации убирать промежуточные файлы и через сколько удалять проект "
               "целиком. Пока VS_RETENTION_AUTO не выставлен — только счёт, без уборки",
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
            # Кто чем пользуется — считаем ПОСЛЕ разрешения через пресет производства.
            # Иначе пресет обложки, заданный в пресете, выглядел бы никому не нужным,
            # а экран для того и сделан, чтобы находить ссылки в никуда.
            rew, cov = channel_presets(c, ch["preset_vsgen_id"],
                                       ch["preset_rewrite"] or "", ch["preset_cover"] or "")
            for kind, val in (("rewrite", rew),
                              ("vsgen", ch["preset_vsgen_id"]),
                              ("cover", cov)):
                if val in (None, ""):
                    continue
                used[kind].setdefault(str(val), []).append(
                    {"id": ch["id"], "name": ch["name"]})
        # Сводка — через preset_links.resolve (батч «Пресеты целые и частичные»):
        # список обязан показывать то, что реально поедет в производство, а не
        # пустые свои поля группы, у которой стоит ссылка на частичный пресет.
        # Экраны кабинета (presets_page/_preset_summary) при этом НЕ трогаем — они
        # уходят вместе с остальными экранами кабинета, а не остаются жить рядом с
        # пультом с двумя разными правдами о том, что показывает пресет.
        # N+1 сознательно (ревью цикла 1 фазы 2, Issue 5): `resolve()` (плюс запрос
        # на каждую ссылку) и `users_of()` (свой проход по каналам/проектам/держателям)
        # зовутся по разу на каждый пресет тенанта, а не одним общим проходом. На
        # dev-базе (7 пресетов, 5 проектов) не заметно; тенант здесь — один пульт на
        # команду, а не публичная витрина, и в продакшне ожидаются десятки пресетов,
        # не тысячи. Если счёт пойдёт на сотни — считать сводки одним проходом, но
        # через общий разбор ссылки из `preset_links` (тот же, что использует
        # `resolve`/`users_of`), а не второй самостоятельный разбор здесь.
        vsgen = []
        for r in c.execute("SELECT id, name, settings FROM presets "
                           "WHERE tenant_id=? ORDER BY name COLLATE NOCASE", (TENANT,)):
            resolved = preset_links.resolve(c, r["id"])
            users = preset_links.users_of(c, r["id"])
            vsgen.append({
                "key": str(r["id"]), "name": r["name"] or f"пресет {r['id']}",
                "facts": _preset_facts(resolved),
                "scope": preset_links.scope_of(resolved),
                "links": resolved.get("_links") or {},
                "users": {"channels": len(users["channels"]),
                         "projects": len(users["projects"]),
                         "presets": len(users["presets"])},
            })
        keys = {n: get_key(c, n) for n in ("rewrite", "cover")}
    finally:
        c.close()

    # Куда идти, чтобы завести пресет с нуля. Заводить его здесь нечем и не нужно:
    # пресет — это два-три десятка полей своего сервиса, и повторять их форму значит
    # заводить вторую правду о том, из чего состоит пресет. Но и тупик оставлять
    # нельзя: раньше экран говорил «правка — в разделе того сервиса» и не говорил,
    # где этот раздел.
    groups = [{"service": "vsgen", "title": "Производство (кабинет)", "items": vsgen,
               "error": "", "note": "ключ пресета — число, а не имя"}]

    body, err = _neighbour_get(REWRITE_BASE, "/api/v1/presets", keys["rewrite"])
    rew = (body or {}).get("rewrite") or {}
    groups.append({
        "service": "rewrite", "title": "Рерайт по ссылке", "error": err,
        "note": "канал ссылается именно на этот набор, а не на брифы генерации",
        "home": REWRITE_BASE, "home_title": "Rewrite Studio",
        "items": [{"key": name, "name": name, "facts": _preset_facts(cfg)}
                  for name, cfg in sorted(rew.items(), key=lambda kv: kv[0].lower())]})
    gen = (body or {}).get("generation") or {}
    if gen:
        groups.append({
            "service": "rewrite_gen", "title": "Брифы генерации с нуля", "error": "",
            "note": "к рерайту по ссылке отношения не имеют — отдельное хранилище",
            "home": REWRITE_BASE, "home_title": "Rewrite Studio",
            "items": [{"key": name, "name": name, "facts": _preset_facts(cfg)}
                      for name, cfg in sorted(gen.items(), key=lambda kv: kv[0].lower())]})

    body, err = _neighbour_get(COVER_BASE, "/presets", keys["cover"])
    cov = body if isinstance(body, dict) else {}
    groups.append({
        "service": "cover", "title": "Обложки", "error": err, "note": "",
        "home": COVER_BASE, "home_title": "Cover Studio",
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
        if low.startswith("_"):
            # Служебные ключи пресета (`_scope`, `_links`, `_link_problems`) — не то,
            # чем пресет узнают, а разметка «Пресетов целые и частичные». Та же
            # причина, по которой `_preset_project_payload` их выбрасывает при
            # переносе настроек в проект.
            continue
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
        "how": "нужен ключ YouTube Data API — без него подтвердить выход нечем",
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


def _cabinet_form(path: str, fields, request, timeout: float = 0.0,
                  expect_json: bool = True) -> dict:
    """POST формой в кабинет от имени вошедшего человека.

    Кабинет отвечает JSON только на пометку «это fetch» — иначе он шлёт редирект на
    свою собственную страницу, показывать которую пульту нечем. Отказ пересказываем
    как есть: «повтор доступен только для ошибочной задачи» — это ответ, а не сбой.

    `expect_json=False` — для ручек, которые про «это fetch» не знают и отвечают
    редиректом всегда (формы пресетов). Тогда нам важен только код ответа: прошло или
    нет. Разбирать их HTML мы не станем — это не контракт, а страница.
    """
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(CABINET_BASE + path, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Cookie", f"{auth.COOKIE}={getattr(request.state, 'sid', '')}")
    req.add_header("X-Requested-With", "fetch")
    try:
        with urllib.request.urlopen(req, timeout=timeout or NEIGHBOUR_TIMEOUT * 3) as resp:
            body = resp.read().decode("utf-8", "replace")
            if not expect_json:
                return {"ok": True}
            return json.loads(body or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = (json.loads(raw or "{}") or {}).get("detail") or raw[:200]
        except ValueError:
            detail = raw[:200]
        raise HTTPException(_pass_code(exc.code), detail or f"кабинет ответил {exc.code}")
    except Exception as exc:                                     # noqa: BLE001
        raise HTTPException(502, f"кабинет не отвечает: {type(exc).__name__}")


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
        "note": "пресеты и язык берутся из настроек канала",
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
        "note": "показаны ваши задачи и ваши результаты",
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
            "промпта пропускаются"},
    {"when": "not_url",
     "kills": ["reference_image", "simple_mode"],
     "why": "редактирование превью возможно только когда превью есть — то есть в режиме "
            "«по превью донора»"},
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
        "note": "показаны ваши задачи и ваши результаты",
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

class PresetNew(BaseModel):
    name: str = ""
    scope: str = "full"


# Часть → заголовок группы и обратно — раз навсегда, из того же источника, что и
# разрешение ссылок (`preset_fields.PARTS`), а не второй список здесь. Заголовки
# `GROUPS` совпадают с ними дословно (см. `preset_fields.py`).
_PART_TITLES = dict(preset_fields.PARTS)
_TITLE_TO_PART = {title: part for part, title in preset_fields.PARTS}
# Ключ поля формы → часть, которой он принадлежит. Только поля самой формы
# (`GROUPS`) — `PART_EXTRA_KEYS` в форму не попадают и отправить их с пульта
# нельзя (см. `known = preset_fields.keys()` в `ui_preset_save` ниже), поэтому
# для проверки «поле связанной группы отправлять нельзя» этой карты достаточно.
_KEY_TO_PART = {f.key: _TITLE_TO_PART.get(title)
                for title, fields in preset_fields.GROUPS for f in fields}


@app.post("/ui/presets/vsgen")
def ui_preset_create(body: PresetNew, request: Request):
    """Завести пресет производства — целый или частичный (батч «Пресеты целые и
    частичные»). Возвращает номер — чтобы сразу открыть редактор.
    """
    name = (body.name or "").strip()[:80]
    if not name:
        raise HTTPException(400, "у пресета должно быть имя")
    scope = (body.scope or preset_links.FULL).strip().lower() or preset_links.FULL
    if scope != preset_links.FULL and scope not in preset_links.PARTS:
        raise HTTPException(400, f"нет такой части: «{body.scope}»")
    c = db()
    try:
        exists = c.execute("SELECT 1 FROM presets WHERE tenant_id=? AND name=?",
                           (TENANT, name)).fetchone()
    finally:
        c.close()
    if exists:
        raise HTTPException(409, f"пресет «{name}» уже есть")
    # Заводит кабинет: у него и таблица, и умолчания, и нумерация. Здесь только форма.
    _cabinet_form("/api/presets/create", [("name", name)], request,
                  expect_json=False)
    c = db()
    try:
        r = c.execute("SELECT id FROM presets WHERE tenant_id=? AND name=? "
                      "ORDER BY id DESC LIMIT 1", (TENANT, name)).fetchone()
        if not r:
            raise HTTPException(502, "кабинет не завёл пресет")
        prid = r["id"]
    finally:
        c.close()
    if scope != preset_links.FULL:
        # Кабинет уже создал пресет отдельным HTTP-запросом выше — та транзакция
        # своя и уже закоммичена, откатить её отсюда нельзя (ревью цикла 1 фазы 2,
        # Issue 4: комментарий раньше обещал общий откат создания и разметки, но
        # его не было — оставался целый пресет без `_scope`, занявший имя навсегда).
        # Честная гарантия здесь другая: разметка `_scope` — либо она проходит целиком
        # в своей транзакции, либо на сбое явным DELETE убирается только что заведённый
        # пресет — так имя освобождается и попытку можно повторить, а не наткнуться
        # молча на «уже есть» для пресета без разметки.
        c = db()
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT settings FROM presets WHERE id=? AND tenant_id=?",
                            (prid, TENANT)).fetchone()
            try:
                settings = json.loads(row["settings"]) if row and row["settings"] else {}
            except (ValueError, TypeError):
                settings = {}
            if not isinstance(settings, dict):
                settings = {}
            settings["_scope"] = scope
            c.execute("UPDATE presets SET settings=? WHERE id=? AND tenant_id=?",
                      (json.dumps(settings, ensure_ascii=False), prid, TENANT))
            c.commit()
        except Exception:
            c.rollback()
            try:
                c.execute("DELETE FROM presets WHERE id=? AND tenant_id=?", (prid, TENANT))
                c.commit()
            except Exception:                                      # noqa: BLE001
                pass          # лучшая попытка — не заслонять исходную ошибку своей
            raise
        finally:
            c.close()
    return {"id": prid, "name": name, "scope": scope}


def _neighbour_preset_names() -> dict:
    """Имена пресетов соседей — для пятой группы общего пресета.

    Спрашиваем у соседей, а не читаем их хранилища: список принадлежит тому, кто им
    управляет. Сосед молчит — группа останется без вариантов, и это честнее выдуманного
    списка: пустой выбор видно, а выдуманный выглядит рабочим.
    """
    c = db()
    try:
        keys_ = {name: get_key(c, name) for name in ("rewrite", "cover")}
    finally:
        c.close()
    body, _err = _neighbour_get(REWRITE_BASE, "/api/v1/presets", keys_["rewrite"])
    rew = sorted((body or {}).get("rewrite") or {}) if isinstance(body, dict) else []
    body, _err = _neighbour_get(COVER_BASE, "/presets", keys_["cover"])
    cov = sorted(body or {}) if isinstance(body, dict) else []
    return {"preset_rewrite": rew, "preset_cover": cov}


def _locked_parts(resolved: dict) -> set:
    """Части целого пресета, у которых ссылка реально сработала — `resolve()`
    подставил в выдачу поля частичного пресета вместо своих. Только эти группы
    заблокированы для правки.

    Одно правило на форму (`source`/`readonly` в `ui_preset_get`) и на сохранение
    (какие поля отвергать в `ui_preset_save`) — а не два разных разбора одного и
    того же `_links` (ревью цикла 1 фазы 2, Issue 2). Сломанная ссылка
    (`_link_problems`) часть НЕ блокирует: по разделу 1 спецификации и примечанию
    оркестратора к Issue 2, `resolve()` в этом случае оставляет группу на своих
    полях — значит и здесь она обязана остаться редактируемой, иначе форма
    показывала бы «только чтение», а сохранение то же самое поле бы приняло
    (или наоборот).
    """
    if not isinstance(resolved, dict):
        return set()
    links = resolved.get("_links")
    if not isinstance(links, dict):
        return set()
    broken = {p.get("part") for p in (resolved.get("_link_problems") or [])}
    return {part for part in links if part in preset_links.PARTS and part not in broken}


def _partial_options_by_part(c) -> dict:
    """Частичные пресеты тенанта, сгруппированные по части — варианты для выбора
    ссылки в редакторе целого пресета.

    Только те, которые `preset_links.resolve()` реально примет: нужная часть и без
    собственных ссылок (правило 4 — ссылок на ссылки нет), проверено общей функцией
    `preset_links.partial_fits_part` (Issue 1 ревью цикла 2 фазы 2) — той же, что
    использует `resolve()`. Иначе список предлагал бы ссылку, которую разрешение
    потом откажется признать.
    """
    out = {part: [] for part in preset_links.PARTS}
    for row in c.execute("SELECT id, name, settings FROM presets WHERE tenant_id=? "
                         "ORDER BY name COLLATE NOCASE", (TENANT,)):
        try:
            s = json.loads(row["settings"]) if row["settings"] else {}
        except (ValueError, TypeError):
            continue
        if not isinstance(s, dict):
            continue
        scope = preset_links.scope_of(s)
        if scope not in out or not preset_links.partial_fits_part(s, scope):
            continue
        out[scope].append({"id": row["id"], "name": row["name"] or f"пресет {row['id']}"})
    return out


@app.get("/ui/presets/vsgen/{prid:int}")
def ui_preset_get(prid: int):
    """Поля пресета и их текущие значения.

    Список полей приходит из `server/preset_fields.py` — одного описания на кабинет и
    пульт. Своей копии формы здесь нет намеренно: разойдясь, две формы начнут показывать
    разные настройки одного и того же пресета.

    Значения — через `preset_links.resolve` (батч «Пресеты целые и частичные»): если у
    группы стоит ссылка на частичный пресет, форма обязана показать то, чем реально
    будет делаться ролик, а не пустые свои поля целого.

    Каждая группа дополнительно несёт `part` (часть, которую она закрывает), `source`
    (`own` или `link`) и явный признак `readonly` — оба вычислены одним правилом
    (`_locked_parts`, ревью цикла 1 фазы 2, Issue 2): группа только для чтения
    ровно тогда, когда `resolve()` реально подставил в неё частичный пресет.
    Сломанная ссылка — это НЕ `readonly`: `resolve()` в этом случае оставляет
    группу на своих полях, и сохранение (`ui_preset_save`) те же поля примет —
    форма и запись не должны расходиться в том, что можно поправить.
    `link_id`/`link_name` — на что стоит ссылка (по сырому `_links`, а не по тому,
    сработала ли она — это нужно, чтобы показать «на что указывает» даже у
    сломанной ссылки), `link_problem` — причина, если она сломана (`_link_problems`
    от `resolve()`), а рядом — `link_options`: частичные пресеты этой части, из
    которых можно выбрать. Свои поля целого при этом никуда не делись: они
    остаются в самих настройках пресета, `resolve()` их не стирает, только
    подменяет в выдаче — снятие ссылки их и вернёт.
    """
    c = db()
    try:
        r = c.execute("SELECT id, name FROM presets WHERE id=? AND tenant_id=?",
                      (prid, TENANT)).fetchone()
        if not r:
            raise HTTPException(404, "нет такого пресета")
        resolved = preset_links.resolve(c, prid)
        users = preset_links.users_of(c, prid)
        links = resolved.get("_links") or {}
        problems = {p.get("part"): p.get("why") for p in resolved.get("_link_problems") or []}
        link_ids = sorted({lid for lid in (preset_links.parse_link_ref(links.get(p))
                                           for p in preset_links.PARTS) if lid is not None})
        names_by_id = {}
        if link_ids:
            placeholders = ",".join("?" for _ in link_ids)
            for row in c.execute(
                    f"SELECT id, name FROM presets WHERE tenant_id=? AND id IN ({placeholders})",
                    (TENANT, *link_ids)):
                names_by_id[row["id"]] = row["name"] or f"пресет {row['id']}"
        options_by_part = _partial_options_by_part(c)
    finally:
        c.close()
    mod = sys.modules.get("main")
    groups = preset_fields.describe(mod, resolved, _neighbour_preset_names())
    locked_parts = _locked_parts(resolved)
    for g in groups:
        part = _TITLE_TO_PART.get(g["title"])
        g["part"] = part
        if part is None:
            continue
        lid = preset_links.parse_link_ref(links.get(part))
        g["source"] = "link" if part in locked_parts else "own"
        g["readonly"] = part in locked_parts
        g["link_id"] = lid
        g["link_name"] = names_by_id.get(lid) if lid is not None else None
        g["link_problem"] = problems.get(part, "")
        g["link_options"] = options_by_part.get(part, [])
    return {"id": r["id"], "name": r["name"] or f"пресет {r['id']}",
            "scope": preset_links.scope_of(resolved),
            "links": links, "link_problems": resolved.get("_link_problems") or [],
            "users": users,
            "groups": groups,
            "note": ("Пустое поле означает «как по умолчанию». Проверку значений делает "
                     "кабинет при сохранении — теми же контрактами, что и всегда. "
                     "Уже поставленные задачи правка не меняет — их настройки заморожены "
                     "в момент запуска. Изменится со следующего запуска.")}


class PresetSave(BaseModel):
    name: str = ""
    values: dict = {}


@app.post("/ui/presets/vsgen/{prid:int}")
def ui_preset_save(prid: int, body: PresetSave, request: Request):
    """Сохранить пресет.

    Уходит в кабинет его же формой: там значения проходят контракты
    (`_canonical_image_params`, `_normalize_lumean_settings`) и там же делается merge —
    ключи, которых в этой форме нет, не затираются. Повторять проверки здесь значило бы
    завести вторую, отстающую версию правил.

    Поля связанной группы (та, у которой ссылка реально сработала) отправлять нельзя:
    они не этого пресета, а частичного, на который он ссылается — правка тут ничего бы
    не изменила по-настоящему, только создала бы у человека иллюзию, что изменила.
    Заблокированные части — те же самые, что `ui_preset_get` показывает `readonly`
    (`_locked_parts` — общее правило, ревью цикла 1 фазы 2, Issue 2): если у ссылки
    есть проблема (`_link_problems`), группа не блокируется — `resolve()` в этом
    случае и так оставляет её на своих полях, значит их можно и сохранить.
    """
    known = preset_fields.keys()
    unknown = sorted(set(body.values) - known)
    if unknown:
        raise HTTPException(400, "неизвестные поля: " + ", ".join(unknown))
    c = db()
    try:
        row = c.execute("SELECT id FROM presets WHERE id=? AND tenant_id=?",
                        (prid, TENANT)).fetchone()
        if not row:
            raise HTTPException(404, "нет такого пресета")
        resolved = preset_links.resolve(c, prid)
    finally:
        c.close()
    locked_parts = _locked_parts(resolved)
    locked = sorted({k for k in body.values if _KEY_TO_PART.get(k) in locked_parts})
    if locked:
        raise HTTPException(
            400, "поля связанной группы не отправляются: " + ", ".join(locked))
    fields = [("name", (body.name or "").strip()[:80])]
    fields += [(k, "" if v is None else str(v)) for k, v in body.values.items()]
    _cabinet_form(f"/api/presets/{prid}/edit", fields, request,
                  expect_json=False)
    return {"ok": True}


class PresetLinkIn(BaseModel):
    part: str = ""
    # StrictInt, а не int (ревью цикла 1 фазы 2, Issue 1): у pydantic 2.13 обычный
    # `int` в нестрогом режиме молча приводит к числу и строку из цифр ("12"), и
    # дробное без остатка (12.0), и даже bool (True → 1). Решение владельца — «при
    # записи приводить к int, строку отвергать» (ревью цикла 2 фазы 1, п.5) — именно
    # запрет, а не тихое приведение. StrictInt отвергает всё перечисленное 422-м ещё
    # до входа в обработчик, настоящее целое JSON-число проходит как есть.
    #
    # Без значения по умолчанию (Issue 2 ревью цикла 2 фазы 2): контракт ручки —
    # `{part, preset_id | null}`, снятие ссылки задаётся ЯВНЫМ `null`, а не
    # отсутствием поля. С умолчанием `= None` тело `{"part": "images"}` без
    # `preset_id` молча читалось бы как «снять ссылку» и отвечало 200 — непроверенный
    # ввод на границе. Без умолчания то же тело даёт 422 ещё до обработчика, а
    # явный `{"part": "images", "preset_id": null}` по-прежнему снимает ссылку.
    preset_id: StrictInt | None


@app.post("/ui/presets/vsgen/{prid:int}/links")
def ui_preset_link_set(prid: int, body: PresetLinkIn):
    """Поставить или снять ссылку одной группы целого пресета на частичный.

    `preset_id=null` — снять: своя группа уже лежит в настройках нетронутой
    (`resolve()` её не стирает, только подменяет в выдаче), достаточно убрать запись
    из `_links`. Ссылка хранится только числом (решения ревью цикла 2 фазы 1): тело —
    pydantic-модель с `preset_id: StrictInt | None`, и строка (в том числе из цифр),
    дробное и `bool` отвергаются ещё до входа в обработчик, а не приводятся к числу
    молча (ревью цикла 1 фазы 2, Issue 1 — обычный `int` у pydantic это делал).

    Проверки — те же, что и у `resolve()`, потому что ссылку, которую он не примет,
    ставить незачем: пресет обязан быть целым, частичный — существовать, быть того же
    тенанта, нужной части и без собственных ссылок (правило 4).
    """
    part = (body.part or "").strip().lower()
    if part not in preset_links.PARTS:
        raise HTTPException(400, f"нет такой части: «{body.part}»")
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT settings FROM presets WHERE id=? AND tenant_id=?",
                        (prid, TENANT)).fetchone()
        if not row:
            raise HTTPException(404, "нет такого пресета")
        try:
            settings = json.loads(row["settings"]) if row["settings"] else {}
        except (ValueError, TypeError):
            settings = {}
        if not isinstance(settings, dict):
            settings = {}
        if preset_links.scope_of(settings) != preset_links.FULL:
            raise HTTPException(400, "ссылки ставятся только у целого пресета")
        # `dict(...)` на списке или строке роняет `TypeError`/`ValueError` (ревью
        # цикла 1 фазы 2, Issue 3): испорченный `_links` (объект/список/мусор,
        # решения после ревью цикла 3 фазы 1 называют это сломанной ссылкой, а не
        # поводом падать) начинаем как пустой набор — снять или переставить ссылку
        # такого пресета через API можно, а не только правкой базы напрямую.
        links_raw = settings.get("_links")
        links = dict(links_raw) if isinstance(links_raw, dict) else {}
        if body.preset_id is None:
            links.pop(part, None)
        else:
            partial_row = c.execute(
                "SELECT settings FROM presets WHERE id=? AND tenant_id=?",
                (body.preset_id, TENANT)).fetchone()
            if not partial_row:
                raise HTTPException(404, "частичный пресет не найден")
            try:
                partial = json.loads(partial_row["settings"]) if partial_row["settings"] else {}
            except (ValueError, TypeError):
                partial = {}
            if not isinstance(partial, dict):
                partial = {}
            if preset_links.scope_of(partial) != part:
                raise HTTPException(400, "частичный пресет не той части")
            # Общая функция вместо своей проверки own_links (Issue 1 ревью цикла 2
            # фазы 2): часть уже совпала веткой выше, значит `partial_fits_part`
            # здесь проверяет ровно «нет своих ссылок» — тем же кодом, что и `resolve()`.
            if not preset_links.partial_fits_part(partial, part):
                raise HTTPException(
                    400, "у частичного пресета есть свои ссылки — так нельзя")
            links[part] = int(body.preset_id)
        settings["_links"] = links
        c.execute("UPDATE presets SET settings=? WHERE id=? AND tenant_id=?",
                  (json.dumps(settings, ensure_ascii=False), prid, TENANT))
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
    return {"ok": True}


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


# {cid:int} — иначе этот путь перехватывает «/ui/channels/import».
@app.post("/ui/channels/{cid:int}")
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


@app.post("/ui/channels/{cid:int}/status")
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


class ChannelUCIn(BaseModel):
    uc: str = ""


@app.post("/ui/channels/{cid:int}/uc")
def ui_channel_set_uc(cid: int, body: ChannelUCIn):
    """Вписать ID YouTube каналу, у которого его пока нет (дополнение 16.09, вопрос 1).

    Отдельный роут, а не поле в `/ui/channels/{cid}` (обычные настройки): у ID своё
    правило («вписать можно только в пустое») и своя ошибка, которые не должны
    путаться с проверками остальной формы. Эту возможность знает только пульт —
    кабинет её не получает по прямому указанию владельца.
    """
    c = db()
    try:
        try:
            pub_ui.set_channel_uc(c, TENANT, cid, body.uc)
        except pub_ui.ChannelError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:                                     # noqa: BLE001
            if "UNIQUE" in str(exc):
                raise HTTPException(409, "такой ID уже используется другим каналом")
            raise
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
            "note": "значение ключа нигде не показывается — только его хвост"}


class ImportIn(BaseModel):
    payload: str = ""


@app.post("/ui/channels/import")
def ui_channels_import(body: ImportIn):
    """Перенести каналы списком: JSON-массив или CSV с заголовком.

    Разбор и запись — кабинета (`pub_ui.import_channels`). Главное правило переноса,
    «пустая колонка ничего не затирает», живёт в `read_import_row` и должно остаться
    единственным: иначе один интерфейс стирал бы то, что бережёт другой.

    Отклонённые строки возвращаются поимённо с причиной. Молчаливый импорт, который
    «прошёл», а половину строк выбросил, — худший исход, чем отказ.
    """
    c = db()
    try:
        out = pub_ui.import_channels(c, TENANT, body.payload or "")
    except pub_ui.ChannelError as exc:
        raise HTTPException(400, str(exc))
    finally:
        c.close()
    return out


# ──────────────────────────────── проекты ────────────────────────────────

# Проект — то, что делает выпуск: папка с кадрами, озвучкой, рендером и субтитрами.
# В целевой навигации ТЗ отдельного раздела «Проекты» нет, и заводить их руками пульт
# не даёт: их создаёт выпуск. Но посмотреть, что получилось, и забрать — нужно каждый
# день, и ради этого не должно приходиться уходить в кабинет.
#
# Счётчики и список файлов считает кабинет своими же функциями (`_project_status`,
# `_dl_items`): они знают, где что лежит, включая исторические раскладки папок.

@app.get("/ui/projects")
def ui_projects(limit: int = 50):
    """Проекты и то, насколько каждый готов."""
    mod = sys.modules.get("main")
    c = db()
    try:
        rows = []
        for p in c.execute("SELECT id, name, status, created, updated FROM projects "
                           "WHERE tenant_id=? ORDER BY updated DESC LIMIT ?",
                           (TENANT, max(1, min(200, limit)))):
            rel = c.execute("SELECT release_id, phase FROM pub_releases "
                            "WHERE project_id=? ORDER BY updated DESC LIMIT 1",
                            (p["id"],)).fetchone()
            busy = c.execute("SELECT COUNT(*) FROM queue_tasks WHERE project_id=? "
                             "AND status IN ('queued','running')", (p["id"],)).fetchone()[0]
            st = {}
            if mod is not None and hasattr(mod, "_project_status"):
                try:
                    st = mod._project_status(TENANT, p["id"]) or {}
                except Exception:                                  # noqa: BLE001
                    st = {}
            rows.append({
                "id": p["id"], "name": p["name"] or p["id"], "status": p["status"] or "",
                "updated": p["updated"], "busy": busy,
                "release_id": (rel["release_id"] if rel else ""),
                "phase": (rel["phase"] if rel else ""),
                "ready": {"text": bool(st.get("text")), "audio": bool(st.get("audio")),
                          "images": st.get("images") or 0,
                          "images_expected": st.get("images_expected"),
                          "clips": st.get("clips") or 0,
                          "render": bool(st.get("render"))},
            })
    finally:
        c.close()
    return {"projects": rows,
            "note": "материалы роликов: кадры, озвучка, готовые файлы"}


@app.get("/ui/projects/{pid}")
def ui_project(pid: str):
    """Один проект: что в нём есть, чем он занят и что можно забрать."""
    mod = sys.modules.get("main")
    c = db()
    try:
        p = c.execute("SELECT * FROM projects WHERE id=? AND tenant_id=?",
                      (pid, TENANT)).fetchone()
        if not p:
            raise HTTPException(404, "проект не найден")
        rel = c.execute("SELECT release_id, phase, status FROM pub_releases "
                        "WHERE project_id=? ORDER BY updated DESC LIMIT 1",
                        (pid,)).fetchone()
        jobs = [{"id": q["job_id"], "kind": q["kind"], "status": q["status"],
                 "state": _state(q["status"]),
                 "progress": (float(q["progress"]) if q["progress"] is not None else None),
                 "stage": q["stage"] or "", "error": q["error"] or "",
                 "created": q["created"],
                 # Правило 6 ТЗ: настройки замораживаются в момент запуска, и в
                 # интерфейсе должно быть видно, КАКИЕ применились. Снимок лежит
                 # в queue_tasks.params с постановки; правка пресета его не меняет,
                 # и именно поэтому показывать надо его, а не текущий пресет.
                 "applied": _applied_params(q["params"])}
                for q in c.execute(
                    "SELECT job_id, kind, status, progress, stage, error, created, params "
                    "FROM queue_tasks WHERE project_id=? ORDER BY job_id DESC LIMIT 30",
                    (pid,))]
        paid = [{"kind": r["kind"], "delivered": r["n"], "cost": r["s"] or 0.0}
                for r in c.execute(
                    "SELECT kind, COUNT(*) n, SUM(COALESCE(cost,0)) s FROM provider_calls "
                    "WHERE project_id=? AND status='delivered' GROUP BY kind", (pid,))]
    finally:
        c.close()

    st = {}
    if mod is not None and hasattr(mod, "_project_status"):
        try:
            st = mod._project_status(TENANT, pid) or {}
        except Exception:                                          # noqa: BLE001
            st = {}
    groups = _project_files(mod, pid)
    return {
        "project": {"id": p["id"], "name": p["name"] or p["id"],
                    "status": p["status"] or "", "created": p["created"],
                    "updated": p["updated"],
                    "release_id": (rel["release_id"] if rel else ""),
                    "phase": (rel["phase"] if rel else "")},
        "ready": st, "jobs": jobs, "files": groups, "paid": paid,
        # Ссылки на файлы и архивы — кабинета: он отдаёт их с проверкой, что путь
        # не выводит за пределы проекта, и с миниатюрами по ?w=.
        "file_base": f"/p/{urllib.parse.quote(pid)}/file/",
        "zip_base": f"/api/projects/{urllib.parse.quote(pid)}/download/",
    }


# Служебные ключи снимка: они про механику постановки, а не про то, чем сделан ролик.
# Показывать их — значит утопить настройки в шуме.
_PARAM_SKIP = ("token", "key", "secret", "claim", "_bundle", "callback", "deferred")


def _applied_params(raw) -> list:
    """Настройки, с которыми задача РЕАЛЬНО пошла в работу.

    Не текущий пресет: он мог измениться после постановки, и показать его значило бы
    соврать про то, что применилось. Правило 6 ТЗ именно об этом.
    """
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    out = []
    for k, v in sorted(data.items()):
        low = str(k).lower()
        if any(mark in low for mark in _PARAM_SKIP):
            continue
        if isinstance(v, (dict, list)):
            v = f"{len(v)} шт."
        if v in (None, "", False):
            continue
        out.append({"name": k, "value": str(v)[:120]})
    return out


_FILE_GROUPS = (("images", "кадры"), ("clips", "клипы"), ("renders", "готовый ролик"),
                ("audio", "озвучка"), ("subs", "субтитры"), ("chapters", "тайм-коды"),
                ("scenes", "план сцен и промпты"))


def _project_files(mod, pid: str) -> list:
    """Файлы проекта по группам — теми же шаблонами, что и скачивание в кабинете.

    Свой список шаблонов здесь завёлся бы отдельной правдой: экран показывал бы одно,
    а архив отдавал другое. Уже случалось — субтитры и тайм-коды лежали на диске,
    а кабинет их не отдавал, потому что их не было в его списке категорий.
    """
    if mod is None or not hasattr(mod, "_dl_items"):
        return []
    pdir = mod._project_dir(TENANT, pid)
    out = []
    for code, title in _FILE_GROUPS:
        try:
            items = mod._dl_items(pdir, code)
        except Exception:                                          # noqa: BLE001
            items = []
        if not items:
            continue
        out.append({"code": code, "title": title, "count": len(items),
                    "kind": ("image" if code == "images" else
                             "video" if code in ("clips", "renders") else
                             "audio" if code == "audio" else "text"),
                    # Имена относительные — их же ждёт отдача файла кабинетом.
                    "names": [rel for _full, rel in items[:60]],
                    "more": max(0, len(items) - 60)})
    return out


class ProjectNew(BaseModel):
    name: str = ""
    text: str = ""


@app.post("/ui/projects")
def ui_project_new(body: ProjectNew, request: Request):
    """Завести проект из вставленного текста.

    Проект заводит сам кабинет (`_create_project`) — своей таблицы проектов у пульта
    нет, и второй способ их создавать означал бы вторую правду о том, что такое проект.
    Поэтому не HTTP-прокси, а прямой вызов: он возвращает номер нового проекта, а
    форма кабинета отвечает редиректом на свою страницу и номер теряет.

    Название и текст здесь разные поля. В кабинете первая строка блока служит и тем и
    другим — название потом читается вслух вместе с текстом. Повторять это не за чем.
    """
    mod = sys.modules.get("main")
    if mod is None or not hasattr(mod, "_create_project"):
        raise HTTPException(503, "кабинет не поднят — заводить проект нечем")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "пустой текст: нечего озвучивать и не из чего делать кадры")
    name = (body.name or "").strip() or text.splitlines()[0].strip()[:80] or "Без названия"
    pid = mod._create_project(TENANT, name, text)
    return {"id": pid, "name": name}


# Поля запуска. Пустое значение = «как в проекте»: кабинет кладёт настройки проекта
# базой и накрывает только непустыми полями формы (`batch_run`). Список намеренно
# короткий — это то, что человек решает перед конкретным прогоном, а не весь пресет.
class RunIn(BaseModel):
    sentences_per_prompt: str = ""   # сколько предложений на один кадр
    model: str = ""                  # модель картинок
    engine: str = ""                 # движок озвучки
    quality: str = ""                # качество сборки
    transitions: str = ""            # переходы между кадрами
    sync_audio: str = ""             # подгонять кадры под озвучку
    subtitles: str = ""              # вшивать субтитры


@app.post("/ui/projects/{pid}/run")
def ui_project_run(pid: str, body: RunIn, request: Request):
    """Сделать всё: озвучка, кадры и сборка одной командой.

    Здесь именно прокси, а не прямой вызов: постановка в очередь проверяет ключи,
    лимиты и уже идущие задачи того же вида, и повторять эти проверки на своей
    стороне значит однажды разойтись с кабинетом и поставить оплачиваемую задачу
    дважды.

    Сборку берёт отдельная машина: планировщик кабинета выдаёт себе всё, кроме
    `render`. Без рендер-ноды проект дойдёт до сборки и остановится — молча, поэтому
    об этом сказано на экране.
    """
    fields = [("pid", pid)]
    fields += [(k, v.strip()) for k, v in body.model_dump().items() if str(v).strip()]
    return _cabinet_form("/api/batch/pipeline", fields, request)


@app.get("/ui/limits")
def ui_limits(request: Request):
    """Лимиты серверных ключей: сколько задач и потоков разрешено провайдеру.

    Только админу — это настройка, тратящая ключи владельца. Проверяем на сервере,
    а не прятанием кнопки: запрос может прислать кто угодно.
    """
    _require_admin(request)
    mod = sys.modules.get("main")
    if mod is None or not hasattr(mod, "_concurrency_cfg"):
        raise HTTPException(503, "кабинет не поднят — лимиты читать неоткуда")
    try:
        cfg = mod._concurrency_cfg() or {}
    except Exception as exc:                                       # noqa: BLE001
        raise HTTPException(502, f"лимиты не прочитались: {type(exc).__name__}")
    bounds = getattr(mod, "_SERVER_LIMIT_BOUNDS", {})
    rows = []
    for provider in sorted(cfg):
        block = cfg[provider] if isinstance(cfg[provider], dict) else {}
        rows.append({"provider": provider,
                     "values": {k: block.get(k) for k in sorted(block)}})
    return {"rows": rows,
            "bounds": {k: list(v) for k, v in bounds.items()},
            "note": ("Пишется в блок «_concurrency» файла ключей — с бэкапом и "
                     "атомарной заменой, применяется без рестарта. Выше кода-потолка "
                     "значение не поднимется: его обрезают на чтении.")}


@app.get("/ui/projects/{pid}/settings")
def ui_project_settings(pid: str):
    """Настройки одного проекта — тем же описанием полей, что и пресет.

    Это и есть один и тот же набор: пресет — заготовка настроек, проект — их копия,
    сделанная в момент заведения. Две разные формы для одного набора разошлись бы в
    первый же месяц, поэтому форма ровно одна (`server/preset_fields.py`).
    """
    c = db()
    try:
        r = c.execute("SELECT id, name, settings FROM projects WHERE id=? AND tenant_id=?",
                      (pid, TENANT)).fetchone()
    finally:
        c.close()
    if not r:
        raise HTTPException(404, "проект не найден")
    try:
        settings = json.loads(r["settings"] or "{}")
    except ValueError:
        settings = {}
    mod = sys.modules.get("main")
    # Пятая группа («Текст и обложка») в проект не идёт: какими пресетами Rewrite и
    # Cover делают текст и обложку, решает КАНАЛ, а не отдельный проект. Показать здесь
    # эти поля значило бы предложить настройку, которую никто не прочитает.
    groups = [g for g in preset_fields.describe(mod, settings)
              if g["title"] != "Текст и обложка"]
    return {"id": r["id"], "name": r["name"] or r["id"],
            "groups": groups,
            "note": ("Пустое поле означает «как по умолчанию». Настройки уже стоящих "
                     "в очереди задач эта правка не меняет — они заморожены в момент "
                     "запуска (правило 6 ТЗ).")}


@app.post("/ui/projects/{pid}/settings")
def ui_project_settings_save(pid: str, body: PresetSave, request: Request):
    """Сохранить настройки проекта.

    Уходит в кабинет его же ручкой: там `_sanitize_task_params`, контракт режима
    картинок и правила Lumean (голос и шаблон должны принадлежать одному сервису —
    предохранитель, поставленный в production 02–03.09 перед оплатой заказа).
    """
    known = preset_fields.keys()
    unknown = sorted(set(body.values) - known)
    if unknown:
        raise HTTPException(400, "неизвестные поля: " + ", ".join(unknown))
    fields = [(k, "" if v is None else str(v)) for k, v in body.values.items()]
    if not fields:
        raise HTTPException(400, "нечего сохранять")
    _cabinet_form(f"/api/projects/{pid}/settings", fields, request, expect_json=False)
    return {"ok": True}


@app.post("/ui/releases/{release_id}/revive")
def ui_release_revive(release_id: str):
    """Вернуть выпуск из разбора в работу.

    Карантин был односторонней дверью: движок помечал выпуск «сбой» и больше его не
    трогал, а вернуть в работу было нечем. Здесь эта дверь открывается — но не насквозь:
    фаза не откатывается (сделанное и оплаченное остаётся сделанным), а из необратимой
    фазы возврата нет вовсе, потому что повтор заливки запрещён навсегда.
    """
    c = db()
    try:
        out = pub_engine_revive(c, release_id)
        if not out["ok"]:
            raise HTTPException(409, out["why"])
        c.commit()
    finally:
        c.close()
    return out


# ─────────────────────── перегенерация одного кадра или клипа ───────────────────────

# ТЗ §14: «повторить один кадр», «повторить выбранные клипы». Повтор этапа целиком
# переделывает всё — и то, что получилось. Здесь пересоздаётся ровно один файл.
#
# Пульт этого не делает: у кабинета есть свои роуты со всеми проверками — путь внутри
# проекта, доступность провайдера, контракт референсов, запрет второй правки картинок
# одновременно. Мы их пересылаем кукой человека, как отмену и повтор задач.
REGEN = {
    "image": ("/api/projects/{pid}/images/regen", "image_rel"),
    "clip": ("/api/projects/{pid}/videos/regen", "video_rel"),
}


class RegenIn(BaseModel):
    project_id: str
    name: str                 # путь файла внутри проекта, как его отдаёт список
    prompt: str = ""          # свой промпт — необязательно


@app.post("/ui/regen/{what}")
def ui_regen(what: str, body: RegenIn, request: Request):
    """Переделать один кадр или один клип."""
    plan = REGEN.get(what)
    if not plan:
        raise HTTPException(404, f"нечего переделывать: «{what}»")
    path, field = plan
    if not body.name.strip():
        raise HTTPException(400, "не указан файл")

    fields = [(field, body.name.strip())]
    if body.prompt.strip():
        fields.append(("prompt", body.prompt.strip()))
    return _cabinet_form(path.format(pid=urllib.parse.quote(body.project_id)),
                         fields, request)


# ─────────────────────────── учётные записи ───────────────────────────

# Заводить людей раньше можно было только из командной строки (`create_partner.py`)
# либо через админ-панель кабинета по статичному токену в теле формы. Ни то ни другое
# не годится для ежедневной работы: первое требует доступа к серверу, второе — знания,
# что такой токен вообще есть.
#
# Сам список и правила живут в `server/accounts.py` — одном месте на кабинет и пульт.
# Хэширование пароля берём у кабинета: второго алгоритма в системе быть не должно.

def _require_admin(request) -> dict:
    """Управление людьми — только администратору.

    Проверяем на сервере, а не прятанием кнопки: запрос может прислать кто угодно.
    """
    user = getattr(request.state, "user", None)
    role = (user["role"] if user is not None and "role" in user.keys() else "") or ""
    if role != "admin":
        raise HTTPException(403, "управление учётными записями — только администратору")
    return {"id": user["id"], "name": user["name"]}


def _hash_password():
    mod = sys.modules.get("main")
    if mod is None or not hasattr(mod, "hash_password"):
        raise HTTPException(503, "кабинет не поднят — задать пароль нечем")
    return mod.hash_password


@app.get("/ui/accounts")
def ui_accounts(request: Request):
    """Кто есть в системе. Ни пароля, ни его хэша, ни API-токена здесь нет."""
    _require_admin(request)
    c = db()
    try:
        users = accounts.list_users(c)
        admins = accounts.admin_count(c)
    finally:
        c.close()
    me = getattr(request.state, "user", None)
    return {
        "users": users,
        "me": (me["id"] if me is not None else None),
        "admins": admins,
        "roles": [{"key": "admin", "title": "администратор"},
                  {"key": "user", "title": "пользователь"}],
        "note": "вход у человека один на всю платформу: кабинет, пульт, тексты и обложки",
    }


class AccountIn(BaseModel):
    login: str = ""
    password: str = ""
    role: str = "user"
    quota: str = ""


@app.post("/ui/accounts")
def ui_account_create(body: AccountIn, request: Request):
    """Завести человека: учётка, тенант, каталог — всё три или ничего.

    Заводить его у соседей не нужно: после слияния личности Rewrite и Cover узнают
    человека по сессии кабинета, а его данные создаются при первом обращении.
    """
    _require_admin(request)
    c = db()
    try:
        out = accounts.create_user(c, body.login, body.password, _hash_password(),
                                   role=body.role, quota=body.quota,
                                   tenants_dir=TENANTS_DIR)
    except accounts.AccountError as exc:
        raise HTTPException(400, str(exc))
    finally:
        c.close()
    return {"ok": True, **out}


class AccountEditIn(BaseModel):
    disabled: bool | None = None
    role: str | None = None
    password: str | None = None
    quota: str | None = None


@app.post("/ui/accounts/{uid:int}")
def ui_account_edit(uid: int, body: AccountEditIn, request: Request):
    """Заблокировать, сменить роль, задать пароль, поменять квоту.

    По одному изменению за запрос — так понятнее и в интерфейсе, и в отказе: «это
    последний администратор» относится к конкретному действию, а не к пачке.
    """
    me = _require_admin(request)
    c = db()
    try:
        done = {}
        if body.disabled is not None:
            done["disabled"] = accounts.set_disabled(c, uid, body.disabled,
                                                     actor_id=me["id"])
        if body.role is not None:
            done["role"] = accounts.set_role(c, uid, body.role, actor_id=me["id"])
        if body.password is not None:
            done["password"] = accounts.set_password(c, uid, body.password,
                                                     _hash_password())
        if body.quota is not None:
            done["quota"] = accounts.set_quota(c, uid, body.quota)
        if not done:
            raise HTTPException(400, "нечего менять")
    except accounts.AccountError as exc:
        raise HTTPException(400, str(exc))
    finally:
        c.close()
    return {"ok": True, "done": sorted(done)}
