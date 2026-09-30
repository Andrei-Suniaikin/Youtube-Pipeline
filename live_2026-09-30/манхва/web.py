# -*- coding: utf-8 -*-
"""Русский интерфейс студии. Очередью и генерацией владеют web_queue/web_worker."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

try:
    from . import source as SRC
    from . import languages as LG
    from . import channels as CH
    from . import watch as WT
    from . import publish as PB
    from . import cover as CV
except ImportError:
    import source as SRC
    import languages as LG
    import channels as CH
    import watch as WT
    import publish as PB
    import cover as CV

BASE = Path(__file__).resolve().parent.parent
WEB = BASE / "web"
ROOT = Path(os.environ.get("MANGA_ROOT", str(BASE / "projects_web")))
STATUSES = {"pending": "ждёт", "running": "идёт", "complete": "готово", "error": "ошибка",
            # шаг появился в студии позже этого ролика: его никто не делал и не будет
            "skipped": "не делалась"}
VERDICTS = {"accept": "принят", "warn": "предупреждение", "reject": "отказ", "unknown": "ещё не проверен"}
STYLES = [
    {"key": "S0", "name": "Классический", "description": "Чёткий контур и контрастные тени"},
    {"key": "S1", "name": "Мягкий", "description": "Плавный свет и приглушённые цвета"},
    {"key": "S2", "name": "Драматичный", "description": "Глубокие тени и сильный контраст"},
    {"key": "S3", "name": "Светлый", "description": "Лёгкие тени и пастельные цвета"},
    {"key": "S4", "name": "Вебтун", "description": "Ярко и крупно, как у топ-каналов"},
]
VOICES = {key: list(spec["voices"]) for key, spec in LG.LANGS.items()}
LANGUAGES = {key: spec["label"] for key, spec in LG.LANGS.items()}
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_BATCH_CHARS = 20000
NO_CHANNEL = "-"   # фильтр списка: ролики без канала
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def duration(seconds):
    if seconds is None:
        return "—"
    seconds = max(0, int(float(seconds)))
    if seconds < 60:
        return f"{seconds} с"
    return f"{seconds // 60} мин {seconds % 60:02d} с"


def file_url(project_id, path):
    if not path:
        return None
    return f"/p/{quote(str(project_id), safe='')}/file/{quote(str(path), safe='/')}"


def error_view(error, step_label=""):
    """Human-facing explanation; retain the original diagnostic in collapsed details."""
    details = str(error or "")
    if not details:
        return {"message": "", "hint": "", "details": "", "needs_setup": False,
                "retry_label": "Повторить незавершённое"}
    if details.startswith("Очередь приостановлена"):  # web_queue.QUEUE_PAUSED: the job waits, nothing to retry by hand
        return {"message": "Очередь приостановлена. Ролик не потерян и ждёт.",
                "hint": ("Студия сама повторяет попытку и продолжит, когда причина будет устранена. "
                         "Передайте технические подробности администратору."),
                "details": details, "needs_setup": False, "retry_label": "Повторить незавершённое"}
    if step_label == "Исходник":
        # Шаг «Исходник» сам пишет причину по-русски (source.SourceError): показываем её, а не общий текст.
        return {"message": details.split("\n", 1)[0][:300], "hint": (
                    "Исправьте причину (ссылку, прокси MANGA_SUBS_PROXY / MANGA_SUBS_PROXIES или текст) и нажмите «Повторить незавершённое»."),
                "details": details, "needs_setup": False, "retry_label": "Повторить незавершённое"}
    lower = details.lower()
    needs_setup = any(token in lower for token in (
        "codex_home", "not logged in", "login required", "authentication", "unauthorized",
        "command not found", "permission denied", "не принимает токен", "неверный manga_claude_tokens", "альт-ключ не принят",
        "перелогиньте", "неверный manga_codex_homes"))
    needs_setup = needs_setup or ("no such file or directory" in lower and any(name in lower for name in ("codex", "claude", "executable")))
    if needs_setup:
        message = "Не удалось запустить обработку. Нужно проверить настройки сервиса."
        hint = ("Передайте технические подробности администратору. Повторите после исправления "
                "настройки — один повтор сейчас не устранит причину. Готовые шаги сохранятся.")
    elif any(token in lower for token in ("quota", "rate limit", "too many requests", "429", "квот", "лимит подписки", "упираются в лимит")):
        message = "Сервис временно не принимает новые запросы."
        hint = "Дождитесь восстановления лимита, затем повторите незавершённое. Готовые шаги сохранятся."
        if "не приняты" in lower:
            hint += " Если после сброса лимита ошибка повторится, передайте технические подробности администратору: возможно, подписки не принимают токены."
    elif any(token in lower for token in ("timeout", "timed out", "connection", "таймаут")):
        message = "Сервис не ответил вовремя."
        hint = "Повторите незавершённое через несколько минут. Готовые шаги сохранятся."
    else:
        message = f"Не удалось завершить этап «{step_label}»." if step_label else "Не удалось завершить сборку."
        hint = "Повторите незавершённое. Если ошибка повторится, передайте технические подробности администратору."
    return {"message": message, "hint": hint, "details": details, "needs_setup": needs_setup,
            "retry_label": "Повторить после настройки" if needs_setup else "Повторить незавершённое"}


def when(ts):
    return time.strftime("%d.%m в %H:%M", time.localtime(ts)) if ts else "—"


# Название, описание и теги ролика для выдачи. Их пишет шаг «Название» (publish.py) в text/publish.json;
# запасной путь — out/publish.json {"title", "description", "tags": [..] или строка} или out/title.txt, description.txt, tags.txt.
PUBLISH_JSON = "out/publish.json"
PUBLISH_TXT = (("title", "out/title.txt"), ("description", "out/description.txt"), ("tags", "out/tags.txt"))


def publish_texts(root):
    """{title, description, tags} — что есть; None — шаг текстов для выдачи ещё ничего не сделал.
    Шаг «Название» (publish.py) пишет text/publish.json — он главный; out/ — запасной путь старой договорённости."""
    root = Path(root)
    done = PB.read(root)
    if done:
        found = {key: str(done.get(src) or "").strip() for key, src in
                 (("title", "title"), ("description", "description"), ("tags", "tags_text"))}
        return {k: v for k, v in found.items() if v} or None
    found = {}
    try:
        data = json.loads((root / PUBLISH_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict):
        for key in ("title", "description", "tags"):
            value = data.get(key)
            if isinstance(value, list):
                value = ", ".join(str(v).strip() for v in value if str(v).strip())
            if isinstance(value, str) and value.strip():
                found[key] = value.strip()
    else:
        for key, rel in PUBLISH_TXT:
            try:
                value = (root / rel).read_text(encoding="utf-8").strip()
            except (OSError, ValueError):
                continue
            if value:
                found[key] = value
    return found or None


def publish_text_file(texts):
    parts = []
    for key, label in (("title", "Название"), ("description", "Описание"), ("tags", "Теги")):
        if texts.get(key):
            parts.append(f"{label}:\n{texts[key]}")
    return "\n\n".join(parts) + "\n"


def download_stem(project):
    """Имя файлов выдачи: канал и начало номера ролика — 20 роликов в сутки не перепутать в «Загрузках»."""
    channel = project.get("channel") or ""
    channel = channel if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,59}", channel) else "video"
    return f"{channel}_{str(project['id'])[:8]}" if channel != "video" else str(project["id"])[:8]


def cover_is_clean(root) -> bool:
    """Обложку можно отдавать обычной кнопкой: выбирающий не нашёл брака и буквы совпали.
    Иначе её скачивают только осознанно — из блока «Обложка», увидев причину."""
    state = CV.read_json(Path(root) / "out" / "cover.json", {}) or {}
    if not state:
        return True          # обложка сделана не шагом студии (положена руками) — как раньше
    verdict = state.get("verdict") or ("ok" if not (state.get("problems_left") or []) else "warn")
    return verdict == "ok" and not state.get("letter_faults")


def delivery_view(project, has_cover, texts, cleanup, cover_clean=True):
    """Блок «Выдача» на странице ролика: что скачать, отметка «скачано», когда и что уберётся."""
    archived, delivery = project.get("archived"), project.get("delivery")
    ready = bool(project.get("video_ready"))
    hours = (cleanup or {}).get("delivered_hours")
    days = (cleanup or {}).get("done_days")
    view = {"ready": ready, "cover": bool(has_cover and ready and cover_clean),
            "cover_held": bool(has_cover and ready and not cover_clean), "texts": bool(texts and ready),
            "title": (texts or {}).get("title", "") if ready else "", "stem": download_stem(project),
            "delivered": bool(delivery and delivery["current"]), "can_mark": False, "can_unmark": False,
            "status": "", "cleanup_note": "", "archived_note": ""}
    if delivery and delivery["current"]:
        how = "видео скачано целиком" if delivery["how"] == "download" else "отмечено вручную"
        view["status"] = f"Скачано {when(delivery['delivered'])} — {how}."
    elif delivery:
        view["status"] = (f"Отметка «скачано» ({when(delivery['delivered'])}) относится к прошлой версии видео: "
                          "после неё видео пересобрано. Скачайте новую версию.")
    elif ready:
        view["status"] = "Скачайте видео целиком — отметка поставится сама, или отметьте вручную."
    if archived:
        state = "удалены" if archived.get("finished") else "удаляются"
        view["archived_note"] = (
            f"Кадры, части сборки и черновики пересдач {state} уборкой после выдачи ({when(archived['started'])}, "
            f"{archived['reason']}). Остались видео, обложка и название с описанием (если студия их сделала), озвучка, тексты, "
            "листы героев и квитанции. Пересдать или принять кадр и собрать видео заново уже нельзя.")
        return view
    view["can_mark"] = ready and not view["delivered"]
    view["can_unmark"] = bool(delivery)
    if ready and hours is not None:
        delay = "сразу" if hours == 0 else f"через {hours} ч"
        if view["delivered"]:
            due = delivery["delivered"] + hours * 3600
            view["cleanup_note"] = (f"Кадры и черновики этого ролика удалятся при ближайшей уборке после {when(due)} "
                                    f"(уборка идёт раз в час; {delay} после отметки — MANGA_CLEANUP_DELIVERED_HOURS). "
                                    "До этого можно пересдать кадр или снять отметку.")
        else:
            view["cleanup_note"] = (f"После отметки «скачано» ({delay}, при ближайшей уборке раз в час) кадры и черновики "
                                    "этого ролика удалятся — пересдать кадр после этого будет нельзя. "
                                    "Видео, обложка и тексты останутся.")
    if ready and days is not None:
        view["cleanup_note"] = (view["cleanup_note"] + " " if view["cleanup_note"] else "") + (
            f"Без отметки кадры и черновики удалятся через {days} сут. после готовности (MANGA_CLEANUP_DONE_DAYS).")
    return view


def video_wait_view(project):
    if project.get("hold") and project.get("status") == "pending":
        # MANGA_DISK_MIN_GB: the queue does not start new videos while the disk is almost full.
        return {"title": "Ролик ждёт места на диске", "message": project["hold"]}
    if project.get("wait") and project.get("status") == "running":
        # The video stands in its lane or waits for the FastGen hour (web_worker.Worker.wait_note).
        return {"title": "Ролик ждёт", "message": project["wait"]}
    if project.get("current_step") == "frame_retry" and project.get("status") in ("pending", "running"):
        frame_id = project.get("retry_frame_id")
        number = f" {frame_id}" if type(frame_id) is int and frame_id > 0 else ""
        title = (f"В очереди пересдача кадра{number}" if project["status"] == "pending"
                 else f"Обновляется кадр{number} и видео")
        return {"title": title, "message": "После пересдачи видео обновится. Предыдущая версия временно недоступна, чтобы не показывать устаревший кадр."}
    return {"title": "Ролик готовится", "message": "После сборки здесь появится готовый ролик."}


def frame_issues(frame):
    """Explain actual verdict fields, leaving the original observations untouched."""
    row = frame.get("judge_row")
    row = row if isinstance(row, dict) else {}
    issues = []
    identity = row.get("identity") if isinstance(row.get("identity"), dict) else {}
    mismatched = [str(name) for name, ok in identity.items() if ok is False]
    if mismatched:
        russian_note = next((note.split(":", 1)[1].strip() for note in (row.get("notes") or [])
                             if isinstance(note, str) and note.lower().startswith("identity:")
                             and re.search(r"[А-Яа-яЁё]", note)), None)
        issues.append((russian_note[:177] + "…" if len(russian_note) > 180 else russian_note) if russian_note
                      else f"Внешность не совпадает с эталоном: {', '.join(mismatched)}. Проверьте лицо и причёску.")
    for key, bad, message in (
        ("duplicate", True, "Персонаж нарисован несколько раз. Проверьте состав кадра."),
        ("limbs_bad", True, "Судья обнаружил ошибку в руках или ногах. Проверьте анатомию."),
        ("text_or_margins", True, "Обнаружены надписи или поля. Проверьте изображение."),
        ("people_ok", False, "Состав персонажей не совпадает с заданием."),
        ("action_ok", False, "Действие расходится с описанием кадра. Проверьте позу, участников и предметы."),
        ("shot_ok", False, "Крупность плана не совпадает с заданием."),
        ("style_ok", False, "Манера рисунка отличается от соседнего кадра."),
    ):
        if row.get(key) is bad:
            issues.append(message)
    uncertain = [(key, label) for key, label in (("limbs_bad", "анатомию"), ("action_ok", "действие"),
                 ("shot_ok", "крупность плана"), ("style_ok", "единство стиля")) if key in row and row[key] is None]
    if uncertain:
        issues.append("Не удалось уверенно проверить: " + ", ".join(label for _, label in uncertain) + ".")
    if not issues:
        issues.append({"accept": "Замечаний нет.", "warn": "У судьи есть замечание. Откройте подробности проверки.",
                       "reject": "Кадр не прошёл проверку. Откройте подробности перед пересдачей."}.get(
                       frame.get("verdict"), "Проверка ещё не завершена или не дала уверенного ответа."))
    return issues


def project_for_browser(project):
    result = dict(project)
    result["steps"] = [dict(step, error_ui=error_view(step.get("error"), step.get("label", "")))
                       for step in project.get("steps", [])]
    # Сбой «Названия» ролик не останавливает: он виден у шага и в блоке «Название и описание», не в тревоге ролика.
    failed = next((step for step in result["steps"] if step.get("status") == "error" and step.get("key") != "title"), {})
    result["error_ui"] = error_view(project.get("error") or failed.get("error"), failed.get("label", ""))
    result["error_ui"]["title"] = "Сборка остановилась"
    if not project.get("error") and failed.get("key") == "cover" and project.get("video_ready"):
        # Ролик собран и лежит на месте: остановилась ОДНА обложка. Кричать «сборка
        # остановилась» на сданном ролике — неправда, из-за которой владелец полезет
        # искать поломку там, где её нет.
        result["error_ui"] = {**result["error_ui"], "title": "Ролик готов, обложка не сделана",
                              "message": "Сам ролик собран и его можно скачать. Не сделана только обложка.",
                              "hint": ("Кадры и черновики ролика уже убраны уборкой — обложку студия доделать не может."
                                       if project.get("archived") else
                                       "Нажмите «Повторить незавершённое» — студия доделает одну обложку.")}
    result["video_wait"] = video_wait_view(project)
    result["auto_ui"] = auto_retry_view(project)
    return result


SUMMARY_TTL = 30   # сводка на главной отстаёт от очереди не больше чем на 30 с
_summary_clock = time.monotonic   # подменяется в тестах


def auto_retry_view(project, now=None):
    """Отметка «повторено автоматически» (watch.py): чем был сбой и что с повтором сейчас."""
    auto = project.get("auto_retry")
    if not auto:
        return None
    import time as _time
    now = _time.time() if now is None else now
    status, start = auto.get("status"), auto.get("not_before")
    if status == "pending" and isinstance(start, (int, float)) and start > now:
        minutes = max(1, round((start - now) / 60))
        state = f"Повтор начнётся сам примерно через {minutes} мин."
    elif status == "pending":
        state = "Повтор ждёт своей очереди."
    elif status == "running":
        state = "Повтор идёт."
    elif status == "complete":
        state = "Повтор прошёл успешно."
    else:
        state = "Повтор тоже не удался. Больше сам не повторяю — нажмите «Повторить незавершённое» после проверки причины."
    return {"title": "Повторено автоматически", "reason": f"Временный сбой: {auto.get('reason', '')}", "state": state}


def create_app(store=None, root=None, worker_enabled=None):
    """Store injection lets HTTP tests run without generators, keys or a worker."""
    templates = Jinja2Templates(directory=str(WEB / "templates"))
    templates.env.filters["duration"] = duration
    templates.env.globals.update(statuses=STATUSES, verdicts=VERDICTS, file_url=file_url,
                                 languages=LANGUAGES, voices=VOICES)

    @asynccontextmanager
    async def lifespan(application):
        if application.state.store is None:
            try:
                from .web_queue import QueueStore
            except ImportError:
                from web_queue import QueueStore
            application.state.store = QueueStore(Path(root or ROOT))
        enabled = worker_enabled if worker_enabled is not None else os.getenv("MANGA_WORKER_ENABLED", "1") != "0"
        worker = None
        application.state.expected_worker = enabled
        application.state.worker = None
        if enabled:
            try:
                from .web_worker import Worker
            except ImportError:
                from web_worker import Worker
            worker = Worker(application.state.store)
            application.state.worker = worker
            worker.start()
        try:
            yield
        finally:
            if worker:
                worker.stop()

    application = FastAPI(title="Манхва-студия", lifespan=lifespan)
    application.state.store = store
    application.mount("/static", StaticFiles(directory=str(WEB / "static")), name="static")

    def db():
        if application.state.store is None:
            raise HTTPException(503, "Студия запускается. Обновите страницу через несколько секунд.")
        return application.state.store

    def render(request, template, *, code=200, **context):
        context.update(request=request, styles=STYLES, test_limit=os.getenv("MANGA_TEST_FRAME_LIMIT"))
        return templates.TemplateResponse(request=request, name=template, context=context, status_code=code)

    def project_or_404(project_id):
        if not ID_RE.fullmatch(project_id):
            raise HTTPException(404, "Ролик не найден.")
        try:
            return db().get_project(project_id)
        except KeyError:
            raise HTTPException(404, "Ролик не найден.")

    def project_view(project_id):
        project = project_or_404(project_id)
        worker = getattr(application.state, "worker", None)
        problem = getattr(worker, "problem", None)
        if problem and project["status"] in ("running", "pending"):
            # The queue loop itself is paused (web_worker.Worker.problem): every video in work or in line waits for it.
            project = {**project, "error": problem}
        wait = worker.wait_note(project_id) if project["status"] == "running" and hasattr(worker, "wait_note") else None
        if wait:
            project = {**project, "wait": wait}
        hold = getattr(worker, "disk_hold", None)
        if hold and project["status"] == "pending" and project.get("never_started"):
            project = {**project, "hold": hold}
        result = project_for_browser(project)
        root = Path(db().project_path(project_id))
        result["delivery_ui"] = delivery_view(result, (root / "out" / "cover.png").is_file(), publish_texts(root),
                                              cleanup_config(), cover_clean=cover_is_clean(root))
        result["publish"] = publish_view(project)
        return result

    def cleanup_config():
        worker = getattr(application.state, "worker", None)
        if worker is not None and isinstance(getattr(worker, "cleanup", None), dict):
            return {**worker.cleanup, **(getattr(worker, "delivery", None) or {})}
        try:
            from . import cleanup as cleanup_module
        except ImportError:
            import cleanup as cleanup_module
        try:
            return {**cleanup_module.settings(), **cleanup_module.delivery_settings()}
        except ValueError:
            return None

    def publish_view(project):
        """Блок «Название и описание»: готовый text/publish.json и состояние шага «Название»."""
        step = next((s for s in project.get("steps", []) if s.get("key") == "title"), {})
        try:
            done = PB.read(db().project_path(project["id"]))
        except (ValueError, OSError):
            done = None
        # Кнопка «Сделать заново» закрыта, пока у ролика идёт или ждёт любая задача: вторую очередь не примет.
        view = {"status": step.get("status", "pending"),
                "error": error_view(step.get("error"), "Название")["message"] if step.get("status") == "error" else "",
                "error_details": str(step.get("error") or "") if step.get("status") == "error" else "",
                "busy": step.get("status") == "running" or bool(project.get("title_job"))
                        or project.get("status") in ("running", "pending")}
        if done:
            warnings = list(done.get("warnings") or [])
            if PB.stale(db().project_path(project["id"]), done):
                warnings.append(PB.STALE)
            view.update(title=done.get("title", ""), description=done.get("description", ""),
                        tags=done.get("tags_text", ""), warnings=warnings,
                        title_length=len(done.get("title", "")))
        return view

    def frame_or_404(project_id, idx):
        project_or_404(project_id)
        if idx < 1:
            raise HTTPException(404, "Кадр не найден.")
        try:
            return db().get_frame(project_id, idx)
        except KeyError:
            raise HTTPException(404, "Кадр не найден.")

    def wants_json(request):
        return request.url.path.startswith("/api/") or "application/json" in request.headers.get("accept", "")

    def mutation_result(request, target, **data):
        if wants_json(request):
            return JSONResponse({"ok": True, "redirect": target, **data})
        return RedirectResponse(target, status_code=303)

    def render_notice(project_id):
        """What a frame retry will rebuild when the render drafts are already gone."""
        archive_info = getattr(db(), "archive_info", None)   # a store of an older release has no such state
        archive = archive_info(project_id) if archive_info else None
        if archive:
            try:
                from .web_queue import archived_refusal
            except ImportError:
                from web_queue import archived_refusal
            return archived_refusal(archive)
        try:
            from .cleanup import render_cache_removed as removed
        except ImportError:
            from cleanup import render_cache_removed as removed
        gone = removed(db().project_path(project_id))
        if not gone:
            return None
        if gone.get("error"):
            return (f"Не удалось проверить черновики сборки этого ролика: {gone['error']}. Пересдача от этого"
                    " не отменяется; если сборка не сможет собрать видео, причина появится на странице ролика.")
        if gone["missing"] >= gone["total"]:
            return ("Черновики сборки этого ролика уже удалены. После пересдачи видео соберётся заново"
                    " из всех кадров — это займёт столько же времени, сколько первая сборка.")
        return (f"Черновиков сборки не хватает: нет {gone['missing']} из {gone['total']} частей видео."
                " После пересдачи они соберутся заново вместе с этим кадром.")

    def source_view(project):
        """Откуда текст ролика: ссылка, название оригинала и объём; None — текст вставлен вручную."""
        if not project.get("source_url"):
            return None
        root = Path(db().project_path(project["id"]))
        meta = SRC.read_meta(root) or {}
        seconds = meta.get("duration_seconds")
        return {"url": project["source_url"], "title": meta.get("title") or "", "fetched": bool(meta),
                "chars": meta.get("chars"), "lines": meta.get("lines"),
                "language": meta.get("language") or "", "is_generated": meta.get("is_generated"),
                "duration": duration(seconds) if seconds else None,
                "title_from_source": bool(project.get("title_from_source")),
                "thumbnail": file_url(project["id"], "source/thumbnail.jpg") if (root / "source" / "thumbnail.jpg").is_file() else None,
                "proxies": subs_proxies_view()}

    def subs_proxies_view():
        """Прокси субтитров для страницы: по номеру, схеме и порту, без адресов. Ошибка настройки — текстом."""
        try:
            exits = SRC.proxies()
            rest = SRC.rest_minutes()
        except ValueError as exc:
            return {"rows": [], "error": str(exc), "rest": 0}
        return {"rows": SRC.proxies_status(SRC.state_path(db().root), exits), "error": "", "rest": rest}

    def cover_view(project):
        """Блок «Обложка»: наша обложка, оригинал донора и цена. None — шаг ещё ничего не сделал."""
        root = Path(db().project_path(project["id"]))
        state = CV.read_json(root / "out" / "cover.json", {}) or {}
        ours = (root / "out" / "cover.png").is_file()
        donor = (root / "source" / "thumbnail.jpg").is_file()
        if not ours and not donor:
            return None
        step = next((s for s in project.get("steps", []) if s["key"] == "cover"), {})
        return {"ready": ours,
                "cover": file_url(project["id"], "out/cover.png") if ours else None,
                "preview": file_url(project["id"], "out/cover_preview.jpg") if (root / "out" / "cover_preview.jpg").is_file() else None,
                "donor": file_url(project["id"], "source/thumbnail.jpg") if donor else None,
                "sheet": file_url(project["id"], "out/cover_sheet.jpg") if (root / "out" / "cover_sheet.jpg").is_file() else None,
                "donor_url": (state.get("donor") or {}).get("url") or project.get("source_url"),
                "texts": state.get("texts") or [], "by_code": state.get("texts_by_code") or [],
                # Все варианты кругов: мелко на странице, с оценкой выбирающего и кнопкой «взять этот».
                "variants": [{"key": v.get("key"), "round": v.get("round"), "k": v.get("k"),
                              "url": file_url(project["id"], v["file"]) if v.get("file") and (root / v["file"]).is_file() else None,
                              "gone": bool(v.get("file")) and not (root / v["file"]).is_file(),
                              "score": v.get("score"), "why": v.get("why") or "", "blocking": v.get("blocking") or [],
                              "chosen": v.get("key") == (state.get("chosen") or {}).get("key")}
                             for v in CV.all_variants(state)],
                "rounds": len(state.get("rounds") or []),
                "chosen_by": (state.get("chosen") or {}).get("by"),
                "judge_why": next((r.get("judge", {}).get("why_ru") for r in state.get("rounds") or []
                                   if r.get("n") == (state.get("chosen") or {}).get("round") and r.get("judge")), "") or "",
                "can_pick": bool(state.get("done")) and step.get("status") != "running",
                "problems": state.get("problems_left") or [],
                # «Буквы не совпали» в этой нише — брак обложки: её тоже прячем под
                # осознанное «всё равно скачать», а не отдаём кнопкой наравне с годной.
                "letters": [f'ожидалось «{f.get("want")}», прочитано «{f.get("read")}»'
                            for f in state.get("letter_faults") or []],
                # «reject» — пункты [СТОП] чек-листа: такую обложку не отдаём и скачать
                # её можно только осознанно, увидев причину.
                "verdict": state.get("verdict") or ("ok" if not (state.get("problems_left") or []) else "warn"),
                "units": state.get("units"), "status": step.get("status", "pending"),
                # Выбор оператора меняет заметку в cover.json, а не в строке шага очереди.
                "note": (state.get("note") if (state.get("chosen") or {}).get("by") == "operator" else None)
                        or step.get("note") or state.get("note") or ""}

    def safe_file(project_id, path, *, project_checked=False):
        if not project_checked:
            if not ID_RE.fullmatch(project_id) or not db().has_project(project_id):
                raise HTTPException(404, "Ролик не найден.")
        pdir = Path(db().project_path(project_id)).resolve()
        requested = Path(path)
        if requested.is_absolute() or any(p.startswith(".") for p in requested.parts):
            raise HTTPException(404, "Файл не найден.")
        full = (pdir / requested).resolve()
        try:
            full.relative_to(pdir)
        except ValueError:
            raise HTTPException(404, "Файл не найден.")
        # Only product media, never arbitrary logs, keys, source code or database files.
        if full.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"} and path != "out/video.mp4":
            raise HTTPException(404, "Этот файл недоступен для просмотра.")
        if not full.is_file():
            raise HTTPException(404, "Файл ещё не появился. Попробуйте позже.")
        return full

    @application.middleware("http")
    async def browser_safety(request, call_next):
        origin = request.headers.get("origin")
        if request.method == "POST" and origin and urlsplit(origin).netloc != request.url.netloc:
            return JSONResponse({"error": "Откройте форму в самой студии."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; media-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type in {"image/png", "image/jpeg", "image/webp"}:
            # Chrome's native raster viewer adds inline styles to its image document.
            # Keep document/SVG policies strict; only this raster response allows them.
            response.headers["Content-Security-Policy"] = response.headers["Content-Security-Policy"].replace(
                "style-src 'self'", "style-src 'self' 'unsafe-inline'")
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.exception_handler(HTTPException)
    async def http_error(request, exc):
        if wants_json(request):
            return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)
        return render(request, "error.html", code=exc.status_code, error=str(exc.detail))

    @application.exception_handler(sqlite3.OperationalError)
    async def queue_unavailable(request, exc):
        # The queue database cannot be read (full disk, no descriptors): the reason in Russian, not a bare 500.
        problem = getattr(getattr(application.state, "worker", None), "problem", None)
        if not problem:
            try:
                from .web_queue import queue_problem
            except ImportError:
                from web_queue import queue_problem
            problem = queue_problem(exc, db().root)
        if wants_json(request):
            return JSONResponse({"error": problem}, status_code=503)
        return render(request, "error.html", code=503, error=problem)

    @application.exception_handler(RequestValidationError)
    async def invalid_route(request, exc):
        message = "Не удалось прочитать запрос. Проверьте адрес и поля формы."
        if wants_json(request):
            return JSONResponse({"error": message}, status_code=422)
        return render(request, "error.html", code=422, error=message)

    @application.exception_handler(ValueError)
    async def invalid_value(request, exc):
        if wants_json(request):
            return JSONResponse({"error": str(exc)}, status_code=400)
        return render(request, "error.html", code=400, error=str(exc))

    @application.get("/health")
    def health():
        store = application.state.store
        worker = getattr(application.state, "worker", None)
        expected = getattr(application.state, "expected_worker", True)
        state = "running" if worker and worker._thread and worker._thread.is_alive() else "stopped"
        if not expected:
            state = "disabled"
        queue_root = str(getattr(store, "root", ""))
        # The loop's own pause is known even when the database cannot be read.
        problem = getattr(worker, "problem", None)
        paused = {"queue_problem": problem} if problem else {}
        try:
            if store is None:
                raise RuntimeError("Очередь ещё не открыта")
            if hasattr(store, "_db"):
                with store._db() as connection:
                    connection.execute("SELECT 1 FROM jobs LIMIT 1").fetchall()
            reason = problem or (store.paused_reason() if hasattr(store, "paused_reason") else None)
            if reason:
                # The service is up and nothing is lost, but no job moves until the cause is removed.
                paused = {"queue_problem": reason}
            hold = getattr(worker, "disk_hold", None)
            if hold:
                # Not an error: the service works, only new videos wait for disk space (MANGA_DISK_MIN_GB).
                paused = {**paused, "disk_hold": hold}
            if expected and state != "running":
                raise RuntimeError("Обработчик очереди остановился")
            if problem:
                raise RuntimeError(problem)  # the worker is alive but cannot use the queue files: not ready
        except Exception:
            return JSONResponse({"status": "error", "worker": state, "queue_root": queue_root, **paused}, status_code=503)
        return {"status": "ok", "worker": state, "queue_root": queue_root, **paused}

    def channel_table():
        """(пресеты, ошибка файла): битый channels.json — причина на странице, а не 500."""
        try:
            return CH.load(db().root), None
        except CH.ChannelsError as exc:
            return [], str(exc)

    def filtered_projects(channel):
        projects = db().list_projects()
        if channel == NO_CHANNEL:
            return [p for p in projects if not p.get("channel")]
        if channel:
            return [p for p in projects if p.get("channel") == channel]
        return projects

    def filter_options(projects):
        """Каналы для фильтра: из таблицы и из уже созданных роликов (канал могли убрать из файла)."""
        options = {ch["key"]: ch["name"] for ch in channel_table()[0]}
        for p in projects:
            if p.get("channel") and p["channel"] not in options:
                options[p["channel"]] = p.get("channel_name") or p["channel"]
        return sorted(options.items(), key=lambda kv: kv[1].casefold())

    @application.get("/", response_class=HTMLResponse)
    def index(request: Request, channel: str = ""):
        channel = channel.strip()
        everything = db().list_projects()
        return render(request, "index.html", projects=filtered_projects(channel), channel=channel,
                      channel_options=filter_options(everything), no_channel=NO_CHANNEL,
                      summary=WT.summarize(everything),
                      disk_hold=getattr(getattr(application.state, "worker", None), "disk_hold", None))

    summary_cache = {"at": 0.0, "value": None}

    @application.get("/api/summary")
    def summary_api():
        """Сводка за сутки по всем каналам: готово, идёт, ждёт, ошибок и ссылки на упавшие.
        Считается не чаще раза в SUMMARY_TTL секунд: главная опрашивается каждые 4 с, а обход всех роликов дорог."""
        now = _summary_clock()
        if summary_cache["value"] is None or now - summary_cache["at"] >= SUMMARY_TTL:
            summary_cache["value"], summary_cache["at"] = WT.summarize(db().list_projects()), now
        return summary_cache["value"]

    def batch_page(request, *, code=200, text="", default="", results=None, error=None):
        channels, file_error = channel_table()
        return render(request, "batch.html", code=code, channels=channels, file_error=file_error,
                      channels_path=str(CH.path(db().root)), text=text, default=default,
                      results=results, error=error, max_lines=CH.MAX_LINES)

    @application.get("/batch", response_class=HTMLResponse)
    def batch_form(request: Request):
        return batch_page(request)

    @application.post("/batch")
    async def batch_create(request: Request):
        """Пачка «ссылка ; канал»: годные строки становятся роликами, плохие — ошибка с номером строки.
        Пара «канал + ролик-донор» создаётся один раз: повторная отправка той же пачки дублей не даёт."""
        form = await request.form(max_files=0, max_fields=4)
        text, default_key = str(form.get("lines", "")), str(form.get("channel", "")).strip()
        try:
            if len(text) > MAX_BATCH_CHARS:
                raise ValueError(f"Слишком много текста: до {CH.MAX_LINES} строк «ссылка ; канал».")
            try:
                channels = CH.load(db().root)
            except CH.ChannelsError as exc:
                raise ValueError(f"Таблица каналов с ошибкой, пачка не принята: {exc}") from None
            if not channels:
                raise ValueError("Таблица каналов пуста: сначала заполните файл каналов (подсказка ниже).")
            default = CH.find(channels, default_key) if default_key else None
            if default_key and default is None:
                raise ValueError("Канал для всей пачки не найден в таблице. Обновите страницу.")
            good, errors, repeats = CH.parse_batch(text, channels, default)
            limit = os.getenv("MANGA_TEST_FRAME_LIMIT")
            if limit and (not limit.isdigit() or not 1 <= int(limit) <= 12):
                raise HTTPException(503, "Неверная настройка проверочного прогона. Нужен предел от 1 до 12 кадров.")
        except ValueError as exc:
            if wants_json(request):
                return JSONResponse({"error": str(exc)}, status_code=400)
            return batch_page(request, code=400, text=text, default=default_key, error=str(exc))
        results = []
        for item in good:
            ch, token = item["channel"], CH.request_token(item["channel"], item["video_id"])
            row = dict(line=item["line"], url=item["url"], channel=ch["name"], project_id=None)
            existing = db().project_for_request(token)
            if existing:
                results.append({**row, "status": "exists", "project_id": existing,
                                "message": "уже был заказан для этого канала — новый не создан"})
                continue
            try:
                project = db().create_project("", "", ch["minutes"], ch["style"], ch["language"], ch["voice"],
                                              frame_limit=int(limit) if limit else None, idempotency_key=token,
                                              source_url=item["url"], channel=ch)
            except ValueError as exc:
                results.append({**row, "status": "error", "message": str(exc)})
                continue
            if project.get("reused"):   # вторая такая же пачка успела раньше — ролик тот же, не новый
                results.append({**row, "status": "exists", "project_id": project["id"],
                                "message": "уже был заказан для этого канала — новый не создан"})
                continue
            results.append({**row, "status": "created", "project_id": project["id"],
                            "message": f"{ch['minutes']} мин · {ch['style']} · {LANGUAGES[ch['language']]}"})
        for e in errors:
            results.append(dict(line=e["line"], url=e["text"], channel="", project_id=None, status="error",
                                message=e["error"]))
        for r in repeats:
            results.append(dict(line=r["line"], url=r["text"], channel="", project_id=None, status="repeat",
                                message=f"повтор строки {r['same_as']} — пропущена"))
        results.sort(key=lambda r: r["line"])
        summary = {k: sum(r["status"] == k for r in results) for k in ("created", "exists", "error", "repeat")}
        if wants_json(request):
            return JSONResponse({"ok": True, "results": results, "summary": summary})
        return batch_page(request, text=text, default=default_key, results=results)

    @application.get("/new", response_class=HTMLResponse)
    def new_form(request: Request):
        return render(request, "new.html", values={}, error=None)

    @application.post("/new")
    async def new_project(request: Request):
        form = await request.form(max_files=1, max_fields=10, max_part_size=MAX_SOURCE_BYTES)
        names = ("title", "source_text", "minutes", "style", "language", "voice")
        values = {key: str(form.get(key, "")) for key in names}
        try:
            title = values["title"].strip()
            source = values["source_text"].strip()
            upload = form.get("file")
            if upload and getattr(upload, "filename", ""):
                if not upload.filename.lower().endswith(".txt"):
                    raise ValueError("Загрузите текстовый файл .txt.")
                data = await upload.read(MAX_SOURCE_BYTES + 1)
                if len(data) > MAX_SOURCE_BYTES:
                    raise ValueError("Файл слишком большой. Максимум — 2 МБ.")
                if source:
                    raise ValueError("Оставьте один исходник: текст в поле или файл .txt.")
                try:
                    source = data.decode("utf-8-sig").strip()
                except UnicodeDecodeError:
                    raise ValueError("Не удалось прочитать файл. Сохраните .txt в кодировке UTF-8.")
                values["source_text"] = source
            link = None
            if (not SRC.looks_like_link(source) and len(source) < 200
                    and re.search(r"(?i)(?:youtube\.com|youtu\.be)/", source)):
                raise ValueError("Похоже на ссылку с лишним текстом. Оставьте в поле только ссылку "
                                 "на ролик YouTube или вставьте текст истории целиком.")
            if SRC.looks_like_link(source):   # ссылка в поле или единственной строкой файла .txt
                if not SRC.video_id(source):
                    raise ValueError("Ссылка не похожа на ролик YouTube. Вставьте адрес вида "
                                     "https://www.youtube.com/watch?v=… или текст истории.")
                link = source
            if (title or not link) and (not title or len(title) > 120):
                raise ValueError("Введите название ролика — от 1 до 120 символов. При ссылке на YouTube его можно не вводить.")
            if not source:
                raise ValueError("Вставьте ссылку на ролик YouTube, текст истории или загрузите файл .txt.")
            if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
                raise ValueError("Исходный текст слишком большой. Максимум — 2 МБ.")
            try:
                minutes = int(values["minutes"] or "40")
            except ValueError:
                raise ValueError("Укажите длительность целым числом от 1 до 180 минут.")
            if not 1 <= minutes <= 180:
                raise ValueError("Длительность должна быть от 1 до 180 минут.")
            style = values["style"] or "S0"
            language = values["language"] or "Russian"
            voice = values["voice"] or (VOICES[language][0][0] if language in VOICES else "")
            if style not in {s["key"] for s in STYLES}:
                raise ValueError("Выберите один из четырёх стилей.")
            if language not in VOICES or voice not in {v[0] for v in VOICES[language]}:
                raise ValueError("Выберите голос для выбранного языка.")
            limit = os.getenv("MANGA_TEST_FRAME_LIMIT")
            if limit and (not limit.isdigit() or not 1 <= int(limit) <= 12):
                raise HTTPException(503, "Неверная настройка проверочного прогона. Нужен предел от 1 до 12 кадров.")
            extra = {"source_url": link} if link else {}
            project = db().create_project(title, "" if link else source, minutes, style, language, voice,
                                          frame_limit=int(limit) if limit else None, **extra)
        except ValueError as exc:
            if wants_json(request):
                return JSONResponse({"error": str(exc)}, status_code=400)
            return render(request, "new.html", code=400, values=values, error=str(exc))
        return mutation_result(request, f"/p/{project['id']}", project_id=project["id"])

    @application.get("/api/projects")
    def projects_api(channel: str = ""):
        return filtered_projects(channel.strip())

    @application.get("/api/projects/{project_id}")
    def project_api(project_id: str):
        return project_view(project_id)

    @application.get("/api/projects/{project_id}/report")
    def report_api(project_id: str):
        project_or_404(project_id)
        return db().report(project_id)

    def frame_for_browser(project_id, frame):
        # A successful retry can begin and finish between polls. Its URL remains
        # frames/00001.png, so expose an HTTP-only revision without changing Store.
        result = dict(frame)
        result["issues"] = frame_issues(frame)
        result["image_version"] = None
        if frame.get("image"):
            try:
                image = safe_file(project_id, frame["image"], project_checked=True)
                stamp = image.stat()
                result["image_version"] = f"{stamp.st_mtime_ns}-{stamp.st_size}"
            except (HTTPException, OSError):
                pass
        return result

    @application.get("/api/projects/{project_id}/frames")
    def frames_api(project_id: str, compact: bool = False):
        project_or_404(project_id)
        frames = [frame_for_browser(project_id, frame) for frame in db().list_frames(project_id, include_details=not compact)]
        if compact:
            # Grid polls do not display prompts, blanks, references or raw judge rows.
            fields = ("idx", "scene_num", "verdict", "issues", "image", "image_version",
                      "manual_accepted", "busy")
            return [{key: frame.get(key) for key in fields} for frame in frames]
        return frames

    @application.get("/api/projects/{project_id}/frame/{idx}")
    def frame_api(project_id: str, idx: int):
        return frame_for_browser(project_id, frame_or_404(project_id, idx))

    @application.get("/p/{project_id}", response_class=HTMLResponse)
    def project_page(request: Request, project_id: str):
        project = project_view(project_id)
        # Каналы на ДРУГИХ языках: тот же сюжет можно разложить по ним, переведя текст по долям.
        try:
            others = [c for c in CH.load(db().root) if c["language"] != project.get("language")]
        except CH.ChannelsError:
            others = []
        return render(request, "project.html", project=project, report=db().report(project_id),
                      source=source_view(project), other_channels=others, languages=LANGUAGES,
                      can_translate=bool(others) and bool(project.get("video_ready")),
                      cover=cover_view(project))

    @application.get("/api/subs-proxies")
    def subs_proxies_api():
        return subs_proxies_view()

    @application.post("/subs-proxies/check")
    async def subs_proxies_check(request: Request):
        form = await request.form(max_fields=2, max_files=0)
        target = str(form.get("next", "")).strip()
        if not re.fullmatch(r"/p/[\w-]+", target):
            target = "/"
        exits = SRC.proxies()   # неверная настройка — ValueError, страница покажет причину
        from starlette.concurrency import run_in_threadpool
        rows = await run_in_threadpool(SRC.check_proxies, SRC.state_path(db().root), exits)
        alive = sum(1 for r in rows if r["alive"])
        return mutation_result(request, target + "#source-panel",
                               message=f"Прокси проверены: отвечают {alive} из {len(rows)}.", rows=rows)

    @application.post("/p/{project_id}/retry")
    def retry_project(request: Request, project_id: str):
        project_or_404(project_id)
        db().enqueue(project_id, action="retry")
        return mutation_result(request, f"/p/{project_id}", message="Незавершённые шаги добавлены в очередь.")

    @application.post("/p/{project_id}/title")
    def remake_title(request: Request, project_id: str):
        project_or_404(project_id)
        job = db().enqueue(project_id, action="title")
        if job["action"] != "title":
            message = ("Ролик сейчас в работе. Название сделает шаг «Название» в этой работе; "
                       "если оно уже готово — нажмите «Сделать заново» после её окончания.")
        else:
            message = "Название и описание добавлены в очередь. Готовое появится здесь через минуту-две."
        return mutation_result(request, f"/p/{project_id}", message=message)

    @application.post("/p/{project_id}/language")
    async def same_story_other_language(request: Request, project_id: str):
        """Тот же сюжет на другом языке: берём у этого ролика разбор истории и раскадровку,
        текст переводим по долям, картинки рисуем заново своим зерном."""
        project = project_or_404(project_id)
        form = await request.form(max_files=0, max_fields=4)
        key = str(form.get("channel", "")).strip()
        try:
            channels = CH.load(db().root)
            ch = CH.find(channels, key) if key else None
            if ch is None:
                raise ValueError("Выберите канал: у него берутся язык, голос и стиль.")
            if ch["language"] == project.get("language"):
                raise ValueError("У этого канала тот же язык, что у ролика. Перевод не нужен: "
                                 "закажите обычный ролик по ссылке.")
            token = f"lang:{project_id}:{ch['key']}"
            existing = db().project_for_request(token)
            if existing:
                return mutation_result(request, f"/p/{existing}",
                                       message="Такой перевод уже заказан — вот он.")
            # Название — эталона (так же делал ночной скрипт переводов 21.09): пустое название
            # create_project отвергает, и кнопка не могла создать ни одного перевода.
            made = db().create_project(project.get("title") or f"Перевод {project_id[:8]}", "",
                                       project.get("minutes") or 40, project.get("style") or "S0",
                                       ch["language"], ch["voice"], idempotency_key=token,
                                       channel=ch, source_project=project_id)
        except (ValueError, CH.ChannelsError) as exc:
            return mutation_result(request, f"/p/{project_id}", message=str(exc))
        return mutation_result(request, f"/p/{made['id']}",
                               message=f"Готовлю тот же сюжет на канале «{ch['name']}»: текст переведётся "
                                       f"по долям, картинки будут свои.")

    @application.post("/p/{project_id}/cover/pick")
    async def pick_cover(request: Request, project_id: str):
        """«Взять другой»: обложкой становится другой уже нарисованный вариант, без покупки."""
        project = project_or_404(project_id)
        form = await request.form(max_fields=2, max_files=0)
        key = str(form.get("variant", "")).strip()
        if not re.fullmatch(r"\d{1,2}-\d{1,2}", key):
            raise HTTPException(400, "Не указан вариант обложки.")
        step = next((s for s in project.get("steps", []) if s["key"] == "cover"), {})
        if step.get("status") == "running":
            raise HTTPException(409, "Обложка ещё делается — переключить можно после шага «Обложка».")
        try:
            # Чистка выбранной (Real-ESRGAN — ssh на машину с видеокартой) идёт минутами: в потоке,
            # чтобы не вставал весь веб-сервер студии.
            picked = await run_in_threadpool(CV.pick_variant, db().project_path(project_id), key)
        except CV.CoverError as exc:
            raise HTTPException(409, str(exc))
        if step.get("status") == "complete" and (picked or {}).get("note"):
            db().step(project_id, "cover", "complete", note=picked["note"])
        return mutation_result(request, f"/p/{project_id}#cover-panel", message="Обложка переключена, без покупки.")

    @application.get("/p/{project_id}/frames", response_class=HTMLResponse)
    def frames_page(request: Request, project_id: str):
        project = project_view(project_id)
        frames = [frame_for_browser(project_id, frame) for frame in db().list_frames(project_id, include_details=False)]
        scenes = sorted({str(f.get("scene_num")) for f in frames if f.get("scene_num") is not None}, key=lambda s: (not s.isdigit(), int(s) if s.isdigit() else s))
        return render(request, "frames.html", project=project, frames=frames, scenes=scenes)

    @application.get("/p/{project_id}/frame/{idx}", response_class=HTMLResponse)
    def frame_page(request: Request, project_id: str, idx: int):
        frame = frame_for_browser(project_id, frame_or_404(project_id, idx))
        project = project_or_404(project_id)
        ids = sorted(f["idx"] for f in db().list_frames(project_id, include_details=False))
        return render(request, "frame.html", project=project, frame=frame,
                      render_notice=render_notice(project_id),
                      previous=max((i for i in ids if i < idx), default=None),
                      following=min((i for i in ids if i > idx), default=None),
                      blank_json=json.dumps(frame.get("blank", {}), ensure_ascii=False, indent=2))

    @application.post("/p/{project_id}/frame/{idx}/retry")
    async def retry_frame(request: Request, project_id: str, idx: int):
        frame_or_404(project_id, idx)
        form = await request.form(max_fields=2, max_files=0)
        correction = str(form.get("correction", "")).strip()
        if len(correction) > 3000:
            raise ValueError("Сократите правку до 3000 символов.")
        db().enqueue(project_id, action="frame_retry", frame_id=idx, correction=correction)
        notice = render_notice(project_id)
        message = "Кадр добавлен в очередь на пересдачу." + (" " + notice if notice else "")
        return mutation_result(request, f"/p/{project_id}/frame/{idx}", message=message)

    @application.post("/p/{project_id}/frame/{idx}/accept")
    def accept_frame(request: Request, project_id: str, idx: int):
        frame_or_404(project_id, idx)
        db().accept_frame(project_id, idx)
        return mutation_result(request, f"/p/{project_id}/frame/{idx}", message="Кадр принят вами. Исходная оценка сохранена.")

    @application.get("/p/{project_id}/file/{path:path}")
    def media_file(project_id: str, path: str):
        if path == "out/video.mp4" and not project_or_404(project_id).get("video_ready"):
            raise HTTPException(409, "Видео обновляется. Актуальная версия появится после сборки. "
                                     "Во время шага «Обложка» ролик доступен: он уже собран.")
        full = safe_file(project_id, path)
        return FileResponse(full, headers={"Cache-Control": "no-cache"})

    @application.get("/p/{project_id}/download")
    def download(request: Request, project_id: str):
        project = project_or_404(project_id)
        if not project.get("video_ready"):
            raise HTTPException(409, "Ролик ещё собирается. Скачивание появится после сборки.")
        full = safe_file(project_id, "out/video.mp4")
        name = download_stem(project) + "_video.mp4"
        if request.headers.get("range"):
            # A resumed or partial download: served as before, but it proves nothing about the whole file.
            return FileResponse(full, media_type="video/mp4", filename=name)
        stream = full.open("rb")
        st = os.fstat(stream.fileno())
        stamp = f"{st.st_size}:{st.st_mtime_ns}"   # the version really sent, even if a retry replaces the file meanwhile

        def body():
            with stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
            # Reached only after the last chunk was handed to the server: an interrupted download never gets here.
            try:
                db().mark_delivered(project_id, "download", stamp=stamp)
            except Exception as exc:  # noqa: BLE001 — the file is already sent; the mark is a convenience
                print(f"отметка «скачано» {project_id} не записана: {type(exc).__name__}: {exc}", flush=True)

        return StreamingResponse(body(), media_type="video/mp4", headers={
            "Content-Length": str(st.st_size), "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-cache"})

    @application.get("/p/{project_id}/download/cover")
    def download_cover(project_id: str, confirm: int = 0):
        project = project_or_404(project_id)
        if not project.get("video_ready"):
            raise HTTPException(409, "Ролик ещё собирается. Обложка скачивается вместе с готовым роликом.")
        if not confirm and not cover_is_clean(db().project_path(project_id)):
            raise HTTPException(409, "У обложки есть замечания — посмотрите блок «Обложка» на странице ролика "
                                     "и скачайте её там кнопкой «Всё равно скачать обложку».")
        try:
            full = safe_file(project_id, "out/cover.png", project_checked=True)
        except HTTPException:
            raise HTTPException(404, "Обложки у этого ролика нет.")
        return FileResponse(full, media_type="image/png", filename=download_stem(project) + "_cover.png")

    @application.get("/p/{project_id}/download/texts")
    def download_texts(project_id: str):
        project = project_or_404(project_id)
        texts = publish_texts(db().project_path(project_id))
        if not texts:
            raise HTTPException(404, "Названия и описания у этого ролика пока нет.")
        return PlainTextResponse(publish_text_file(texts), headers={
            "Content-Disposition": f'attachment; filename="{download_stem(project)}_texts.txt"'})

    @application.post("/p/{project_id}/delivered")
    async def delivered(request: Request, project_id: str):
        project_or_404(project_id)
        form = await request.form(max_fields=2, max_files=0)
        if str(form.get("undo", "")) == "1":
            db().unmark_delivered(project_id)
            return mutation_result(request, f"/p/{project_id}", message="Отметка «скачано» снята.")
        db().mark_delivered(project_id, "button")
        config = cleanup_config() or {}
        hours = config.get("delivered_hours")
        tail = ("" if hours is None else " Кадры и черновики удалятся при ближайшей уборке (раз в час)"
                + ("." if hours == 0 else f", не раньше чем через {hours} ч."))
        return mutation_result(request, f"/p/{project_id}", message="Ролик отмечен скачанным." + tail)

    return application


app = create_app()
