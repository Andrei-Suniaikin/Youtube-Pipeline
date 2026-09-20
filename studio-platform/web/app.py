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
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
SERVER = os.path.join(BASE, "server")
sys.path.insert(0, SERVER)

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402
from pydantic import BaseModel, StrictInt                         # noqa: E402

sys.path.insert(0, HERE)
import pult_auth as auth                                       # noqa: E402

from channel_slots import (ANY_DAY, DAY_KEYS, _localize, _zone,  # noqa: E402
                           normalize_schedule, upcoming_slots)
from pub_engine import PHASES, PHASE_DEADLINES, IRREVERSIBLE_FROM                   # noqa: E402
from pub_engine import revive as pub_engine_revive                # noqa: E402
import preset_fields                                             # noqa: E402
import preset_links                                              # noqa: E402
from pub_engine import channel_presets                            # noqa: E402
from pub_remote import (COVER_BASE, LANG_TO_RU, REWRITE_BASE,     # noqa: E402
                        YT_ID_RE, donor_subs, donor_text, get_key, subs_exits_state,
                        subs_probe_async, video_id)
from pub_services import (COLLECT_BATCH, COLLECT_REASONS,        # noqa: E402
                          MEDIA_STAGES, OPTIONAL_STAGES, REWRITE_BAD, REWRITE_DONE,
                          collect_paid_texts, paid_texts_waiting)
from pub_neighbors import (cover_presets, rewrite_preset_config,  # noqa: E402
                           rewrite_presets, vsgen_presets)
from pub_tags import (DONOR_UNKNOWN_WHY, add_links, check_donor,   # noqa: E402
                      donor_facts_blank, donor_limits, donor_verdict,
                      parse_links, plan_rows)
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

# Слова трёх разных сервисов про один и тот же исход. Соседи не говорят «done»:
# рерайт заканчивает задачу словом `completed` (rewrite/job_queue.py JobStatus), Cover —
# `completed` и `completed_partial` (cover/job_queue_cover.py). Пока их тут не было,
# законченная задача текста показывалась в очереди как «в очереди»: `_state` не узнавал
# статус и отдавал состояние по умолчанию. Это ровно та ложь экрана, из-за которой
# 20.09 нельзя было понять, что с задачей происходит.
TERMINAL_OK = ("done", "completed")
TERMINAL_BAD = ("error", "cancelled", "failed")
# `partial` у рерайта — «часть языков сделана», `interrupted` — «прервана и продолжится
# с места обрыва»: оба отдают файлы (DOWNLOADABLE_STATUSES в rewrite/api.py), оба — не
# отказ. `completed_partial` — то же самое у Cover.
TERMINAL_PART = ("partial", "completed_partial", "interrupted")

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
    if s in TERMINAL_PART:
        return "partial"        # рерайт умеет «сделал часть языков» — это не ошибка
    if s == "cancelled":
        return "cancelled"      # остановлено человеком — тоже не ошибка
    if s in TERMINAL_BAD:
        return "error"
    # `resetting` у рерайта — подготовка к повтору: работа уже идёт, и показывать её
    # как «в очереди» значит обещать человеку, что задача ещё не начиналась.
    if s in ("running", "claimed", "in_progress", "resetting"):
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
        # `repeats`/`last_seen`: подряд идущий повтор одной фазы или одной строки
        # журнала не заводит новую запись (pub_engine.log_phase, pub_log.add), поэтому
        # экран показывает «с какого времени» и «сколько раз», а не стену одинаковых
        # строк. Старые записи повторов не знают — отсюда COALESCE.
        events = [{"seq": e["seq"], "phase": e["phase"],
                   "phase_title": PHASE_TITLE.get(e["phase"], e["phase"]),
                   "video_id": e["video_id"] or "", "created": e["created"],
                   "repeats": e["repeats"] or 1,
                   "last_seen": e["last_seen"] or e["created"]}
                  for e in c.execute(
                      "SELECT *, COALESCE(repeats,1) repeats, "
                      "COALESCE(last_seen, created) last_seen "
                      "FROM pub_release_events WHERE release_id=? ORDER BY seq",
                      (release_id,))]
        log = [{"ts": l["ts"], "level": l["level"], "step": l["step"],
                "message": l["message"], "repeats": l["repeats"] or 1,
                "last_ts": l["last_ts"] or l["ts"]}
               for l in c.execute(
                   "SELECT *, COALESCE(repeats,1) repeats, COALESCE(last_ts, ts) last_ts "
                   "FROM pub_release_log WHERE release_id=? ORDER BY id", (release_id,))]
        return {"release": rel, "stages": _stages(c, rel), "events": events, "log": log}
    finally:
        c.close()


@app.get("/ui/channels")
def ui_channels():
    c = db()
    try:
        out = []
        for ch in c.execute("SELECT * FROM pub_channels_live WHERE tenant_id=? ORDER BY name",
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

# Формы ответа у соседей РАЗНЫЕ, и это стоило владельцу вечера 20.09: строка задачи
# текста приходила в очередь без номера, без названия и без стадии — «просто висит
# задача, даже без названия». Причина в одной букве. Списочный элемент отдаёт:
#   cover:   id, status, created_at, progress_percent, results_count
#            (cover/api_cover.py ApiJobListItem)
#   rewrite: job_id, status, progress_percent, progress_detail, model, files_count,
#            languages_count, created_at, started_at, finished_at
#            (rewrite/api.py _job_to_summary — поля `id` у него нет вовсе)
# Старый код читал `it["id"]` у обоих, и у рерайта получал пусто: пустой номер, пустое
# название, пустая ссылка на задачу. Ни `results_count`, ни `error` рерайт в списке не
# отдаёт тоже.
# Ключевая авторизация у обоих живёт под /api/v1 (см. pub_remote.RewriteClient._url).
NEIGHBOURS = (("rewrite", "Текст", REWRITE_BASE), ("cover", "Обложки", COVER_BASE))
NEIGHBOUR_TIMEOUT = 6

# Как называется работа соседа по-русски. ТЗ §13: внутренние слова наружу не показываем,
# а «rewrite» в строке очереди — внутреннее слово.
NEIGHBOUR_KIND = {"rewrite": ("text", "текст"), "cover": ("cover", "обложка")}

# Сколько раз за ОДИН показ очереди можно дочитать подробности у соседей — на всех, а
# не на каждого.
#
# Зачем вообще дочитывать. Стадию идущей задачи и причину отказа сосед держит в логе
# задачи, а лог отдаётся только поштучно: `GET /api/v1/jobs/{id}?include_log=true`.
# В списке их нет — у рерайта `progress_detail` идущей задачи пуст (проверено на задаче
# владельца 0a6c6716dbd1: log «ru: старт — 1 частей», progress_detail ''), а `error` в
# списке не отдаётся вовсе.
#
# Почему бюджет, а не «дочитать всё». Сорок строк — это сорок запросов по сети на каждое
# открытие экрана, и очередь из инструмента наблюдения превращается в нагрузку на тот
# самый сервис, за которым наблюдает. Бюджет тратится на строки, где ответ ВИДЕН
# человеку: идущие (нужна стадия) и кончившиеся плохо (нужна причина), и только на те,
# что пережили фильтры экрана (см. `ui_queue`: дочитывание идёт ПОСЛЕ отбора).
# Остальным подробности не нужны — по статусу и так всё понятно. Кому не хватило бюджета, тот
# честно помечен `reason_pending`: «не дочитали» и «причины нет» — разное.
QUEUE_DETAIL_BUDGET = 12
DETAIL_TTL_LIVE = 5.0          # задача идёт: лог меняется, держим недолго
DETAIL_TTL_DONE = 600.0        # задача кончилась: лог больше не изменится
DETAIL_CACHE_MAX = 500
_DETAIL_CACHE: dict = {}


def _span_ru(sec) -> str:
    """Длительность словами: «40 с», «12 мин», «2 ч 10 мин»."""
    sec = int(max(0.0, float(sec or 0)))
    if sec < 90:
        return f"{sec} с"
    minutes = sec // 60
    if minutes < 90:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч" if not minutes else f"{hours} ч {minutes} мин"


def _job_age(created, started, finished, state="", now=None,
             times_known=True) -> dict:
    """Сколько задача ждала очереди и сколько уже работает — двумя числами.

    Владелец 20.09 смотрел на строку и не мог отличить «висит» от «идёт давно»: в
    строке стояло одно время создания. Рерайт расшифровки — это минуты, и это
    нормально; ненормально не знать, взяли задачу в работу вообще или нет. У задачи
    владельца между «создана» (18:44) и «взята» (18:48) прошло четыре минуты, и
    именно эти четыре минуты человек читал как «ничего не происходит».

    Состояние нужно ради одного случая, который без него читается ложью: задача,
    которую отменили или похоронили ДО того, как её взяли в работу. Времени начала у
    неё нет, и по одним числам выходит «в очереди 1 ч 48 мин» — про задачу, которой
    в очереди давно нет. Проверено на боевых строках 17 и 18 стенда 20.09.

    `times_known` отделяет «не бралась» от «не знаем». Бывает, что времён работы нет
    не потому, что работы не было, а потому, что источник их не передаёт: списочная
    ручка Cover отдаёт только `id/status/created_at/progress_percent/results_count`
    (cover/api_cover.py `ApiJobListItem`) — ни `started_at`, ни `finished_at`. По
    одним числам законченная обложка читалась как «в работу не бралась», а идущая —
    как «в очереди 13 мин»: оба раза уверенное враньё о чужой работе. Незнание
    называем незнанием; в числах его тоже не подменяем — `waiting` там пуст, потому
    что ожидание очереди без времени начала не считается, а возраст строки — это не
    ожидание.
    """
    now = time.time() if now is None else float(now)

    def _at(value):
        return float(value) if isinstance(value, (int, float)) and value else None

    created, started, finished = _at(created), _at(started), _at(finished)
    over = state in ("done", "error", "cancelled", "partial")
    # Источник молчит о временах, а статус говорит, что работа шла или уже кончилась:
    # единственный честный ответ — «не знаем». Если же работа ещё в очереди, времени
    # начала нет по существу, и «в очереди N» остаётся верным даже от молчуна.
    silent = not times_known and started is None and (over or state == "running")
    waiting = working = None
    if created is not None and not silent:
        waiting = max(0.0, (started if started is not None else now) - created)
    if started is not None:
        working = max(0.0, (finished if finished is not None else now) - started)

    if silent:
        text = "сосед не сообщает, когда взял в работу"
    elif over and started is None:
        text = "в работу не бралась"
    elif finished is not None and working is not None:
        text = f"заняла {_span_ru(working)}"
    elif working is not None and over:
        # Кончилась, а время окончания не записано: сказать «идёт» было бы обещанием
        # работы, которой уже нет.
        text = f"работала {_span_ru(working)}"
    elif working is not None:
        text = f"идёт {_span_ru(working)}"
        # Ожидание в очереди упоминаем, только когда оно заметное: приписка «ждала 3 с»
        # к каждой строке — шум, который учит не читать строку целиком.
        if waiting and waiting >= 60:
            text += f", ждала очереди {_span_ru(waiting)}"
    elif waiting is not None:
        text = f"в очереди {_span_ru(waiting)}"
    else:
        text = ""
    return {"started": started, "finished": finished,
            "waiting": (round(waiting) if waiting is not None else None),
            "working": (round(working) if working is not None else None),
            "age_text": text}


# Отметка времени в начале строки лога соседа: «[18:50:52] » или «[2026-09-20 18:48:24] ».
# Только цифры и разделители — иначе под правило попадает «[migrate 001] …», а это уже
# содержание строки, а не её время.
_LOG_TIME = re.compile(r"^\s*\[[\d\s:.\-]{4,24}\]\s*")
_LOG_GLUE = re.compile(r"(?=\[\d{2}:\d{2}:\d{2}\])")
_LOG_BAD = re.compile(r"(ошибк|не удалось|не завершен|отказ|E-[A-Z]{2,8})", re.I)
# «задача не завершена (E-FAIL)» — это ИТОГ работы, а не её причина. Человеку в очереди
# нужна причина: из-за чего именно не завершена.
_LOG_OUTCOME = re.compile(r"\(E-FAIL\)", re.I)


def _log_lines(log: str) -> list:
    """Лог соседа строками, без отметок времени и без пустот."""
    out = []
    for raw in str(log or "").splitlines():
        # Рерайт дописывает служебную пометку без перевода строки, и две записи
        # слипаются в одну: «…продолжит с места обрыва[18:48:24] Задача принята».
        # Читать такую склейку как одну строку — значит показать человеку мусор.
        for piece in _LOG_GLUE.split(raw):
            line = _LOG_TIME.sub("", piece).strip()
            if line:
                out.append(line)
    return out


def _stage_from_log(log: str, skip_errors: bool = False) -> str:
    """Последняя осмысленная строка лога — она же стадия задачи для очереди.

    Сосед пишет их по-русски и для человека («Подготовка», «Обработка…», «ru: старт —
    1 частей»), сочинять свои слова поверх незачем: разойдясь с ними, экран начнёт
    рассказывать не то, что сосед на самом деле делает.

    `skip_errors` — для упавшей задачи. У неё последняя строка лога это сама ошибка, и
    без пропуска в строке очереди дважды стоит одно и то же: «этап: задача не завершена»
    и рядом «причина: задача не завершена». Человеку нужно другое — ГДЕ она встала.
    """
    lines = _log_lines(log)
    if skip_errors:
        lines = [ln for ln in lines if not _LOG_BAD.search(ln)]
    return _short(lines[-1], 160) if lines else ""


def _reason_from_log(log: str) -> str:
    """Причина отказа словами: та строка лога, из-за которой всё встало."""
    bad = [ln for ln in _log_lines(log) if _LOG_BAD.search(ln)]
    if not bad:
        return ""
    cause = [ln for ln in bad if not _LOG_OUTCOME.search(ln)]
    # «Ошибка: сеть недоступна (E-NET)» точнее, чем «ru: не удалось завершить (E-PART)»:
    # второе — следствие первого, и повторять человеку следствие бессмысленно.
    named = [ln for ln in cause if ln.lower().startswith("ошибка")]
    return _short((named or cause or bad)[-1], 200)


def _join_reason(cause: str, said: str) -> str:
    """Причина и итог в одной строке, без повторов одного и того же."""
    cause, said = (cause or "").strip(), (said or "").strip()
    if cause and said and said not in cause and cause not in said:
        return _short(f"{cause} · {said}", 300)
    return _short(cause or said, 300)


def _neighbour_jobs(name: str, base: str, key: str) -> tuple:
    """Задачи соседа. Возвращает (строки, ошибка). Недоступный сосед не роняет экран.

    Строка приводится к тому же виду, что и своя (`queue_tasks`), включая поля канала
    и проекта: иначе фильтр «по каналу» молча выбрасывал задачи соседей — у них
    `channel_id` не было вовсе, а `None` не равен номеру канала никогда.
    """
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
    kind, kind_title = NEIGHBOUR_KIND.get(name, (name, name))
    rows = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        pct = it.get("progress_percent")
        status = str(it.get("status") or "")
        # `job_id` — рерайт, `id` — Cover. Оба спрашиваем всегда: гадать, кто перед
        # нами, по имени сервиса значит сломаться от первой же смены формы у одного.
        job_id = str(it.get("id") or it.get("job_id") or "")
        stage = str(it.get("progress_detail") or "").strip()
        if not stage and it.get("results_count"):
            stage = f'готово {it["results_count"]}'
        row = {
            "service": name,
            "id": job_id,
            "title": "", "title_why": "",
            "channel": "", "channel_id": None, "lang": "",
            "release_id": "", "project_id": "", "user": "",
            "kind": kind, "kind_title": kind_title,
            "status": status,
            "state": _state(status),
            "progress": (float(pct) / 100 if isinstance(pct, (int, float)) else None),
            "stage": _short(stage, 160),
            "provider": "",
            "created": it.get("created_at"),
            "error": "", "warning": "",
            # Подробности ещё не спрашивали. Строке, которой они нужны, это поле
            # переставит `_fill_neighbour_details` — в True, если бюджет кончился.
            "reason_pending": False,
            # Сообщает ли сосед времена работы ВООБЩЕ. Решаем по наличию поля, а не
            # его значения: у рерайта `started_at` есть всегда и ноль в нём означает
            # «не бралась», у Cover поля нет вовсе и означать оно не может ничего.
            # Разбирать по имени сервиса нельзя — сменит форму один, соврут оба.
            "times_known": ("started_at" in it) or ("finished_at" in it),
        }
        row.update(_job_age(it.get("created_at"), it.get("started_at"),
                            it.get("finished_at"), row["state"],
                            times_known=row["times_known"]))
        rows.append(row)
    return rows, ""


def _name_neighbour_rows(c, service: str, rows: list) -> None:
    """Имя задачи соседа — название ролика, как требует ТЗ, а не hex-номер.

    Сосед про наш проект и наш канал не знает ничего: у него есть номер задачи и файл.
    Связь держим мы, в двух местах и по двум разным поводам:
      * `project_service_jobs` — задачу поставил человек с экрана проекта (пульт
        записывает связь сразу после того, как сосед задачу принял, `_link_project_jobs`);
      * `pub_release_jobs` — задачу поставил ночной движок по выпуску канала, и там же
        лежит путь к `pub_plan_items.donor_title`, то есть к настоящему названию ролика.
    Оба — ОДНИМ запросом на сервис: очередь на сотню строк не должна превращаться в
    сотню обращений за именем.

    Третий повод — заказ ссылкой прямо с экрана проекта. С 20.09.2026 такая ссылка
    у проекта ЗАПОМИНАЕТСЯ (`project_origin`), и очередь показывает её вместо имени
    проекта. Отдельным запросом, а не join'ом к двум запросам выше: таблица заводится
    лениво, и на базе, где ею ещё не пользовались, join уронил бы имена ВСЕХ строк.

    Чего в этой цепочке по-прежнему нет — настоящего НАЗВАНИЯ ролика по ссылке:
    спросить его у YouTube отсюда нельзя, `donor_title` ходит в сеть и стоит до 48 с
    на ссылку (замер 19.09). Нет ни названия, ни ссылки — в строке стоит имя проекта,
    а `title_why` говорит, почему имя именно такое. «Без названия» не годится: человек
    должен понимать, что это за задача.
    """
    ids = [r["id"] for r in rows if r.get("id")]
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    found: dict = {}

    def _put(row) -> None:
        cur = found.setdefault(row["job_id"], {})
        for field in ("project_id", "project_name", "release_id", "donor_title",
                      "donor_url", "ch_id", "ch_name", "lang"):
            value = row[field] if field in row.keys() else None
            if value and not cur.get(field):
                cur[field] = value

    try:
        # Задача, поставленная человеком с экрана проекта.
        for q in c.execute(
                "SELECT j.job_id, j.project_id, p.name project_name, "
                "       r.release_id, i.donor_title, i.donor_url, "
                "       ch.id ch_id, ch.name ch_name, ch.lang lang "
                "FROM project_service_jobs j "
                "LEFT JOIN projects p ON p.id=j.project_id AND p.tenant_id=j.tenant_id "
                "LEFT JOIN pub_releases r ON r.project_id=j.project_id "
                "     AND r.tenant_id=j.tenant_id "
                "LEFT JOIN pub_plan_items i ON i.id=r.plan_item_id "
                "LEFT JOIN pub_channels ch ON ch.id=r.channel_id "
                f"WHERE j.tenant_id=? AND j.service=? AND j.job_id IN ({marks}) "
                "ORDER BY r.updated", (TENANT, service, *ids)):
            _put(q)
        # Задача, поставленная движком по выпуску: у неё есть строка плана, а значит и
        # настоящее название ролика.
        for q in c.execute(
                "SELECT j.remote_job_id job_id, j.release_id, r.project_id, "
                "       i.donor_title, i.donor_url, "
                "       ch.id ch_id, ch.name ch_name, ch.lang lang "
                "FROM pub_release_jobs j "
                "JOIN pub_releases r ON r.release_id=j.release_id "
                "LEFT JOIN pub_plan_items i ON i.id=r.plan_item_id "
                "LEFT JOIN pub_channels ch ON ch.id=r.channel_id "
                f"WHERE r.tenant_id=? AND j.service=? AND j.remote_job_id IN ({marks}) "
                "ORDER BY j.updated", (TENANT, service, *ids)):
            _put(q)
    except sqlite3.Error:
        # Имя — это удобство. Потерять из-за него всю очередь нельзя: строка без
        # названия хуже названной, но лучше пустого экрана.
        found = {}

    # Ссылка, записанная на экране проекта, — последний шанс назвать задачу. Своим
    # запросом и со своим отказом: нет таблицы — теряем одну подпись, а не все.
    try:
        own = sorted({v.get("project_id") for v in found.values() if v.get("project_id")})
        if own:
            links = {r["project_id"]: (r["donor_url"] or "") for r in c.execute(
                "SELECT project_id, donor_url FROM project_origin WHERE tenant_id=? "
                f"AND project_id IN ({','.join('?' * len(own))})", (TENANT, *own))}
            for info in found.values():
                if not (info.get("donor_url") or "").strip():
                    info["donor_url"] = links.get(info.get("project_id")) or ""
    except sqlite3.Error:
        pass

    for row in rows:
        info = found.get(row["id"]) or {}
        row["project_id"] = info.get("project_id") or ""
        row["release_id"] = info.get("release_id") or ""
        row["channel"] = info.get("ch_name") or ""
        row["channel_id"] = info.get("ch_id")
        row["lang"] = (info.get("lang") or "").upper()
        title, why = (info.get("donor_title") or "").strip(), ""
        if not title:
            link = (info.get("donor_url") or "").strip()
            project = (info.get("project_name") or "").strip()
            if link:
                title = link
                why = "названия ролика у нас нет — показываем ссылку донора"
            elif project:
                title = project
                why = ("название ролика не сохранено: задачу поставили ссылкой с "
                       "экрана проекта — в строке имя проекта")
            elif row["id"]:
                title = f"задача {row['id']}"
                why = ("задача заведена не из кабинета: ни проекта, ни выпуска за ней "
                       "не записано, названия ролика взять негде")
            else:
                title = "задача без номера"
                why = "сосед не назвал номер задачи — спросить его не о чем"
        row["title"] = _short(title, 200)
        row["title_why"] = why


def _neighbour_detail(name: str, base: str, key: str, job_id: str, status: str) -> dict:
    """Подробности одной задачи соседа, с коротким кэшем.

    Кэш — ускорение, а не хранилище: кончившаяся задача свой лог больше не меняет,
    поэтому её держим долго, а идущую — секунды, иначе экран начнёт показывать
    вчерашнюю стадию. Ключ включает статус: сменился статус — ответ устарел заведомо.
    """
    now = time.time()
    hit = _DETAIL_CACHE.get((name, job_id))
    if hit is not None and hit[0] > now and hit[1] == status:
        return hit[2]
    path = f"/api/v1/jobs/{urllib.parse.quote(job_id)}"
    if name == "rewrite":
        path += "?include_log=true"   # стадия и причина у рерайта живут только в логе
    body, _err = _neighbour_get(base, path, key)
    out = body if isinstance(body, dict) else {}
    if not out:
        # Неудачу не запоминаем. Иначе одна сорвавшаяся сетевая попытка прячет причину
        # отказа на десять минут — ровно у той строки, ради которой человек и пришёл
        # в очередь. Пустой кэш стоит одного лишнего запроса, пустой экран — вечера.
        return out
    ttl = DETAIL_TTL_LIVE if _state(status) in ("running", "queued") else DETAIL_TTL_DONE
    if len(_DETAIL_CACHE) > DETAIL_CACHE_MAX:
        _DETAIL_CACHE.clear()
    _DETAIL_CACHE[(name, job_id)] = (now + ttl, status, out)
    return out


def _apply_detail(row: dict, detail: dict) -> None:
    """Положить в строку то, ради чего подробности и спрашивали."""
    if not detail:
        row["reason_pending"] = True      # спросили и не узнали — так и говорим
        return
    log = str(detail.get("log_tail") or "")
    over = row["state"] in ("error", "partial", "cancelled")
    stage = (_stage_from_log(log, skip_errors=over)
             or str(detail.get("progress_detail") or "").strip())
    if stage:
        row["stage"] = _short(stage, 160)
    said = str(detail.get("error") or "").strip()
    if over:
        row["error"] = _join_reason(_reason_from_log(log), said) or row["error"]
    elif said and not row["error"]:
        row["error"] = _short(said, 300)
    warning = str(detail.get("warning") or "").strip()
    if warning and not row["warning"]:
        row["warning"] = _short(warning, 200)
    # Поштучная ручка Cover времена отдаёт (`ApiJobResponse`), в отличие от списка:
    # раз дочитали — строка перестаёт быть строкой молчуна.
    if "started_at" in detail or "finished_at" in detail:
        row["times_known"] = True
    row.update(_job_age(row.get("created"),
                        detail.get("started_at") or row.get("started"),
                        detail.get("finished_at") or row.get("finished"),
                        row["state"], times_known=row.get("times_known", True)))
    row["reason_pending"] = False


def _detail_needed(row: dict) -> bool:
    """Нужны ли этой строке подробности. Спрашиваем только там, где это видно."""
    if not row.get("id"):
        return False
    if row["state"] == "running":
        return not row["stage"]                       # чем занята прямо сейчас
    if row["state"] in ("error", "partial", "cancelled"):
        return not row["error"]                       # почему кончилась так
    return False


def _fill_neighbour_details(parcels: list, budget: int = QUEUE_DETAIL_BUDGET) -> dict:
    """Дочитать стадию и причину тем строкам, без которых строка бессмысленна.

    `parcels` — по одной записи на сервис: (имя, база, ключ, строки).
    """
    need = []
    for name, base, key, rows in parcels:
        if not key:
            continue
        need.extend((row.get("created") or 0, name, base, key, row)
                    for row in rows if _detail_needed(row))
    # Свежие первыми: если бюджета на всех не хватит, человек смотрит на то, что
    # происходит сейчас, а не на позавчерашний отказ.
    need.sort(key=lambda item: -item[0])
    asked = 0
    for _created, name, base, key, row in need:
        if asked >= budget:
            row["reason_pending"] = True
            continue
        asked += 1
        _apply_detail(row, _neighbour_detail(name, base, key, row["id"], row["status"]))
    return {"asked": asked, "needed": len(need), "budget": budget}


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
                "SELECT q.job_id, q.kind, q.status, q.created, q.started, q.finished, "
                "       q.progress, q.stage, "
                "       q.provider, q.error, q.project_id, q.uid, "
                "       r.release_id, ch.id ch_id, ch.name ch_name, ch.lang lang, "
                "       p.donor_title, p.donor_url, pr.name project_name, "
                "       u.name user_name "
                "FROM queue_tasks q "
                "LEFT JOIN pub_releases r ON r.project_id = q.project_id "
                "LEFT JOIN pub_channels ch ON ch.id = r.channel_id "
                "LEFT JOIN pub_plan_items p ON p.id = r.plan_item_id "
                "LEFT JOIN projects pr ON pr.id = q.project_id "
                "LEFT JOIN users u ON u.id = q.uid "
                "WHERE q.tenant_id=? ORDER BY q.job_id DESC LIMIT 100",
                (TENANT,)):
            # Имя проекта раньше в этой цепочке не участвовало, и строка своей задачи
            # без выпуска подписывалась идентификатором `p_a66a80ca50`. Человеку он не
            # говорит ничего — а имя проекта он сам и задал.
            title, why = (q["donor_title"] or "").strip(), ""
            if not title:
                if (q["donor_url"] or "").strip():
                    title = q["donor_url"].strip()
                    why = "названия ролика у нас нет — показываем ссылку донора"
                elif (q["project_name"] or "").strip():
                    title = q["project_name"].strip()
                    why = ("название ролика не сохранено: у задачи нет выпуска — "
                           "в строке имя проекта")
                else:
                    title = q["project_id"] or f'задача {q["job_id"]}'
                    why = ("название ролика не сохранено: у этой задачи нет ни "
                           "выпуска, ни названного проекта")
            row = {
                "service": "vsgen", "id": str(q["job_id"]),
                "title": _short(title, 200), "title_why": why,
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
                "error": q["error"] or "", "warning": "",
                "reason_pending": False,
                # Своя очередь: `started`/`finished` лежат в той же строке таблицы,
                # спрашивать не у кого и молчать некому.
                "times_known": True,
            }
            row.update(_job_age(q["created"], q["started"], q["finished"],
                                row["state"]))
            rows.append(row)
        services = [{"key": "vsgen", "title": "Видео", "ok": True, "error": "",
                     "count": len(rows)}]
        # Выпуск канала, который ещё не дошёл до задач (или ждёт между этапами), в
        # очереди не появлялся вовсе: задач у него пока нет, и человек видел пустую
        # очередь при работающем конвейере (жалоба владельца 18.09.2026). Показываем
        # его отдельной строкой — с фазой и причиной ожидания, а не выдуманным этапом.
        releases = _live_release_rows(c)
        rows.extend(releases)
        services.append({"key": "release", "title": "Выпуски", "ok": True, "error": "",
                         "count": len(releases)})
        parcels = []
        for name, title, base in NEIGHBOURS:
            key = get_key(c, name)
            got, err = _neighbour_jobs(name, base, key)
            _name_neighbour_rows(c, name, got)
            parcels.append((name, base, key, got))
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

    # Дочитывание — ПОСЛЕ фильтров, и порядок здесь содержательный, а не косметика.
    # Каждое дочитывание — отдельный запрос по сети к соседу, а бюджет на весь показ
    # один. Потратив его выше, на строки, которые фильтр тут же выбрасывает, экран
    # отнимал сеть ровно у тех строк, ради которых фильтр и поставлен: при выбранном
    # «Обложки» весь бюджет уходил на задачи рерайта, а обложки оставались без стадии
    # с пометкой «не дочитали». Теперь бюджет уходит только на видимое.
    # Переставлять безопасно: фильтры смотрят на сервис, канал, состояние и время
    # создания, а подробности меняют стадию, причину и время работы — пересечений нет.
    # База уже закрыта, и это тоже нарочно: дочитывание — чистая сеть, держать под неё
    # открытое соединение к SQLite незачем.
    visible = {id(r) for r in rows}
    details = _fill_neighbour_details([(name, base, key,
                                        [r for r in got if id(r) in visible])
                                       for name, base, key, got in parcels])

    rows.sort(key=lambda r: (r["created"] or 0), reverse=True)
    return {
        "rows": rows, "services": services, "channels": channels,
        "total": total, "shown": len(rows),
        "states": [{"key": k, "title": v} for k, v in STATE_TITLE.items()],
        "periods": [{"key": "1h", "title": "за час"},
                    {"key": "24h", "title": "за сутки"},
                    {"key": "7d", "title": "за неделю"}],
        # Сколько строк пришлось дочитывать у соседей и скольким не хватило бюджета.
        # Наружу это не украшение: строка с `reason_pending` объясняется именно этим
        # числом, и без него «причина не показана» выглядело бы как «причины нет».
        "details": details,
        # Канал и язык есть только у задач, рождённых выпуском. Ручная задача их не
        # имеет, и это не пробел данных, а другое происхождение — так и показываем.
        "note": "канал и язык видны у задач, созданных выпуском",
    }


# ───────────────── рубильник очереди: состояние словами ─────────────────

def _cabinet():
    """Модуль кабинета. Импорт ленивый — и это не осторожность ради осторожности.

    Состояние паузы живёт в `server/main.py`, и считать его здесь заново нельзя:
    посчитанная второй раз, она разойдётся с тем правилом, по которому шедулер на
    самом деле отказывает выдавать задачи, — а экран, который врёт про рубильник,
    хуже отсутствующего. В бою пульт и кабинет — один процесс (`web/platform.py`),
    и модуль там уже поднят; при отдельном запуске пульта он поднимется на первый
    запрос. Фоновых потоков импорт не заводит: планировщик поднимает startup
    кабинета, а он здесь не выполняется.
    """
    import main                                                   # noqa: PLC0415
    return main


def _pause_story(request: Request) -> dict:
    u = getattr(request.state, "user", None)
    uid = tenant = None
    if u is not None:
        try:
            uid = int(u["id"])
            tenant = u["tenant_id"] or TENANT
        except Exception:                                          # noqa: BLE001
            uid, tenant = None, TENANT
    return _cabinet()._queue_pause_story(uid=uid, tenant_id=tenant or TENANT)


@app.get("/ui/queue-pause")
def ui_queue_pause(request: Request):
    """Что остановлено, с какого времени, кем и что держится прямо сейчас.

    Владелец 18.09 полтора месяца смотрел на стоящие выпуски и не мог ответить ни
    на один из этих вопросов: рубильник — одно поле `admin_state.queue_pause`, а
    экран говорил только «ждём». Фразы приходят готовыми из кабинета (`_queue_pause_story`),
    здесь они не сочиняются: иначе в пульте и в админ-панели будут разные слова про
    одно состояние.

    Снятия паузы тут нет и не будет: отпускать конвейер вслепую из двух разных мест —
    это ровно августовский залп. Пульт показывает числа и отправляет в админ-панель.
    """
    story = _pause_story(request)
    # Что можно прогнать вхолостую. «Всё» имеет смысл только при общей паузе, точечные
    # цели — только те, которые реально стоят: кнопка, которая всегда есть и через раз
    # отвечает «такой паузы нет», учит не нажимать её вовсе.
    targets = []
    if story.get("all"):
        targets.append({"key": "all", "title": "снять общую паузу"})
    m = _cabinet()
    for prov in story.get("providers") or ():
        targets.append({"key": prov,
                        "title": "возобновить: " + str(m._provider_title(prov))})
    if story.get("mine_prompt"):
        targets.append({"key": "prompt_mine", "title": "возобновить тексты моего профиля"})
    story["targets"] = targets
    story["where_to_resume"] = ("Снимают паузу в админ-панели кабинета: у рубильника "
                                "одна точка записи, и вторая кнопка означала бы два "
                                "места, откуда конвейер отпускают вслепую.")
    return story


@app.get("/ui/queue-pause/dry-run")
def ui_queue_pause_dry_run(request: Request, target: str = "all"):
    """Сухой прогон: сколько задач уйдёт в работу немедленно, если это снять.

    Ничего не меняет — только читает. Вопрос, на который отвечает экран, ровно один:
    «сколько денег начнёт тратиться в первую же секунду после снятия».
    """
    story = _pause_story(request)
    allowed = ({"all"} if story.get("all") else set()) | set(story.get("providers") or ())
    if story.get("mine_prompt"):
        allowed.add("prompt_mine")
    if target not in allowed:
        raise HTTPException(400, "такой паузы сейчас нет — прогонять нечего")
    u = getattr(request.state, "user", None)
    dry = _cabinet()._queue_pause_dry_run(target, u=u)
    return {"target": target, "title": dry["title"], "text": dry["text"],
            "freed": dry["freed"], "held": dry["held"], "money": dry["money"],
            "changed": False,
            "note": "Прогон ничего не снял и не записал: рубильник он только читает."}


# ─────────────── оплачено, но не забрано: список и подбор ───────────────

# Проход `collect_paid_texts` ходит в сеть: на каждую задачу запрос статуса и
# скачивание файла, до ~30 с на неотвечающей. Держать на нём запрос пульта нельзя —
# это и есть «экран завис». Поэтому проход живёт в фоновом потоке, а экран спрашивает
# его состояние и показывает ожидание честно: сколько опрошено из скольких и кто
# опрашивается прямо сейчас.
_COLLECT_GATE = threading.Lock()
_COLLECT = {"running": False, "started": 0.0, "finished": 0.0,
            "total": 0, "done": 0, "taken": 0, "current": "", "res": None, "error": ""}


def _collect_snapshot() -> dict:
    with _COLLECT_GATE:
        out = dict(_COLLECT)
    now = time.time()
    # Пока идёт — сколько уже ждём; когда кончился — сколько шёл. Иначе на законченном
    # проходе экран продолжал бы наращивать «ждём уже», хотя ждать нечего.
    end = now if out["running"] else (out["finished"] or now)
    out["elapsed"] = int(end - out["started"]) if out["started"] else 0
    out["reasons"] = dict(COLLECT_REASONS)
    out["batch"] = COLLECT_BATCH
    return out


def _collect_worker(tenant_id: str, limit: int) -> None:
    """Проход по одному выпуску за раз — ради честного «опрошено N из M».

    Пачку целиком (`collect_paid_texts` со своим limit) экран бы тоже пережил, но
    тогда он до самого конца показывал бы одно и то же: «идёт». На 25 задачах, каждая
    из которых может думать полминуты, это неотличимо от зависшего пульта. Решение по
    каждой задаче принимает та же функция кабинета — здесь только список и счётчики.
    """
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    total = {"candidates": 0, "checked": 0, "taken": 0, "skipped": 0, "left": 0,
             "stopped": "", "by_reason": {}, "items": []}
    try:
        rows = paid_texts_waiting(c, tenant_id=tenant_id)
        total["candidates"] = len(rows)
        todo = rows if limit <= 0 else rows[:limit]
        with _COLLECT_GATE:
            _COLLECT["total"] = len(todo)
        for number, row in enumerate(todo):
            with _COLLECT_GATE:
                _COLLECT["current"] = (row.get("ch_name") or "") + " · " + str(row["release_id"])
            res = collect_paid_texts(c, tenant_id=tenant_id,
                                     release_ids=[row["release_id"]], limit=1)
            total["checked"] += res["checked"]
            total["taken"] += res["taken"]
            total["skipped"] += res["skipped"]
            total["items"] += res["items"]
            for key, count in (res["by_reason"] or {}).items():
                total["by_reason"][key] = total["by_reason"].get(key, 0) + count
            with _COLLECT_GATE:
                _COLLECT["done"] = number + 1
                _COLLECT["taken"] = total["taken"]
            if res["stopped"]:
                # Сосед лёг или ключ не принят. Стучаться остальными сотнями
                # бессмысленно и вредно — ждёт весь проход целиком.
                total["stopped"] = res["stopped"]
                total["left"] = total["candidates"] - (number + 1)
                break
        else:
            total["left"] = total["candidates"] - len(todo)
    except Exception as exc:                                       # noqa: BLE001
        with _COLLECT_GATE:
            _COLLECT["error"] = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        try:
            c.close()
        except Exception:                                          # noqa: BLE001
            pass
        with _COLLECT_GATE:
            _COLLECT["running"] = False
            _COLLECT["finished"] = time.time()
            _COLLECT["current"] = ""
            _COLLECT["res"] = total


class CollectIn(BaseModel):
    limit: int = COLLECT_BATCH


@app.get("/ui/paid-texts")
def ui_paid_texts():
    """За что уже заплачено, а текста у нас нет. Ни одного сетевого запроса.

    Экран обязан открываться и тогда, когда рерайт лежит: он отвечает на вопрос «за
    что деньги уже ушли», а не «что сосед думает об этом сейчас». 146 таких выпусков
    простояли до 18.09 ровно потому, что списка не существовало.
    """
    c = db()
    try:
        rows = paid_texts_waiting(c, tenant_id=TENANT)
    finally:
        c.close()
    out = []
    for r in rows:
        # `payload` — сырой ответ соседа на заказ, экрану он не нужен, а весит.
        out.append({k: v for k, v in r.items() if k != "payload"})
    return {
        "rows": out,
        "batch": COLLECT_BATCH,
        "reasons": dict(COLLECT_REASONS),
        "collect": _collect_snapshot(),
        "free": "Подбор денег не тратит: он спрашивает статус уже оплаченной задачи "
                "и скачивает готовый файл. Ничего не заказывает и паузу не трогает.",
        "how": "Номер задачи взят из нашей строки о заказе либо из журнала оплат. "
               "Повторный заказ такого текста — это вторые деньги за то же самое, "
               "поэтому кнопки «заказать заново» здесь нет.",
    }


@app.post("/ui/paid-texts/collect")
def ui_paid_texts_collect(body: CollectIn):
    """Запустить подбор в фоне. Возвращает состояние, а не результат.

    Результат приходит не сюда, а в GET того же адреса: проход может думать минуты,
    и ответ «ждите, вот сколько уже сделано» честнее, чем молчащий запрос.
    """
    limit = int(body.limit or 0)
    if limit < 0:
        limit = 0
    with _COLLECT_GATE:
        if _COLLECT["running"]:
            raise HTTPException(409, "подбор уже идёт — дождитесь его окончания")
        _COLLECT.update({"running": True, "started": time.time(), "finished": 0.0,
                         "total": 0, "done": 0, "taken": 0, "current": "",
                         "res": None, "error": ""})
    try:
        threading.Thread(target=_collect_worker, args=(TENANT, limit),
                         name="paid-texts-collect", daemon=True).start()
    except Exception as exc:                                       # noqa: BLE001
        # Флаг «идёт» ставится ДО старта намеренно: иначе два нажатия подряд запустили
        # бы два прохода по одному списку. Но если поток не стартовал, снять флаг
        # больше некому — он залипал навсегда, и все последующие нажатия получали 409
        # «подбор уже идёт» до перезапуска пульта, а экран вечно показывал «идёт».
        # Честный `error` рядом уже есть — сюда он просто не попадал.
        with _COLLECT_GATE:
            _COLLECT.update({"running": False, "finished": time.time(),
                             "error": f"поток подбора не стартовал: "
                                      f"{type(exc).__name__}: {exc}"[:300]})
        raise HTTPException(503, "подбор не запустился: не удалось создать поток "
                                 f"({type(exc).__name__}). Денег это не стоило — "
                                 "проход ничего не заказывает. Попробуйте ещё раз.")
    return _collect_snapshot()


@app.get("/ui/paid-texts/collect")
def ui_paid_texts_collect_state():
    """Идёт ли подбор, сколько сделано и чем кончился прошлый."""
    return _collect_snapshot()


@app.get("/ui/keys")
def ui_keys(request: Request):
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
        "rewrite": _rewrite_keys(getattr(request.state, "sid", "")),
        "unavailable": [],
    }


def _rewrite_keys(sid: str) -> dict:
    """Чем Rewrite Studio делает текст у этого человека (18.09.2026).

    Свои ключи рерайта живут у него, а не в кабинете: они нужны его моделям, а не нашей
    очереди. Пока их не было видно в пульте, выпуск просто вставал с «нет доступных
    ключей провайдера», и где это чинить — не говорил никто. Значения ключей сюда не
    приходят (Rewrite их наружу не отдаёт) — только признаки «задан» и что сработает.
    """
    code, body = _rw("/settings/keys", sid)
    if code != 200 or not isinstance(body, dict):
        return {"ok": False, "why": body if isinstance(body, str) else "рерайт не ответил"}
    return {"ok": True,
            "own_openai": bool(body.get("own_openai_key_set")),
            "own_claude": bool(body.get("own_claude_token_set")),
            "server_openai": bool(body.get("openai_configured")),
            "server_claude": bool(body.get("claude_configured")),
            "source": body.get("openai_key_source") or "",
            "claude_source": body.get("claude_key_source") or ""}


class RewriteKeyIn(BaseModel):
    openai_api_key: str = ""      # пусто = снять свой ключ, снова заработает серверный


@app.post("/ui/keys/rewrite")
def ui_keys_rewrite(body: RewriteKeyIn, request: Request):
    """Записать свой ключ OpenAI для текста. Хранит его Rewrite, пульт только передаёт:
    второго места хранения секрета заводить нельзя."""
    sid = getattr(request.state, "sid", "")
    code, out = _rw("/settings/keys", sid, {"openai_api_key": body.openai_api_key})
    if code >= 400:
        raise HTTPException(_pass_code(code), out)
    return {"ok": True, "set": bool(body.openai_api_key.strip())}


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
        ch = c.execute("SELECT * FROM pub_channels_live WHERE id=? AND tenant_id=?",
                       (channel_id, TENANT)).fetchone()
        if ch is None:
            raise HTTPException(404, "канал не найден")
        cal = _calendar(c, ch, history)
        # Ссылки, стоящие без даты: раньше их было видно только числом в плашке — что
        # именно поставлено, посмотреть было негде (жалоба владельца 18.09.2026).
        waiting_rows = [
            {"id": r["id"], "url": r["donor_url"], "title": r["donor_title"] or "",
             "note": r["note"] or "", "status": r["status"], "created": r["created"]}
            for r in c.execute(
                "SELECT id, donor_url, donor_title, note, status, created "
                "FROM pub_plan_items WHERE channel_id=? AND tenant_id=? AND slot_at IS NULL "
                "AND status NOT IN ('cancelled','done') ORDER BY position, id LIMIT 200",
                (channel_id, TENANT))]
        waiting = len(waiting_rows)
        host = c.execute("SELECT code, title FROM pub_hosts WHERE id=?",
                         (ch["host_id"],)).fetchone() if ch["host_id"] else None
        prof = c.execute("SELECT serial, title FROM pub_profiles WHERE id=?",
                         (ch["profile_id"],)).fetchone() if ch["profile_id"] else None
        try:
            schedule = json.loads(ch["schedule"] or "{}")
        except ValueError:
            schedule = {}
        # Сырая строка канала (18.09.2026): в форме настроек надо показать СВОИ значения
        # канала и отдельно сказать, какие из них сейчас перекрыты пресетом канала.
        # Иначе форма покажет унаследованное и сохранит его как своё.
        own = c.execute("SELECT * FROM pub_channels WHERE id=? AND tenant_id=?",
                        (channel_id, TENANT)).fetchone()
        ch_presets = [{"id": r["id"], "name": r["name"]} for r in c.execute(
            "SELECT id, name FROM pub_channel_presets WHERE tenant_id=? ORDER BY name",
            (TENANT,))]
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
            "channel_preset": {
                "id": own["preset_channel_id"] if own else None,
                "name": ch["preset_channel_name"] if "preset_channel_name" in ch.keys() else "",
                # Какие поля сейчас приходят из пресета, а не из канала: форма гасит их
                # и подписывает, а не делает вид, что это собственные настройки канала.
                "inherited": [k for k in CHANNEL_PRESET_COLUMNS
                              if own is not None and own["preset_channel_id"]
                              and ch[k] != own[k]],
                "own": {k: (own[k] if own is not None else None)
                        for k in CHANNEL_PRESET_COLUMNS},
                "all": ch_presets,
            },
            "waiting_without_slot": waiting,
            "waiting_items": waiting_rows,
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
        ch = c.execute("SELECT * FROM pub_channels_live WHERE id=? AND tenant_id=?",
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


@app.get("/ui/subs-exits")
def ui_subs_exits():
    """Живы ли выходы, через которые качаются субтитры донора.

    Зачем отдельная плитка. Мёртвый туннель (`127.0.0.1:1080`, который никто не
    поднял) раньше выглядел на экране как «всё встало»: выпуски стояли, задачи
    не двигались, и причину искали в очереди, в ключах, в соседях — где угодно,
    кроме связи. Сторож (`pub_remote`) знает ответ, здесь он просто показан.

    Состояние читается ИЗ БАЗЫ (соединение передаём): сторож живёт в кабинете, и
    при отдельном запуске пульта его память отсюда не видна, а таблица видна.
    Сеть не трогаем вовсе — экран обязан открываться и тогда, когда выходов нет.
    """
    c = db()
    try:
        state = subs_exits_state(c)
    finally:
        c.close()
    # Три вещи, которые экран не имеет права выдумать сам, и потому приходят словами
    # с сервера: куда стучится проверка, чем «мёртв» отличается от «отдыхает» и что
    # делать с адресом, которым два выхода видны снаружи одинаково.
    state["how"] = ("Проверка стучится не в YouTube, а в нейтральный адрес: иначе она "
                    "жгла бы ту же квоту и сама готовила бы бан выходам, живость "
                    "которых проверяет.")
    state["rest_vs_dead"] = ("«Отдыхает» — YouTube ответил 429 этому выходу, это "
                             "проходит само. «Мёртв» — выход не отвечает на проверку: "
                             "сам не починится, нужен владелец.")
    state["shared_ip_note"] = ("Выходы, которые снаружи видны ОДНИМ адресом, делят и "
                               "квоту, и репутацию: вторым выходом они только "
                               "притворяются. Факт показан, списки не переставляются.")
    return state


@app.post("/ui/subs-exits/probe")
def ui_subs_exits_probe():
    """Разбудить обход выходов. Запрос не держим и в сеть из него не ходим.

    Принудительный обход (`subs_probe_all(force=True)`) сюда НЕ вынесен намеренно:
    он идёт по выходам по очереди, до ~10 с на каждом, и на списке владельца из
    двух десятков это минуты в одном запросе пульта — то есть ровно «экран завис».
    Здесь будится тот же фоновый обход, что и при заказе субтитров, и только если
    сведения успели устареть. Когда будить нечего, так и сказано — кнопка не делает
    вид, что что-то произошло.
    """
    started = subs_probe_async()
    return {"started": started,
            "text": ("Обход выходов запущен в фоне — обновите плитку через несколько "
                     "секунд." if started else
                     "Будить нечего: обход уже идёт либо все выходы проверены недавно. "
                     "Новых сведений прямо сейчас не появится.")}


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
                            "preset_cover FROM pub_channels_live WHERE tenant_id=? "
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
                "SELECT e.release_id, e.payload, "
                "COALESCE(e.last_seen, e.created) created, r.phase, r.blocked_reason "
                "FROM pub_release_events e JOIN pub_releases r "
                "  ON r.release_id = e.release_id "
                "WHERE e.phase='public_check' AND r.tenant_id=? "
                "ORDER BY created DESC LIMIT 40", (TENANT,)):
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


# Не ходить за редиректом кабинета (18.09.2026). Раньше `urlopen` сам шёл по 302, а
# кабинет на POST формы отвечает редиректом на свою страницу проекта — и каждый старт
# этапа дополнительно рендерил её целиком (server/main.py `project_page`, сотни строк и
# обходы каталога проекта). Человек ждал этот рендер на каждое нажатие «Начать».
# Нам нужен только адрес из заголовка Location — в нём и лежит исход (`?job=`, `?dup=`…).
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_CABINET_OPENER = urllib.request.build_opener(_NoRedirect)


def _cabinet_form(path: str, fields, request, timeout: float = 0.0,
                  expect_json: bool = True) -> dict:
    """POST формой в кабинет от имени вошедшего человека.

    Кабинет отвечает JSON только на пометку «это fetch» — иначе он шлёт редирект на
    свою собственную страницу, показывать которую пульту нечем. Отказ пересказываем
    как есть: «повтор доступен только для ошибочной задачи» — это ответ, а не сбой.

    `expect_json=False` — для ручек, которые про «это fetch» не знают и отвечают
    редиректом всегда (старт этапа, формы пресетов). Нам важен адрес редиректа: в его
    query лежит исход. Саму страницу мы не запрашиваем — это не контракт, а страница,
    и её сборка занимала секунды на каждое нажатие.
    """
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(CABINET_BASE + path, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Cookie", f"{auth.COOKIE}={getattr(request.state, 'sid', '')}")
    req.add_header("X-Requested-With", "fetch")
    # За редиректом не ходим только там, где нам нужен сам адрес: у JSON-ручек кабинета
    # редиректа нет, и обычный `urlopen` для них короче и привычнее.
    open_ = urllib.request.urlopen if expect_json else _CABINET_OPENER.open
    try:
        with open_(req, timeout=timeout or NEIGHBOUR_TIMEOUT * 3) as resp:
            body = resp.read().decode("utf-8", "replace")
            if not expect_json:
                return {"ok": True, "url": resp.geturl()}
            return json.loads(body or "{}")
    except urllib.error.HTTPError as exc:
        if not expect_json and exc.code in (301, 302, 303, 307, 308):
            # Это и есть успех формы: кабинет говорит, куда пошёл бы браузер. Сам
            # адрес абсолютный или относительный — приводим к абсолютному, чтобы
            # разбор query у вызывающего не зависел от формы ответа.
            location = exc.headers.get("Location") or ""
            exc.close()
            return {"ok": True, "url": urllib.parse.urljoin(CABINET_BASE + path, location)}
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
        ch = c.execute("SELECT * FROM pub_channels_live WHERE id=? AND tenant_id=?",
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
    # Что за ролики стоят за ссылками, спрашивается ОТДЕЛЬНЫМ запросом
    # (`/ui/links/check`). Здесь сети нет и быть не должно: раскладка по датам обязана
    # появляться мгновенно, а проверка донора на мёртвом туннеле думает минутами —
    # склеенные в один запрос, они выглядят как зависший пульт.
    # Отсев по ЗНАЧЕНИЮ здесь безопасен, и это держится на `parse_links`: она уже
    # схлопывает буквальные повторы, поэтому одинаковых строк в `urls` не бывает и
    # выбросить «обе копии разом» нечего. Повтор ОДНОГО РОЛИКА в разных написаниях
    # (`youtu.be/X` и `watch?v=X`) — это две РАЗНЫЕ строки: `add_links` ловит его по
    # `video_id` и кладёт в `skipped` именно ту, которая не встала, а вставшая
    # остаётся в `fresh`. Заведётся второй вход в эту ручку, минующий `parse_links`, —
    # отсев придётся считать по числу копий, а не по множеству.
    skipped = set(res.get("skipped", []))
    return {
        "channel": {"id": ch["id"], "name": ch["name"], "lang": ch["lang"] or "",
                    "tz": ch["tz"], "horizon_days": ch["horizon_days"],
                    "lead_days": ch["lead_days"]},
        "presets": presets,
        "placed": [str(p) for p in res.get("placed", [])],
        "queued": res.get("queued", 0),
        "skipped": list(res.get("skipped", [])),
        # Ссылки, которые действительно встанут, — их и надо проверять: донор, который
        # никуда не встанет, зря дёргает выходы субтитров.
        "fresh": [u for u in urls if u not in skipped],
        "note": "пресеты и язык берутся из настроек канала",
    }


# Потолок ожидания проверки доноров. Бюджет `check_donors` (VS_DONOR_CHECK_BUDGET_SEC)
# спрашивается ПЕРЕД каждым походом в сеть, а не обрывает уже начатый: на мёртвом
# туннеле один поход за субтитрами живёт минуту с лишним, и предпросмотр двух ссылок
# держал запрос пульта три с половиной минуты (замерено 19.09.2026 на стенде с
# VS_SUBS_PROXIES=socks5h://127.0.0.1:1080). Браузерный fetch таймаута не имеет, и
# экран всё это время выглядит сломанным. Поэтому здесь свой предел по часам: что
# успели — показываем, остальное честно называем непроверенным.
# Умолчание «бюджет + 10 с» выбрано здесь, а не владельцем; вынесено в настройку.
DONOR_WALL_EXTRA = float(os.environ.get("VS_DONOR_CHECK_WALL_SEC", "10"))


def donor_unchecked(url: str, limits: dict, reason: str = "budget") -> dict:
    """Ответ про ссылку, которую мы проверить не успели.

    Разобрать ссылку здесь ОБЯЗАТЕЛЬНО, и это не мелочь: пустой лист фактов не имеет
    `video_id`, а по его отсутствию вердикт выходит «это не ссылка на ролик» — то есть
    ОТКАЗ по ссылке, про которую мы ничего не узнали. Ровно та подмена, которой на
    экране быть не должно: «не смогли проверить» и «донор плохой» это разные вещи.
    """
    facts = donor_facts_blank(url, time.time())
    facts["video_id"] = video_id(url)
    facts.update({"fail": reason,
                  "fail_text": DONOR_UNKNOWN_WHY.get(reason, "проверить не удалось")})
    facts.update(donor_verdict(facts, limits))
    return facts


def donor_net_deadline(now: float, budget_sec, wall: float) -> float:
    """Срок сетевой части для ФОНОВОГО потока проверки доноров.

    `check_donors` трактует нулевой бюджет как «срока нет», и для синхронного вызова
    это верно: кто позвал, тот и ждёт. Здесь — нет. Запрос пульта отпускается по
    потолку ожидания (`wall`), а поток остаётся, и поток БЕЗ срока ходит в сеть, пока
    не переберёт все ссылки: по 48 с на название и по таймауту на каждый выход
    субтитров, уже после того, как экран получил ответ. Настройка
    `VS_DONOR_CHECK_BUDGET_SEC=0`, задуманная как «не ограничивай проверку», давала
    таким образом утечку потоков: каждое нажатие «Показать, что будет» оставляло
    позади себя поток на минуты. Поэтому срок есть всегда: нет бюджета — живём по
    потолку ожидания. Возвращается МОМЕНТ, а не None.
    """
    return float(now) + (float(budget_sec or 0) or float(wall))


def donor_second_pass(found: dict, urls: list) -> tuple:
    """Кого спрашивать сетью во втором проходе и в каком порядке.

    Возвращает `(по_существу, косметика)` — списки `(номер, ссылка)`.

    По существу — те, про кого кэш не сказал ничего (вердикт `unknown`): без вердикта
    строка на экране бесполезна. Косметика — те, у кого не хватает одного названия.
    Название по коду `check_donor` — украшение («из-за него проверка не падает»), но
    добывается ПЕРВЫМ и стоит секунд: замер 19.09 — `donor_title` отвечает 48,4 с при
    бюджете пачки 20 с. Пока оба разряда шли вперемешку, в порядке ссылок, один
    безымянный сосед съедал срок целиком, и ссылки, про которые не известно НИЧЕГО,
    возвращались как «не успели»: бюджет ушёл на косметику соседней строки. Строка без
    названия читается («название не добыли»), строка без вердикта — нет.
    """
    need, cosmetic = [], []
    for number, url in enumerate(urls):
        known = found.get(url) or {}
        if known.get("verdict") == "unknown":
            need.append((number, url))
        elif not known.get("title"):
            cosmetic.append((number, url))
    return need, cosmetic


class LinksCheckIn(BaseModel):
    # `channel_id` спрашивается ради ПРАВ, а не ради порогов: пороги здесь общие
    # (`donor_limits`), канальных нет. Поле в контракте, которого нет в поведении, —
    # будущая ошибка, поэтому канал проверяется так же, как в `/ui/links/preview`.
    channel_id: int
    urls: list[str] = []


@app.post("/ui/links/check")
def ui_links_check(body: LinksCheckIn):
    """Есть ли у доноров субтитры и сколько в них знаков. Денег не тратит.

    Отдельно от `/ui/links/preview` сознательно: раскладка по датам — это база, она
    обязана отвечать мгновенно, а это — сеть через выходы субтитров, и её время нам
    не принадлежит. Зовётся ПОСЛЕ отсева дублей: пульт присылает `fresh` из ответа
    предпросмотра.

    Повтор одной и той же ссылки внутри пачки проверяется ОДИН раз, и это сказано
    числом `duplicates`: результаты всё равно лежат в словаре по url и схлопнулись бы
    в одну строку — но молча, а экран считает строки и сдвинул бы весь список.
    Поэтому строк в ответе ровно столько, сколько РАЗНЫХ ссылок пришло.

    Что не успело за отведённые часы, возвращается вердиктом `unknown` с причиной
    `budget` — «не смогли проверить», а НЕ «донор плохой». Разница здесь не
    косметическая: unknown приходит ровно тогда, когда лёг наш туннель, и выбрасывать
    из-за него исправные ссылки дороже, чем поставить сомнительную.
    """
    asked = [str(u).strip() for u in (body.urls or []) if str(u).strip()]
    if not asked:
        raise HTTPException(400, "нечего проверять")
    urls, seen = [], set()
    for u in asked:
        if u in seen:
            continue              # тот же ролик через дефицитный выход второй раз — нет
        seen.add(u)
        urls.append(u)
    duplicates = len(asked) - len(urls)
    c = db()
    try:
        # Канал спрашивается ради прав: у соседней `/ui/links/preview` проверка
        # принадлежности есть, и разница между двумя соседними ручками ничем не
        # объяснима. Пороги при этом общие — канальных порогов в схеме нет.
        ch = c.execute("SELECT id FROM pub_channels_live WHERE id=? AND tenant_id=?",
                       (body.channel_id, TENANT)).fetchone()
        if not ch:
            raise HTTPException(404, "канал не найден")
        limits = donor_limits(c)
    finally:
        c.close()                 # соединение закрыто ДО сети: транзакций под сетью нет

    found, gate = {}, threading.Lock()

    def one(url, number, net, deadline):
        try:
            return check_donor(url, limits=limits, net=net, deadline=deadline,
                               want_subs=number < int(limits["limit"]))
        except Exception as exc:                                   # noqa: BLE001
            item = donor_facts_blank(url, time.time())
            item["video_id"] = video_id(url)
            item.update({"fail": "ours",
                         "fail_text": f"сбой проверки: {type(exc).__name__}"})
            item.update(donor_verdict(item, limits))
            return item

    def worker():
        # ДВА прохода, и это не оптимизация, а единственный способ вообще что-то
        # показать на медленной сети. `check_donor` сначала добывает название (сеть) и
        # только потом смотрит субтитры, поэтому один медленный ответ YouTube съедает
        # весь наш срок — вместе с ответом про ролик, расшифровка которого уже лежит
        # в кэше. Сначала спрашиваем ВСЕХ по кэшу (`net=False`, мгновенно), и только
        # потом уточняем сетью тех, про кого кэш ничего не знает. Срежет нас потолок
        # ожидания — на экране всё равно будут факты, а не пустота.
        for number, url in enumerate(urls):
            item = one(url, number, False, None)
            with gate:
                found[url] = item
        # Срок сетевой части и порядок второго прохода — два отдельных решения,
        # и оба вынесены рядом с ручкой: см. `donor_net_deadline` и `donor_second_pass`.
        deadline = donor_net_deadline(time.time(), limits["budget_sec"], wall)
        with gate:
            snapshot = dict(found)
        need, cosmetic = donor_second_pass(snapshot, urls)
        for number, url in need:
            item = one(url, number, True, deadline)
            with gate:
                found[url] = item
        for number, url in cosmetic:
            # За косметикой ходим, только пока срок ещё есть: строка без названия
            # читается («название не добыли»), строка без вердикта — нет.
            if time.time() >= deadline:
                break
            item = one(url, number, True, deadline)
            with gate:
                found[url] = item

    started = time.time()
    wall = float(limits["budget_sec"] or 0) + DONOR_WALL_EXTRA
    thread = threading.Thread(target=worker, name="donor-check", daemon=True)
    thread.start()
    thread.join(wall)
    with gate:
        items = list(found.values())
    # Недоделанное добираем заглушками в том же порядке, что пришли ссылки: экран
    # показывает их строка к строке, и пропуск сдвинул бы весь список.
    checked_urls = {i["url"] for i in items}
    out = [i for i in items]
    for url in urls:
        if url not in checked_urls:
            out.append(donor_unchecked(url, limits))
    order = {u: n for n, u in enumerate(urls)}
    out.sort(key=lambda i: order.get(i["url"], 0))
    summary = {"ok": 0, "warn": 0, "reject": 0, "unknown": 0}
    for item in out:
        summary[item["verdict"]] = summary.get(item["verdict"], 0) + 1
    return {
        "donors": out,
        "donor_summary": summary,
        "donor_limits": limits,
        "spent_sec": round(time.time() - started, 2),
        "wall_sec": wall,
        "timed_out": thread.is_alive(),
        # Сколько повторов схлопнуто. Ноль — обычный случай; не ноль экран говорит
        # словами, иначе строк окажется меньше, чем ссылок, и это выглядит пропуском.
        "duplicates": duplicates,
        "donor_note": "Проверка бесплатна: квоту YouTube Data API она не тратит. "
                      "«Не смогли проверить» — это про нашу сторону (туннель, квота "
                      "выходов, время), а не про донора: такую ссылку ставить можно.",
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


def _rw(path: str, sid: str, payload=None, method=None, raw=None, ctype=None,
        extra=None):
    """Запрос к Rewrite от имени вошедшего. Возвращает (код, тело).

    `extra` — заголовки сверх обычных. Нужен ровно для одного: `Idempotency-Key`,
    которым пакетная постановка защищается от потерянного ответа на POST (сосед
    узнаёт по нему тот же заказ и не создаёт второй — rewrite/job_idempotency.py).
    """
    data = raw
    headers = {"Cookie": f"{auth.COOKIE}={sid}"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif ctype:
        headers["Content-Type"] = ctype
    headers.update(extra or {})
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


# ─────────────── задачи Rewrite/Cover, поставленные из проекта ───────────────
# Связь хранится в `project_service_jobs` (схема — в кабинете рядом с `projects`).
# Сам сервис про проект не знает; пульт записывает связь сразу после того, как
# сервис принял задачу, и по ней же показывает список во вкладке проекта.
PROJECT_JOBS_SHOWN = 30


def _project_or_404(c, pid: str) -> None:
    if not c.execute("SELECT 1 FROM projects WHERE id=? AND tenant_id=?",
                     (pid, TENANT)).fetchone():
        raise HTTPException(404, "нет такого проекта")


def _link_project_jobs(pid: str, service: str, out) -> None:
    """Записать связь «проект → задача сервиса». Задача уже принята и, возможно,
    оплачивается — поэтому сбой записи не превращаем в ошибку создания (человек нажал бы
    ещё раз и получил дубль), а только теряем строку в списке проекта."""
    ids = []
    if isinstance(out, dict):
        ids = [str(x) for x in (out.get("job_ids") or []) if str(x)]
        if not ids and out.get("job_id"):
            ids = [str(out["job_id"])]
    if not pid or not ids:
        return
    c = db()
    try:
        now = time.time()
        c.executemany("INSERT OR IGNORE INTO project_service_jobs"
                      "(tenant_id, project_id, service, job_id, created) VALUES (?,?,?,?,?)",
                      [(TENANT, pid, service, jid, now) for jid in ids])
        c.commit()
    except sqlite3.Error:
        pass
    finally:
        c.close()


# ─────────────── из чего делается проект: ссылка донора, тема, файл ───────────────
# Жалоба владельца 20.09.2026: «попробовал видос по новой запустить, ссылку на донора не
# сохранило». Так и было: `ui_text_create` превращал ссылку в текст и забывал её —
# у проекта не оставалось НИЧЕГО, из чего он сделан, и поле на экране открывалось
# пустым. Заодно от этого страдала очередь: `_name_neighbour_rows` не могла назвать
# задачу иначе как именем проекта, потому что ссылки нигде не было.
#
# Почему источник живёт своей строкой, а не в `projects.settings`. Две причины, обе
# проверены по коду кабинета, и обе стоят денег:
#   * новый проект наследует настройки последнего заведённого целиком
#     (`server/main.py` ≈4872: `SELECT settings … ORDER BY created DESC LIMIT 1`,
#     отсекаются только `_DEFERRED_KEYS`) — ссылка донора сама переехала бы в
#     следующий проект, и пакетный заказ текста оплатил бы рерайт ЧУЖОГО ролика;
#   * «снимок пресета» кладёт в пресет всё, что не начинается с подчёркивания
#     (`server/main.py` ≈14275), а «применить пресет» ЗАМЕНЯЕТ настройки проекта
#     целиком (≈14332) — источник то разъезжался бы по пресетам, то исчезал бы от
#     нажатия, к нему не относящегося.
# Таблицу пульт заводит сам и лениво — тем же способом, каким `pub_calls` заводит
# журнал платных вызовов: в кабинет (`server/**`) за схемой не ходим.
_ORIGIN_FIELDS = ("kind", "donor_url", "video_id", "topic", "file_name", "ordered")


def _origin_schema(c) -> None:
    c.execute(
        "CREATE TABLE IF NOT EXISTS project_origin("
        "tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, "
        # Чем человек пользовался последним: link | text | topic | file. Это ответ на
        # вопрос «какой источник подставить в поле», а не «что лежит в проекте».
        "kind TEXT NOT NULL DEFAULT '', "
        # Ссылка — как её вставил человек: он её такой и узнаёт.
        "donor_url TEXT NOT NULL DEFAULT '', "
        # Тот же ролик в другом написании (`youtu.be/X` и `watch?v=X`) — это один
        # ролик, и повтор заказа узнаётся по id, а не по строке.
        "video_id TEXT NOT NULL DEFAULT '', "
        "topic TEXT NOT NULL DEFAULT '', "
        "file_name TEXT NOT NULL DEFAULT '', "
        # Когда по ЭТОМУ источнику реально заказывали текст. 0 — «вставил, но не
        # заказывал»: предупреждать о повторе не о чем.
        "ordered REAL NOT NULL DEFAULT 0, "
        "updated REAL NOT NULL DEFAULT 0, "
        "PRIMARY KEY(tenant_id, project_id))")


def _origin_read(c, pid: str) -> dict:
    """Запомненный источник проекта. Нет строки или нет таблицы — пустой словарь.

    Схему тут НЕ заводим, хотя это и было бы короче: чтение — горячий путь (экран
    проекта, разбор поля на каждое нажатие), а `CREATE TABLE` это запись, и на занятой
    базе она отвечала бы «database is locked» — то есть поле на экране пустело бы под
    нагрузкой. Таблицу создаёт запись (`_origin_write`); пока её нет, читать всё равно
    нечего.
    """
    if not pid:
        return {}
    try:
        r = c.execute("SELECT * FROM project_origin WHERE tenant_id=? AND project_id=?",
                      (TENANT, pid)).fetchone()
    except sqlite3.Error:
        return {}
    return dict(r) if r else {}


def _origin_write(pid: str, **fields) -> None:
    """Запомнить источник проекта. Меняются только переданные поля.

    Сбой записи не превращаем в ошибку: заказ текста к этому моменту уже может быть
    оплачен, и отказ заставил бы человека нажать ещё раз — то есть заплатить дважды.
    Потеряем строку на экране, а не деньги.
    """
    cols = [k for k in _ORIGIN_FIELDS if k in fields]
    if not pid or not cols:
        return
    try:
        c = db()
    except HTTPException:
        return
    try:
        # Запись источника — отдельной сделкой и первой. Уборка ниже делит с ней и
        # `try`, и `commit`, и это стоило бы ровно того, ради чего всё затевалось:
        # `projects` — чужая таблица кабинета, её `DELETE … NOT IN (SELECT … FROM
        # projects)` спотыкается на занятой базе («database is locked») и на любой
        # правке чужой схемы, а один `except` на двоих откатывал бы вместе с уборкой
        # САМУ ЗАПИСЬ ССЫЛКИ — уже после того, как за текст заплатили.
        try:
            _origin_schema(c)
            c.execute(
                "INSERT INTO project_origin(tenant_id, project_id, updated, "
                + ", ".join(cols) + ") VALUES (?,?,?" + ",?" * len(cols) + ") "
                "ON CONFLICT(tenant_id, project_id) DO UPDATE SET "
                "updated=excluded.updated, "
                + ", ".join(f"{k}=excluded.{k}" for k in cols),
                (TENANT, pid, time.time(), *[fields[k] for k in cols]))
            c.commit()
        except sqlite3.Error:
            return
        # Проект удаляют в кабинете, и его триггер знает только про свою таблицу
        # (`project_service_jobs`, server/main.py ≈2588). Свой триггер в чужую схему
        # пульт не ставит: триггер переживёт нашу таблицу и сломает удаление проекта
        # всем сразу. Поэтому осиротевшие строки подметаем здесь — их одна на проект,
        # и запись бывает раз на заказ, а не в цикле. Не вышло — не беда: строка ничья,
        # никому не мешает и уйдёт со следующей записью.
        try:
            c.execute("DELETE FROM project_origin WHERE tenant_id=? AND project_id "
                      "NOT IN (SELECT id FROM projects WHERE tenant_id=?)",
                      (TENANT, TENANT))
            c.commit()
        except sqlite3.Error:
            pass
    finally:
        c.close()


def _origin_link(pid: str, url: str, *, ordered: bool = False) -> None:
    """Запомнить ссылку донора у проекта.

    `ordered` — «по ней прямо сейчас поставлен заказ текста». Ссылку запоминаем ДО
    похода за субтитрами: человек её вставил, и даже если донор не отдал расшифровку,
    поле на экране должно открыться с ней, а не пустым — вставлять второй раз незачем.
    Сменилась ссылка — прежняя отметка о заказе к ней не относится и сбрасывается.
    """
    url = (url or "").strip()
    if not pid or not url:
        return
    # Идентификатор — узким разбором (`_yt_video_id`), а не `pub_ids.video_id`: та
    # достаёт «идентификатор» из хвоста ЛЮБОГО адреса, и чужая ссылка получила бы
    # чужой id. По нему потом сверяется повтор оплаты — сверять было бы нечего.
    vid = _yt_video_id(url)
    try:
        c = db()
    except HTTPException:
        return
    try:
        row = _origin_read(c, pid)
    finally:
        c.close()
    fields = {"kind": "link", "donor_url": url, "video_id": vid}
    if ordered:
        fields["ordered"] = time.time()
    elif not _origin_same_link(row, url, vid):
        fields["ordered"] = 0.0
    _origin_write(pid, **fields)


def _origin_topic(pid: str, topic: str) -> None:
    """Запомнить тему генерации с нуля. Сменилась тема — прежний заказ не про неё."""
    topic = (topic or "").strip()
    if not pid or not topic:
        return
    try:
        c = db()
    except HTTPException:
        return
    try:
        row = _origin_read(c, pid)
    finally:
        c.close()
    fields = {"kind": "topic", "topic": topic}
    if topic != (row.get("topic") or "").strip():
        fields["ordered"] = 0.0
    _origin_write(pid, **fields)


def _origin_same_link(row: dict, url: str, vid: str = None) -> bool:
    """Та же ли это ссылка, что запомнена. По id ролика, а не по строке: `youtu.be/X`,
    `watch?v=X` и `watch?v=X&t=30` — один и тот же донор и один и тот же платёж."""
    if not row:
        return False
    url = (url or "").strip()
    vid = _yt_video_id(url) if vid is None else vid
    if vid and (row.get("video_id") or ""):
        return vid == row["video_id"]
    return bool(url) and url == (row.get("donor_url") or "")


def _release_donor(c, pid: str) -> str:
    """Ссылка донора ВЫПУСКА, если проект заведён ночным движком по плану канала.

    Тот же путь, которым её читает пакетный запуск (`_project_release`): выпуск →
    строка плана. Это другая ссылка, чем запомненная на экране, и подменять одну
    другой нельзя — по ссылке выпуска ходит движок, а деньги считаются по ней же.
    """
    if not pid:
        return ""
    try:
        r = c.execute("SELECT i.donor_url donor_url FROM pub_releases r "
                      "LEFT JOIN pub_plan_items i ON i.id=r.plan_item_id "
                      "WHERE r.project_id=? AND r.tenant_id=? "
                      "ORDER BY r.updated DESC LIMIT 1", (pid, TENANT)).fetchone()
    except sqlite3.Error:
        return ""
    return ((r["donor_url"] if r else "") or "").strip()


def _when(ts) -> str:
    """Момент по-человечески — тем же видом, что и в остальном пульте (ДД.ММ ЧЧ:ММ)."""
    try:
        return dt.datetime.fromtimestamp(float(ts)).strftime("%d.%m %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def _project_origin(c, pid: str, scenario: str = "") -> dict:
    """Из чего делается проект — одним куском для экрана.

    Экран показывает ОДНО поле источника (заявка владельца 20.09.2026), поэтому здесь
    же сказано, что в него подставить (`kind` + `value`) и почему именно это (`why`).
    Сценарий проекта (`source.txt`) остаётся отдельным полем ответа `/ui/projects/{pid}`
    — он тут не дублируется, только меряется в знаках.

    Ссылка выпуска показывается ВСЕГДА, когда она есть и её нет в самом поле, — своим
    полем `release_url`. Сравнивать её с `donor_url`, как было до 20.09.2026 вечера,
    нельзя: `donor_url` подставляется ИЗ НЕЁ ЖЕ, если своей ссылки у проекта нет, и
    первое же «Сохранить текст» (оно проставляет `kind='text'`) уводило поле в сценарий,
    а `release_url` при этом оставался пустым как «совпадающий». Ссылка выпуска
    пропадала с экрана целиком — у проекта ночного движка, то есть ровно там, где по
    ней и работают.
    """
    row = _origin_read(c, pid)
    release = _release_donor(c, pid)
    kind = (row.get("kind") or "").strip()
    donor = (row.get("donor_url") or "").strip()
    topic = (row.get("topic") or "").strip()
    src, from_release = ("project" if row else ""), False
    if not donor and release:
        # Проект заведён движком по плану канала: его источник — ссылка выпуска, и
        # другой у него не было. Это не догадка, а та же строка плана, из которой
        # ночной движок брал расшифровку.
        donor, src, from_release = release, "release", True
        if not kind:
            kind = "link"
    if not kind:
        kind = "link" if donor else ("text" if (scenario or "").strip() else "")
    ordered = float(row.get("ordered") or 0)
    # Что реально встанет в поле источника. Дальше всё считается от него, а не от
    # `donor`: у поля, занятого темой, файлом или сценарием, ссылки нет вовсе.
    value = donor if kind == "link" else (topic if kind == "topic" else "")
    aside = bool(release) and release != value
    why = ""
    if from_release and kind == "link":
        why = "ссылка выпуска канала — из неё заведён проект"
    if kind == "link" and ordered:
        why = f"по этой ссылке заказывали текст {_when(ordered)}"
    if aside:
        # Ссылка выпуска есть, а в поле её нет. Молчать нельзя: кнопки экрана возьмут
        # то, что в поле, а ночной движок и пакетный запуск — ссылку выпуска, и человек
        # получит ролик не по той.
        why = ((why + "; ") if why else "") + (
            "у выпуска канала записана другая ссылка — движок берёт её" if kind == "link"
            else "из неё заведён проект, по ней же работает ночной движок")
    return {
        "kind": kind, "value": value,
        "donor_url": donor, "video_id": (row.get("video_id") or ""),
        "topic": topic, "file_name": (row.get("file_name") or ""),
        "release_url": release if aside else "",
        "from": src, "ordered": ordered, "updated": float(row.get("updated") or 0),
        "scenario_chars": len(scenario or ""), "why": why,
    }


def _project_origin_now(pid: str) -> dict:
    """То же самое, но со своим соединением — для ответов на действие: экран сразу
    получает новое состояние поля и не обязан перезагружать проект целиком."""
    if not pid:
        return {}
    try:
        c = db()
    except HTTPException:
        return {}
    try:
        return _project_origin(c, pid, _project_scenario(sys.modules.get("main"), pid))
    except sqlite3.Error:
        return {}
    finally:
        c.close()


# ───────────────── разбор того, что человек вставил в поле источника ─────────────────
# Поле одно на все источники (заявка владельца 20.09.2026: «желательно чтобы в строку он
# и текст и ссылку распознавал»), и решать, ссылка это или текст, обязан сервер: экран,
# который судит об этом сам, рано или поздно разойдётся с сервером — и разойдётся на
# платном заказе. Разбор бесплатный и без сети: только регулярки и уже записанное в базе.
#
# Что здесь считается роликом YouTube — и почему так узко.
#
# `pub_ids.video_id` шире, чем кажется по имени: не найдя `v=`, `youtu.be/`, `/shorts/`
# или `/embed/`, она берёт ПОСЛЕДНИЙ сегмент любого адреса, если в нём одиннадцать
# разрешённых знаков. Ей это нужно для миграции (в колонке лежал голый идентификатор), и
# трогать её нельзя — она в кабинете и ею пользуются схема, план и клиент субтитров. Но
# здесь та же широта врала человеку в лицо: `example.com/aAbBcCdDeEf` объявлялся
# «ссылкой на ролик YouTube», а `donor_subs` шла с этим огрызком в YouTube — то есть
# жгла выход из общей ротации (их на всю систему два десятка, и отказ отправляет выход
# отдыхать ДЛЯ ВСЕХ) ради ролика, которого нет.
#
# Поэтому ссылка считается роликом, только если совпало ОБА: хост из списка YouTube и
# идентификатор, найденный именно шаблонами `YT_ID_RE`, а не хвостом пути.
SOURCE_TOPIC_MAX = 160          # сколько знаков ещё похоже на тему, а не на сценарий
_BARE_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
# Домены, с которых приходит ссылка на ролик. Короткие ссылки телефона (`youtu.be`),
# мобильный и музыкальный поддомены, встраивание без cookie — всё это один и тот же
# YouTube. Чужой домен сюда не попадает никогда, даже если хвост у него похож.
_YT_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")
# Два вида пути, которых в `YT_ID_RE` нет: трансляция `/live/<id>` и старый `/v/<id>`.
# Дописываем их ЗДЕСЬ, а не в кабинете: от той регулярки зависят схема, постановка
# ссылок в план и клиент субтитров, и править её ради разбора одного поля нельзя.
# Канальные пути (`/c/Имя`, `/@имя`, `/channel/UC…`) сюда не попадают намеренно:
# «SomeChannel» — тоже одиннадцать разрешённых знаков, и хвостом пути их не отличить.
_YT_PATH_RE = re.compile(r"/(?:live|v)/([A-Za-z0-9_-]{11})(?:[/?#]|$)")


def _yt_host(url: str) -> bool:
    """Это адрес YouTube? Именно хост, а не «строка, где встретилось слово youtube»:
    `evil.com/?u=youtube.com/watch?v=…` словом обмануло бы, хостом — нет."""
    try:
        host = urllib.parse.urlsplit(str(url or "")).hostname or ""
    except ValueError:
        return False
    host = host.lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in _YT_HOSTS)


def _yt_video_id(url: str) -> str:
    """Идентификатор ролика YouTube из ссылки — или пусто.

    Сужение `pub_ids.video_id` до того, за что можно отвечать: хост YouTube плюс
    идентификатор из шаблона — `v=`, `youtu.be/`, `/shorts/`, `/embed/` (`YT_ID_RE`)
    либо `/live/`, `/v/` (`_YT_PATH_RE`). Хвост пути сам по себе идентификатором не
    считается: именно на нём `pub_ids.video_id` и объявляла роликом что угодно.
    """
    url = str(url or "")
    if not _yt_host(url):
        return ""
    m = YT_ID_RE.search(url) or _YT_PATH_RE.search(url)
    return m.group(1) if m else ""


def _looks_like_id(token: str) -> bool:
    """Похоже ли это на голый идентификатор ролика.

    Только «похоже»: роликом мы такую строку НЕ считаем (см. `detect_source`) —
    одиннадцать знаков с цифрой или дефисом это и «Top-10-2026», и «Cyberpunk77», то
    есть обычная тема. Ответ нужен ровно для подсказки «если это ролик — вставьте
    ссылку целиком».
    """
    if not _BARE_ID_RE.fullmatch(token or ""):
        return False
    return not (token.isalpha() and (token.islower() or token.isupper()))


def detect_source(value: str) -> dict:
    """Что человек вставил в поле источника: ссылка, текст или ничего.

    Возвращает `kind` (`empty`/`link`/`link_other`/`many_links`/`text`), `mode` — режим
    для `POST /ui/text` (пусто означает «так заказывать нельзя») и человеческое
    объяснение. Ссылки ищутся той же `parse_links`, которой разбирается поле вставки
    ссылок в план: две разные «правды» о том, что такое ссылка, развели бы экраны.
    """
    raw = (value or "").strip()
    if not raw:
        return {"kind": "empty", "mode": "", "value": "", "url": "", "video_id": "",
                "chars": 0, "links": [], "title": "пусто", "note": "",
                "problem": "источник не задан", "topic_possible": False}
    links = parse_links(raw)
    rest = raw
    for one in links:
        rest = rest.replace(one, " ")
    only_links = bool(links) and not rest.strip(" \t\r\n,;·—–-")
    if only_links and len(links) > 1:
        return {"kind": "many_links", "mode": "", "value": raw, "url": "", "video_id": "",
                "chars": len(raw), "links": links, "title": "несколько ссылок", "note": "",
                "problem": f"в поле {len(links)} ссылки, а ролик делается из одного "
                           f"донора — оставьте одну",
                "topic_possible": False}
    if only_links:
        url = links[0]
        vid = _yt_video_id(url)
        if vid:
            return {"kind": "link", "mode": "link", "value": url, "url": url,
                    "video_id": vid, "chars": len(raw), "links": links,
                    "title": "ссылка на ролик YouTube",
                    "note": "текст возьмём из субтитров этого ролика",
                    "problem": "", "topic_possible": False}
        return {"kind": "link_other", "mode": "", "value": url, "url": url,
                "video_id": "", "chars": len(raw), "links": links,
                "title": "ссылка не на ролик YouTube", "note": "",
                "problem": "это ссылка, но ролик в ней не опознан — субтитры брать "
                           "неоткуда; вставьте ссылку вида youtube.com/watch?v=… "
                           "или сам текст",
                "topic_possible": False}
    # Всё остальное — текст. Ссылка ВНУТРИ текста его текстом быть не перестаёт:
    # сценарий со ссылкой в теле — обычное дело, и заказывать по ней донора нельзя.
    short = len(raw) <= SOURCE_TOPIC_MAX and "\n" not in raw and not links
    if links:
        note = ("в тексте есть ссылка, но текстом он быть не перестаёт: отправим в "
                "рерайт как есть. Нужен донор — оставьте в поле одну ссылку")
    elif _looks_like_id(raw):
        # Голые одиннадцать знаков роликом НЕ считаем. Раньше считали — и тема
        # «Top-10-2026» уезжала в YouTube как идентификатор: выход из общей ротации
        # сожжён, текста нет, а человек видел «ссылка на ролик» и не понимал, откуда
        # она взялась. Цена ошибки несимметрична: промах стоит выхода и платного
        # рерайта чужого ролика, а отказ — одной лишней вставки.
        note = ("похоже на идентификатор ролика. Если это ролик YouTube — вставьте "
                "ссылку целиком (youtube.com/watch?v=…): по одиннадцати знакам мы "
                "гадать не будем. Иначе это тема или текст")
    elif short:
        note = ("строка короткая — если это тема для генерации с нуля, выберите режим "
                "«С нуля по теме»: там нужен бриф")
    else:
        note = f"{len(raw)} знаков — отправим в рерайт как есть"
    return {"kind": "text", "mode": "text", "value": raw, "url": "", "video_id": "",
            "chars": len(raw), "links": links, "title": "текст", "note": note,
            "problem": "", "topic_possible": short}


def _project_job_rows(pid: str, service: str, listed: list, fetch_one) -> list:
    """Задачи проекта в порядке постановки, новые первыми.

    Список сервиса (`/jobs`) — последние N задач аккаунта; старая задача проекта могла
    из него выпасть. Её дочитываем по одной (`/jobs/{id}`), не больше PROJECT_JOBS_SHOWN.
    """
    c = db()
    try:
        _project_or_404(c, pid)
        linked = [r["job_id"] for r in c.execute(
            "SELECT job_id FROM project_service_jobs WHERE tenant_id=? AND project_id=? "
            "AND service=? ORDER BY created DESC, id DESC LIMIT ?",
            (TENANT, pid, service, PROJECT_JOBS_SHOWN))]
    finally:
        c.close()
    by_id = {str(j.get("id") or ""): j for j in listed if isinstance(j, dict)}
    out = []
    for jid in linked:
        j = by_id.get(jid)
        if j is None:
            j = fetch_one(jid)
        if isinstance(j, dict):
            out.append(j)
    return out


@app.get("/ui/text")
def ui_text(request: Request, project_id: str = ""):
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
    listed = jobs if isinstance(jobs, list) else []
    if project_id:
        def _one(jid):
            code1, j1 = _rw(f"/jobs/{urllib.parse.quote(jid)}", sid)
            if code1 != 200 or not isinstance(j1, dict):
                return None
            # Одиночная задача у Rewrite — полный dataclass, а не строка списка: поля
            # называются иначе, а лог бывает в мегабайты. Приводим к виду строки списка.
            return {"id": j1.get("id"), "type": j1.get("job_type"),
                    "status": j1.get("status"), "progress": j1.get("progress_percent"),
                    "detail": j1.get("progress_detail"), "created_at": j1.get("created_at")}
        listed = _project_job_rows(project_id, "rewrite", listed, _one)
    rows = []
    for j in listed:
        pct = j.get("progress")
        if pct is None and isinstance(j.get("progress_percent"), (int, float)):
            pct = j.get("progress_percent")
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
    if not project_id:
        rows.sort(key=lambda r: -(r["created"] or 0))   # у проекта — порядок постановки
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


class DetectIn(BaseModel):
    value: str = ""
    project_id: str = ""             # с проектом ответ ещё скажет, что тот уже знает


@app.post("/ui/source/detect")
def ui_source_detect(body: DetectIn):
    """«Вот что вставил человек — что это?» Бесплатно, без сети, до всякого заказа.

    Ответ — разбор (`detect_source`) плюс, если назван проект, то, что о его источнике
    уже записано, и предупреждение о повторе. Предупреждение здесь, а не в ответе на
    заказ, именно потому, что заказ платный: узнать «по этой ссылке уже заказывали»
    нужно ДО нажатия, а не после списания.

    Ручка ничего не записывает: разбор поля не должен менять память проекта, пока
    человек ещё печатает.
    """
    out = detect_source(body.value)
    pid = (body.project_id or "").strip()
    if not pid:
        return out
    c = db()
    try:
        _project_or_404(c, pid)
        row = _origin_read(c, pid)
        scenario = _project_scenario(sys.modules.get("main"), pid)
    finally:
        c.close()
    same = out["kind"] == "link" and _origin_same_link(row, out["url"], out["video_id"])
    ordered = float(row.get("ordered") or 0)
    warn = ""
    if same and ordered:
        warn = (f"по этой ссылке текст для проекта уже заказывали {_when(ordered)} — "
                f"повтор создаст вторую задачу и второй платный рерайт")
    elif out["kind"] == "link" and (row.get("donor_url") or "") and not same:
        warn = (f"у проекта записана другая ссылка ({_short(row['donor_url'])}) — "
                f"заказ по новой её заменит")
    if out["mode"] and scenario.strip():
        # Вторая половина путаницы, на которую жаловался владелец: сверху ссылка, снизу
        # «исходный текст какой-то». Сценарий заказом не перезаписывается — это делает
        # только «Взять в проект», и сказать об этом надо прямо.
        warn = ((warn + ". ") if warn else "") + \
            (f"в проекте уже есть сценарий ({len(scenario)} знаков): новый текст его "
             f"не заменит, пока вы не нажмёте «Взять в проект»")
    out["project"] = {
        "id": pid, "kind": (row.get("kind") or ""),
        "donor_url": (row.get("donor_url") or ""),
        "video_id": (row.get("video_id") or ""),
        "topic": (row.get("topic") or ""),
        "ordered": ordered, "same_link": same,
        "scenario_chars": len(scenario),
    }
    out["warn"] = warn
    return out


class TextIn(BaseModel):
    mode: str
    source: str = ""                 # ссылка, текст или тема — по режиму
    languages: list = []
    model: str = ""
    preset: str = ""
    channel_id: int | None = None
    project_id: str = ""             # поставлено из проекта — запомнить связь


@app.post("/ui/text")
def ui_text_create(body: TextIn, request: Request):
    """Создать задачу текста. Три источника, один экран.

    Ссылка превращается в текст здесь, кабинетом: `donor_text` уже умеет доставать
    субтитры, ходит по нескольким выходам при исчерпанной квоте и кэширует результат
    для соседних каналов. Дублировать это в интерфейсе незачем.

    Источник при этом ЗАПОМИНАЕТСЯ у проекта (жалоба владельца 20.09.2026: «ссылку на
    донора не сохранило»), и запоминается ДО похода за субтитрами: вставленное человеком
    не должно пропадать от того, что донор не отдал расшифровку.
    """
    sid = getattr(request.state, "sid", "")
    mode = (body.mode or "").strip()
    if mode not in {k for k, _t, _n in TEXT_MODES}:
        raise HTTPException(400, f"неизвестный режим «{mode}»")
    langs = [str(x).strip() for x in (body.languages or []) if str(x).strip()]
    if not langs:
        raise HTTPException(400, "не выбран ни один язык")
    pid = (body.project_id or "").strip()
    if pid:
        c0 = db()
        try:
            _project_or_404(c0, pid)     # до постановки: платная задача без проекта не нужна
        finally:
            c0.close()

    if mode == "topic":
        if not body.preset:
            raise HTTPException(400, "для генерации с нуля нужен бриф (пресет)")
        # Тему помним по той же причине, что и ссылку: это единственная запись о том,
        # из чего сделан проект, заведённый «с нуля». Текст в этом режиме пишет модель,
        # и без темы на экране не остаётся вообще ничего.
        _origin_topic(pid, body.source)
        code, out = _rw("/jobs/generation", sid, {
            "preset_name": body.preset, "topic": body.source,
            "languages": [{"code": x, "display": x} for x in langs],
            "model": body.model or "gpt-4.1-nano"})
        if code >= 400:
            raise HTTPException(_pass_code(code), out)
        _link_project_jobs(pid, "rewrite", out)
        if (body.source or "").strip():
            # Отметка об оплате — только когда есть чему её принадлежать: заказ без
            # темы пометил бы «заказывали» пустую строку и предупреждал бы о повторе
            # того, чего не было.
            _origin_write(pid, ordered=time.time())
        return {"ok": True, "job": out, "origin": _project_origin_now(pid)}

    if mode == "link":
        _origin_link(pid, body.source)
        try:
            text = donor_text(body.source)
        except Exception as exc:                                 # noqa: BLE001
            raise HTTPException(400, f"текст ролика не получен: {exc}")
        name = (video_id(body.source) or "донор") + ".txt"
    else:
        text = body.source or ""
        name = "вставленный.txt"
        # Вставленный текст своей строки не заводит и НЕ трогает запомненную ссылку:
        # его дом — `source.txt` проекта (кнопки «Сохранить текст» и «Взять в проект»),
        # а копия в памяти источника была бы вторым, расходящимся экземпляром сценария.
        # Меняется только отметка «чем пользовались последним», и только если проект
        # ещё ничем не помечен, — иначе заказ вставленным текстом стирал бы с экрана
        # ссылку, из которой этот проект сделан.
        if pid:
            c0 = db()
            try:
                known = bool((_origin_read(c0, pid).get("kind") or "").strip())
            finally:
                c0.close()
            if not known:
                _origin_write(pid, kind="text")
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
    _link_project_jobs(pid, "rewrite", out)
    if mode == "link":
        # Отметка «по этой ссылке уже заказывали» ставится только теперь: до этой
        # строки денег не тратили, и пугать повтором было бы не за что.
        _origin_link(pid, body.source, ordered=True)
    return {"ok": True, "job": out, "chars": len(text),
            "origin": _project_origin_now(pid)}


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


# Кто на самом деле рисует обложку (18.09.2026). В подписях Cover Studio провайдер
# назван только у VeoNonStop, и в списке было не понять, чем отличается «Flow — Nano Pro»
# от «Nano Banana Pro (VeoNonStop)». Карта короткая и своя, потому что у соседа таких
# подписей нет вовсе — это подпись интерфейса, а не контракт.
COVER_PROVIDER_TITLE = {
    "flow": "FastGen", "flower": "FastGen", "openai_img": "FastGen",
    "grok": "FastGen", "grok_speed": "FastGen",
    "grok_api": "xAI напрямую", "gpt_image2": "OpenAI напрямую",
    "veo": "VeoNonStop",
}

# Платит личный счёт владельца ключа, а не кредиты FastGen (решение владельца 18.09.2026:
# пометить официальный Grok и GPT Image как платные).
COVER_PAID_PROVIDERS = {"grok_api", "gpt_image2"}

# Скрытые модели — чёрным списком (решение владельца 17.09.2026: «держим список
# запрещённых»): новая модель у провайдера появится в списке сама. Гроки через FastGen
# убраны 18.09 — остаётся только официальный API.
COVER_HIDDEN = {("grok", "quality"), ("grok", "speed")}


def _cover_options(is_admin: bool) -> list:
    """Варианты генерации — из самого Cover, а не переписанные у нас.

    `GENERATION_OPTIONS` там и есть источник правды: по нему же Cover проверяет пару
    provider+model на входе. Список, разъехавшийся с проверкой, обещал бы то, что
    сервер отвергнет. Мы только прячем лишнее и дописываем в подпись провайдера и
    признак «платно» — сам список у соседа не трогаем.
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
        if (provider, model) in COVER_HIDDEN:
            continue
        paid = provider in COVER_PAID_PROVIDERS
        who = COVER_PROVIDER_TITLE.get(provider, provider)
        # Подпись собираем сами: «модель · провайдер», платное — с пометкой. Имя модели
        # берём как есть у соседа, но убираем из него скобку с провайдером, если она уже
        # там есть, — иначе выйдет «… (VeoNonStop) · VeoNonStop».
        name = re.sub(r"\s*\((VeoNonStop|OpenAI)\)\s*$", "", label).strip()
        shown = f"{name} · {who}" + (" · 💲 платно" if paid else "")
        out.append({"label": shown, "provider": provider, "model": model,
                    "admin_only": provider in admin_only, "paid": paid,
                    "provider_title": who})
    return out


@app.get("/ui/covers")
def ui_covers(request: Request, project_id: str = ""):
    """Экран обложек: стратегия, поля этой стратегии и что в ней не действует."""
    sid = getattr(request.state, "sid", "")
    user = getattr(request.state, "user", None)
    is_admin = bool(user is not None and (user["role"] if "role" in user.keys() else "")
                    == "admin")
    code, jobs = _cover("/jobs?page=1&page_size=50", sid)
    items = (jobs.get("items") if isinstance(jobs, dict) else jobs) or []
    items = items if isinstance(items, list) else []
    if project_id:
        def _one(jid):
            code1, j1 = _cover(f"/jobs/{urllib.parse.quote(jid)}", sid)
            if code1 != 200 or not isinstance(j1, dict):
                return None
            j1.pop("log", None)          # в строке списка лога нет, и тащить его незачем
            return j1
        items = _project_job_rows(project_id, "cover", items, _one)
    rows = []
    for j in items:
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
    project_id: str = ""             # поставлено из проекта — запомнить связь


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
    pid = (body.project_id or "").strip()
    if pid:
        c0 = db()
        try:
            _project_or_404(c0, pid)     # до постановки: платная задача без проекта не нужна
        finally:
            c0.close()

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
    _link_project_jobs(pid, "cover", out)
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


# ─────────────────────── настоящий лог задачи соседа ───────────────────────
# Повод, дословно: «можем добавить лог?». 20.09.2026 владелец полтора часа чинил
# интернет, которого не ломал. Заказ текста трижды падал с «Ошибка: сеть недоступна
# (E-NET)», а настоящая причина лежала строкой ниже в СЫРОМ логе задачи:
#     [SSL: CERTIFICATE_VERIFY_FAILED] … unable to get local issuer certificate
# Публичный фильтр рерайта (`rewrite/log_public.py`, `_classify_error`) по правилу
# default-deny заменяет текст исключения кодом: слово «сеть» в сообщении → `E-NET`.
# Для клиента чужого сервиса это правильно, для владельца собственной платформы —
# дыра: сама поломка чинится (`dev/run.sh` даёт связку certifi), а ВИДИМОСТЬ причины
# без этой ручки остаётся нулевой.
#
# Откуда берём лог. У соседей он лежит по-разному, и это не прихоть, а разная история:
#
#   рерайт — публичный прогресс в своей SQLite, сырой лог ФАЙЛОМ на диске
#            (`rewrite/server.py: _raw_log_path`, «наружу не отдаётся никогда»).
#            По HTTP сырого лога нет вовсе: и `GET /jobs/{id}`, и
#            `GET /api/v1/jobs/{id}?include_log=true` отдают ту самую колонку `log`,
#            то есть уже переведённый E-NET. Звать их за причиной бессмысленно —
#            получим ровно то, от чего убегаем;
#   Cover  — весь лог целиком в своей SQLite и целиком же в `GET /jobs/{id}`
#            (`_job_public_dict`, поле `log`); никакого файла у него нет.
#
# Поэтому путь один на обоих и состоит из двух шагов, и оба нужны:
#   1. спрашиваем у соседа `GET /jobs/{id}` КУКОЙ ВОШЕДШЕГО. Это одновременно
#      проверка прав (сосед сам знает, чья это задача, и отвечает 403/404 — пульт
#      её не повторяет и не может ослабить, как и в `/ui/files`), номер владельца
#      для пути к файлу и готовый запасной лог;
#   2. если у сервиса есть файл сырого лога и он читается — отдаём его хвост.
#
# Так ручка работает в обоих развёртываниях. В одном процессе (`web/platform.py`,
# `dev/run.sh` — один порт на всё) файл лежит рядом и виден шагу 2. Разнесённые по
# машинам, шаг 2 файла не находит — и тогда отдаётся то, что дал шаг 1, с прямо
# сказанным «это публичный лог, настоящий остался на машине рерайта, вот путь».
# Молча подсунуть публичный лог вместо сырого нельзя: это и есть исходная ловушка.
LOG_TAIL_DEFAULT = 64 * 1024     # хвоста в 64 КБ хватает на причину падения
LOG_TAIL_MAX = 512 * 1024        # потолок: лог задачи растёт до 20 МБ (JOB_LOG_MAX_BYTES)
# Тот же каталог и те же правила имени, что у рерайта (`rewrite/server.py`): вторая
# «правда» о том, где лежит лог, разошлась бы с первой на первой же смене переменной.
REWRITE_LOG_DIR = (os.environ.get("REWRITE_JOB_LOG_DIR")
                   or os.path.join(BASE, "rewrite", "job_logs"))
_LOG_NAME_BAD = re.compile(r"[^A-Za-z0-9_.-]")


def _log_part(value: str, default: str) -> str:
    """Кусок пути к логу — ровно как его строит рерайт, плюс запрет на «..».

    Рерайт чистит имя от всего, кроме `[A-Za-z0-9_.-]`, — точки при этом остаются, то
    есть `..` его чистку переживает. У него это безопасно (имя приходит из его же базы),
    у нас номер задачи приходит из URL, и повторять чужую выкладку буквально мы не
    обязаны: на выходе имя, которое не умеет выводить из каталога логов.
    """
    part = _LOG_NAME_BAD.sub("_", str(value or ""))[:64] or default
    return default if part in (".", "..") else part


def _redact_log(text: str) -> tuple:
    """Секреты из лога вон. Возвращает (текст, чем чистили).

    Сырой лог рерайта уже почищен на записи (`redact_server_credentials`), но только от
    СВОИХ серверных ключей. В логе задачи бывает и ответ провайдера с нашим же ключом, и
    свой ключ пользователя (BYOK), и подписанный URL, а обрезанный по дороге ключ
    («огрызок») точную замену переживает — ровно для этого в кабинете есть
    `_redact_worker_output` с `_cut_secret_prefixes`. Гоним хвост через него вторым
    проходом: редактура, которая иногда не применяется, защитой не является.

    Сетка по образцу (`pub_calls.default_redactor`) идёт ВСЕГДА и последней. Она не
    знает значений ключей, зато не зависит от того, поднят ли кабинет, — а не поднят он
    ровно в том режиме (пульт отдельным процессом), где лог и смотрят руками.
    """
    text = str(text or "")
    exact = True
    try:
        text = _cabinet()._redact_calls_error(text)
    except Exception:                                            # noqa: BLE001
        # Кабинет не поднят или редактура сломалась. Лог всё равно показываем: он для
        # того и нужен, чтобы видеть причину. Но говорим, чем именно чистили.
        exact = False
    text = pub_calls.default_redactor(text)
    return text, ("точная и по образцу" if exact else "только по образцу")


def _tail_file(path: str, limit: int) -> dict:
    """Хвост файла с потолком. Только чтение, целую строку, без падений.

    Первую строку хвоста отбрасываем, если обрезали не по границе: половина строки в
    начале лога выглядит как отдельное сообщение и врёт про причину.
    """
    size = os.path.getsize(path)
    start = max(0, size - limit)
    with open(path, "rb") as fh:
        fh.seek(start)
        raw = fh.read()
    text = raw.decode("utf-8", "replace")
    if start and "\n" in text:
        text = text.split("\n", 1)[1]
    return {"text": text, "bytes": size, "cut": start > 0}


@app.get("/ui/jobs/{service}/{job_id}/log")
def ui_job_log(service: str, job_id: str, request: Request, tail: int = 0):
    """Настоящий лог задачи соседа — тот, где написана причина, а не её код.

    Только чтение: ручка ничего не запускает, не повторяет и не стоит денег, поэтому
    её можно дёргать сколько угодно, в том числе у идущей задачи.

    `tail` — сколько байт хвоста вернуть (по умолчанию 64 КБ, потолок 512 КБ). Обрезка
    названа в ответе своим именем (`truncated`, `total_bytes`): молчаливый хвост
    заставляет искать в логе то, чего в выдаче нет. `bytes` и `total_bytes` — оба в
    БАЙТАХ, а не в знаках: лог русский, в UTF-8 это два байта на букву, и «отдали 6395
    из 9174» с разными единицами выглядело бы как потеря трети лога.
    """
    base = RESULT_BASES.get(service)
    if not base:
        raise HTTPException(404, f"нет сервиса «{service}»")
    limit = max(4096, min(int(tail or LOG_TAIL_DEFAULT), LOG_TAIL_MAX))
    # Шаг 1. Права, владелец и запасной лог — одним вопросом у самого соседа.
    # `safe=""` — слэш в номере задачи тоже кодируем: иначе «a/b» ушло бы соседу
    # отдельным сегментом пути и спросило бы у него не ту ручку.
    code, job = _proxy_get(base, f"/jobs/{urllib.parse.quote(job_id, safe='')}",
                           getattr(request.state, "sid", ""))
    if code >= 400:
        raise HTTPException(_pass_code(code), job)
    if not isinstance(job, dict):
        raise HTTPException(502, f"сервис «{service}» ответил не задачей")
    out = {"service": service, "job_id": job_id,
           "status": str(job.get("status") or ""),
           "error": _short(job.get("error") or "", 300),
           "source": "", "why": "", "problem": "", "path": "",
           "text": "", "bytes": 0, "chars": 0, "total_bytes": 0, "truncated": False,
           "redacted": ""}
    # Шаг 2. Файл сырого лога — он есть только у рерайта и только на его машине.
    path = ""
    if service == "rewrite":
        path = os.path.join(REWRITE_LOG_DIR,
                            _log_part(job.get("user_id"), "anon"),
                            _log_part(job_id, "job") + ".log")
        out["path"] = path
    if path and os.path.isfile(path):
        try:
            got = _tail_file(path, limit)
        except OSError as exc:
            # Внятно, а не пустотой: человек должен понимать, идти ли ему на машину
            # рерайта руками или файла там и нет.
            out["problem"] = (f"сырой лог есть, но не прочитан ({type(exc).__name__}): "
                              f"{path}")
        else:
            out.update(source="raw", text=got["text"], total_bytes=got["bytes"],
                       truncated=got["cut"],
                       why="сырой лог задачи с диска — то, что писал сам пайплайн")
    if not out["source"]:
        # Запасной путь: публичный лог из ответа соседа. Для Cover он же и
        # единственный — и он же полный: публичного фильтра у Cover нет.
        text = str(job.get("log") or job.get("log_tail") or "")
        # Размер меряем ДО обрезки: `total_bytes` отвечает на вопрос «сколько всего
        # лога», и посчитанный после хвоста он всегда равнялся бы самому хвосту, то
        # есть `truncated: true` при равных числах — ложь на ровном месте.
        whole = len(text.encode("utf-8"))
        if len(text) > limit:
            text, out["truncated"] = text[-limit:], True
        out.update(source=("service" if service == "cover" else "public"),
                   text=text, total_bytes=whole)
        out["why"] = ("лог задачи, как его отдаёт Cover: у него он один и не фильтруется"
                      if service == "cover" else
                      "ПУБЛИЧНЫЙ лог: настоящая причина заменена кодом (E-NET, E-TIMEOUT "
                      "и т. п.). Сырой лежит файлом на машине рерайта — "
                      f"{out['path'] or REWRITE_LOG_DIR}")
        if service == "rewrite" and not out["problem"]:
            out["problem"] = ("файла сырого лога здесь нет: либо рерайт работает на "
                              "другой машине, либо задача ещё ничего не писала")
    if not out["text"]:
        # Пустой ответ сам по себе ничего не объясняет: «ничего не показали» и «нечего
        # показывать» человек различить не может, а разница между ними — это разница
        # между «ищи дальше» и «задача ещё не начиналась».
        out["problem"] = ((out["problem"] + ". ") if out["problem"] else "") + \
            "лог пуст: задача ещё не начиналась или ничего не записала"
    out["text"], out["redacted"] = _redact_log(out["text"])
    # Причину отказа сосед отдаёт отдельным полем, и это тоже текст провайдера — путь
    # в обход редактуры был бы ровно той дырой, ради которой она здесь стоит.
    out["error"] = _redact_log(out["error"])[0]
    out["bytes"] = len(out["text"].encode("utf-8"))
    out["chars"] = len(out["text"])
    return out


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
                f"SELECT name FROM pub_channels_live WHERE tenant_id=? AND {column}=?",
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


# Поля пресетов соседей, которые пульт даёт править (18.09.2026). Раньше их можно было
# только скопировать или удалить, а настроить — уйти в чужой интерфейс, которого по ТЗ
# быть не должно. Список намеренно короткий: ровно то, что есть в формах самих соседей.
# Всё остальное, что лежит в пресете (в т.ч. поля, похожие на секреты), пульт не
# показывает и не трогает — оно остаётся в пресете как было, правка его не стирает.
NEIGHBOUR_PRESET_FIELDS = {
    "cover": (
        # Режим «по превью донора» — им работает автоконвейер канала
        # (server/pub_remote.py `CoverClient.create_job`, mode=url), и до 18.09.2026 в
        # пульте не было ни одного его поля: правились только поля режима «по семени».
        {"key": "simple_mode", "kind": "switch", "label": "Редактировать превью донора",
         "hint": "включено — модель правит само превью (перевод текста, позы); "
                 "выключено — анализ превью и сборка промпта"},
        {"key": "style_prompt", "kind": "text", "label": "Промпт стиля",
         "hint": "действует, когда «стиль автоматически» выключен"},
        {"key": "use_auto_style", "kind": "switch", "label": "Стиль автоматически",
         "hint": "включено — стиль берётся из превью донора, промпт стиля не читается"},
        {"key": "analysis", "kind": "select", "label": "Чем анализировать превью"},
        {"key": "fallback_mode", "kind": "select", "label": "Если текст не лёг"},
        {"key": "max_generation_retries", "kind": "number", "label": "Попыток генерации",
         "min": 1, "max": 20},
        # Режим «по семени» — им человек делает обложки руками на экране обложек.
        {"key": "prompt_template", "kind": "text", "label": "Шаблон промпта",
         "hint": "постоянные правила стиля. {ЯЗЫК} — название языка, {СЕМЯ} — описание "
                 "конкретной обложки. Пусто — семя станет полным промптом"},
        {"key": "seed_n_variants", "kind": "number", "label": "Обложек на каждый язык",
         "min": 1, "max": 8},
        {"key": "generation", "kind": "select", "label": "Модель генерации",
         "hint": "кем рисуется обложка"},
        {"key": "seed_reference_image", "kind": "file", "label": "Образец стиля",
         "hint": "картинка-референс для всех языков: модель получает её напрямую и "
                 "повторяет стиль. JPG/PNG до 20 МБ"},
    ),
    "rewrite": (
        {"key": "model", "kind": "select", "label": "Модель"},
        {"key": "chunk", "kind": "number", "label": "Размер куска, знаков",
         "min": 500, "max": 20000,
         "hint": "текст режется на куски такой длины — по абзацам"},
        {"key": "threads", "kind": "number", "label": "Потоков", "min": 1, "max": 8},
        {"key": "address", "kind": "text", "label": "Обращение",
         "hint": "«вы», «ты» — как рерайт обращается к зрителю"},
        {"key": "preserve_paragraphs", "kind": "switch", "label": "Сохранять абзацы"},
        {"key": "safe_text", "kind": "switch", "label": "Безопасный текст"},
        {"key": "languages", "kind": "langrows", "label": "Языки и ведущие",
         "hint": "для каждого языка — имя ведущего и название канала: их рерайт "
                 "подставляет в приветствие"},
    ),
    "rewrite_gen": (
        {"key": "content", "kind": "text", "label": "Бриф",
         "hint": "о чём и как писать текст с нуля"},
        {"key": "target_length_words", "kind": "number", "label": "Длина, слов",
         "min": 0, "max": 20000},
    ),
}


def _neighbour_preset_options(service: str, sid: str, request) -> dict:
    """Варианты для выпадающих списков — у самого соседа, не переписанные здесь."""
    if service == "cover":
        user = getattr(request.state, "user", None)
        is_admin = bool(user is not None
                        and (user["role"] if "role" in user.keys() else "") == "admin")
        opts = [{"value": f'{o["provider"]}|{o["model"]}', "title": o["label"]}
                for o in _cover_options(is_admin)]
        # Анализ превью и поведение при неудачной подстановке текста: значения те же,
        # что принимает сам Cover (`cover/api_cover.py`), подписи — человеческие.
        analysis = [{"value": "openai|gpt-4.1-mini", "title": "OpenAI · gpt-4.1-mini"},
                    {"value": "openai|gpt-4o", "title": "OpenAI · gpt-4o"},
                    {"value": "google|gemini-2.0-flash", "title": "Google · gemini-2.0-flash"}]
        fallback = [{"value": "auto", "title": "как решит сервис"},
                    {"value": "always_overlay", "title": "всегда накладывать текст"},
                    {"value": "never_overlay", "title": "никогда не накладывать"}]
        return {"generation": opts, "analysis": analysis, "fallback_mode": fallback}
    if service == "rewrite":
        code, models = _rw("/models", sid)
        items = models if code == 200 else []
        out = []
        for m in (items if isinstance(items, list) else []):
            val = m.get("id") or m.get("name") or m.get("value") if isinstance(m, dict) else m
            if not val:
                continue
            title = (m.get("title") or m.get("label") or val) if isinstance(m, dict) else val
            out.append({"value": str(val), "title": str(title)})
        return {"model": out}
    return {}


# Умолчания нового пресета соседа: ровно то, что он подставил бы сам, если поле не
# прислать. Держим их здесь, чтобы форма заведения не показывала пустоту там, где у
# сервиса есть осмысленное значение.
NEIGHBOUR_PRESET_DEFAULTS = {
    "cover": {"seed_n_variants": 3, "prompt_template": "", "use_auto_style": True,
              "simple_mode": False, "fallback_mode": "auto", "aspect_ratio": "16:9",
              "max_generation_retries": 5, "analysis_provider": "openai",
              "analysis_model": "gpt-4.1-mini"},
    "rewrite": {"model": "gpt-4.1-nano", "chunk": 3500, "threads": 1, "address": "",
                "preserve_paragraphs": True, "safe_text": False, "languages": []},
    "rewrite_gen": {"content": "", "target_length_words": ""},
}


def _neighbour_preset_fields(service: str, cfg: dict, options: dict) -> list:
    """Поля формы пресета соседа по его настройкам. Одна сборка на правку и на
    заведение (18.09.2026): две разошлись бы на первом же новом поле."""
    fields = []
    for f in NEIGHBOUR_PRESET_FIELDS.get(service, ()):
        item = dict(f)
        if f["key"] == "generation":
            item["value"] = f'{cfg.get("generation_provider") or "flow"}|' \
                            f'{cfg.get("generation_model") or ""}'
        elif f["key"] == "analysis":
            item["value"] = f'{cfg.get("analysis_provider") or "openai"}|' \
                            f'{cfg.get("analysis_model") or "gpt-4.1-mini"}'
        elif f["kind"] == "langrows":
            item["value"] = [{"code": str(x.get("code") or ""),
                              "host": str(x.get("host") or ""),
                              "channel": str(x.get("channel") or "")}
                             for x in (cfg.get("languages") or [])
                             if isinstance(x, dict)]
        elif f["kind"] == "file":
            item["value"] = str(cfg.get(f["key"]) or "")
            item["filename"] = str(cfg.get("seed_reference_filename") or "")
        else:
            val = cfg.get(f["key"])
            item["value"] = "" if val is None else val
        item["options"] = options.get(f["key"], [])
        if f["kind"] == "select" and not item["options"]:
            # Список неоткуда взять — поле не рисуем: пустой выпадающий список при
            # сохранении стёр бы модель, которую человек не менял.
            continue
        fields.append(item)
    return fields


def _neighbour_preset_read(service: str, name: str, sid: str, request) -> dict:
    if service == "rewrite_gen":
        # У брифов есть своя ручка одного пресета: список отдаёт только сводку (id, имя,
        # длина), без самого текста, и править бриф по сводке было бы правкой вслепую.
        code, cfg = _rw(f"/presets/{urllib.parse.quote(name)}", sid)
        if code != 200 or not isinstance(cfg, dict):
            raise HTTPException(404, f"нет брифа «{name}»")
    else:
        store, _put = _preset_store(service, sid, request)
        if name not in store:
            raise HTTPException(404, f"нет пресета «{name}»")
        cfg = store[name] if isinstance(store[name], dict) else {}
    return {"service": service, "name": name,
            "fields": _neighbour_preset_fields(
                service, cfg, _neighbour_preset_options(service, sid, request))}


def _neighbour_preset_write(service: str, name: str, values: dict, sid: str, request) -> None:
    if service == "rewrite_gen":
        return _rewrite_gen_write(name, values, sid)
    store, put = _preset_store(service, sid, request)
    if name not in store:
        # Правка — только существующего: иначе опечатка в имени молча заводила бы
        # третий пресет, на который никто не ссылается. Новый заводится «Копией».
        raise HTTPException(404, f"нет пресета «{name}»")
    cfg = dict(store.get(name) or {}) if isinstance(store.get(name), dict) else {}
    known = {f["key"]: f for f in NEIGHBOUR_PRESET_FIELDS.get(service, ())}
    unknown = [k for k in values if k not in known]
    if unknown:
        raise HTTPException(400, "не поля этого пресета: " + ", ".join(sorted(unknown)))
    for key, raw in values.items():
        f = known[key]
        if key == "generation":
            provider, _sep, model = str(raw or "").partition("|")
            if not provider:
                raise HTTPException(400, "не выбрана модель генерации")
            cfg["generation_provider"], cfg["generation_model"] = provider, model
        elif key == "analysis":
            provider, _sep, model = str(raw or "").partition("|")
            if not provider:
                raise HTTPException(400, "не выбрано, чем анализировать превью")
            cfg["analysis_provider"], cfg["analysis_model"] = provider, model
        elif f["kind"] == "number":
            try:
                num = int(str(raw).strip() or 0)
            except ValueError:
                raise HTTPException(400, f"{f['label']}: нужно целое число")
            lo, hi = f.get("min", 0), f.get("max", 10 ** 9)
            if not (lo <= num <= hi):
                raise HTTPException(400, f"{f['label']}: допустимо от {lo} до {hi}")
            cfg[key] = num
        elif f["kind"] == "switch":
            cfg[key] = str(raw).strip().lower() in ("1", "true", "on", "да")
        elif f["kind"] == "langrows":
            # Правим только свои поля строки. У языка бывают и чужие (`voice` — чем
            # озвучивать), и стереть их формой, которая их не показывает, нельзя.
            was = {str((x or {}).get("code") or "").strip().lower(): dict(x)
                   for x in (cfg.get(key) or []) if isinstance(x, dict)}
            rows = []
            for x in (raw if isinstance(raw, list) else []):
                code = str((x or {}).get("code") or "").strip().lower()
                if not code:
                    continue
                row = dict(was.get(code) or {})
                row["code"] = code
                for fld in ("host", "channel"):
                    val = str((x or {}).get(fld) or "").strip()[:80]
                    # Пустое поле — это «не задано», а не пустая строка: пустой ключ в
                    # строке языка потом не отличить от заданного пустым.
                    if val:
                        row[fld] = val
                    else:
                        row.pop(fld, None)
                rows.append(row)
            cfg[key] = rows
        else:
            cfg[key] = str(raw)
    if service == "rewrite_gen":
        cfg["name"] = name
    store[name] = cfg
    put(store)


@app.post("/ui/presets/cover/{name}/reference")
async def ui_cover_preset_reference(name: str, request: Request,
                                    file: UploadFile = File(...)):
    """Положить картинку-образец стиля в пресет обложки.

    Файл хранит Cover Studio (`POST /api/v1/files`) и отвечает `file_id` — его и пишем
    в пресет. Своей копии файла пульт не заводит: второе хранилище разошлось бы с тем,
    что реально читает генерация.
    """
    data = await file.read()
    if not data:
        raise HTTPException(400, "файл пустой")
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413, "файл больше 20 МБ")
    c = db()
    try:
        key = get_key(c, "cover")
    finally:
        c.close()
    if not key:
        raise HTTPException(503, "ключ доступа к Cover Studio не сохранён")
    boundary = "----studio" + secrets.token_hex(8)
    head = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; '
            f'filename="{os.path.basename(file.filename or "reference")}"\r\n'
            f"Content-Type: {file.content_type or 'application/octet-stream'}\r\n\r\n"
            ).encode("utf-8")
    raw = head + data + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(f"{COVER_BASE}/api/v1/files", data=raw, method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=NEIGHBOUR_TIMEOUT * 5) as resp:
            out = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            detail = (json.loads(body or "{}") or {}).get("detail") or body[:200]
        except ValueError:
            detail = body[:200]
        raise HTTPException(_pass_code(exc.code), detail)
    except Exception as exc:                                       # noqa: BLE001
        raise HTTPException(502, f"обложки не отвечают: {type(exc).__name__}")
    file_id = str(out.get("file_id") or "")
    if not file_id:
        raise HTTPException(502, "Cover Studio не вернула идентификатор файла")
    sid = getattr(request.state, "sid", "")
    store, put = _preset_store("cover", sid, request)
    cfg = dict(store.get(name) or {})
    if not cfg and name not in store:
        raise HTTPException(404, f"нет пресета «{name}»")
    cfg["seed_reference_image"] = file_id
    cfg["seed_reference_filename"] = str(out.get("filename") or file.filename or "")
    store[name] = cfg
    put(store)
    return {"ok": True, "file_id": file_id, "filename": cfg["seed_reference_filename"]}


def _rewrite_gen_write(name: str, values: dict, sid: str, create: bool = False) -> None:
    """Бриф генерации пишет сама Rewrite Studio своей ручкой `POST /presets/{name}`.

    Через общий `/presets/sync` писать нельзя: список брифов отдаёт сводку без текста,
    и полная замена набора стёрла бы содержимое всех остальных брифов.
    """
    known = {f["key"] for f in NEIGHBOUR_PRESET_FIELDS["rewrite_gen"]}
    unknown = [k for k in values if k not in known]
    if unknown:
        raise HTTPException(400, "не поля этого пресета: " + ", ".join(sorted(unknown)))
    code, cfg = _rw(f"/presets/{urllib.parse.quote(name)}", sid)
    was = cfg if code == 200 and isinstance(cfg, dict) else {}
    if not was and not create:
        raise HTTPException(404, f"нет брифа «{name}»")
    body = {"name": name,
            "content": str(values.get("content", was.get("content") or "")),
            "target_length_words": values.get("target_length_words",
                                              was.get("target_length_words") or "")}
    if not str(body["content"]).strip():
        raise HTTPException(400, "бриф пустой: рерайту нечем работать")
    try:
        body["target_length_words"] = (int(str(body["target_length_words"]).strip())
                                       if str(body["target_length_words"]).strip() else None)
    except ValueError:
        raise HTTPException(400, "длина: нужно целое число")
    code, out = _rw(f"/presets/{urllib.parse.quote(name)}", sid, body)
    if code >= 400:
        raise HTTPException(_pass_code(code), out)


# ─────────────────── шаблоны озвучки Lumean (18.09.2026) ───────────────────
# Решение владельца: шаблоны собираются прямо у нас. Провайдер это позволяет —
# `POST /templates` (обязательны service_key, name, config) возвращает шаблон вместе с
# id, у шаблона есть PATCH и DELETE (проверено живыми запросами 18.09). Поля те же, что
# у настроек озвучки в пресете: второго словаря имён не заводим.
LUMEAN_SERVICES = (("elevenlabs", "ElevenLabs TTS"), ("heygen", "HeyGen"), ("sfx", "Звуки"))
LUMEAN_TEMPLATE_FIELDS = (
    {"key": "name", "kind": "text", "label": "Название"},
    {"key": "service_key", "kind": "select", "label": "Сервис",
     "hint": "чем озвучивать; маршрут кабинета «Lumean · только HeyGen» ждёт heygen"},
    {"key": "voice_id", "kind": "text", "label": "ID голоса",
     "hint": "голос из библиотеки Lumean — обязателен"},
    {"key": "model_id", "kind": "select", "label": "Модель"},
    {"key": "stability", "kind": "number", "label": "Стабильность", "min": 0, "max": 1},
    {"key": "similarity_boost", "kind": "number", "label": "Схожесть", "min": 0, "max": 1},
    {"key": "style", "kind": "number", "label": "Стиль", "min": 0, "max": 1},
    {"key": "speed", "kind": "number", "label": "Скорость", "min": 0.5, "max": 2},
    {"key": "use_speaker_boost", "kind": "switch", "label": "Усиление голоса"},
    {"key": "apply_text_normalization", "kind": "select", "label": "Нормализация текста"},
    {"key": "chunk_size", "kind": "number", "label": "Размер куска, знаков",
     "min": 100, "max": 20000},
    {"key": "pauses", "kind": "switch", "label": "Паузы"},
    {"key": "stress", "kind": "switch", "label": "Ударения"},
)
LUMEAN_TEMPLATE_DEFAULTS = {"service_key": "elevenlabs", "model_id": "eleven_v3",
                            "stability": 0.5, "similarity_boost": 0.75, "style": 0,
                            "speed": 1, "use_speaker_boost": "1",
                            "apply_text_normalization": "auto", "chunk_size": 2000,
                            "pauses": "", "stress": ""}


def _lumean_field_options(mod) -> dict:
    models = [{"value": v, "title": t} for v, t in
              (getattr(preset_fields, "_lumean_models")(mod) or []) if v]
    norm = [{"value": v, "title": t} for v, t in
            (getattr(preset_fields, "_lumean_normalization")(mod) or []) if v]
    return {"model_id": models, "apply_text_normalization": norm,
            "service_key": [{"value": k, "title": t} for k, t in LUMEAN_SERVICES]}


def _lumean_template_form(mod, values: dict) -> list:
    options = _lumean_field_options(mod)
    out = []
    for f in LUMEAN_TEMPLATE_FIELDS:
        item = dict(f)
        val = values.get(f["key"], "")
        item["value"] = "" if val is None else val
        item["options"] = options.get(f["key"], [])
        out.append(item)
    return out


def _lumean_template_values(info: dict) -> dict:
    """Шаблон провайдера → значения полей формы. Имена у него и у нас совпадают, но
    лежат на разной глубине: `config.tts_settings.voice_settings.*`."""
    cfg = info.get("config") if isinstance(info.get("config"), dict) else {}
    tts = cfg.get("tts_settings") if isinstance(cfg.get("tts_settings"), dict) else {}
    vs = tts.get("voice_settings") if isinstance(tts.get("voice_settings"), dict) else {}
    worker = cfg.get("worker_settings") if isinstance(cfg.get("worker_settings"), dict) else {}
    pause = cfg.get("pause_settings") if isinstance(cfg.get("pause_settings"), dict) else {}
    stress = cfg.get("stress_settings") if isinstance(cfg.get("stress_settings"), dict) else {}
    return {"name": info.get("name") or "", "service_key": info.get("service_key") or "",
            "voice_id": tts.get("voice_id") or "", "model_id": tts.get("model_id") or "",
            "stability": vs.get("stability", ""), "similarity_boost": vs.get("similarity_boost", ""),
            "style": vs.get("style", ""), "speed": vs.get("speed", ""),
            "use_speaker_boost": "1" if vs.get("use_speaker_boost") else "0",
            "apply_text_normalization": tts.get("apply_text_normalization") or "",
            "chunk_size": worker.get("chunk_size", ""),
            "pauses": "1" if pause.get("enabled") else "0",
            "stress": "1" if stress.get("enabled") else "0"}


def _lumean_template_payload(values: dict) -> dict:
    """Значения формы → тело запроса к Lumean. Проверяем здесь, а не у провайдера:
    отказ после отправки читается хуже, чем понятная фраза до неё."""
    name = str(values.get("name") or "").strip()[:120]
    if not name:
        raise HTTPException(400, "пустое название шаблона")
    service = str(values.get("service_key") or "elevenlabs").strip()
    if service not in {k for k, _t in LUMEAN_SERVICES}:
        raise HTTPException(400, f"неизвестный сервис «{service}»")
    voice = str(values.get("voice_id") or "").strip()
    if not voice:
        raise HTTPException(400, "не задан ID голоса — шаблон без голоса не работает")
    # Формат идентификатора у сервисов РАЗНЫЙ, и перепутать их проще всего: у ElevenLabs
    # это 20 знаков букв и цифр, у HeyGen — 32 шестнадцатеричных. Провайдер на такую
    # путаницу отвечает «ID голоса указан в неверном формате», не говоря, чей это формат
    # и что с ним делать. Проверяем здесь и называем вещи своими именами (20.09.2026:
    # владелец взял голос HeyGen к сервису ElevenLabs и получил ровно этот тупик).
    # Отказываем ТОЛЬКО когда id заведомо принадлежит ДРУГОМУ сервису. Формат «не узнали»
    # пропускаем к провайдеру: он и есть власть над форматом, а вторым валидатором мы
    # только начнём отвергать то, чего ещё не знаем (новый сервис, новая форма id).
    LOOKS = {"elevenlabs": (re.compile(r"^[A-Za-z0-9]{20}$"), "ElevenLabs"),
             "heygen": (re.compile(r"^[0-9a-f]{32}$"), "HeyGen")}
    foreign = next((title for key, (rx, title) in LOOKS.items()
                    if key != service and rx.match(voice)), "")
    if foreign and service in LOOKS:
        raise HTTPException(400, f"это идентификатор {foreign}, а сервис выбран "
                                 f"«{LOOKS[service][1]}»: возьмите голос выбранного "
                                 f"сервиса или смените сервис")
    voice_settings, tts = {}, {"voice_id": voice}
    for key, lo, hi in (("stability", 0.0, 1.0), ("similarity_boost", 0.0, 1.0),
                        ("style", 0.0, 1.0), ("speed", 0.5, 2.0)):
        raw = str(values.get(key, "")).strip().replace(",", ".")
        if raw == "":
            continue
        try:
            val = float(raw)
        except ValueError:
            raise HTTPException(400, f"{key}: нужно число от {lo} до {hi}")
        if not (lo <= val <= hi):
            raise HTTPException(400, f"{key}: допустимо от {lo} до {hi}")
        voice_settings[key] = val
    boost = str(values.get("use_speaker_boost", "")).strip().lower()
    if boost:
        voice_settings["use_speaker_boost"] = boost in ("1", "true", "on", "да")
    model = str(values.get("model_id") or "").strip()
    if model:
        tts["model_id"] = model
    norm = str(values.get("apply_text_normalization") or "").strip().lower()
    if norm:
        if norm not in ("auto", "on", "off"):
            raise HTTPException(400, "нормализация текста — auto, on или off")
        tts["apply_text_normalization"] = norm
    # Стабильность провайдер требует ВСЕГДА («Необходимо указать параметр стабильности»),
    # поэтому раздел настроек голоса уходит даже когда человек ничего не трогал.
    voice_settings.setdefault("stability", 0.5)
    tts["voice_settings"] = voice_settings
    # Поля, которые сервис считает обязательными и у которых допустимо РОВНО ОДНО
    # значение (проверено обращением к провайдеру 20.09.2026: mode_v2/mode_v3 и
    # написания ElevenLabs/elevenlabs он отвергает). Спрашивать у человека нечего —
    # подставляем сами, иначе форма отдавала бы заведомо неполный шаблон и падала на
    # валидации у провайдера, как это и было.
    if service == "elevenlabs":
        tts["mode"] = "mode_v1"
    elif service == "heygen":
        tts["voice_engine"] = "elevenLabs"
    config = {"tts_settings": tts,
              # Формат вывода задаём сами: кабинет собирает ролик из mp3, и шаблон,
              # отдающий wav, ломает сборку молча.
              "output_settings": {"audio_format": "mp3"}}
    chunk = str(values.get("chunk_size", "")).strip()
    if chunk:
        try:
            num = int(chunk)
        except ValueError:
            raise HTTPException(400, "размер куска — целое число знаков")
        if not (100 <= num <= 20000):
            raise HTTPException(400, "размер куска — от 100 до 20000 знаков")
        config["worker_settings"] = {"chunk_size": num}
    for key, section in (("pauses", "pause_settings"), ("stress", "stress_settings")):
        raw = str(values.get(key, "")).strip().lower()
        if raw:
            config[section] = {"enabled": raw in ("1", "true", "on", "да")}
    return {"service_key": service, "name": name, "config": config}


def _lumean_users(template_id: str) -> list:
    """Кто ссылается на этот шаблон: пресеты и проекты. Нужно перед удалением —
    у них озвучка встанет в момент запуска, а не сейчас."""
    tid = (template_id or "").strip().lower()
    if not tid:
        return []
    out = []
    c = db()
    try:
        rows = list(c.execute("SELECT id, name, settings FROM presets WHERE tenant_id=?",
                              (TENANT,)))
        rows += list(c.execute("SELECT id, name, settings FROM projects WHERE tenant_id=? "
                               "AND settings IS NOT NULL AND settings!=''", (TENANT,)))
    finally:
        c.close()
    for r in rows:
        try:
            st = json.loads(r["settings"] or "{}")
        except ValueError:
            continue
        used = {str(st.get("voice") or "").strip().lower(),
                str(st.get("voice_uuid") or "").strip().lower()}
        used |= {str(v).strip().lower()
                 for v in preset_fields.parse_voice_map(st.get("voice_by_lang")).values()}
        if tid in used:
            out.append(r["name"] or str(r["id"]))
    return out


def _lumean_mod():
    mod = sys.modules.get("main")
    if mod is None or not hasattr(mod, "lumean_templates"):
        raise HTTPException(503, "кабинет не поднят")
    return mod


@app.get("/ui/lumean/templates")
def ui_lumean_templates():
    """Шаблоны озвучки Lumean и поля формы для нового."""
    mod = _lumean_mod()
    got = mod.lumean_templates()
    return {"ok": got.get("ok", False), "why": got.get("why", ""),
            "templates": got.get("templates", []),
            "services": [{"value": k, "title": t} for k, t in LUMEAN_SERVICES],
            "fields": _lumean_template_form(mod, dict(LUMEAN_TEMPLATE_DEFAULTS)),
            "note": "шаблон живёт в аккаунте Lumean: правка меняет его и там"}


@app.get("/ui/lumean/templates/{tid}/form")
def ui_lumean_template_form(tid: str):
    mod = _lumean_mod()
    info = mod.lumean_template_info(tid)
    if not info.get("ok"):
        raise HTTPException(404, info.get("why") or "шаблон не прочитался")
    raw = mod._lumean_call("GET", f"/templates/{tid}")[1]
    full = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    return {"id": tid, "fields": _lumean_template_form(mod, _lumean_template_values(full)),
            "used_by": _lumean_users(tid)}


class LumeanTemplateIn(BaseModel):
    values: dict = {}


@app.post("/ui/lumean/templates")
def ui_lumean_template_create(body: LumeanTemplateIn):
    """Завести шаблон в Lumean. Ответ провайдера содержит id — возвращаем его, чтобы
    человеку не приходилось копировать идентификатор руками."""
    mod = _lumean_mod()
    out = mod.lumean_template_save(_lumean_template_payload(body.values or {}))
    if not out.get("ok"):
        raise HTTPException(502, out.get("why") or "Lumean не принял шаблон")
    return {"ok": True, "id": out.get("id"), "name": out.get("name")}


@app.post("/ui/lumean/templates/{tid}")
def ui_lumean_template_update(tid: str, body: LumeanTemplateIn):
    mod = _lumean_mod()
    out = mod.lumean_template_save(_lumean_template_payload(body.values or {}), tid)
    if not out.get("ok"):
        raise HTTPException(502, out.get("why") or "Lumean не принял правку")
    return {"ok": True, "id": tid}


@app.delete("/ui/lumean/templates/{tid}")
def ui_lumean_template_remove(tid: str):
    """Удалить шаблон у провайдера. Если на него ссылается пресет или проект — сначала
    сказать, какой: озвучка у них встанет в момент запуска, а не сейчас."""
    used = _lumean_users(tid)
    if used:
        raise HTTPException(409, "на него ссылаются: " + ", ".join(used[:10])
                            + ". Сначала переключите их на другой шаблон.")
    out = _lumean_mod().lumean_template_delete(tid)
    if not out.get("ok"):
        raise HTTPException(502, out.get("why") or "Lumean не удалил шаблон")
    return {"ok": True}


@app.get("/ui/lumean/voices")
def ui_lumean_voices(service: str = "", search: str = ""):
    """Голоса Lumean для подстановки в поле шаблона."""
    return _lumean_mod().lumean_voices(service=service, search=search)


@app.get("/ui/lumean/template")
def ui_lumean_template(id: str = ""):
    """Проверить шаблон Lumean до оплаты (18.09.2026).

    Сегодня id шаблона вписывают вслепую, и ошибка вскрывается платным заказом. Читает
    кабинет своим ключом (`lumean_template_info`) — своего похода к провайдеру у пульта
    нет, как и своего ключа.
    """
    mod = sys.modules.get("main")
    if mod is None or not hasattr(mod, "lumean_template_info"):
        raise HTTPException(503, "кабинет не поднят")
    return mod.lumean_template_info(id)


@app.get("/ui/presets/{service}/new/form")
def ui_neighbour_preset_new_form(service: str, request: Request):
    """Поля для НОВОГО пресета соседа (18.09.2026). До этого завести пресет обложек или
    рерайта из пульта было нечем — только скопировать существующий."""
    if service not in NEIGHBOUR_PRESET_FIELDS:
        raise HTTPException(404, f"у сервиса «{service}» своих пресетов нет")
    sid = getattr(request.state, "sid", "")
    cfg = dict(NEIGHBOUR_PRESET_DEFAULTS.get(service) or {})
    return {"service": service, "name": "",
            "fields": _neighbour_preset_fields(
                service, cfg, _neighbour_preset_options(service, sid, request))}


class NeighbourPresetNew(BaseModel):
    name: str = ""
    values: dict = {}


@app.post("/ui/presets/{service}/new")
def ui_neighbour_preset_create(service: str, body: NeighbourPresetNew, request: Request):
    """Завести пресет у соседа. Пишет его сам сосед — своим же способом полной замены
    набора (`/presets/sync`, `/settings/presets`): второго хранилища пресетов у пульта
    нет и быть не должно."""
    if service not in NEIGHBOUR_PRESET_FIELDS:
        raise HTTPException(404, f"у сервиса «{service}» своих пресетов нет")
    name = (body.name or "").strip()[:80]
    if not name:
        raise HTTPException(400, "пустое название")
    sid = getattr(request.state, "sid", "")
    store, put = _preset_store(service, sid, request)
    if name in store:
        raise HTTPException(409, f"пресет «{name}» уже есть")
    if service == "rewrite_gen":
        # Бриф заводится своей ручкой сразу с текстом: пустой бриф Rewrite не примет.
        _rewrite_gen_write(name, body.values or {}, sid, create=True)
        return {"ok": True, "name": name}
    cfg = dict(NEIGHBOUR_PRESET_DEFAULTS.get(service) or {})
    if service == "cover":
        cfg["name"] = name          # у Cover имя лежит и внутри самого пресета
    store[name] = cfg
    put(store)
    # Поля формы дописываем той же дорогой, что и обычную правку: одни проверки на
    # заведение и на правку, а не вторая копия разбора значений.
    if body.values:
        try:
            _neighbour_preset_write(service, name, body.values, sid, request)
        except HTTPException:
            # Пресет уже заведён, а поля не приняты — откатываем, чтобы не оставлять
            # наполовину заполненный пресет, о котором человек не знает.
            store2, put2 = _preset_store(service, sid, request)
            store2.pop(name, None)
            put2(store2)
            raise
    return {"ok": True, "name": name}


@app.get("/ui/presets/{service}/{name}/form")
def ui_neighbour_preset_form(service: str, name: str, request: Request):
    """Поля пресета соседа для формы. Секреты сюда не попадают: отдаём только те поля,
    которые сами же и перечислили."""
    if service not in NEIGHBOUR_PRESET_FIELDS:
        raise HTTPException(404, f"у сервиса «{service}» правки полей нет")
    return _neighbour_preset_read(service, name, getattr(request.state, "sid", ""), request)


class NeighbourPresetIn(BaseModel):
    values: dict = {}


@app.post("/ui/presets/{service}/{name}/form")
def ui_neighbour_preset_save(service: str, name: str, body: NeighbourPresetIn,
                             request: Request):
    """Сохранить поля пресета соседа. Читаем весь набор, меняем известные поля и пишем
    обратно целиком — остальные поля пресета остаются как были."""
    if service not in NEIGHBOUR_PRESET_FIELDS:
        raise HTTPException(404, f"у сервиса «{service}» правки полей нет")
    if not body.values:
        raise HTTPException(400, "пустое сохранение")
    _neighbour_preset_write(service, name, body.values,
                            getattr(request.state, "sid", ""), request)
    return {"ok": True}


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
        # Список брифов отдаёт СВОДКУ (id, имя, длина) — без самого текста. Полная
        # замена набора такой сводкой стёрла бы текст всех брифов разом, поэтому
        # каждый бриф читается своей ручкой, а пишется и удаляется — по одному
        # (18.09.2026; до этого копия и удаление бриф обесточивали).
        code, body = _rw("/presets", sid)
        if code >= 400 or not isinstance(body, dict):
            raise HTTPException(_pass_code(code), body or "брифы не получены")
        store = {}
        for name in body:
            code2, full = _rw(f"/presets/{urllib.parse.quote(name)}", sid)
            store[name] = full if code2 == 200 and isinstance(full, dict) else dict(body[name])
        # Снимок «что было»: вызывающий меняет тот же словарь, что мы вернули
        # (`store.pop(name)` в удалении), и сравнивать с ним самим — значит не увидеть
        # ни одной правки.
        was = {k: dict(v) if isinstance(v, dict) else v for k, v in store.items()}

        def put(s):
            for name, cfg in s.items():
                if was.get(name) == cfg:
                    continue          # не трогаем то, что не менялось
                payload = {"name": name,
                           "content": str((cfg or {}).get("content") or ""),
                           "target_length_words": (cfg or {}).get("target_length_words")}
                code3, out = _rw(f"/presets/{urllib.parse.quote(name)}", sid, payload)
                if code3 >= 400:
                    raise HTTPException(_pass_code(code3), out)
            for name in was:
                if name not in s:
                    code3, out = _rw(f"/presets/{urllib.parse.quote(name)}", sid,
                                     method="DELETE")
                    if code3 >= 400:
                        raise HTTPException(_pass_code(code3), out)
        return store, put

    raise HTTPException(404, f"нет сервиса «{service}»")


# ─────────────────────────── каналы: завести и настроить ───────────────────────────

# Проверку полей и запись делает кабинет своими же функциями (`pub_ui.read_channel_form`,
# `create_channel`, `update_channel`): расписание, часовой пояс, горизонт и лимиты
# разбираются там по одним правилам для обоих интерфейсов. Вторая копия этих правил
# разошлась бы с первой на первом же новом поле.

def _language_options() -> list:
    """Языки канала списком, а не полем для ввода (18.09.2026). Источник тот же, что у карты
    голосов в пресете: полный список Rewrite Studio с кодами ISO (`preset_fields`). Один
    справочник на оба места — иначе в канале окажется код, которого пресет не знает."""
    return [{"value": code, "title": title}
            for code, title in preset_fields.lang_options_for_form()]


# ─────────────────────── пресеты каналов (18.09.2026) ───────────────────────
# Решение владельца: те же настройки, что у канала, кроме языка — чтобы завести два
# канала на разных языках по одному набору правил. Привязка ЖИВАЯ: канал ссылается на
# пресет, правка пресета сразу меняет поведение всех каналов на нём. Значения не
# копируются в канал: их разрешает вид `pub_channels_live` (server/pub_schema.py).
CHANNEL_PRESET_FIELDS = (
    {"key": "schedule_days", "kind": "days", "label": "Дни выходов"},
    {"key": "schedule_times", "kind": "text", "label": "Время выходов",
     "hint": "через запятую: 10:00, 18:00. Пусто вместе с днями — расписание не задано"},
    {"key": "tz", "kind": "text", "label": "Часовой пояс", "hint": "например Europe/Warsaw"},
    {"key": "horizon_days", "kind": "number", "label": "Горизонт, дней", "min": 1, "max": 90},
    {"key": "lead_days", "kind": "number", "label": "Готовить заранее, дней",
     "min": 0, "max": 30},
    {"key": "deferred", "kind": "switch", "label": "Отложенная публикация"},
    {"key": "deferred_limit", "kind": "number", "label": "Отложенных максимум",
     "min": 1, "max": 200},
    {"key": "preset_vsgen_id", "kind": "select", "label": "Пресет производства"},
    {"key": "host_id", "kind": "select", "label": "Машина заливки"},
    {"key": "niche", "kind": "text", "label": "Ниша"},
    {"key": "collaborator", "kind": "text", "label": "Соавтор", "hint": "@имя на YouTube"},
)
# Колонки таблицы, которые правит форма. `schedule` собирается из двух полей (дни и
# время) той же функцией кабинета, что и расписание канала, — вторых правил нет.
CHANNEL_PRESET_COLUMNS = ("tz", "schedule", "horizon_days", "lead_days", "deferred",
                          "deferred_limit", "preset_vsgen_id", "host_id", "niche",
                          "collaborator")


def _channel_preset_options(c) -> dict:
    hosts = [{"value": str(r["id"]), "title": r["title"] or r["code"]}
             for r in c.execute("SELECT id, code, title FROM pub_hosts "
                                "WHERE tenant_id=? AND status='active' ORDER BY code",
                                (TENANT,))]
    vsgen = [{"value": str(pid), "title": name} for pid, name in vsgen_presets(c, TENANT)]
    return {"host_id": hosts, "preset_vsgen_id": vsgen}


def _channel_preset_row(c, pid: int):
    row = c.execute("SELECT * FROM pub_channel_presets WHERE id=? AND tenant_id=?",
                    (pid, TENANT)).fetchone()
    if row is None:
        raise HTTPException(404, "нет такого пресета канала")
    return row


def _channel_preset_payload(c, row) -> dict:
    """Пресет канала полями формы. NULL — «в пресете не задано»: канал возьмёт своё."""
    try:
        sched = json.loads(row["schedule"] or "{}") if row["schedule"] else {}
    except ValueError:
        sched = {}
    days = [d for d in sched if d != ANY_DAY]
    times = sorted({t for v in sched.values() for t in (v or [])})
    options = _channel_preset_options(c)
    values = {"schedule_days": ([] if not sched else (list(DAY_KEYS) if ANY_DAY in sched
                                                      else days)),
              "schedule_times": ", ".join(times)}
    for key in CHANNEL_PRESET_COLUMNS:
        if key == "schedule":
            continue
        val = row[key]
        values[key] = "" if val is None else val
    fields = [dict(f, value=values.get(f["key"], ""), options=options.get(f["key"], []))
              for f in CHANNEL_PRESET_FIELDS]
    used = [{"id": r["id"], "name": r["name"]} for r in c.execute(
        "SELECT id, name FROM pub_channels WHERE tenant_id=? AND preset_channel_id=? "
        "ORDER BY name", (TENANT, row["id"]))]
    return {"id": row["id"], "name": row["name"], "note": row["note"] or "",
            "fields": fields, "channels": used,
            "days": list(DAY_KEYS), "any_day": ANY_DAY}


@app.get("/ui/channel-presets")
def ui_channel_presets():
    """Список пресетов канала и кто на них бежит."""
    c = db()
    try:
        rows = []
        for r in c.execute("SELECT * FROM pub_channel_presets WHERE tenant_id=? ORDER BY name",
                           (TENANT,)):
            used = [{"id": x["id"], "name": x["name"]} for x in c.execute(
                "SELECT id, name FROM pub_channels WHERE tenant_id=? AND preset_channel_id=? "
                "ORDER BY name", (TENANT, r["id"]))]
            given = [f for f in CHANNEL_PRESET_COLUMNS if r[f] not in (None, "")]
            rows.append({"id": r["id"], "name": r["name"], "note": r["note"] or "",
                         "channels": used, "given": given})
    finally:
        c.close()
    return {"presets": rows,
            "note": "правка пресета меняет поведение всех каналов на нём со следующего "
                    "прохода очереди; уже поставленные задачи не пересчитываются"}


class ChannelPresetIn(BaseModel):
    name: str = ""
    note: str = ""
    values: dict = {}


def _channel_preset_write(c, pid: int, body: ChannelPresetIn) -> None:
    """Разбор полей пресета канала. Проверки — кабинетные (`pub_ui`): расписание, часовой
    пояс и числа обязаны жить по одним правилам с настройками самого канала."""
    vals = body.values or {}
    sets, args = [], []
    if body.name.strip():
        sets.append("name=?")
        args.append(body.name.strip()[:80])
    sets.append("note=?")
    args.append((body.note or "").strip()[:200])
    # Расписание: та же функция, что у канала. Пустое и дни, и время — «не задано»,
    # NULL, канал берёт своё.
    days = [str(x) for x in (vals.get("schedule_days") or [])]
    times = str(vals.get("schedule_times") or "").strip()
    try:
        schedule = pub_ui.clean_schedule(times, days) if (times or days) else None
    except (pub_ui.ChannelError, pub_ui.ScheduleError) as exc:
        raise HTTPException(400, str(exc))
    if schedule == "{}":
        schedule = None
    sets.append("schedule=?")
    args.append(schedule)
    tz = str(vals.get("tz") or "").strip()
    if tz:
        try:
            zoneinfo.ZoneInfo(tz)
        except Exception:                                          # noqa: BLE001
            raise HTTPException(400, f"часовой пояс «{tz}» не найден")
    sets.append("tz=?")
    args.append(tz or None)
    numeric = {"horizon_days": (1, 90), "lead_days": (0, 30), "deferred_limit": (1, 200),
               "preset_vsgen_id": (1, 10 ** 9), "host_id": (1, 10 ** 9)}
    for key, (lo, hi) in numeric.items():
        raw = str(vals.get(key) or "").strip()
        if not raw:
            sets.append(f"{key}=?")
            args.append(None)
            continue
        try:
            num = int(raw)
        except ValueError:
            raise HTTPException(400, f"{key}: нужно целое число")
        if not (lo <= num <= hi):
            raise HTTPException(400, f"{key}: допустимо от {lo} до {hi}")
        sets.append(f"{key}=?")
        args.append(num)
    deferred = str(vals.get("deferred") or "").strip().lower()
    sets.append("deferred=?")
    args.append(None if deferred == "" else (1 if deferred in ("1", "true", "on", "да") else 0))
    for key in ("niche", "collaborator"):
        val = str(vals.get(key) or "").strip()[:80]
        if key == "collaborator" and val and not val.startswith("@"):
            val = "@" + val
        sets.append(f"{key}=?")
        args.append(val or None)
    sets.append("updated=?")
    args.append(time.time())
    args += [pid, TENANT]
    c.execute(f"UPDATE pub_channel_presets SET {', '.join(sets)} "
              "WHERE id=? AND tenant_id=?", args)


@app.post("/ui/channel-presets")
def ui_channel_preset_create(body: ChannelPresetIn):
    name = (body.name or "").strip()[:80]
    if not name:
        raise HTTPException(400, "пустое название")
    c = db()
    try:
        now = time.time()
        try:
            cur = c.execute("INSERT INTO pub_channel_presets(tenant_id,name,created,updated) "
                            "VALUES(?,?,?,?)", (TENANT, name, now, now))
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"пресет канала «{name}» уже есть")
        pid = cur.lastrowid
        if body.values:
            _channel_preset_write(c, pid, ChannelPresetIn(values=body.values, note=body.note))
        c.commit()
    finally:
        c.close()
    return {"ok": True, "id": pid, "name": name}


@app.get("/ui/channel-presets/{pid:int}")
def ui_channel_preset_get(pid: int):
    c = db()
    try:
        return _channel_preset_payload(c, _channel_preset_row(c, pid))
    finally:
        c.close()


@app.post("/ui/channel-presets/{pid:int}")
def ui_channel_preset_save(pid: int, body: ChannelPresetIn):
    c = db()
    try:
        _channel_preset_row(c, pid)
        try:
            _channel_preset_write(c, pid, body)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "пресет канала с таким названием уже есть")
        c.commit()
        return _channel_preset_payload(c, _channel_preset_row(c, pid))
    finally:
        c.close()


@app.delete("/ui/channel-presets/{pid:int}")
def ui_channel_preset_delete(pid: int):
    """Удалить пресет канала. Если на нём кто-то бежит — сначала сказать, кто именно:
    молча вернуть эти каналы к их собственным настройкам значило бы поменять им
    расписание и машину заливки без предупреждения."""
    c = db()
    try:
        row = _channel_preset_row(c, pid)
        used = [r["name"] for r in c.execute(
            "SELECT name FROM pub_channels WHERE tenant_id=? AND preset_channel_id=?",
            (TENANT, pid))]
        if used:
            raise HTTPException(409, "на нём бегут каналы: " + ", ".join(used)
                                + ". Сначала отвяжите их — иначе они молча вернутся к "
                                  "своим старым настройкам.")
        c.execute("DELETE FROM pub_channel_presets WHERE id=? AND tenant_id=?", (pid, TENANT))
        c.commit()
        return {"ok": True, "name": row["name"]}
    finally:
        c.close()


class ChannelPresetPick(BaseModel):
    preset_id: int | None = None


@app.post("/ui/channels/{cid:int}/channel-preset")
def ui_channel_preset_attach(cid: int, body: ChannelPresetPick):
    """Посадить канал на пресет канала или снять его с пресета."""
    c = db()
    try:
        if not c.execute("SELECT 1 FROM pub_channels WHERE id=? AND tenant_id=?",
                         (cid, TENANT)).fetchone():
            raise HTTPException(404, "канал не найден")
        pid = body.preset_id
        if pid is not None:
            _channel_preset_row(c, pid)
        c.execute("UPDATE pub_channels SET preset_channel_id=?, updated=? WHERE id=? "
                  "AND tenant_id=?", (pid, time.time(), cid, TENANT))
        c.commit()
    finally:
        c.close()
    return {"ok": True, "preset_id": body.preset_id}


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
        "languages": _language_options(),
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


@app.post("/ui/plan/{pid:int}/cancel")
def ui_plan_cancel(pid: int, request: Request):
    """Убрать ссылку из плана. Снимает её кабинет (`cancel_item`): он же проверяет,
    что выпуск по ней ещё не пошёл, и он же владеет статусами."""
    c = db()
    try:
        row = c.execute("SELECT id FROM pub_plan_items WHERE id=? AND tenant_id=?",
                        (pid, TENANT)).fetchone()
    finally:
        c.close()
    if not row:
        raise HTTPException(404, "ссылка не найдена")
    _cabinet_form(f"/api/plan/{pid}/cancel", [], request, expect_json=False)
    return {"ok": True}


# {cid:int} — иначе этот путь перехватывает «/ui/channels/import».
@app.post("/ui/channels/{cid:int}")
def ui_channel_edit(cid: int, body: dict):
    """Сохранить настройки канала.

    Тело — те же имена полей, что у формы кабинета: их разбирает его же
    `read_channel_form`, вместе со всеми проверками и нормализацией расписания.
    """
    # Значения приводим к строкам: форма кабинета разбирает текст (`one`/`many` зовут
    # `.strip()`), а JSON приносит и числа, и null — раньше число давало 500 вместо
    # внятного отказа.
    form = {k: ["" if x is None else str(x)
                for x in (v if isinstance(v, list) else [v])]
            for k, v in (body or {}).items()}
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

def _can_drop(phase: str) -> bool:
    """Выпуск ещё можно убрать из очереди? С `upload_running` — нет (правило 5 ТЗ)."""
    try:
        return PHASES.index(phase or "created") < IRREVERSIBLE_FROM
    except ValueError:
        return True


def _live_release_rows(c) -> list:
    """Выпуски в работе — строками для очереди. Не подменяют задачи: у задачи свой этап
    и свой сервис, а здесь состояние самого выпуска, включая «ждём: …»."""
    out = []
    for r in c.execute(
            "SELECT r.release_id, r.phase, r.status, r.blocked_reason, r.updated, "
            "       r.plan_item_id, r.project_id, ch.id ch_id, ch.name ch_name, ch.lang lang, "
            "       p.donor_title, p.donor_url "
            "FROM pub_releases r "
            "LEFT JOIN pub_channels ch ON ch.id = r.channel_id "
            "LEFT JOIN pub_plan_items p ON p.id = r.plan_item_id "
            "WHERE r.tenant_id=? AND r.status NOT IN ('done','cancelled') "
            "ORDER BY r.updated DESC LIMIT 100", (TENANT,)):
        ph = r["phase"] or "created"
        out.append({
            "service": "release", "id": r["release_id"],
            "title": r["donor_title"] or r["donor_url"] or r["release_id"],
            "title_why": ("" if r["donor_title"] else
                          "название ролика не добыто — показываем ссылку донора"),
            "channel": r["ch_name"] or "", "channel_id": r["ch_id"],
            "lang": (r["lang"] or "").upper(),
            "release_id": r["release_id"], "project_id": r["project_id"] or "",
            "plan_item_id": (r["plan_item_id"] if _can_drop(ph) else None),
            "user": "", "kind": "release",
            "kind_title": f"выпуск · {PHASE_TITLE.get(ph, ph)}",
            "status": r["status"] or "", "state": _state(r["status"]),
            "progress": None, "stage": PHASE_TITLE.get(ph, ph), "provider": "",
            "created": r["updated"], "error": r["blocked_reason"] or "",
            "warning": "", "reason_pending": False,
            # У выпуска нет «взят в работу»: он не задача, а цепочка задач, и его
            # время — это время последнего движения. Поэтому «ждёт с …», а не «идёт»:
            # обещать работу там, где её может не быть, экран не должен.
            "started": None, "finished": None, "working": None,
            "waiting": (round(max(0.0, time.time() - float(r["updated"] or 0)))
                        if r["updated"] else None),
            "age_text": (f'в этой фазе {_span_ru(time.time() - float(r["updated"]))}'
                         if r["updated"] else ""),
        })
    return out


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
            rel = c.execute("SELECT release_id, phase, plan_item_id FROM pub_releases "
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
                "kind": "project",
                "release_id": (rel["release_id"] if rel else ""),
                # Убрать можно только то, что ещё не ушло на канал: с фазы
                # `upload_running` заливка необратима, и кабинет такую отмену отвергает.
                # Кнопка, которая через раз отвечает «поздно», учит её не нажимать.
                "plan_item_id": (rel["plan_item_id"] if rel and _can_drop(rel["phase"])
                                 else None),
                "phase": (rel["phase"] if rel else ""),
                "phase_title": (PHASE_TITLE.get(rel["phase"], rel["phase"]) if rel else ""),
                "ready": {"text": bool(st.get("text")), "audio": bool(st.get("audio")),
                          "images": st.get("images") or 0,
                          "images_expected": st.get("images_expected"),
                          "clips": st.get("clips") or 0,
                          "render": bool(st.get("render"))},
            })
        # Выпуск, у которого проекта ещё нет: проект заводится только после готового
        # текста, и до тех пор ролик канала не было видно нигде, кроме самого канала
        # (жалоба владельца 18.09.2026). Показываем его здесь же, отдельным видом строки.
        for r in c.execute(
                "SELECT r.release_id, r.phase, r.status, r.blocked_reason, r.updated, "
                "       r.plan_item_id, ch.name ch_name, p.donor_title, p.donor_url "
                "FROM pub_releases r "
                "LEFT JOIN pub_channels ch ON ch.id = r.channel_id "
                "LEFT JOIN pub_plan_items p ON p.id = r.plan_item_id "
                "WHERE r.tenant_id=? AND (r.project_id IS NULL OR r.project_id='') "
                "AND r.status NOT IN ('done','cancelled') "
                "ORDER BY r.updated DESC LIMIT 50", (TENANT,)):
            ph = r["phase"] or "created"
            rows.append({
                "id": r["release_id"], "kind": "release",
                "name": r["donor_title"] or r["donor_url"] or r["release_id"],
                "status": r["status"] or "", "updated": r["updated"], "busy": 0,
                "release_id": r["release_id"],
                "plan_item_id": (r["plan_item_id"] if _can_drop(ph) else None),
                "phase": ph, "phase_title": PHASE_TITLE.get(ph, ph),
                "channel": r["ch_name"] or "",
                "waiting": r["blocked_reason"] or "",
                "ready": {"text": False, "audio": False, "images": 0,
                          "images_expected": None, "clips": 0, "render": False},
            })
        rows.sort(key=lambda x: -(x["updated"] or 0))
    finally:
        c.close()
    return {"projects": rows,
            "note": "материалы роликов: кадры, озвучка, готовые файлы"}


def _applied_preset_info(c, settings: dict) -> dict | None:
    """`project.applied_preset` (§5.9): пресет, применённый последним снимком/кнопкой
    «Применить», или честное «удалён», если строки уже нет.

    `links` — только части, у которых ссылка на частичный пресет реально разрешилась
    (та же логика `_locked_parts`, что и у редактора пресета выше): сломанную ссылку
    (`_link_problems`) §7.2 требует не показывать как применённую — `resolve()` в этом
    случае и не подставляет частичный, группа остаётся на своих полях целого.
    """
    prid = preset_links.parse_link_ref(settings.get("_applied_preset"))
    if prid is None:
        return None
    row = c.execute("SELECT name FROM presets WHERE id=? AND tenant_id=?",
                    (prid, TENANT)).fetchone()
    if row is None:
        return {"id": prid, "name": None, "missing": True, "links": []}
    resolved = preset_links.resolve(c, prid)
    links = [{"part": part, "title": _PART_TITLES.get(part, part)}
             for part in sorted(_locked_parts(resolved))]
    return {"id": prid, "name": row["name"] or f"пресет {prid}", "missing": False,
            "links": links}


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
        # §5.9: тот же проход, что кабинет делает для мини-степпера на /p/{pid}
        # (main.py ≈4658–4663) — последний статус КАЖДОГО вида задачи, а не последние
        # 30 строк истории выше (`jobs`): редкий вид (например, «render») мог не
        # попасть в лимит 30, если проект успел наплодить много задач других видов.
        km = {r["kind"]: r["status"] for r in c.execute(
            "SELECT kind, status FROM queue_tasks WHERE project_id=? AND job_id IN "
            "(SELECT MAX(job_id) FROM queue_tasks WHERE project_id=? GROUP BY kind)",
            (pid, pid))}
        try:
            settings = json.loads(p["settings"] or "{}")
        except ValueError:
            settings = {}
        if not isinstance(settings, dict):
            settings = {}
        applied_preset = _applied_preset_info(c, settings)
    finally:
        c.close()

    st = {}
    if mod is not None and hasattr(mod, "_project_status"):
        try:
            st = mod._project_status(TENANT, pid) or {}
        except Exception:                                          # noqa: BLE001
            st = {}
    # `project.stage` — обёртка над кабинетной `project_stage_status` (Ф1, батч C,
    # 17.09.2026). Ф1 и Ф2 шли параллельно: если бы функции ещё не было, ручка не
    # должна была падать — честные пустые segments вместо выдуманных пяти шагов.
    stage = {"segments": [], "caption": "", "tone": "dim"}
    if mod is not None and hasattr(mod, "project_stage_status") and hasattr(mod, "_stage_sets"):
        try:
            merged = dict(st)
            merged["running"], merged["queued"], merged["errored"] = mod._stage_sets(km)
            # Шестая плитка — обложка: вкладок на экране шесть, и пять плиток читались
            # как «одну стадию забыли» (18.09.2026). Готовность берём тем же способом,
            # что и файлы проекта, — по самому файлу, а не по статусу задачи: задача
            # могла отработать, а обложки не появиться (правило 1 ТЗ).
            merged["cover"] = bool(_project_cover_ready(mod, pid))
            stage = mod.project_stage_status(merged)
        except Exception:                                          # noqa: BLE001
            pass
    source = ""
    if mod is not None and hasattr(mod, "_project_dir"):
        # Прямое чтение файла, тем же способом, что уже читает `_project_files`
        # (§5.9) — не поход по HTTP до кабинета за собственным же диском.
        try:
            with open(os.path.join(mod._project_dir(TENANT, pid), "source.txt"),
                      encoding="utf-8") as f:
                source = f.read()
        except FileNotFoundError:
            source = ""
    groups = _project_files(mod, pid)
    # Из чего проект делается — рядом с самим сценарием: поле источника на экране одно
    # (заявка владельца 20.09.2026), и собирать его из двух ответов было бы странно.
    # Отдельное короткое соединение, потому что сценарий читается уже после того, как
    # первое закрыто, а знать его размер `origin` обязан.
    origin = {}
    try:
        c2 = db()
        try:
            origin = _project_origin(c2, pid, source)
        finally:
            c2.close()
    except (HTTPException, sqlite3.Error):
        origin = {}
    return {
        "project": {"id": p["id"], "name": p["name"] or p["id"],
                    "status": p["status"] or "", "created": p["created"],
                    "updated": p["updated"],
                    "release_id": (rel["release_id"] if rel else ""),
                    "phase": (rel["phase"] if rel else ""),
                    "stage": stage, "applied_preset": applied_preset},
        "ready": st, "jobs": jobs, "files": groups, "paid": paid, "source": source,
        "origin": origin,
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
                ("scenes", "план сцен и промпты"),
                # Батч C, Ф2, §5.10: категорию завёл Ф1 в `_DL_CATS`/`_DL_PATTERNS`
                # (main.py, 17.09.2026) — кабинет кладёт готовую обложку в cover.jpg,
                # конвейер её не перекодирует. Без этой строки вкладка «Обложка» не
                # увидит уже стоящую обложку.
                ("covers", "обложка"))


# Сколько имён файлов отдаём на первом экране. В проекте бывает 300+ кадров: грузить
# их все сразу незачем — остальное браузер дочитывает по мере прокрутки
# (`GET /ui/projects/{pid}/files/{code}`, 18.09.2026). 40 — экран с запасом.
FILES_PAGE = 40


def _project_cover_ready(mod, pid: str) -> bool:
    """Есть ли у проекта готовая обложка. Тем же списком шаблонов, что и скачивание
    (`_dl_items(..., "covers")`), — второй способ узнать «есть ли файл» разошёлся бы с
    первым на первой же смене имени файла."""
    if mod is None or not hasattr(mod, "_dl_items"):
        return False
    try:
        return bool(mod._dl_items(mod._project_dir(TENANT, pid), "covers"))
    except Exception:                                              # noqa: BLE001
        return False


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
                    # covers → "image": одна миниатюра готовой обложки, не список
                    # ссылок на скачивание, как у текстовых категорий (§5.10).
                    "kind": ("image" if code in ("images", "covers") else
                             "video" if code in ("clips", "renders") else
                             "audio" if code == "audio" else "text"),
                    # Имена относительные — их же ждёт отдача файла кабинетом.
                    "names": [rel for _full, rel in items[:FILES_PAGE]],
                    "more": max(0, len(items) - FILES_PAGE)})
    return out


@app.get("/ui/projects/{pid}/files/{code}")
def ui_project_files_page(pid: str, code: str, request: Request,
                          offset: int = 0, limit: int = FILES_PAGE):
    """Следующая порция имён файлов этой группы — для дочитывания при прокрутке.

    Порядок тот же, что и на первом экране: имена берутся у `_dl_items` кабинета, а не
    перечитываются своим обходом каталога, иначе список на экране разъехался бы с
    архивом. Отдаём только имена: сами файлы по-прежнему отдаёт кабинет.
    """
    if code not in {c for c, _t in _FILE_GROUPS}:
        raise HTTPException(404, "нет такой группы файлов")
    offset = max(0, int(offset))
    limit = max(1, min(200, int(limit)))
    c = db()
    try:
        _project_or_404(c, pid)
    finally:
        c.close()
    mod = sys.modules.get("main")
    if mod is None or not hasattr(mod, "_dl_items"):
        raise HTTPException(503, "кабинет не поднят")
    try:
        items = mod._dl_items(mod._project_dir(TENANT, pid), code)
    except Exception:                                              # noqa: BLE001
        items = []
    return {"code": code, "count": len(items),
            "names": [rel for _full, rel in items[offset:offset + limit]],
            "offset": offset,
            "more": max(0, len(items) - (offset + limit))}


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
    # Текст больше не обязателен (18.09.2026): раньше его спрашивала форма заведения, а
    # потом ровно то же самое спрашивала вкладка «Сценарий» — два ввода одного и того же.
    # Теперь проект заводится пустым и сразу открывается на «Сценарии», где текст можно
    # получить по ссылке, вставить или загрузить файлом.
    name = ((body.name or "").strip()
            or (text.splitlines()[0].strip()[:80] if text else "")
            or "Проект от " + dt.datetime.now().strftime("%d.%m %H:%M"))
    pid = mod._create_project(TENANT, name, text)
    return {"id": pid, "name": name}


class ProjectName(BaseModel):
    name: str = ""


@app.post("/ui/projects/{pid}/name")
def ui_project_rename(pid: str, body: ProjectName, request: Request):
    """Переименовать проект. Нужно с 18.09.2026: имя больше не спрашивают при заведении,
    а «Проект от 18.09 14:20» в списке из сорока проектов ничего не говорит."""
    name = (body.name or "").strip()[:120]
    if not name:
        raise HTTPException(400, "пустое название")
    c = db()
    try:
        cur = c.execute("UPDATE projects SET name=?, updated=? WHERE id=? AND tenant_id=?",
                        (name, time.time(), pid, TENANT))
        c.commit()
    finally:
        c.close()
    if not cur.rowcount:
        raise HTTPException(404, "нет такого проекта")
    return {"ok": True, "name": name}


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


# Частичный прогон пачкой (18.09.2026). В кабинете это были четыре карточки —
# «Озвучка / Картинки / Видео / Рендер» с необязательными полями и своей отменой
# (server/main.py:4141); в пульт переехала только кнопка «Сделать всё», и сделать одну
# озвучку по десяти проектам стало нечем. Ставит по-прежнему кабинет (`batch_run`):
# он проверяет ключи, дубли и готовность материалов, и повторять это здесь нельзя.
BATCH_KINDS = {
    "tts": ("Озвучка", "engine", ("voice", "speed")),
    "images": ("Картинки", "model", ("image_mode", "aspect_ratio", "style",
                                     "sentences_per_prompt")),
    "video": ("Видео", "video_method", ("first_n", "prompt")),
    "render": ("Рендер", "quality", ()),
    "chapters": ("Главы", "", ()),
    "pipeline": ("Сделать всё", "", ()),
}


class BatchIn(BaseModel):
    pids: list = []
    values: dict = {}


# ───────── пакетный текст и обложка: источник берётся из проекта ─────────
#
# Почему это живёт в пульте, а не в кабинетном `/api/batch/{kind}`. Озвучка, картинки,
# видео и рендер — задачи очереди КАБИНЕТА, и пакет для них стоит там же (`batch_run`).
# Текст и обложка — СОСЕДНИЕ сервисы, к которым пульт ходит сам и кукой вошедшего
# человека (`/ui/text` и `/ui/covers` выше). Кабинет про них не знает ничего; завести
# для них ветку внутри его batch_run значило бы получить вторую дорогу к соседям, мимо
# которой идёт всё остальное и которую никто не проверяет.
#
# ВАЖНО: обе ручки объявлены ДО `/ui/projects/batch/{kind}` — иначе catch-all перехватит
# «text» и «cover» и ответит «нет вида» (та же причина, по которой в кабинете
# `/api/batch/delete` объявлен выше своего catch-all, server/main.py).
#
# Главное правило (решение владельца 20.09.2026): ИСТОЧНИК БЕРЁТСЯ ИЗ ПРОЕКТА. Проект
# знает про свой источник две вещи, и обе записанные, а не угаданные:
#
#   * донорская ссылка выпуска (`pub_releases` → `pub_plan_items.donor_url`) — то, из
#     чего проект родился; ровно её берёт ночной движок (`pub_services.do_source_ready`
#     для текста и `_preorder_cover` для обложки), и второй ответ на вопрос «из чего
#     этот ролик» системе не нужен;
#   * сценарий проекта — `source.txt` в папке проекта, тот самый текст, который
#     показывает вкладка «Сценарий» (`/ui/projects/{pid}` → `source`).
#
# Нет ни того, ни другого — по проекту честный отказ с причиной. Подставлять чужой
# источник нельзя: пачка тем и опасна, что одна ошибка уезжает сразу во все проекты.
#
# С 20.09.2026 есть и ТРЕТЬЯ запись — ссылка, которую человек сам задал на экране
# проекта (`project_origin`, см. комментарий у таблицы). Пачка её НЕ берёт, и это
# решение не принято, а отложено: правило «источник берёт пачка из выпуска» принимал
# владелец, а здесь платный заказ сразу на двадцать проектов. Понадобится — вопрос
# ставится владельцу отдельно, а не решается тихой правкой приоритета.


# Сколько проектов берём за одно нажатие. Это не вкус: за каждый проект пульт ходит в
# сеть (расшифровка донора, загрузка файла соседу, постановка задачи), а запрос из
# браузера обязан когда-то кончиться. Молча обрезать список нельзя — это платные
# задачи, поэтому лишнее не «не уехало», а отказ с числом.
BATCH_PULT_MAX = int(os.environ.get("VS_BATCH_PULT_MAX", "20") or 20)
# Общий срок на расшифровки ВСЕЙ пачки и потолок выходов на одну ссылку. Повод тот же,
# что у предпросмотра доноров (`pub_tags.donor_limits`): один поход за субтитрами при
# мёртвом туннеле обходит все выходы по VS_SUBS_TIMEOUT каждый, и десять проектов
# превратились бы в час ожидания с выжженными выходами на сдачу.
BATCH_SUBS_BUDGET = float(os.environ.get("VS_BATCH_SUBS_BUDGET_SEC", "150") or 150)
BATCH_SUBS_EXITS = int(os.environ.get("VS_BATCH_SUBS_EXITS", "3") or 3)
# Чем рерайтить, когда не сказано НИГДЕ: ни в карточке, ни в пресете проекта. То же
# умолчание, что у одиночного экрана текста (`ui_text_rewrite`). В отпечаток платного
# вызова оно не попадает намеренно — движок в этом случае кладёт туда пустую модель,
# и подставить сюда своё значение значило бы развести отпечатки на ровном месте.
TEXT_MODEL_FALLBACK = "gpt-4.1-nano"


def _batch_calls_ready(c) -> None:
    """Журнал платных вызовов обязан быть на месте ДО первой постановки.

    Без него у пачки нет единственной защиты от второй оплаты того же самого, а молча
    поставить двадцать платных задач «как-нибудь» — худший из возможных исходов.
    """
    try:
        pub_calls.ensure_calls_schema(c)
        c.commit()
    except sqlite3.Error as exc:
        raise HTTPException(503, f"журнал платных вызовов недоступен "
                                 f"({type(exc).__name__}) — пакетный запуск без него "
                                 f"запрещён: он и есть защита от второй оплаты")


def _batch_pids(body) -> list:
    """Отмеченные проекты: без пустых, без повторов, не больше потолка."""
    pids, seen = [], set()
    for raw in (body.pids or []):
        pid = str(raw).strip()
        if pid and pid not in seen:
            seen.add(pid)
            pids.append(pid)
    if not pids:
        raise HTTPException(400, "не отмечен ни один проект")
    if len(pids) > BATCH_PULT_MAX:
        raise HTTPException(400, f"за одно нажатие берём не больше {BATCH_PULT_MAX} "
                                 f"проектов, а отмечено {len(pids)}")
    return pids


def _project_release(c, pid: str):
    """Выпуск проекта: донорская ссылка, язык канала, пресеты текста и обложки.

    Тот же путь, которым их читает экран проектов (`_releases`): выпуск → строка плана
    (донор) и канал (язык, пресеты). Берём последний по времени: проект в норме
    заводится под один выпуск, но переносы плана оставляют старые строки.

    Пресеты пропускаем через `channel_presets` — ровно как `pub_engine.live_releases`
    перед тем, как отдать выпуск ночному движку. Иначе это были бы РАЗНЫЕ пресеты у
    одного и того же проекта: колонка канала — только запасной путь, а с 09.09.2026
    текст и обложка берутся из пресета производства (и с 18.09 — из частичных пресетов
    по ссылке). Для кнопки это вопрос денег: по пресету считается отпечаток платного
    вызова, и разойтись с движком тут значит заплатить за один текст дважды.

    Возвращает словарь (`sqlite3.Row` неизменяем, а пресеты подменяются), но читатели
    по-прежнему обращаются по имени — для словаря это работает так же.
    """
    row = c.execute(
        "SELECT r.release_id, ch.lang lang, ch.preset_cover preset_cover, "
        "       ch.preset_rewrite preset_rewrite, ch.preset_vsgen_id preset_vsgen_id, "
        "       p.donor_url donor_url "
        "FROM pub_releases r "
        "LEFT JOIN pub_channels ch ON ch.id = r.channel_id "
        "LEFT JOIN pub_plan_items p ON p.id = r.plan_item_id "
        "WHERE r.project_id=? AND r.tenant_id=? "
        "ORDER BY r.updated DESC LIMIT 1", (pid, TENANT)).fetchone()
    if row is None:
        return None
    rel = dict(row)
    try:
        rel["preset_rewrite"], rel["preset_cover"] = channel_presets(
            c, rel.get("preset_vsgen_id"), rel.get("preset_rewrite") or "",
            rel.get("preset_cover") or "")
    except sqlite3.Error:
        # Пресет производства не прочитался. Оставляем колонки канала как есть: это
        # запасной путь, и он честнее, чем пустота, — а пустой пресет обложек ниже
        # всё равно становится отказом, а не тихим заказом на умолчаниях соседа.
        pass
    return rel


def _project_scenario(mod, pid: str) -> str:
    """Сценарий проекта (`source.txt`) — ровно то, что показывает вкладка «Сценарий».

    Путь спрашиваем у кабинета, а при отдельном запуске пульта складываем сами: у
    пульта тот же самый `TENANTS_DIR` (см. комментарий у переменной), и терять ручку
    целиком из-за того, что кабинет не импортирован, незачем.
    """
    try:
        if mod is not None and hasattr(mod, "_project_dir"):
            path = os.path.join(mod._project_dir(TENANT, pid), "source.txt")
        else:
            path = os.path.join(TENANTS_DIR, TENANT, "projects", pid, "source.txt")
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, ValueError):
        return ""


def _batch_open_call(c, pid: str, kind: str):
    """Заказ этого вида, судьба которого ещё не выяснена (`reserved`/`ordered`).

    Это и есть ответ на «такая задача уже идёт». Журнал знает именно про деньги, и
    спрашивается он ПОСЛЕ `_batch_settle_calls`: та уже закрыла всё, что у соседа
    кончилось, поэтому открытая строка здесь означает «работа идёт прямо сейчас», а
    не «кнопку нажали когда-то».
    """
    return c.execute(
        "SELECT id, unit, status, remote_order_id, error FROM provider_calls "
        "WHERE project_id=? AND kind=? AND status IN (?,?) ORDER BY id DESC LIMIT 1",
        (pid, kind, pub_calls.RESERVED, pub_calls.ORDERED)).fetchone()


def _batch_open_why(open_row, what: str) -> str:
    """Словами: почему по этому проекту второй заказ не ставим.

    Разделены два случая, которые журнал держит в одном состоянии `ordered`. Задача
    ИДЁТ — это просто «подождите». Задача у соседа КОНЧИЛАСЬ ПЛОХО — деньги потрачены,
    результата нет, и повтор кнопкой был бы вторым платежом за то же самое: такую
    строку закрывает человек, а не кнопка, и он должен узнать об этом из ответа, а не
    гадать, почему проект «вечно занят».
    """
    was = open_row["remote_order_id"] or ""
    error = (open_row["error"] or "").strip()
    if error:
        return (f"заказ {what} у соседа кончился ничем"
                + (f" (задача {was})" if was else "")
                + f": {_short(error, 160)}. Деньги за него уже ушли — повтор оплатит "
                  f"то же самое второй раз, нужен разбор")
    return (f"у проекта уже идёт заказ {what}"
            + (f" (задача {was})" if was else " (номер ещё не записан)")
            + " — второй не ставлю")


def _ask_rewrite_job(sid: str, job_id: str) -> tuple:
    """Чем кончилась задача рерайта: ("done"|"lost"|"wait"|"unknown", причина).

    Вопрос о статусе бесплатен и повторяется сколько угодно — это ровно тот вид
    повтора, который безопасен (в отличие от повтора заказа). Словарь состояний берём
    у движка (`REWRITE_DONE`/`REWRITE_BAD`), а не заводим второй: разойдясь с ним, мы
    считали бы оплаченным то, что движок считает провалом.
    """
    code, out = _rw(f"/jobs/{urllib.parse.quote(job_id)}", sid)
    if code == 404:
        return "lost", "задача у рерайта не найдена"
    if code != 200 or not isinstance(out, dict):
        return "unknown", ""
    state = str(out.get("status") or "").lower()
    if state in REWRITE_BAD:
        return "lost", "рерайт не справился: " + str(out.get("error") or state)
    if state in REWRITE_DONE:
        return "done", ""
    return "wait", ""


def _ask_cover_job(sid: str, job_id: str) -> tuple:
    """То же самое про обложку. Завершение опознаём семейством статуса и `finished_at`,
    как `do_cover_ready`: сосед умеет и «completed_partial», и такой ответ, не попав ни
    в успех, ни в отказ, оставлял бы строку журнала открытой навсегда."""
    code, out = _cover(f"/jobs/{urllib.parse.quote(job_id)}", sid)
    if code == 404:
        return "lost", "задача у обложек не найдена"
    if code != 200 or not isinstance(out, dict):
        return "unknown", ""
    state = str(out.get("status") or "").lower()
    if state in ("error", "failed", "cancelled"):
        return "lost", "обложка не сделалась: " + str(out.get("error") or state)
    if not (state.startswith(("done", "completed", "finished", "success"))
            or out.get("finished_at")):
        return "wait", ""
    made = int(out.get("results_count") or 0) or len(out.get("output_files") or [])
    if not made:
        return "lost", ("сосед закончил, но ни одной обложки не отдал: "
                        + str(out.get("progress_detail") or state))
    return "done", ""


def _batch_settle_calls(c, pid: str, kind: str, ask) -> None:
    """Закрыть строки журнала, судьба которых у соседа уже решена.

    Кто это делает и почему именно кнопка. Ночной движок закрывает свои строки по
    ВЫПУСКУ (`pub_services._close_text_call` ищет по `release_id`), а строки кнопки
    заведены по ПРОЕКТУ — выпуска у проекта может не быть вовсе, — и `release_id` у
    них пуст. Движок их не увидит никогда. Значит, закрывает их тот, кто открыл:
    кнопка, при следующем нажатии на тот же проект.

    Без этого получался вечный «занят»: при `one_at_a_time` открытая строка навсегда
    запрещала новый заказ, а журнал копил открытые строки, по которым нельзя понять,
    что оплачено, а что ещё делается.

    И ТОЛЬКО СВОИ: строки с непустым `release_id` завёл движок, и трогать их отсюда
    запрещено. `pub_services.collect_paid_texts` ищет забытые оплаченные тексты именно
    по ним (`provider_calls.status='ordered'` у выпуска, потерявшего свою задачу) —
    закрыв такую строку «получено», мы вычеркнули бы оплаченный текст из экрана
    «оплачено, но не забрано» раньше, чем его оттуда забрали. Это те самые 146
    оплаченных и потерянных текстов из разбора 18.09, и повторять их нельзя.

    Три исхода, и различать их обязательно:

      done     сосед отдал результат — `delivered`, повторная оплата запрещена;
      lost     заказ кончился ничем (отказ, пропажа, пустой результат) — деньги ушли,
               строка ОСТАЁТСЯ открытой (`mark_failed(ordered=True)` статус не меняет),
               но в ней появляется причина. Повтор кнопкой был бы вторым платежом;
      wait/unknown — не трогаем ничего. Запись «не справился» по неудавшемуся вопросу
               стоила бы оплаченного результата.
    """
    rows = c.execute(
        "SELECT id, remote_order_id FROM provider_calls WHERE project_id=? AND kind=? "
        "AND status IN (?,?) AND IFNULL(remote_order_id,'')<>'' "
        "AND IFNULL(release_id,'')='' ORDER BY id",
        (pid, kind, pub_calls.RESERVED, pub_calls.ORDERED)).fetchall()
    if not rows:
        return
    asked, changed = {}, False
    for row in rows:
        job_id = row["remote_order_id"]
        if job_id not in asked:
            try:
                asked[job_id] = ask(job_id)
            except Exception as exc:                             # noqa: BLE001
                # Спросить не вышло — это про нас, а не про задачу. Молчим.
                asked[job_id] = ("unknown", f"{type(exc).__name__}")
        verdict, why = asked[job_id]
        if verdict == "done":
            # Доказательством служит номер задачи — тем же способом закрывает оплату
            # штатный путь движка (`_close_text_call(result=remote_job_id)`).
            pub_calls.mark_delivered(c, row["id"], job_id)
            changed = True
        elif verdict == "lost":
            pub_calls.mark_failed(c, row["id"], (why or "результата нет")[:400],
                                  ordered=True)
            changed = True
    if changed:
        c.commit()


def _batch_langs(asked: list, rel, *, display: bool) -> tuple:
    """Языки задачи: что выбрали в карточке, иначе язык канала этого проекта.

    Пустое поле — «как в проекте», как и у соседних карточек. Для пачки это не мелочь:
    в ней бывают проекты разных каналов, и один язык на всех был бы неверен для
    большинства.

    `display` — кому адресовано. Рерайт понимает и короткий код, и русское название
    (`RewriteJobRequest._coerce_languages`), а Cover ждёт только русское название. И
    переводить кодом `lang_ru` тут нельзя: по неизвестному коду он молча отвечает
    «Русский», то есть на пачке это десять русских обложек вместо честного отказа.
    """
    if asked:
        return list(asked), ""
    code = ((rel["lang"] if rel is not None else "") or "").strip().lower()
    if not code:
        return [], ("не выбран язык, а у проекта нет выпуска, из которого его взять "
                    "— выберите язык в карточке")
    if not display:
        return [code], ""
    name = LANG_TO_RU.get(code, "")
    if not name:
        return [], (f"язык канала «{code}» обложкам неизвестен — выберите язык "
                    f"в карточке")
    return [name], ""


def _short(text, n: int = 70) -> str:
    text = str(text or "").strip()
    return text if len(text) <= n else text[:n - 1] + "…"


def _batch_item(pid: str, name: str = "") -> dict:
    """Пустая строка отчёта по проекту.

    Отчёт пофайловый по проектам: молчаливое «ок» на пачке платных задач не даёт
    человеку увидеть, за что он заплатил, — а платит он за каждый проект отдельно.
    """
    return {"id": pid, "name": name or pid, "action": "skip", "why": "",
            "source": "", "source_note": "", "replaced": False, "preset": "",
            "languages": [], "skipped_languages": [], "job_id": "", "chars": 0,
            "repeat_checked": True}


def _batch_langs_left(c, pid: str, kind: str, langs: list, parts: dict, item: dict, *,
                      reserve: bool, provider: str = "", model: str = "") -> list:
    """Какие языки ещё не оплачены. `reserve=False` — только посмотреть (сухой прогон).

    Единица оплаты — язык: и рерайт, и обложка считают деньги по языкам, и ночной
    движок бронирует их так же (`pub_services.do_source_ready`: kind='text', unit=язык).
    Поэтому добавленный язык уезжает, а уже оплаченный — нет, вместо «или всё, или
    ничего» на весь проект.

    Отпечаток считается ТЕМИ ЖЕ КЛЮЧАМИ, что у движка, — иначе защиты от второй оплаты
    нет вовсе. Движок (`pub_services.do_source_ready`) кладёт в отпечаток текста
    `text` (sha донора), `lang`, `preset` (имя пресета рерайта) и `model` (модель ИЗ
    ПРЕСЕТА). Ровно эти четыре ключа собирает и карточка (`_batch_text_plan`). Разойтись
    хоть одним ключом значит получить два разных отпечатка на одну и ту же работу:
    ночью текст оплачивает движок, утром — кнопка, и человек платит дважды, не узнав
    об этом ниоткуда.

    Почему `model` берётся из пресета, а не из того, что реально уедет соседу: так
    считает движок (и объясняет это у себя подробно — заказ с 19.09 может уйти на
    модели, подсказанной соседом, но отпечаток остаётся на модели пресета). Отпечаток
    отвечает на вопрос «за это уже платили?», а не «чем именно делали». Явно выбранная
    в карточке модель отпечаток меняет — это осознанно другая работа за другие деньги.
    """
    left = []
    for lang in langs:
        fp = pub_calls.fingerprint(lang=lang, **parts)
        if not reserve:
            row = c.execute(
                "SELECT status, remote_order_id FROM provider_calls WHERE project_id=? "
                "AND kind=? AND unit=? AND fingerprint=?", (pid, kind, lang, fp)).fetchone()
            if row is None or row["status"] == pub_calls.FAILED:
                left.append({"code": lang, "call_id": None})
                continue
            action = {pub_calls.DELIVERED: "skip"}.get(row["status"], "verify")
            was = row["remote_order_id"] or ""
        else:
            decision = pub_calls.reserve(c, tenant_id=TENANT, project_id=pid, kind=kind,
                                         unit=lang, fp=fp, provider=provider, model=model)
            c.commit()              # строка журнала появляется раньше денег, и сразу
            if decision["action"] == "new":
                left.append({"code": lang, "call_id": decision["call_id"]})
                continue
            action = decision["action"]
            was = decision["row"]["remote_order_id"] or ""
        why = ("уже оплачено и получено" if action == "skip"
               else "уже заказано" + (f", задача {was}" if was
                                      else ", номер задачи неизвестен — нужен разбор"))
        item["skipped_languages"].append({"code": lang, "why": why, "job_id": was})
    return left


def _batch_call_failed(c, fresh: list, code: int, detail) -> str:
    """Закрыть брони после отказа соседа и сказать словами, что случилось.

    Развилка тут про деньги, а не про вежливость. Отказ самого сервиса (4xx и «не
    настроено») приходит ДО создания задачи: заказа нет, денег нет, строка уходит в
    `failed`, и повтор безопасен. Всё остальное — обрыв, таймаут, 5xx — неоднозначно:
    POST мог дойти и задачу создать. Такая строка остаётся `ordered`, и следующее
    нажатие пойдёт разбираться, а не платить второй раз.
    """
    text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    pre_order = (400 <= code < 500) or code == 503
    for one in fresh:
        if one["call_id"] is not None:
            pub_calls.mark_failed(c, one["call_id"], text[:400], ordered=not pre_order)
    c.commit()
    if pre_order:
        return f"сосед отказал ({code}): {_short(text, 160)}"
    return (f"ответа нет ({code}): {_short(text, 160)}. Задача могла создаться — "
            f"посмотрите её на экране и не нажимайте второй раз")


def _batch_answer(items: list, dry_run: bool, service: str) -> dict:
    """Итог пачки. Числа считаются ПО строкам отчёта, а не копятся рядом с ними:
    разойтись они тогда не могут, а решение «платить или нет» принимается по ним."""
    return {
        "ok": True,
        "dry_run": dry_run,
        "service": service,
        "planned": sum(1 for i in items if i["action"] == "order"),
        "ordered": sum(1 for i in items if i["action"] == "ordered"),
        "skipped": sum(1 for i in items if i["action"] == "skip"),
        "failed": sum(1 for i in items if i["action"] == "error"),
        "replaced": sum(1 for i in items if i["replaced"]),
        "projects": items,
        "note": ("источник взят из проекта; пустое поле карточки — «как в проекте». "
                 "Сухой прогон ничего не заказывает и денег не тратит"),
    }


# Две карточки, которых на экране проектов не хватало (решение владельца 20.09.2026).
# Отдельным ключом, а не внутри `kinds`: те четыре вида ставит кабинет и у них общая
# форма полей из пресета, а эти две ручки — пультовые, с полями своего вида. Варианты
# для них (языки, модели, пресеты) берутся там же, откуда их берут одиночные экраны, —
# второго списка тех же значений в пульте быть не должно.
BATCH_PULT_CARDS = {
    "text": {"title": "Текст", "post": "/ui/projects/batch/text",
             "options": "/ui/text",
             "fields": ["languages", "model", "source_from", "source_url",
                        "source_text", "one_at_a_time"]},
    "cover": {"title": "Обложка", "post": "/ui/projects/batch/cover",
              "options": "/ui/covers",
              "fields": ["languages", "preset", "provider", "model", "reference_url",
                         "edit_preview", "one_at_a_time"]},
}


class BatchTextIn(BaseModel):
    pids: list = []
    dry_run: bool = False            # сухой прогон: только показать, кому и что уйдёт
    languages: list = []             # пусто — язык канала проекта («как в проекте»)
    model: str = ""
    source_from: str = "auto"        # auto | donor | text — что считать источником
    source_url: str = ""             # замена: ссылка — уезжает ВСЕМ отмеченным
    source_text: str = ""            # замена: текст (или прочитанный файл) — одному
    one_at_a_time: bool = True       # не ставить второй заказ, пока идёт первый


def _batch_text_plan(c, sid: str, mod, pid: str, *, where: str, repl_url: str,
                     repl_text: str, langs_asked: list, model: str,
                     one_at_a_time: bool, net: bool, deadline: float) -> dict:
    """Что уедет по одному проекту. Ничего не заказывает и денег не тратит.

    Сухой прогон — это ровно этот проход с `net=False`: он читает проект, кэш
    расшифровок и журнал оплат, но в сеть за субтитрами не идёт. Поэтому про
    незнакомую кэшу ссылку он честно отвечает «повтор проверю при запуске»
    (`repeat_checked: false`), а не выдумывает ответ.

    Статус уже поставленных задач спрашивается и в сухом прогоне: это бесплатный
    вопрос, а не платный заказ, и без него предпросмотр врал бы про вчерашние задачи
    («уже идёт заказ» там, где сосед давно закончил).
    """
    p = c.execute("SELECT id, name FROM projects WHERE id=? AND tenant_id=?",
                  (pid, TENANT)).fetchone()
    if p is None:
        item = _batch_item(pid)
        item["why"] = "нет такого проекта"
        return item
    item = _batch_item(pid, p["name"] or pid)
    rel = _project_release(c, pid)

    langs, why = _batch_langs(langs_asked, rel, display=False)
    if why:
        item["why"] = why
        return item
    item["languages"] = list(langs)

    # Сначала закрываем вчерашнее: задачи, которые сосед уже доделал, перестают
    # считаться «идущими», а их строки в журнале — открытыми.
    _batch_settle_calls(c, pid, "text", lambda jid: _ask_rewrite_job(sid, jid))

    # Порядок проверок выбран по цене: сначала то, что читается из базы, и только потом
    # сеть. Проект, который всё равно пропустим, не должен жечь выход субтитров.
    if one_at_a_time:
        open_row = _batch_open_call(c, pid, "text")
        if open_row is not None:
            item["why"] = _batch_open_why(open_row, "текста")
            return item

    text = ""
    if repl_text.strip():
        text = repl_text
        item.update(source="replacement-text", replaced=True,
                    source_note=f"вставленный текст, {len(repl_text)} знаков")
    else:
        url = repl_url
        if url:
            # Та же потеря, что была на экране проекта: ссылку из карточки пачки тоже
            # никто не записывал, и проект после пакетного заказа не знал, из чего он
            # сделан. Запоминаем её в `_batch_text_order` — после удачного заказа, а не
            # здесь: сухой прогон ничего записывать не должен.
            item["_url"] = url
            item.update(source="replacement-url", replaced=True,
                        source_note=f"ссылка карточки {_short(url)}")
        else:
            donor = ((rel["donor_url"] if rel is not None else "") or "").strip()
            own = _project_scenario(mod, pid)
            if where == "donor" and not donor:
                item["why"] = "у проекта нет донорской ссылки — источник брать неоткуда"
                return item
            if where == "text" and not own.strip():
                item["why"] = "в проекте нет сохранённого сценария — источник пуст"
                return item
            if donor and where in ("auto", "donor"):
                item.update(source="donor", source_note=f"ссылка донора {_short(donor)}")
                url = donor
            elif own.strip():
                item.update(source="text",
                            source_note=f"сценарий проекта, {len(own)} знаков")
                text = own
            else:
                item["why"] = ("у проекта нет источника: ни донорской ссылки, "
                               "ни сохранённого сценария")
                return item
        if url:
            try:
                subs = donor_subs(url, net=net, strict=True, deadline=deadline,
                                  max_exits=BATCH_SUBS_EXITS)
            except Exception as exc:                             # noqa: BLE001
                item["why"] = f"текст ролика не получен: {exc}"
                return item
            text = (subs or {}).get("text") or ""
            if not text and not net:
                # Сухой прогон, а расшифровки в кэше нет. Соврать «поставится» нельзя и
                # промолчать нельзя: говорим прямо, что повтор проверим при запуске.
                item["action"] = "order"
                item["repeat_checked"] = False
                return item
            if not text.strip():
                item["why"] = "расшифровка донора пустая"
                return item
            item["source_note"] += (f" — расшифровка {len(text)} знаков"
                                    + (", из кэша" if (subs or {}).get("cached") else ""))
    if not text.strip():
        item["why"] = "текста нет — рерайтить нечего"
        return item

    # Пресет рерайта проекта и его модель — то же самое, из чего собирает отпечаток
    # ночной движок. Пустое поле модели в карточке означает «как в проекте», и работает
    # это буквально: соседу уедет модель пресета, а не наше умолчание поверх неё.
    preset = (rel["preset_rewrite"] if rel is not None else "") or ""
    preset_model = str((rewrite_preset_config(TENANT, preset) or {}).get("model") or "")
    item["preset"] = preset
    item["_model"] = model or preset_model or TEXT_MODEL_FALLBACK
    item["chars"] = len(text)
    item["_text"] = text
    # Ключи — как у движка (`pub_services.do_source_ready`), см. `_batch_langs_left`:
    # text + lang + preset + model. `lang` добавляет сам `_batch_langs_left`.
    item["_parts"] = {"text": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                      "preset": preset, "model": model or preset_model}
    left = _batch_langs_left(c, pid, "text", langs, item["_parts"], item, reserve=False)
    if not left:
        item["why"] = "за эти языки уже платили — новый заказ не ставлю"
        return item
    item["languages"] = [one["code"] for one in left]
    item["action"] = "order"
    return item


def _batch_text_order(c, sid: str, item: dict) -> None:
    """Поставить задачу текста одному проекту. Меняет `item` на месте.

    Порядок ровно такой: бронь в журнале → загрузка текста соседу → заказ. Строка
    журнала появляется раньше денег: обрыв между заказом и записью иначе означал бы
    вторую оплату при следующем нажатии.

    Модель берётся из `item` (`_batch_text_plan`), а не из полей запроса: она своя на
    каждый проект — пустое поле карточки означает модель ЕГО пресета.
    """
    pid = item["id"]
    model = item["_model"]
    fresh = _batch_langs_left(c, pid, "text", item["languages"], item["_parts"], item,
                              reserve=True, provider="rewrite", model=model)
    if not fresh:
        item.update(action="skip", languages=[],
                    why="за эти языки уже платили — новый заказ не ставлю")
        return
    langs = [one["code"] for one in fresh]
    item["languages"] = langs
    text = item["_text"]
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    # Имя файла своё на каждый проект и на каждое содержимое: сосед кладёт загрузку по
    # имени и одноимённую перезаписывает, а в пачке «вставленный.txt» у всех проектов
    # было бы одно — и все получили бы один сценарий.
    name = f"{pid}-{digest[:8]}.txt"
    boundary = "----studio" + secrets.token_hex(8)
    part = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n").encode("utf-8")
    raw = part + text.encode("utf-8") + f"\r\n--{boundary}--\r\n".encode("utf-8")
    code, out = _rw("/upload", sid, raw=raw,
                    ctype=f"multipart/form-data; boundary={boundary}")
    if code >= 400:
        item.update(action="error", why=_batch_call_failed(c, fresh, code, out))
        return
    stored = (out or {}).get("filename") or (out or {}).get("file") or name

    order = {"files": [stored],
             "languages": [{"code": x, "display": x} for x in langs],
             "model": model}
    # Ключ повтора сосед считает по паре «ключ + тело» (rewrite/job_idempotency.identity).
    # Ключ собираем ИЗ САМОГО ТЕЛА: тогда потерянный ответ на POST вернёт ту же задачу,
    # а другой заказ никогда не упрётся в вечный 409 «этот ключ уже был с другим телом».
    key = "pult-batch-" + hashlib.sha256(
        json.dumps(order, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:32]
    code, out = _rw("/jobs/rewrite", sid, order, extra={"Idempotency-Key": key})
    if code >= 400:
        item.update(action="error", why=_batch_call_failed(c, fresh, code, out))
        return
    job_id = str((out or {}).get("job_id") or "")
    for one in fresh:
        pub_calls.mark_ordered(c, one["call_id"], job_id)
    c.commit()
    _link_project_jobs(pid, "rewrite", out)
    if item.get("_url"):
        # Ссылка карточки — это явный выбор источника для ЭТОГО проекта, и после
        # оплаты он обязан остаться записанным: экран проекта показывает её же.
        _origin_link(pid, item["_url"], ordered=True)
    item.update(action="ordered", job_id=job_id, why="")


@app.post("/ui/projects/batch/text")
def ui_projects_batch_text(body: BatchTextIn, request: Request):
    """Поставить текст пачке отмеченных проектов — тем же путём, что и `/ui/text`.

    Источник берётся из проекта (донорская ссылка выпуска, иначе сценарий проекта);
    поля карточки — необязательная замена поверх. Ответ пофайловый по проектам: что
    поставлено, что пропущено и почему. Это платные задачи сразу на пачке, и
    молчаливое «ок» здесь означало бы «заплатили неизвестно за что».

    `dry_run` — сухой прогон: тот же разбор, но без сети и без постановки. Им экран
    показывает «уйдёт в работу N проектов» и спрашивает подтверждение.

    Режима «с нуля по теме» тут по-прежнему нет. С 20.09.2026 тема у проекта
    записывается (`project_origin`), то есть «брать неоткуда» больше не довод, — но
    генерация требует брифа, а бриф у проектов пачки разный и в проекте не хранится.
    Ставить пачкой по теме или нет — решение владельца, пока не принятое; поштучно это
    делает экран текста.
    """
    sid = getattr(request.state, "sid", "")
    pids = _batch_pids(body)
    langs_asked = [str(x).strip() for x in (body.languages or []) if str(x).strip()]
    repl_url = (body.source_url or "").strip()
    repl_text = body.source_text or ""
    where = (body.source_from or "auto").strip().lower()
    if where not in ("auto", "donor", "text"):
        raise HTTPException(400, f"неизвестный источник «{where}»")
    if repl_url and repl_text.strip():
        raise HTTPException(400, "замена источника одна: либо ссылка, либо текст")
    # Замена сильнее выбора «откуда брать»: она проверяется первой и просто отменяет
    # его. Молча — значит человек выбрал «сценарий проекта», получил рерайт чужой
    # ссылки на всю пачку и узнал бы об этом только по счёту. Поэтому спор запрещаем
    # до постановки: денег ещё не потрачено, и выбрать одно из двух ничего не стоит.
    if (repl_url or repl_text.strip()) and where != "auto":
        raise HTTPException(400, f"выбор источника «{where}» спорит с заменой в "
                                 f"карточке: замена сильнее и отменила бы выбор. "
                                 f"Уберите одно — либо замену, либо выбор")
    # Ссылку раздаём всем отмеченным — это один донор на пачку, так её и задумывали.
    # А вставленный текст (и прочитанный в браузере файл) — это ОДИН сценарий, и раздать
    # его десяти проектам значит сделать десять одинаковых роликов за десять оплат.
    if repl_text.strip() and len(pids) > 1:
        raise HTTPException(400, "вставленный текст (или файл) уходит одному проекту: "
                                 "отметьте один проект или уберите текст")
    # Пустое поле не подменяем умолчанием здесь: «как в проекте» решается по каждому
    # проекту отдельно (`_batch_text_plan`), потому что пресеты у них разные.
    model = (body.model or "").strip()

    mod = sys.modules.get("main")
    items, c = [], db()
    try:
        _batch_calls_ready(c)
        deadline = time.time() + BATCH_SUBS_BUDGET
        for pid in pids:
            # Беда одного проекта не должна уносить отчёт по остальным: часть пачки
            # к этому моменту уже оплачена, и ответ «500» скрыл бы от человека, что
            # именно уехало. Строка проекта становится ошибкой, пачка идёт дальше.
            item = _batch_item(pid)
            try:
                item = _batch_text_plan(c, sid, mod, pid, where=where,
                                        repl_url=repl_url,
                                        repl_text=repl_text, langs_asked=langs_asked,
                                        model=model,
                                        one_at_a_time=bool(body.one_at_a_time),
                                        net=not body.dry_run, deadline=deadline)
                if item["action"] == "order" and not body.dry_run:
                    _batch_text_order(c, sid, item)
            except HTTPException as exc:
                # HTTPException изнутри цикла НЕ выпускаем наружу: она унесла бы весь
                # ответ, включая строки проектов, за которые уже заплачено, и человек
                # не узнал бы, за что. Отказ — это беда одного проекта, как и любая
                # другая. Проверки, которые обязаны остановить всю пачку, стоят ДО
                # цикла (потолок, спор источников, журнал оплат).
                item.update(action="error",
                            why=f"отказ по этому проекту ({exc.status_code}): "
                                f"{_short(exc.detail, 160)}")
            except Exception as exc:                             # noqa: BLE001
                item.update(action="error", why=f"сбой на нашей стороне: "
                                                f"{type(exc).__name__}")
            finally:
                item.pop("_text", None)
                item.pop("_parts", None)
                item.pop("_model", None)
                item.pop("_url", None)
                items.append(item)
    finally:
        c.close()
    return _batch_answer(items, bool(body.dry_run), "rewrite")


class BatchCoverIn(BaseModel):
    pids: list = []
    dry_run: bool = False
    languages: list = []             # пусто — язык канала проекта («как в проекте»)
    preset: str = ""                 # пусто — пресет обложек канала
    provider: str = ""
    model: str = ""
    reference_url: str = ""          # замена: ссылка на референс — уезжает ВСЕМ
    edit_preview: bool = False       # явный выбор, а не побочный эффект (ТЗ §10)
    one_at_a_time: bool = True


def _batch_cover_plan(c, sid: str, pid: str, *, repl_url: str, langs_asked: list,
                      preset: str, provider: str, model: str, edit_preview: bool,
                      one_at_a_time: bool) -> dict:
    """Что уедет по обложке одного проекта. Ничего не заказывает и денег не тратит.

    Стратегия здесь одна — «по превью донора»: это и есть источник ПРОЕКТА, и тем же
    путём заказывает обложку ночной движок (`pub_services._preorder_cover`). Заголовок
    и семя источником проекта не являются — их в проекте не записано, поэтому пачкой
    они не ставятся и остаются на одиночной карточке `/ui/covers`.

    К соседу обращается ровно за одним — статусом уже поставленных задач. Это
    бесплатный вопрос, и он закрывает строки журнала, по которым работа давно кончилась.
    """
    p = c.execute("SELECT id, name FROM projects WHERE id=? AND tenant_id=?",
                  (pid, TENANT)).fetchone()
    if p is None:
        item = _batch_item(pid)
        item["why"] = "нет такого проекта"
        return item
    item = _batch_item(pid, p["name"] or pid)
    rel = _project_release(c, pid)

    langs, why = _batch_langs(langs_asked, rel, display=True)
    if why:
        item["why"] = why
        return item
    item["languages"] = list(langs)

    # Сначала закрываем вчерашнее — см. `_batch_settle_calls`.
    _batch_settle_calls(c, pid, "cover", lambda jid: _ask_cover_job(sid, jid))

    if one_at_a_time:
        open_row = _batch_open_call(c, pid, "cover")
        if open_row is not None:
            item["why"] = _batch_open_why(open_row, "обложки")
            return item

    if repl_url:
        url = repl_url
        item.update(source="replacement-url", replaced=True,
                    source_note=f"референс карточки {_short(url)}")
    else:
        url = ((rel["donor_url"] if rel is not None else "") or "").strip()
        if not url:
            item["why"] = ("у проекта нет донорской ссылки — обложку делать не из чего; "
                           "вставьте ссылку на референс")
            return item
        item.update(source="donor", source_note=f"превью донора {_short(url)}")

    item["preset"] = preset or ((rel["preset_cover"] if rel is not None else "") or "")
    if not item["preset"]:
        # Пустой пресет — это НЕ «на усмотрение соседа». Cover в таком заказе возьмёт
        # свои умолчания и нарисует обложку, которой никто не просил: платно и не ту.
        # Тот же ответ даёт ночной движок («у канала не выбран пресет обложек»,
        # `pub_services.do_cover_ready`) — и это отказ, а не тихий заказ.
        item["why"] = ("не выбран пресет обложек: пусто и в карточке, и у канала "
                       "проекта. Без пресета сосед нарисует обложку на своих "
                       "умолчаниях — платно и не ту, поэтому не заказываю")
        return item
    item["_url"] = url
    item["_parts"] = {"url": url, "preset": item["preset"], "provider": provider,
                      "model": model, "mode": "url",
                      "edit": "1" if edit_preview else ""}
    left = _batch_langs_left(c, pid, "cover", langs, item["_parts"], item, reserve=False)
    if not left:
        item["why"] = "за эти языки уже платили — новый заказ не ставлю"
        return item
    item["languages"] = [one["code"] for one in left]
    item["action"] = "order"
    return item


def _batch_cover_order(c, sid: str, item: dict, *, provider: str, model: str,
                       edit_preview: bool) -> None:
    """Поставить задачу обложки одному проекту. Меняет `item` на месте."""
    pid = item["id"]
    fresh = _batch_langs_left(c, pid, "cover", item["languages"], item["_parts"], item,
                              reserve=True, provider=provider or "cover", model=model)
    if not fresh:
        item.update(action="skip", languages=[],
                    why="за эти языки уже платили — новый заказ не ставлю")
        return
    langs = [one["code"] for one in fresh]
    item["languages"] = langs
    # Тело собирается тем же способом, что в одиночной `ui_covers_create`: соотношение
    # сторон у обложки YouTube одно и уходит в пресет явно, а редактирование превью —
    # отдельная галочка, а не следствие приложенного файла (ТЗ §10).
    preset_data = {"aspect_ratio": "16:9", "simple_mode": bool(edit_preview)}
    if provider:
        preset_data["generation_provider"] = provider
        preset_data["generation_model"] = model or ""
    payload = {"languages": langs, "mode": "url", "preset_name": item["preset"],
               "preset_data": preset_data, "urls": [item["_url"]]}
    code, out = _cover("/jobs/cover", sid, payload)
    if code >= 400:
        item.update(action="error", why=_batch_call_failed(c, fresh, code, out))
        return
    job_id = str((out or {}).get("job_id") or "")
    for one in fresh:
        pub_calls.mark_ordered(c, one["call_id"], job_id)
    c.commit()
    _link_project_jobs(pid, "cover", out)
    item.update(action="ordered", job_id=job_id, why="")


@app.post("/ui/projects/batch/cover")
def ui_projects_batch_cover(body: BatchCoverIn, request: Request):
    """Поставить обложку пачке отмеченных проектов — тем же путём, что и `/ui/covers`.

    Источник — превью донора этого проекта; ссылка на референс в карточке заменяет его
    ВСЕМ отмеченным и помечается в ответе (`replaced`), чтобы было видно, кому уехало
    не своё. Сухой прогон и формат ответа — как у пакетного текста.
    """
    sid = getattr(request.state, "sid", "")
    pids = _batch_pids(body)
    langs_asked = [str(x).strip() for x in (body.languages or []) if str(x).strip()]
    provider = (body.provider or "").strip()
    model = (body.model or "").strip()
    items, c = [], db()
    try:
        _batch_calls_ready(c)
        for pid in pids:
            # То же, что и у текста: одна упавшая строка не уносит отчёт по пачке.
            item = _batch_item(pid)
            try:
                item = _batch_cover_plan(
                    c, sid, pid, repl_url=(body.reference_url or "").strip(),
                    langs_asked=langs_asked, preset=(body.preset or "").strip(),
                    provider=provider, model=model,
                    edit_preview=bool(body.edit_preview),
                    one_at_a_time=bool(body.one_at_a_time))
                if item["action"] == "order" and not body.dry_run:
                    _batch_cover_order(c, sid, item, provider=provider, model=model,
                                       edit_preview=bool(body.edit_preview))
            except HTTPException as exc:
                # Та же причина, что и у текста: отказ одного проекта не уносит ответ,
                # в котором уже стоят строки оплаченных.
                item.update(action="error",
                            why=f"отказ по этому проекту ({exc.status_code}): "
                                f"{_short(exc.detail, 160)}")
            except Exception as exc:                             # noqa: BLE001
                item.update(action="error", why=f"сбой на нашей стороне: "
                                                f"{type(exc).__name__}")
            finally:
                item.pop("_url", None)
                item.pop("_parts", None)
                items.append(item)
    finally:
        c.close()
    return _batch_answer(items, bool(body.dry_run), "cover")


@app.post("/ui/projects/batch/{kind}")
def ui_projects_batch(kind: str, body: BatchIn, request: Request):
    """Поставить один этап пачке проектов. Пустое поле — «как в проекте»: кабинет
    кладёт настройки проекта базой и накрывает только непустыми полями формы."""
    if kind not in BATCH_KINDS:
        raise HTTPException(404, f"нет вида «{kind}»")
    pids = [str(p).strip() for p in (body.pids or []) if str(p).strip()]
    if not pids:
        raise HTTPException(400, "не отмечен ни один проект")
    fields = [("pid", p) for p in pids]
    fields += [(k, str(v).strip()) for k, v in (body.values or {}).items() if str(v).strip()]
    return _cabinet_form(f"/api/batch/{kind}", fields, request)


@app.post("/ui/projects/batch/cancel/{kind}")
def ui_projects_batch_cancel(kind: str, body: BatchIn, request: Request):
    """Отменить этап у пачки проектов — вместе с отложенным авто-запуском. Снимает
    кабинет (`batch_cancel`): он один знает, что считать «активной задачей»."""
    pids = [str(p).strip() for p in (body.pids or []) if str(p).strip()]
    if not pids:
        raise HTTPException(400, "не отмечен ни один проект")
    return _cabinet_form(f"/api/batch/cancel/{kind}", [("pid", p) for p in pids], request)


@app.get("/ui/projects/batch/fields")
def ui_projects_batch_fields():
    """Из чего выбирать в карточках частичного прогона: те же варианты, что у пресета
    (`preset_fields`), а не второй список здесь."""
    mod = sys.modules.get("main")
    out = {}
    for kind, (title, main_key, extra) in BATCH_KINDS.items():
        if not main_key:
            out[kind] = {"title": title, "fields": []}
            continue
        fields = []
        for g in preset_fields.describe(mod, {}):
            for f in g["fields"]:
                if f["key"] == main_key or f["key"] in extra:
                    fields.append({"key": f["key"], "label": f["label"], "kind": f["kind"],
                                   "options": f["options"], "hint": f.get("hint", "")})
        # Порядок как в карточке кабинета: главное поле сверху, остальное — «доп.».
        fields.sort(key=lambda f: 0 if f["key"] == main_key else 1)
        out[kind] = {"title": title, "fields": fields}
    # Потолок пачки отдаём экрану вместе с карточками. Без него экран узнаёт о нём
    # только отказом на нажатие — то есть после того, как человек отметил тридцать
    # проектов и решил, что сейчас они уедут. Лежит внутри каждой карточки: потолок —
    # свойство этой кнопки, и карточка остаётся самоописанием целиком.
    pult = {kind: dict(card, max=BATCH_PULT_MAX)
            for kind, card in BATCH_PULT_CARDS.items()}
    return {"kinds": out, "pult": pult,
            "note": "пустое поле — «как в проекте»: настройки проекта не перетираются"}


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
    # Группы «Сценарий» и «Обложка» в проект не идут: какими пресетами Rewrite и
    # Cover делают текст и обложку, решает КАНАЛ, а не отдельный проект. Показать здесь
    # эти поля значило бы предложить настройку, которую никто не прочитает.
    groups = [g for g in preset_fields.describe(mod, settings)
              if g["title"] not in ("Сценарий", "Обложка")]
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


# ─────────────────────────── экран проекта: старт этапа (батч C, Ф2) ───────────────────────────

# kind этапа → часть пресета (`preset_fields.PARTS`), которой он закрывается. Она же
# отвечает на «какая группа — чьи поля»: та же карта, которой пользуется применение
# ссылок частичного пресета (`preset_links.group_keys`), а не вторая копия соответствия.
_STAGE_PART = {"tts": "voice", "images": "images", "video": "clips", "render": "render"}


def _stage_form_fields(c, pid: str, kind: str, overrides: dict) -> list:
    """Поля тела формы старта этапа = текущие эффективные настройки проекта по ключам
    группы `preset_fields`, поверх которых накладываются явные поля из тела запроса
    (поправка оркестратора к §5.4, 2026-09-17).

    Зачем. Кабинетный `gen_submit` (main.py ≈12848–12890) при пустом теле формы всё
    равно молча ЗАПИСЫВАЕТ настройки проекта (`_save_project_settings(tid, pid, dd)`)
    — а для `tts` ещё и подставляет `engine="edge"` по умолчанию (main.py ≈12870),
    если движок не пришёл в форме. Пустое тело с пульта означало бы «нажали „Начать“
    ничего не меняя» — и молча перевело бы проект с Lumean/VoiceGenCore на Edge. Форма
    кабинета на `/p/{pid}` эту дыру не открывает только потому, что рисует все поля
    ТЕКУЩИМИ значениями — обычный ре-сабмит присылает то же самое обратно. Мы
    отправляем то же самое, не рисуя HTML.

    Набор ключей — `part_group_keys(part) | SHARED_KEYS` (видимые поля группы
    `GROUPS` ∪ ключи, которые этап реально читает, но форма не показывает, ∪ общий
    `use_cache`). Это НАДмножество полей, что рисует настоящая форма кабинета на
    `/p/{pid}` (main.py: pane_tts ≈4860–4896, pane_img ≈4954–4984, pane_vid
    ≈5187–5215, pane_rnd ≈5413–5470), сверено построчно — не побайтовое совпадение
    имён `name=`. Известные расхождения, безопасные по коду кабинета:
      - `render`: форма шлёт `auto_chapters`, мы шлём `chapters` — это РАЗНЫЕ ключи
        (`preset_fields.py` называет поле группы «Сборка» `chapters`, а фактическая
        настройка проекта, которую читает главный флаг «считать главы», —
        `auto_chapters`). Расхождение безопасно, потому что глава считается не из
        тела этого запроса: `gen_submit` лишь пересохраняет `dd["chapters"]` в
        `projects.settings` как есть (не трогая `auto_chapters`), а сам расчёт глав
        после рендера читает `auto_chapters` напрямую из настроек проекта отдельным
        кодом (main.py ≈13316) — наш `chapters` этот путь не пересекает и не портит;
      - мы шлём лишние ключи группы, которых нет ни в одной форме (`engine`,
        `tts_engine`, `voice_uuid`, `voice_by_lang`, `auto_pause_duration`,
        `auto_pause_frequency`, `img_source`, `image_source`, `googler_model`,
        `smart_mode`, `t2v`, `t2v_first_n`, `render_media`, `stock_video_*`,
        `vintage_effect`, `randomize_metadata`) — это текущие значения проекта по
        полям, которые этап читает напрямую из настроек, а форма их просто не
        рисует отдельными input'ами; кабинет их примет как есть, ре-сабмит не
        меняет то, что форма не показывает;
      - поля, которых в текущих настройках проекта нет вовсе, мы не отправляем
        (не шлём пустую строку) — настоящая форма прислала бы `""`, но это
        безопасно: `_save_project_settings` не отличает «не пришло» от «пришла
        пустая строка» для этих ключей иначе, чем «не задано».
    """
    part = _STAGE_PART[kind]
    keys = preset_fields.part_group_keys(part) | preset_fields.SHARED_KEYS
    unknown = sorted(set(overrides) - keys)
    if unknown:
        raise HTTPException(400, "неизвестные поля этапа: " + ", ".join(unknown))
    r = c.execute("SELECT settings FROM projects WHERE id=? AND tenant_id=?",
                 (pid, TENANT)).fetchone()
    try:
        settings = json.loads((r["settings"] if r else "") or "{}")
    except ValueError:
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    fields = {}
    for k in keys:
        v = settings.get(k)
        if v not in (None, ""):
            fields[k] = str(v)
    for k, v in (overrides or {}).items():
        fields[k] = "" if v is None else str(v)
    return list(fields.items())


class StageStartIn(BaseModel):
    settings: dict = {}


@app.post("/ui/projects/{pid}/stage/{kind}")
def ui_project_stage_start(pid: str, kind: str, body: StageStartIn, request: Request):
    """Начать этап (`tts`/`images`/`video`/`render`) и честно сказать, что вышло.

    Пять исходов (§5.4, поправка 1 триажа) — по query редиректа кабинета, который
    теперь виден в ответе `_cabinet_form` (§5.3): `job=` — поставлено, `dup=` — уже
    идёт, `imgbusy=1` — правка картинок занята, `dr=1`/`dv=1` — рендер/видео отложены
    (не готовы входные материалы). «Поставлено» — только когда в query есть `job`.
    Дубли/занятость проверяет сам кабинет (`gen_submit`) — здесь их не повторяем.
    """
    if kind not in _STAGE_PART:
        raise HTTPException(404, f"нет этапа «{kind}»")
    c = db()
    try:
        if not c.execute("SELECT 1 FROM projects WHERE id=? AND tenant_id=?",
                         (pid, TENANT)).fetchone():
            raise HTTPException(404, "проект не найден")
        fields = _stage_form_fields(c, pid, kind, body.settings or {})
    finally:
        c.close()
    resp = _cabinet_form(f"/api/projects/{pid}/{kind}", fields, request, expect_json=False)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(resp.get("url") or "").query)
    if "job" in q:
        return {"outcome": "started", "job_id": int(q["job"][0])}
    if "dup" in q:
        return {"outcome": "duplicate"}
    if "imgbusy" in q:
        return {"outcome": "images_busy"}
    if "dr" in q:
        return {"outcome": "render_deferred"}
    if "dv" in q:
        return {"outcome": "video_deferred"}
    raise HTTPException(502, "кабинет ответил на старт этапа непонятным адресом")


class SourceIn(BaseModel):
    # `None` означает «этого не присылали и трогать не надо». Пустая строка — это
    # ЗНАЧЕНИЕ: «очистить». Разница важна, потому что одно поле экрана теперь умеет
    # сохранять и сценарий, и ссылку, и старые вызовы (только `text`) обязаны работать
    # как раньше.
    text: str | None = None
    link: str | None = None          # ссылка донора: "" — забыть её
    file: str = ""                   # имя файла, из которого человек взял сценарий


@app.post("/ui/projects/{pid}/source")
def ui_project_source_save(pid: str, body: SourceIn, request: Request):
    """Сохранить источник проекта: сценарий (`source.txt`), ссылку донора или оба.

    Сценарий уходит в кабинет его же ручкой — и «Сохранить текст», и «Взять в проект»
    (текст рерайта, уже полученный клиентом отдельным запросом). Ссылка живёт у пульта
    (`project_origin`): в настройках проекта ей не место, см. комментарий у таблицы.

    Чего этот вызов НЕ делает: сохранение сценария не меняет пометку «чем пользовались
    последним». Проект, сделанный по ссылке донора, остаётся сделанным по ней и после
    того, как рерайт «взят в проект», — иначе ссылка пропала бы с экрана ровно в тот
    момент, когда работа по ней удалась. Пометку двигает только явный выбор источника:
    ссылка, тема, файл.
    """
    if body.text is None and body.link is None:
        raise HTTPException(400, "нечего сохранять: ни текста, ни ссылки")
    link = None if body.link is None else body.link.strip()
    if link:
        seen = detect_source(link)
        if seen["kind"] != "link":
            # Здесь можно отказывать строго: сохранение ничего не стоит, а молча
            # записать текст в поле ссылки значит показать его потом как донора.
            raise HTTPException(400, seen["problem"]
                                or f"в поле ссылки не ссылка, а {seen['title']}")
    c = db()
    try:
        _project_or_404(c, pid)
        was = (_origin_read(c, pid).get("kind") or "").strip()
    finally:
        c.close()
    known = bool(was)
    if body.text is not None:
        _cabinet_form(f"/api/projects/{pid}/source", [("source", body.text)], request,
                      expect_json=False)
        name = (body.file or "").strip()
        if name:
            # Файл — это выбор источника: человек показал, ОТКУДА взят сценарий.
            # Содержимое при этом уже уехало в `source.txt`, второй копии не заводим —
            # помним только имя, чтобы экран мог сказать, откуда текст.
            _origin_write(pid, kind="file", file_name=_short(name, 120))
        elif not known:
            _origin_write(pid, kind="text")
    if link:
        _origin_link(pid, link)
    elif link == "":
        # Пустая строка пришла явно — «забыть ссылку». Заказы по ней уже стоят, и
        # трогать их нечем: забывается запись о доноре, а не задача. Пометку источника
        # сбрасываем только если она и была на ссылке: тему и файл чистка ссылки не
        # касается, а пустую пометку `_project_origin` заполнит сам.
        fields = {"donor_url": "", "video_id": "", "ordered": 0.0}
        if was == "link":
            fields["kind"] = ""
        _origin_write(pid, **fields)
    return {"ok": True, "origin": _project_origin_now(pid)}


class PresetApplyIn(BaseModel):
    preset_id: int


@app.post("/ui/projects/{pid}/preset/apply")
def ui_project_preset_apply(pid: str, body: PresetApplyIn, request: Request):
    """Заменить настройки проекта целиком настройками пресета (§5.6). Подтверждение
    («заменит настройки проекта целиком») — на клиенте (`confirmInline`, Ф4)."""
    _cabinet_form(f"/api/presets/{body.preset_id}/apply", [("pid", pid)], request,
                 expect_json=False)
    return {"ok": True}


class PresetSnapshotIn(BaseModel):
    name: str = ""


@app.post("/ui/projects/{pid}/preset/snapshot")
def ui_project_preset_snapshot(pid: str, body: PresetSnapshotIn, request: Request):
    """Завести новый пресет из ТЕКУЩИХ эффективных настроек проекта («сохранить как
    новый… → Снимок»).

    Имя проверяем здесь, ДО похода в кабинет (edge case §7.1): кабинетная
    `/api/presets` (main.py ≈12176–12185) на пустом `pid`/`name` не отвечает
    ошибкой, а молча редиректит без сохранения — повторять эту дыру в пульте нельзя.
    Без `setting__*` полей в теле кабинет снимает пресет с текущих эффективных
    настроек проекта как есть (main.py ≈12186–12219: пустой `active_updates` →
    `effective` остаётся тем же, чем сейчас пользуется проект).
    """
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "у пресета должно быть имя")
    _cabinet_form("/api/presets", [("pid", pid), ("preset_name", name)], request,
                 expect_json=False)
    return {"ok": True}


class CoverPickIn(BaseModel):
    service_job_id: str
    name: str


@app.post("/ui/projects/{pid}/cover")
def ui_project_cover_pick(pid: str, body: CoverPickIn, request: Request):
    """«Поставить обложкой проекта» — скопировать готовый вариант из задачи Cover в
    `cover.jpg` проекта (§5.7, алгоритм §6).

    Владение задачей за пульт не проверяет: Cover сам сверяет владельца файла с
    тем же тенантом, что и у пульта. В объединённой платформе (`web/platform.py`)
    Cover резолвит личность запроса через ту же куку кабинета, что шлёт сюда и
    пульт (`server/platform_sso.py:cabinet_user` — `user_id` там это `tenant_id`),
    и `_job_belongs_to_user` (cover/cover_server.py ≈234–237) сравнивает его с
    `job.user_id` без исключения для админа — чужой тенант получает 403 раньше,
    чем пульт успевает прочитать хоть один байт. Второй, дублирующей проверки
    здесь поэтому нет (принцип «не повторять чужую проверку», §6, шаг 2).
    """
    mod = sys.modules.get("main")
    c = db()
    try:
        if not c.execute("SELECT 1 FROM projects WHERE id=? AND tenant_id=?",
                         (pid, TENANT)).fetchone():
            raise HTTPException(404, "проект не найден")
    finally:
        c.close()
    url = (f"{COVER_BASE}/jobs/{urllib.parse.quote(body.service_job_id)}/files/"
           f"{urllib.parse.quote(body.name)}")
    req = urllib.request.Request(url)
    req.add_header("Cookie", f"{auth.COOKIE}={getattr(request.state, 'sid', '')}")
    try:
        with urllib.request.urlopen(req, timeout=NEIGHBOUR_TIMEOUT * 5) as resp:
            data = resp.read()
    except urllib.error.HTTPError as exc:
        raise HTTPException(_pass_code(exc.code),
                            exc.read().decode("utf-8", "replace")[:200])
    except Exception as exc:                                       # noqa: BLE001
        raise HTTPException(502, f"cover не отвечает: {type(exc).__name__}")
    if mod is None or not hasattr(mod, "_project_dir"):
        raise HTTPException(503, "кабинет не поднят — обложку положить некуда")
    pdir = mod._project_dir(TENANT, pid)
    target = os.path.join(pdir, "cover.jpg")
    # Атомарная запись (§6, шаг 5): временный файл РЯДОМ (та же файловая система —
    # иначе `os.replace` не атомарен), затем `os.replace`. Читатель `cover.jpg`
    # никогда не увидит частично записанный файл. Место — то же самое, куда
    # конвейер кладёт обложку (`server/pub_services.py` ≈609), без перекодирования.
    tmp = os.path.join(pdir, f".cover.jpg.tmp-{secrets.token_hex(8)}")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, target)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return {"ok": True, "bytes": len(data)}


@app.get("/ui/jobs/{jid}/logs")
def ui_job_logs(jid: int, after: int = 0):
    """Лог одной задачи, дочитывается опросом (§5.8).

    Права — `SELECT tenant_id FROM queue_tasks` (симметрично `job_logs_sse`,
    main.py ≈13445–13453), но 404, а не 403: задачи чужого тенанта для пульта не
    существуют, а не «доступ запрещён» (§7.5). Без редакции секретов сверх той, что
    уже сделал кабинет на запись (main.py ≈9766–9772, «редактируем stdout
    централизованно») — принцип 3 §5: ручка отдаёт `job_logs.line` как есть.
    """
    c = db()
    try:
        t = c.execute("SELECT tenant_id, status FROM queue_tasks WHERE job_id=?",
                      (jid,)).fetchone()
        if not t or t["tenant_id"] != TENANT:
            raise HTTPException(404, "нет такой задачи")
        total = c.execute("SELECT COUNT(*) FROM job_logs WHERE job_id=?",
                          (jid,)).fetchone()[0]
        truncated = False
        if after <= 0 and total > 1000:
            truncated = True
            rows = list(c.execute(
                "SELECT id, line FROM job_logs WHERE job_id=? ORDER BY id DESC LIMIT 1000",
                (jid,)))
            rows.reverse()
        else:
            rows = list(c.execute(
                "SELECT id, line FROM job_logs WHERE job_id=? AND id>? ORDER BY id",
                (jid, after)))
    finally:
        c.close()
    lines = [{"id": r["id"], "line": r["line"]} for r in rows]
    next_after = lines[-1]["id"] if lines else after
    return {"job_id": jid, "status": t["status"] or "", "total": total,
            "truncated": truncated, "lines": lines, "next_after": next_after}


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
