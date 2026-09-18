#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Вкладка «Каналы»: каталог, карточка канала и очередь донорских ссылок.

Отдельный модуль, а не правка main.py: в основном файле остаётся одна строка вызова,
а всё новое можно проверять тестами и читать целиком.

Устройство карточки: она же и форма. Отдельной страницы редактирования нет — всё
меняется на месте и сохраняется одной кнопкой. Ссылки на рерайт вставляются пачкой
прямо в карточке, система сама раскладывает их по ближайшим свободным выходам.

Осторожно: штатный разбор формы в кабинете берёт из поля ТОЛЬКО первое значение,
поэтому здесь свой разбор — иначе чекбоксы дней недели молча теряются.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import urllib.parse

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from channel_slots import (
    DAY_KEYS,
    ScheduleError,
    normalize_schedule,
    parse_time,
    upcoming_slots,
)
from pub_log import read as read_log
from pub_neighbors import (
    COVER_URL,
    REWRITE_URL,
    cover_presets,
    rewrite_presets,
    vsgen_presets,
)
from pub_tags import (
    add_links,
    all_tags,
    channel_state,
    coverage,
    move_item,
    parse_links,
    parse_tags,
    plan_by_channel,
    plan_rows,
    release_stages,
    upload_jobs_by_release,
    cancel_item,
    releases_by_item,
    set_tags,
    tag_names,
    troubles_by_channel,
    work_by_channel,
)

UC_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
PER_PAGE = 60

DAY_TITLES = {"mon": "Пн", "tue": "Вт", "wed": "Ср", "thu": "Чт",
              "fri": "Пт", "sat": "Сб", "sun": "Вс"}

FLAGS = {"pl": "🇵🇱", "cs": "🇨🇿", "sk": "🇸🇰", "ro": "🇷🇴", "hr": "🇭🇷", "el": "🇬🇷",
         "hu": "🇭🇺", "ru": "🇷🇺", "de": "🇩🇪", "fr": "🇫🇷", "it": "🇮🇹", "es": "🇪🇸",
         "pt": "🇵🇹", "po": "🇵🇹", "fi": "🇫🇮", "bg": "🇧🇬", "uk": "🇺🇦", "en": "🇬🇧",
         "tr": "🇹🇷", "nl": "🇳🇱", "sr": "🇷🇸", "sl": "🇸🇮", "lt": "🇱🇹", "lv": "🇱🇻"}

LANG_TITLES = {"pl": "польский", "cs": "чешский", "sk": "словацкий", "ro": "румынский",
               "hr": "хорватский", "el": "греческий", "hu": "венгерский", "ru": "русский",
               "de": "немецкий", "fr": "французский", "it": "итальянский", "es": "испанский",
               "pt": "португальский", "po": "португальский", "fi": "финский",
               "bg": "болгарский", "uk": "украинский", "en": "английский"}

STATUS_TITLES = {"active": "работает", "paused": "пауза", "archived": "в архиве"}
STATUS_BADGE = {"active": "badge-done", "paused": "badge-queued", "archived": "badge-cancelled"}

PLAN_TITLES = {"planned": "ждёт очереди", "frozen": "взято в работу", "running": "готовится",
               "done": "готово", "error": "сбой"}

# Что показывать владельцу вместо внутренних имён фаз.
PHASE_TITLES = {
    "created": "начинаю",
    "source_ready": "делаю текст",
    "project_ready": "собираю проект",
    "media_ready": "озвучка, картинки, рендер",
    "cover_ready": "делаю обложку",
    "upload_pending": "передаю на заливку",
    "upload_running": "заливаю",
    "scheduled": "стоит в расписании",
    "published": "опубликован",
}


def release_state(rel) -> tuple:
    """(текст, класс бейджа) — состояние выпуска человеческим языком."""
    if rel is None:
        return "", ""
    if rel["status"] == "blocked":
        return "сбой: " + (rel["blocked_reason"] or "нужен разбор"), "badge-error"
    if rel["status"] == "done":
        return "опубликован", "badge-done"
    phase = PHASE_TITLES.get(rel["phase"], rel["phase"] or "начинаю")
    if rel["blocked_reason"] and rel["blocked_reason"].startswith("ждём"):
        return phase + " · " + rel["blocked_reason"], "badge-queued"
    return phase, "badge-running"


class ChannelError(ValueError):
    """Ошибка ввода, текст которой можно показать владельцу как есть."""


# ─────────────────────────── разбор формы ───────────────────────────

async def form_multi(request) -> dict:
    """Разбор формы С СОХРАНЕНИЕМ всех значений ключа."""
    raw = (await request.body()).decode("utf-8", "replace")
    return urllib.parse.parse_qs(raw, keep_blank_values=True)


def one(form: dict, key: str, default: str = "") -> str:
    vals = form.get(key) or []
    return (vals[0] if vals else default).strip()


def many(form: dict, key: str) -> list:
    return [v.strip() for v in (form.get(key) or []) if v.strip()]


# ─────────────────────────── проверки ───────────────────────────

def clean_uc(value: str):
    """Пустое значение — законный случай (пункт 6.1 ТЗ): канал иногда заводят раньше,
    чем узнают его настоящий ID на YouTube. Пусто → None, а не "" — несколько пустых
    строк в UNIQUE-колонке SQLite считает дублями, а несколько NULL — нет."""
    uc = (value or "").strip()
    if not uc:
        return None
    if not UC_RE.match(uc):
        raise ChannelError("ID канала должен быть вида UC и ещё 22 символа, например "
                           "UCK8sQmJBp8GCxrOtXWBpyEA. Получено: " + uc)
    return uc


def clean_schedule(times_raw: str, days: list) -> str:
    times = [t for t in re.split(r"[,\s]+", times_raw or "") if t]
    picked_raw = [d for d in days if d in DAY_KEYS]
    if not times:
        # Дни без времени — расписания не выходит: слот задаётся временем, и до
        # 18.09.2026 такая форма молча сохранялась как «расписания нет» (владелец
        # отметил дни, нажал «Сохранить» и увидел, что они не сохранились). Пустая
        # форма целиком по-прежнему законна: так расписание и снимают.
        if picked_raw:
            raise ScheduleError("выбраны дни, но не задано время выходов — "
                                "укажите время или снимите дни")
        return "{}"
    for t in times:
        parse_time(t)
    picked = picked_raw
    schedule = {"*": times} if (not picked or len(picked) == 7) else {d: times for d in picked}
    normalize_schedule(schedule)
    return json.dumps(schedule, ensure_ascii=False)


def clean_int(value: str, field: str, lo: int, hi: int, default: int) -> int:
    text = (value or "").strip()
    if not text:
        return default
    try:
        num = int(text)
    except ValueError:
        raise ChannelError(f"{field}: нужно целое число, получено {text!r}")
    if not (lo <= num <= hi):
        raise ChannelError(f"{field}: допустимо от {lo} до {hi}, получено {num}")
    return num


def read_channel_form(form: dict) -> dict:
    """Собрать поля канала из формы и проверить их. UC здесь не трогается."""
    name = one(form, "name")
    if not name:
        raise ChannelError("название канала обязательно")
    tz = one(form, "tz", "Europe/Warsaw") or "Europe/Warsaw"
    schedule = clean_schedule(one(form, "times"), many(form, "days"))
    if schedule != "{}":
        upcoming_slots(json.loads(schedule), tz, 1)

    collab = one(form, "collaborator")
    if collab and not collab.startswith("@"):
        collab = "@" + collab

    return {
        "name": name,
        "handle": one(form, "handle"),
        "lang": one(form, "lang").lower()[:8],
        "niche": one(form, "niche"),
        "tz": tz,
        "schedule": schedule,
        "horizon_days": clean_int(one(form, "horizon_days"), "горизонт", 1, 90, 3),
        "deferred": 1 if one(form, "deferred") in ("1", "on", "true") else 0,
        "deferred_limit": clean_int(one(form, "deferred_limit"), "лимит отложенных", 1, 200, 5),
        "lead_days": clean_int(one(form, "lead_days"), "готовить заранее", 0, 30, 2),
        "profile_id": clean_int(one(form, "profile_id"), "профиль AdsPower", 0, 99999, 0) or None,
        "host_id": clean_int(one(form, "host_id"), "машина", 0, 99999, 0) or None,
        "preset_rewrite": one(form, "preset_rewrite"),
        "preset_vsgen_id": clean_int(one(form, "preset_vsgen_id"), "пресет VideoStudio",
                                     0, 10**9, 0) or None,
        "preset_cover": one(form, "preset_cover"),
        "collaborator": collab,
        "note": one(form, "note"),
    }


def import_channels(c, tenant_id: str, payload: str, now: float = None) -> dict:
    """Перенести каналы списком. Возвращает {created, updated, rejected: [строки]}.

    ВЫНЕСЕНО 04.09.2026 из роута: тот же перенос делает пульт. Правило «пустая колонка
    ничего не затирает» живёт в `read_import_row` и должно остаться единственным —
    иначе один интерфейс будет стирать то, что другой бережёт.
    """
    import time as _t
    now = _t.time() if now is None else now
    items = parse_import(payload)
    created = updated = 0
    rejected = []
    for idx, item in enumerate(items, 1):
        try:
            uc, fields = read_import_row(item)
        except (ChannelError, ScheduleError) as exc:
            rejected.append(f"строка {idx}: {exc}")
            continue
        old = c.execute("SELECT id, tenant_id FROM pub_channels WHERE uc=?",
                        (uc,)).fetchone()
        if old:
            if old["tenant_id"] != tenant_id:
                rejected.append(f"строка {idx}: канал заведён другим пользователем")
                continue
            if not fields:
                rejected.append(f"строка {idx}: нечего обновлять")
                continue
            sets = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE pub_channels SET {sets}, updated=? WHERE id=? AND tenant_id=?",
                      (*fields.values(), now, old["id"], tenant_id))
            updated += 1
        else:
            if not fields.get("name"):
                rejected.append(f"строка {idx}: у нового канала обязательно название")
                continue
            cols = ["tenant_id", "uc", *fields.keys(), "created", "updated"]
            c.execute(f"INSERT INTO pub_channels({','.join(cols)}) "
                      f"VALUES({','.join('?' * len(cols))})",
                      (tenant_id, uc, *fields.values(), now, now))
            created += 1
    c.commit()
    return {"created": created, "updated": updated, "rejected": rejected}


def create_channel(c, tenant_id: str, uc: str, name: str, lang: str = "",
                   niche: str = "", now: float = None) -> int:
    """Завести канал. Возвращает его id.

    ВЫНЕСЕНО 04.09.2026 из роута: ту же запись делает пульт, и вторая её копия
    разошлась бы с первой на первом же новом столбце.
    """
    import time as _t
    now = _t.time() if now is None else now
    uc = clean_uc(uc)
    name = (name or "").strip()
    if not name:
        raise ChannelError("название канала обязательно")
    cur = c.execute(
        "INSERT INTO pub_channels(tenant_id,uc,name,lang,niche,created,updated)"
        " VALUES(?,?,?,?,?,?,?)",
        (tenant_id, uc, name, (lang or "").lower()[:8], (niche or "").strip(), now, now))
    c.commit()
    return cur.lastrowid


def update_channel(c, tenant_id: str, cid: int, data: dict, now: float = None) -> None:
    """Сохранить настройки канала. `data` — то, что вернул `read_channel_form`.

    UC сюда намеренно не входит: он «не меняется после создания» (см. комментарий в
    `pub_schema.py`). Дописать пустой ID можно — но отдельной функцией `set_channel_uc`
    ниже, а не через эту: та ничего не проверяет, кроме «поле раньше было пусто», а
    все остальные поля формы настроек проверяет `read_channel_form`."""
    import time as _t
    now = _t.time() if now is None else now
    c.execute(
        "UPDATE pub_channels SET name=?,handle=?,lang=?,niche=?,profile_id=?,host_id=?,tz=?,"
        "schedule=?,horizon_days=?,lead_days=?,deferred=?,deferred_limit=?,"
        "preset_rewrite=?,preset_vsgen_id=?,"
        "preset_cover=?,collaborator=?,note=?,updated=? WHERE id=? AND tenant_id=?",
        (data["name"], data["handle"], data["lang"], data["niche"], data["profile_id"],
         data["host_id"], data["tz"], data["schedule"], data["horizon_days"],
         data["lead_days"], data["deferred"], data["deferred_limit"],
         data["preset_rewrite"], data["preset_vsgen_id"],
         data["preset_cover"], data["collaborator"], data["note"], now, cid, tenant_id))


def set_channel_uc(c, tenant_id: str, cid: int, uc: str, now: float = None) -> None:
    """Вписать ID YouTube каналу, у которого его ещё нет (дополнение 16.09, вопрос 1,
    вариант «а»). Зовёт только пульт (`web/app.py`) со страницы канала, вкладка
    «Настройки» — кабинет (`server/pub_ui.py` формы настроек, `register_pub_routes`)
    эту возможность не получает по прямому указанию владельца: он работает с ID
    исключительно в пульте.

    Уже заданный UC — read-only навсегда: он определяет, куда льётся видео, и смена
    задним числом означает ролик на чужом канале (прецедент, из-за которого и введено
    правило «UC не меняется после создания», см. `pub_schema.py`). Поэтому запрет — на
    сервере, а не только в интерфейсе: попытка поменять или стереть уже заданный ID
    отклоняется здесь, а не молчаливо принимается, если фронт вдруг пришлёт поле.

    Доработка ревьюера (code review, итерация 2, Issue 2): раньше проверка «ID ещё
    пуст» и сама запись были двумя отдельными запросами — SELECT, потом UPDATE без
    условия на uc. Два одновременных запроса (двойной клик, две вкладки) оба видели
    uc IS NULL и оба писали, второй поверх первого. Решает сам UPDATE: он затрагивает
    строку, только если uc там всё ещё NULL, — это атомарно в пределах одной операции
    SQLite. Предварительный SELECT оставлен ради понятного «канал не найден».
    """
    import time as _t
    now = _t.time() if now is None else now
    row = c.execute("SELECT id FROM pub_channels WHERE id=? AND tenant_id=?",
                    (cid, tenant_id)).fetchone()
    if row is None:
        raise ChannelError("канал не найден")
    new_uc = clean_uc(uc)
    if not new_uc:
        raise ChannelError("ID канала должен быть вида UC и ещё 22 символа, например "
                           "UCK8sQmJBp8GCxrOtXWBpyEA")
    cur = c.execute(
        "UPDATE pub_channels SET uc=?, updated=? WHERE id=? AND tenant_id=? AND uc IS NULL",
        (new_uc, now, cid, tenant_id))
    if cur.rowcount == 0:
        raise ChannelError("у канала уже задан ID — его нельзя изменить или стереть")
    c.commit()


def set_channel_status(c, tenant_id: str, cid: int, status: str, now: float = None) -> None:
    if status not in STATUS_TITLES:
        raise ChannelError("неизвестный статус канала")
    import time as _t
    c.execute("UPDATE pub_channels SET status=?, updated=? WHERE id=? AND tenant_id=?",
              (status, _t.time() if now is None else now, cid, tenant_id))


IMPORT_FIELDS = ("name", "handle", "lang", "niche", "profile_id", "host_id", "tz", "note")


def read_import_row(item: dict) -> tuple:
    """Строка переноса → (uc, только реально присланные поля).

    Дополнение 16.09, вопрос 2: канал без ID заводится ТОЛЬКО формой пульта
    (`create_channel`), перенос списком такие строки отклоняет — как было до пункта
    6.1. Причина: у `import_channels` сопоставление старой и новой строки идёт по
    `uc` (`WHERE uc=?`), а SQL `NULL` никогда не равен `NULL` — при пустом ID перенос
    не смог бы отличить повторную загрузку той же строки от новой и плодил бы
    дубликаты канала при каждом повторе импорта.
    """
    uc_raw = str(item.get("uc") or "").strip()
    if not uc_raw:
        raise ChannelError("в строке переноса не задан ID канала — заведите канал без "
                           "ID через форму в пульте, перенос списком таких строк не "
                           "принимает")
    uc = clean_uc(uc_raw)
    got = {k: str(item[k]).strip() for k in IMPORT_FIELDS
           if item.get(k) is not None and str(item[k]).strip() != ""}
    out = {}
    for key in ("name", "handle", "lang", "niche", "tz", "note"):
        if key in got:
            out[key] = got[key].lower()[:8] if key == "lang" else got[key]
    for key, title in (("profile_id", "профиль AdsPower"), ("host_id", "машина")):
        if key in got:
            out[key] = clean_int(got[key], title, 0, 99999, 0) or None
    times = str(item.get("times") or "").strip()
    if times:
        days = str(item.get("days") or "").replace(",", " ").split()
        out["schedule"] = clean_schedule(times, days)
        upcoming_slots(json.loads(out["schedule"]), out.get("tz", "Europe/Warsaw"), 1)
    return uc, out


def parse_import(payload: str) -> list:
    """JSON-массив или CSV с заголовком → список словарей."""
    text = (payload or "").strip()
    if not text:
        raise ChannelError("пусто: вставьте JSON или CSV")
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except Exception as exc:
            raise ChannelError(f"это не похоже на JSON: {exc}")
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise ChannelError("ожидался массив записей")
        return [d for d in data if isinstance(d, dict)]
    import csv
    import io as _io
    rows = list(csv.DictReader(_io.StringIO(text)))
    if not rows:
        raise ChannelError("в CSV нет строк с данными")
    if "uc" not in rows[0].keys():
        raise ChannelError("в CSV нет колонки uc")
    return rows


# ─────────────────────────── показ ───────────────────────────

def e(value) -> str:
    return html.escape("" if value is None else str(value))


def flag(lang: str) -> str:
    return FLAGS.get((lang or "").lower(), "🏳️")


def lang_title(lang: str) -> str:
    code = (lang or "").lower()
    return LANG_TITLES.get(code, code or "язык не задан")


def plural(n: int, one: str, few: str, many_: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many_


def per_day(schedule_raw: str) -> int:
    try:
        plan = json.loads(schedule_raw or "{}")
    except Exception:
        return 0
    return max((len(v) for v in plan.values()), default=0) if plan else 0


def schedule_text(raw: str) -> str:
    try:
        plan = json.loads(raw or "{}")
    except Exception:
        return "не задано"
    if not plan:
        return "не задано"
    if "*" in plan:
        return ", ".join(plan["*"]) + " · каждый день"
    times = sorted({t for v in plan.values() for t in v})
    days = " ".join(DAY_TITLES[d] for d in DAY_KEYS if d in plan)
    return ", ".join(times) + " · " + days


# Состояние ролика человеческим языком: точка + слово, без внутренних терминов.
PHASE_LOOK = {
    "created": ("начинаю", "muted"),
    "source_ready": ("делаю текст", "work"),
    "project_ready": ("собираю проект", "work"),
    "media_ready": ("озвучка и картинки", "work"),
    "cover_ready": ("делаю обложку", "work"),
    "upload_pending": ("готов, жду заливки", "work"),
    "upload_running": ("заливаю", "work"),
    "scheduled": ("стоит в расписании", "ok"),
    "published": ("выложен на канал", "ok"),
}


# Этапы ролика теми же полосками, что на странице проекта.
STEP_LABELS = ("текст", "озвучка", "картинки", "рендер", "обложка", "заливка", "вышел")


def release_steps(rel, stages: dict, upload) -> str:
    """Семь полосок на весь путь ролика: классы те же, что у степпера проектов."""
    from pub_engine import PHASE_INDEX
    at = PHASE_INDEX.get((rel["phase"] if rel else "") or "created", 0)
    blocked = bool(rel and rel["status"] == "blocked")
    stages = stages or {}

    def from_queue(kind, ready_at):
        st = stages.get(kind)
        if st in ("error", "running", "done", "queued"):
            return st
        return "done" if at >= ready_at else "pending"

    def simple(ready_at, running_at):
        if blocked and at == running_at:
            return "error"
        if at >= ready_at:
            return "done"
        return "running" if at == running_at else "pending"

    if upload is not None:
        up = {"queued": "queued", "running": "running", "done": "done",
              "error": "error", "repair": "error"}.get(upload["status"], "pending")
    else:
        up = "done" if at >= PHASE_INDEX["upload_running"] else "pending"

    out = ("done" if at >= PHASE_INDEX["published"]
           else "running" if at >= PHASE_INDEX["scheduled"] else "pending")

    segs = [simple(PHASE_INDEX["source_ready"], PHASE_INDEX["created"]),
            from_queue("tts", PHASE_INDEX["media_ready"]),
            from_queue("images", PHASE_INDEX["media_ready"]),
            from_queue("render", PHASE_INDEX["media_ready"]),
            simple(PHASE_INDEX["cover_ready"], PHASE_INDEX["media_ready"]),
            up, out]
    words = {"done": "готово", "running": "идёт", "queued": "в очереди",
             "error": "сбой", "pending": "ждёт"}
    tip = " · ".join(f"{lbl}: {words[st]}" for lbl, st in zip(STEP_LABELS, segs))
    return (f'<div class="stepper" style="width:118px" title="{html.escape(tip)}">'
            + "".join(f'<span class="seg {c}"></span>' for c in segs) + "</div>")


def release_look(rel, item_status: str) -> tuple:
    """(текст, цвет) для строки ролика."""
    if rel is None:
        return ("ждёт очереди", "muted")
    if rel["status"] == "blocked":
        return ("сбой: " + (rel["blocked_reason"] or "нужен разбор"), "err")
    if rel["status"] == "done":
        return ("выложен на канал", "ok")
    text, color = PHASE_LOOK.get(rel["phase"] or "created", ("в работе", "work"))
    if rel["blocked_reason"] and rel["blocked_reason"].startswith("ждём"):
        return (text + " · " + rel["blocked_reason"].replace("ждём: ", "жду: "), "muted")
    return (text, color)


PUB_CSS = """<style>
.pub-flags{display:flex;gap:7px;flex-wrap:wrap;font-size:19px;line-height:1}
.pub-flags a{text-decoration:none;opacity:.55;padding:3px 5px;border-radius:7px}
.pub-flags a:hover{opacity:1;background:var(--surface2)}
.pub-flags a.on{opacity:1;background:var(--surface2);box-shadow:inset 0 0 0 1px var(--accent,#e8833a)}
.pub-drop{display:none;margin-top:12px;padding-top:12px;border-top:1px solid var(--border)}
.pub-drop.open{display:block}
.pub-lbl{color:var(--muted);font-size:11.5px;width:64px;flex:none;text-transform:uppercase;letter-spacing:.4px}
.pub-bar{height:5px;background:var(--surface2);border-radius:3px;overflow:hidden;margin-top:8px}
.pub-bar i{display:block;height:100%}
.pub-ok{color:#5fa463}.pub-warn{color:#e0a33e}.pub-err{color:#c25a4e}.pub-work{color:var(--accent,#e8833a)}
.pub-dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:6px}
.pub-tab{width:100%;border-collapse:collapse;font-size:13.5px}
.pub-tab th{text-align:left;color:var(--muted);font-weight:500;font-size:11.5px;
text-transform:uppercase;letter-spacing:.4px;padding:0 10px 8px}
.pub-tab td{padding:9px 10px;border-top:1px solid var(--border)}
.pub-tab tr.drag-over td{background:var(--surface2);box-shadow:inset 0 2px 0 var(--accent,#e8833a)}
.pub-grip{cursor:grab;color:var(--muted);margin-right:7px;user-select:none}
.pub-grip:active{cursor:grabbing}.pub-grip:hover{color:var(--text)}
.pub-add{background:none;border:1px dashed var(--border2);color:var(--muted);border-radius:8px;
padding:7px 12px;font-size:13px;cursor:pointer;width:100%;text-align:left}
.pub-add:hover{border-color:var(--accent,#e8833a);color:var(--text)}
.btn-primary:hover{color:#1a1512 !important}
/* Меню действий прямо в плитке: пауза и архив без захода внутрь канала. */
.pub-menu{position:relative;margin-left:auto;flex:none}
.pub-menu>summary{list-style:none;cursor:pointer;color:var(--muted);line-height:1;
padding:3px 7px;border-radius:7px;font-size:17px;user-select:none}
.pub-menu>summary::-webkit-details-marker{display:none}
.pub-menu>summary:hover,.pub-menu[open]>summary{background:var(--surface2);color:var(--text)}
.pub-menu-body{position:absolute;right:0;top:28px;z-index:40;min-width:196px;
background:var(--surface,#1b1815);border:1px solid var(--border);border-radius:10px;
padding:5px;box-shadow:0 12px 32px rgba(0,0,0,.45)}
.pub-menu-body button,.pub-menu-body a{display:block;width:100%;box-sizing:border-box;
text-align:left;background:none;border:0;color:var(--text);font-size:13px;padding:7px 9px;
border-radius:7px;cursor:pointer;text-decoration:none;font-family:inherit}
.pub-menu-body button:hover,.pub-menu-body a:hover{background:var(--surface2)}
.pub-menu-sep{height:1px;background:var(--border);margin:4px 2px}
</style>"""

PUB_JS = """<script>
function pubToggle(id){var el=document.getElementById(id);if(el)el.classList.toggle('open');}
function pubDrag(){
  var rows=document.querySelectorAll('.pub-tab tr[data-slot]');
  rows.forEach(function(tr){
    var id=tr.getAttribute('data-item');
    var grip=tr.querySelector('.pub-grip');
    // Перетаскиваем ТОЛЬКО за ручку: draggable на всей строке съедает клики
    // по кнопкам и ссылкам внутри неё — из-за этого не удалялся ролик.
    if(id&&grip){grip.setAttribute('draggable','true');
      grip.addEventListener('dragstart',function(e){e.dataTransfer.setData('text/plain',id);});}
    tr.addEventListener('dragover',function(e){e.preventDefault();tr.classList.add('drag-over');});
    tr.addEventListener('dragleave',function(){tr.classList.remove('drag-over');});
    tr.addEventListener('drop',function(e){
      e.preventDefault();tr.classList.remove('drag-over');
      var from=e.dataTransfer.getData('text/plain');
      if(!from||from===tr.getAttribute('data-item'))return;
      var f=document.createElement('form');f.method='post';f.action='/api/plan/'+from+'/move';
      var i=document.createElement('input');i.name='slot';i.value=tr.getAttribute('data-slot');
      f.appendChild(i);document.body.appendChild(f);f.submit();
    });
  });
}
document.addEventListener('DOMContentLoaded',pubDrag);
// Открытое меню плитки закрывает соседние и закрывается кликом мимо. Если скрипт
// не отработает, меню всё равно открывается — это нативный <details>.
document.addEventListener('click',function(e){
  var cur=e.target.closest?e.target.closest('details.pub-menu'):null;
  document.querySelectorAll('details.pub-menu[open]').forEach(function(d){
    if(d!==cur){d.removeAttribute('open');}
  });
});
</script>"""


def tile_menu(row, back: str) -> str:
    """Три точки в углу плитки: пауза и архив без захода внутрь канала.

    Сделано на нативном <details>, а не на скрипте: меню обязано открываться даже
    если скрипт страницы упал. Каждое действие — отдельная форма со своим `назад`,
    чтобы после клика вернуться в тот же отфильтрованный список, а не на карточку.
    """
    st = row["status"] or "active"
    пункты = []

    def действие(значение, подпись):
        return (f'<form method=post action="/api/channels/{row["id"]}/status" style="margin:0">'
                f'<input type=hidden name=status value="{значение}">'
                f'<input type=hidden name=back value="{e(back)}">'
                f'<button type=submit>{подпись}</button></form>')

    if st != "active":
        пункты.append(действие("active", "▶ Включить"))
    if st != "paused":
        пункты.append(действие("paused", "⏸ На паузу"))
    if st != "archived":
        пункты.append(действие("archived", "🗄 В архив"))
    пункты.append('<div class=pub-menu-sep></div>')
    пункты.append(f'<a href="/channels/{row["id"]}">Открыть канал</a>')
    if row["uc"]:
        пункты.append(f'<a href="https://studio.youtube.com/channel/{e(row["uc"])}/videos" '
                      f'target=_blank rel=noopener>Открыть в Studio</a>')
    return (f'<details class=pub-menu><summary title="Действия">⋯</summary>'
            f'<div class=pub-menu-body>{"".join(пункты)}</div></details>')


def channel_tile(row, state, back: str = "/channels") -> str:
    # Плитка не вправе падать из-за незнакомой пометки состояния: канал со сбоем
    # приходит как "error", а таблица цветов знала только "err" — и весь список каналов
    # отдавал пятисотую, стоило одному выпуску уйти в разбор.
    kind = {"error": "err"}.get(state["kind"], state["kind"])
    color = {"ok": "pub-ok", "warn": "pub-warn", "err": "pub-err", "none": "muted"}.get(kind, "muted")
    fill = {"ok": "#5fa463", "warn": "#e0a33e", "err": "#c25a4e", "none": "#4a443e"}.get(kind, "#4a443e")
    pct = int(100 * state["filled"] / state["total"]) if state["total"] else (100 if kind == "err" else 0)
    bits = [x for x in (row["niche"], f"профиль {row['profile_id']}" if row["profile_id"] else "") if x]
    sched = schedule_text(row["schedule"])
    if sched != "не задано":
        bits.append(sched.split(" · ")[0])
    метка = {"paused": " · на паузе", "archived": " · в архиве"}.get(row["status"] or "active", "")
    return f"""<div class=card style="margin:0;overflow:visible">
<div style="display:flex;align-items:flex-start;gap:8px">
<a href="/channels/{row['id']}" style="font-weight:600;text-decoration:none;font-size:14.5px">
{flag(row['lang'])} {e(row['name'])}</a>{tile_menu(row, back)}</div>
<div class=muted style="font-size:12.5px;margin-top:5px">{e(' · '.join(bits) + метка) or '—'}</div>
<div class=pub-bar><i style="width:{pct}%;background:{fill}"></i></div>
<div class="{color}" style="font-size:12px;margin-top:5px">{e(state['text'])}</div>
</div>"""


def preset_field(field: str, items: list, current, create_url: str, create_title: str) -> str:
    link = (f'<a class="btn btn-ghost btn-sm" href="{create_url}" target=_blank rel=noopener>'
            f'{e(create_title)}</a>')
    if not items:
        return (f'<input name={field} value="{e(current or "")}" style="width:280px;flex:none">'
                f'{link}<span class=hint>список недоступен — впишите вручную</span>')
    opts = ['<option value="">— не выбран —</option>']
    for value, label in items:
        sel = " selected" if str(current or "") == str(value) else ""
        opts.append(f'<option value="{e(value)}"{sel}>{e(label)}</option>')
    return f'<select class=select name={field} style="width:280px;flex:none">{"".join(opts)}</select>{link}'


# ─────────────────────────── роуты ───────────────────────────

def register_pub_routes(app, ctx):
    db = ctx["db"]
    _page = ctx["_page"]
    _header = ctx["_header"]
    _tid = ctx["_tid"]
    _now = ctx["_now"]
    web_user = ctx["web_user"]
    require_web_user = ctx["require_web_user"]

    def _err_page(u, text, back="/channels"):
        body = _header(u, "channels") + f"""<div class=wrap>
<div class=card style="border-color:#a33"><b>Не получилось</b>
<div style="margin-top:6px">{e(text)}</div>
<div style="margin-top:10px"><a class="btn btn-ghost btn-sm" href="{back}">Назад</a></div></div></div>"""
        return _page("Ошибка", body)

    # ── каталог ──
    @app.get("/channels", response_class=HTMLResponse)
    def channels_page(request: Request):
        u = web_user(request)
        if not u:
            return RedirectResponse("/login", 302)
        tid = _tid(u)
        qp = request.query_params
        q = (qp.get("q") or "").strip()
        tag = (qp.get("tag") or "").strip()
        # Статусы копятся списком: нажал «работают» и «пауза» — видно и тех и других.
        # «С проблемами» — не статус, а условие поверх выбранных, поэтому сужает выбор,
        # а не расширяет: «работают + с проблемами» = работающие, у которых есть сбой.
        status = qp.get("status") or "active"
        picked = [s for s in status.split(",") if s]
        statuses = [s for s in ("active", "paused", "archived") if s in picked]
        trouble = "trouble" in picked

        where, args = "tenant_id=?", [tid]
        if statuses:
            where += f" AND status IN ({','.join('?' * len(statuses))})"
            args += statuses
        elif not trouble:
            where += " AND status!='archived'"
        if trouble:
            where += (" AND id IN (SELECT channel_id FROM pub_releases"
                      " WHERE tenant_id=? AND status='blocked')")
            args.append(tid)
        if tag.startswith("lang:"):
            where += " AND lang=?"
            args.append(tag[5:])
        elif tag.startswith("niche:"):
            where += " AND niche=?"
            args.append(tag[6:])
        elif tag:
            where += (" AND id IN (SELECT ct.channel_id FROM pub_channel_tags ct "
                      "JOIN pub_tags t ON t.id=ct.tag_id WHERE t.name=? AND t.tenant_id=?)")
            args += [tag, tid]
        if q:
            needle = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where += (" AND (name LIKE ? ESCAPE '\\' OR handle LIKE ? ESCAPE '\\'"
                      " OR uc LIKE ? ESCAPE '\\' OR niche LIKE ? ESCAPE '\\')")
            args += [needle] * 4

        c = db()
        rows = c.execute(f"SELECT * FROM pub_channels WHERE {where}"
                         " ORDER BY lang, name COLLATE NOCASE", args).fetchall()
        now = _now()
        taken = plan_by_channel(c, tid, now - 86400, now + 92 * 86400)
        troubles = troubles_by_channel(c, tid)
        work = work_by_channel(c, tid)      # что реально происходит на каждом канале
        langs = c.execute(
            "SELECT lang, COUNT(*) n FROM pub_channels WHERE tenant_id=? AND status!='archived'"
            " AND lang IS NOT NULL AND lang!='' GROUP BY lang ORDER BY n DESC", (tid,)).fetchall()
        niches = c.execute(
            "SELECT niche, COUNT(*) n FROM pub_channels WHERE tenant_id=? AND status!='archived'"
            " AND niche IS NOT NULL AND niche!='' GROUP BY niche ORDER BY n DESC LIMIT 12",
            (tid,)).fetchall()
        own = all_tags(c, tid)
        c.close()

        # Куда вернуться после действия из меню плитки: тот же список с теми же фильтрами.
        back = "/channels" + (f"?{request.url.query}" if request.url.query else "")
        tiles = "".join(channel_tile(r, channel_state(r, taken.get(r["id"], set()),
                                                      troubles.get(r["id"], ""),
                                                      work=work.get(r["id"])), back)
                        for r in rows)
        if not tiles:
            tiles = ('<div class=card><span class=muted>'
                     + ("Ничего не найдено." if (q or tag) else "Пока ни одного канала.")
                     + "</span></div>")

        def link(new_tag=None, new_status=None):
            params = {"q": q, "tag": tag if new_tag is None else new_tag,
                      "status": ",".join(picked) if new_status is None else new_status}
            params = {k: v for k, v in params.items() if v and not (k == "status" and v == "active")}
            return "/channels?" + urllib.parse.urlencode(params) if params else "/channels"

        flags_html = "".join(
            f'<a class="{"on" if tag == f"lang:{l}" else ""}" href="{link(new_tag="" if tag == f"lang:{l}" else f"lang:{l}")}"'
            f' title="{e(lang_title(l))} · {n}">{flag(l)}</a>' for l, n in langs)
        niche_html = "".join(
            f'<a class="chip" href="{link(new_tag="" if tag == f"niche:{nm}" else f"niche:{nm}")}"'
            f' style="text-decoration:none;{"background:var(--accent);color:#1a1512;font-weight:600" if tag == f"niche:{nm}" else ""}">'
            f'{e(nm)} <span class=muted>{n}</span></a>' for nm, n in niches)
        tags_html = "".join(
            f'<a class="chip" href="{link(new_tag="" if tag == nm else nm)}" style="text-decoration:none">'
            f'{e(nm)} <span class=muted>{n}</span></a>' for nm, n in own)

        def st_chip(key, label):
            on = key in picked
            # Клик по горящему чипу снимает его, по погасшему — добавляет к уже выбранным.
            # Сняли последний — показываем всё, кроме архива («все»), иначе список
            # молча вернулся бы к «работают» и чип нельзя было бы погасить.
            дальше = [x for x in picked if x != key] if on else picked + [key]
            style = ("background:var(--accent);color:#1a1512;font-weight:600;"
                     if on else "")
            return (f'<a class="chip" href="{link(new_status=",".join(дальше) or "все")}" '
                    f'style="text-decoration:none;{style}">{label}</a>')

        opened = " open" if (tag or picked != ["active"]) else ""
        body = _header(u, "channels") + PUB_CSS + f"""<div class=wrap>
<h1>Каналы <span class=muted style="font-size:14px;font-weight:400">{len(rows)}</span></h1>
<div class=card>
  <form method=get action="/channels" style="display:flex;gap:8px;align-items:center;margin:0;flex-wrap:wrap">
    <input name=q value="{e(q)}" placeholder="название, ниша или ID канала" style="min-width:280px">
    <input type=hidden name=status value="{e(status)}">
    <button class="btn btn-secondary btn-sm">Искать</button>
    <button type=button class="btn btn-ghost btn-sm" onclick="pubToggle('pubflt')">Фильтры ▾</button>
    <span style="flex:1"></span>
    <a class="btn btn-primary btn-sm" href="/channels/new">+ Добавить канал</a>
  </form>
  <div class="pub-drop{opened}" id=pubflt>
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
      <span class=pub-lbl>Язык</span><div class=pub-flags>{flags_html}</div></div>
    <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
      <span class=pub-lbl>Ниша</span>{niche_html}</div>
    {'<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:10px"><span class=pub-lbl>Теги</span>' + tags_html + '</div>' if tags_html else ''}
    <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
      <span class=pub-lbl>Статус</span>
      {st_chip('active', 'Работают')}{st_chip('paused', 'Пауза')}
      {st_chip('archived', 'Архив')}{st_chip('trouble', 'С проблемами')}
    </div>
  </div>
</div>
<div class=grid style="grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px;margin-top:12px">{tiles}</div>
<p class=hint style="margin-top:14px"><a href="/channels/import">Перенести каналы списком</a></p>
</div>""" + PUB_JS
        return _page("Каналы", body)

    # ── карточка канала ──
    def _card_html(u, row, error=""):
        c = db()
        tid = _tid(u)
        tags = tag_names(c, row["id"])
        vs_items = vsgen_presets(c, tid)
        slots = coverage(c, row)
        rows_plan = plan_rows(c, row["id"])
        rels = releases_by_item(c, row["id"])
        stages = release_stages(c, row["id"])
        ups = upload_jobs_by_release(c, row["id"])
        c.close()

        # 04.09.2026: спрашиваем по общему идентификатору, а не по логину. Логин
        # сопоставлял людей на глаз; tenant_id — это один и тот же человек во всех трёх.
        rew = preset_field("preset_rewrite", [(x, x) for x in rewrite_presets(tid)],
                           row["preset_rewrite"], REWRITE_URL, "Создать")
        vs = preset_field("preset_vsgen_id", vs_items, row["preset_vsgen_id"], "/presets", "Создать")
        cov = preset_field("preset_cover", [(x, x) for x in cover_presets(tid)],
                           row["preset_cover"], COVER_URL, "Создать")

        sched = json.loads(row["schedule"] or "{}")
        times = ", ".join(sched.get("*", []) or sorted({t for v in sched.values() for t in v}))
        picked = set(DAY_KEYS) if ("*" in sched or not sched) else set(sched.keys())
        days_html = "".join(
            f'<label class=chip style="cursor:pointer"><input type=checkbox name=days value="{d}"'
            f'{" checked" if d in picked else ""}> {DAY_TITLES[d]}</label>' for d in DAY_KEYS)

        n_day = per_day(row["schedule"])
        lead = row["lead_days"] if row["lead_days"] is not None else 2
        defer_on = " checked" if (row["deferred"] if row["deferred"] is not None else 1) else ""
        defer_limit = row["deferred_limit"] if row["deferred_limit"] is not None else 5

        body_rows = []
        for moment, item in slots:
            when = moment.strftime("%d.%m · %H:%M")
            slot_ts = round(moment.timestamp())
            if item:
                rel_row = rels.get(item["id"])
                text, color = release_look(rel_row, item["status"])
                if rel_row is None and not row["uc"]:
                    # Движок (pub_engine.materialize) намеренно не заводит выпуск на
                    # канале без ID — «ждёт очереди» тут вводило бы в заблуждение,
                    # выпуск и не начнёт заводиться, пока ID не появится. Цвет — из
                    # уже существующего набора (см. `dot` ниже): это не сбой движка.
                    text, color = "нет ID канала — выпуск не заводится", "muted"
                dot = {"ok": "#5fa463", "err": "#c25a4e", "work": "var(--accent)",
                       "muted": "var(--muted)"}[color]
                # Название донорского ролика, а не длинная ссылка. Клик ведёт в проект,
                # когда он уже заведён, иначе — на сам ролик-донор.
                title = (item["donor_title"] or "").strip() or item["donor_url"]
                project = rel_row["project_id"] if rel_row else None
                href = f"/p/{project}" if project else item["donor_url"]
                target = "" if project else ' target=_blank rel=noopener'
                hint = "открыть проект" if project else "проект ещё не создан — откроется донор"
                body_rows.append(
                    f'<tr data-slot="{slot_ts}" data-item="{item["id"]}">'
                    f'<td style="width:130px;white-space:nowrap">'
                    f'<span class="pub-grip" title="перетащить в другой выход">⠿</span>{when}</td>'
                    f'<td><a href="{e(href)}"{target} title="{hint}" '
                    f'style="text-decoration:none">{e(title[:70])}</a></td>'
                    f'<td style="width:125px">'
                    + (release_steps(rel_row, stages.get(rel_row["release_id"], {}),
                                     ups.get(rel_row["release_id"]))
                       if rel_row else '<span class=muted style="font-size:12px">не начат</span>')
                    + '</td>'
                    + f'<td style="width:205px" class="pub-{color if color != "muted" else ""}">'
                    + (f'<a href="/releases/{rel_row["release_id"]}/log" title="показать журнал" '
                       f'style="text-decoration:none;color:inherit">'
                       f'<span class=pub-dot style="background:{dot}"></span>{e(text)}</a>'
                       if rel_row else
                       f'<span class=pub-dot style="background:{dot}"></span>{e(text)}')
                    + '</td>'
                    f'<td style="width:40px"><form method=post action="/api/plan/{item["id"]}/cancel"'
                    f' style="margin:0"><button class="btn btn-ghost btn-sm" title="убрать">×</button>'
                    f'</form></td></tr>')
            else:
                body_rows.append(
                    f'<tr data-slot="{slot_ts}"><td style="width:130px;white-space:nowrap" class=muted>{when}</td>'
                    f'<td colspan=3><form method=post action="/api/channels/{row["id"]}/links"'
                    f' style="display:flex;gap:6px;margin:0">'
                    f'<input type=hidden name=slot value="{slot_ts}">'
                    f'<input name=links placeholder="вставьте ссылку на донора" style="flex:1">'
                    f'<button class="btn btn-ghost btn-sm">Поставить</button></form></td></tr>')

        waiting = [r for r in rows_plan if not r["slot_at"]]
        wait_html = (f'<p class=hint style="margin:10px 0 0">Ждут свободного места: {len(waiting)} '
                     f'{plural(len(waiting), "ссылка", "ссылки", "ссылок")}.</p>') if waiting else ""

        st = row["status"] or "active"
        acts = []
        if st != "paused":
            acts.append('<button class="btn btn-ghost btn-sm" name=status value="paused">На паузу</button>')
        if st != "active":
            acts.append('<button class="btn btn-ghost btn-sm" name=status value="active">Включить</button>')
        if st != "archived":
            acts.append('<button class="btn btn-ghost btn-sm" name=status value="archived">В архив</button>')
        err = f'<div class=card style="border-color:#a33">{e(error)}</div>' if error else ""

        return _header(u, "channels") + PUB_CSS + f"""<div class=wrap>
<div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap">
<h1>{flag(row['lang'])} {e(row['name'])}</h1>
<span class=muted style="font-size:13px">{e(lang_title(row['lang']))}{' · ' + e(row['niche']) if row['niche'] else ''}</span>
<span style="flex:1"></span><a class="btn btn-ghost btn-sm" href="/channels">К списку</a></div>
{err}
<div class=panel style="margin-top:10px">
<div class=panel-title>Ролики канала</div>
<table class=pub-tab>
<tr><th style="width:130px">Когда</th><th>Ролик</th><th style="width:125px">Этапы</th>
<th style="width:205px">Что с ним</th><th></th></tr>
{"".join(body_rows) or '<tr><td colspan=4><span class=muted>расписание не задано — укажите время выхода в настройках</span></td></tr>'}
</table>
{wait_html}
<form method=post action="/api/channels/{row['id']}/links" style="margin-top:12px">
<details><summary style="cursor:pointer;color:var(--muted);font-size:13px">Вставить сразу много</summary>
<textarea name=links rows=3 style="width:100%;margin-top:8px;font-size:13px"
placeholder="по одной ссылке в строке — разложу по свободным выходам"></textarea>
<button class="btn btn-primary btn-sm" style="margin-top:8px">Разложить</button></details></form>
</div>

<details class=panel><summary style="cursor:pointer;font-weight:600;padding:2px 0">Настройки канала</summary>
<form method=post action="/api/channels/{row['id']}/edit" style="margin-top:12px">
<div class=fld><label class=cap>Выходит</label>
  <input name=times value="{e(times)}" placeholder="10:00, 18:00" style="width:170px;flex:none">
  <span class=hint>{n_day} {plural(n_day, 'ролик', 'ролика', 'роликов')} в день</span></div>
<div class=fld><label class=cap>Дни</label>
  <div style="display:flex;gap:5px;flex-wrap:wrap">{days_html}</div></div>
<div class=fld><label class=cap>Готовить заранее</label>
  <input name=lead_days value="{lead}" style="width:60px;flex:none"> <span class=hint>дня до выхода</span></div>
<div class=fld><label class=cap>Выкладывать</label>
  <label class=chip style="cursor:pointer"><input type=checkbox name=deferred value="1"{defer_on}>
  по расписанию</label>
  <span class=hint>снять — ролик выходит сразу по готовности; на новых каналах лучше сразу</span></div>
<div class=fld><label class=cap>В отложке максимум</label>
  <input name=deferred_limit value="{defer_limit}" style="width:60px;flex:none">
  <span class=hint>роликов; больше — готовый ждёт освободившегося места</span></div>
<div class=fld><label class=cap>Текст</label>{rew}</div>
<div class=fld><label class=cap>Видео</label>{vs}</div>
<div class=fld><label class=cap>Обложка</label>{cov}</div>
<details style="margin:10px 0"><summary style="cursor:pointer;color:var(--muted);font-size:13px">Куда заливать и метки</summary>
<div class=fld><label class=cap>Название</label>
  <input name=name value="{e(row['name'])}" required style="width:280px;flex:none"></div>
<div class=fld><label class=cap>Профиль / машина</label>
  <input name=profile_id value="{e(row['profile_id'] or '')}" style="width:80px;flex:none">
  <input name=host_id value="{e(row['host_id'] or '')}" style="width:80px;flex:none"></div>
<div class=fld><label class=cap>Часовой пояс</label>
  <input name=tz value="{e(row['tz'])}" style="width:190px;flex:none">
  <span class=hint>в нём понимается время выхода</span></div>
<div class=fld><label class=cap>Коллаборант</label>
  <input name=collaborator value="{e(row['collaborator'] or '')}" style="width:170px;flex:none"></div>
<div class=fld><label class=cap>Язык / ниша</label>
  <input name=lang value="{e(row['lang'] or '')}" style="width:60px;flex:none">
  <input name=niche value="{e(row['niche'] or '')}" style="width:190px;flex:none"></div>
<div class=fld><label class=cap>Свои теги</label>
  <input name=tags value="{e(', '.join(tags))}" placeholder="ww2, приоритет" style="width:300px;flex:none"></div>
<div class=fld><label class=cap>Заметка</label>
  <input name=note value="{e(row['note'] or '')}" style="width:100%;max-width:460px"></div>
<input type=hidden name=handle value="{e(row['handle'] or '')}">
<input type=hidden name=horizon_days value="{row['horizon_days']}">
</details>
<div style="display:flex;gap:8px;margin:12px 0"><button class="btn btn-primary">Сохранить</button></div>
</form>
<form method=post action="/api/channels/{row['id']}/status" style="display:flex;gap:6px;padding-bottom:12px">{''.join(acts)}</form>
</details>
</div>""" + PUB_JS

    @app.get("/channels/new", response_class=HTMLResponse)
    def channel_new(request: Request):
        u = web_user(request)
        if not u:
            return RedirectResponse("/login", 302)
        body = _header(u, "channels") + """<div class=wrap>
<h1>Новый канал</h1>
<form method=post action="/api/channels/create">
<div class=panel>
<div class=fld><label class=cap>ID канала</label>
  <input name=uc placeholder="UCK8sQmJBp8GCxrOtXWBpyEA" style="width:320px;flex:none">
  <span class=hint>необязательно — можно завести канал раньше, чем узнаете его ID</span></div>
<div class=fld><label class=cap>Название</label><input name=name required style="width:320px;flex:none"></div>
<div class=fld><label class=cap>Язык / ниша</label>
  <input name=lang placeholder="pl" style="width:60px;flex:none">
  <input name=niche style="width:190px;flex:none"></div>
<p class=hint style="margin:6px 0 0">Расписание, пресеты и ссылки — в карточке, она откроется сразу.</p></div>
<div style="display:flex;gap:8px;margin-top:12px">
<button class="btn btn-primary">Создать</button>
<a class="btn btn-ghost" href="/channels">Отмена</a></div></form></div>"""
        return _page("Новый канал", body)

    @app.get("/channels/import", response_class=HTMLResponse)
    def channel_import_form(request: Request):
        u = web_user(request)
        if not u:
            return RedirectResponse("/login", 302)
        body = _header(u, "channels") + """<div class=wrap>
<h1>Перенести каналы списком</h1>
<div class=card><span class=muted>JSON-массив или CSV с заголовком. Обязательны
<b>uc</b> и <b>name</b>. Пустые колонки ничего не затрут.</span></div>
<form method=post action="/api/channels/import">
<textarea name=payload rows=14 style="width:100%;font-family:monospace;font-size:12.5px"
placeholder='[{"uc":"UC...","name":"Канал","lang":"pl","times":"10:00, 18:00"}]'></textarea>
<div style="display:flex;gap:8px;margin-top:10px">
<button class="btn btn-primary">Перенести</button>
<a class="btn btn-ghost" href="/channels">Отмена</a></div></form></div>"""
        return _page("Перенос каналов", body)

    def _load(u, cid):
        c = db()
        row = c.execute("SELECT * FROM pub_channels WHERE id=? AND tenant_id=?",
                        (cid, _tid(u))).fetchone()
        c.close()
        return row

    @app.get("/channels/{cid}", response_class=HTMLResponse)
    def channel_page(cid: int, request: Request):
        u = web_user(request)
        if not u:
            return RedirectResponse("/login", 302)
        row = _load(u, cid)
        if not row:
            return _err_page(u, "Канал не найден.")
        return _page(row["name"], _card_html(u, row))

    @app.get("/releases/{release_id}/log", response_class=HTMLResponse)
    def release_log_page(release_id: str, request: Request):
        """Журнал одного ролика: что происходило и почему остановилось."""
        u = web_user(request)
        if not u:
            return RedirectResponse("/login", 302)
        c = db()
        rel = c.execute(
            "SELECT r.*, ch.name ch_name, ch.id cid FROM pub_releases r "
            "JOIN pub_channels ch ON ch.id=r.channel_id "
            "WHERE r.release_id=? AND r.tenant_id=?", (release_id, _tid(u))).fetchone()
        if not rel:
            c.close()
            return _err_page(u, "Ролик не найден.")
        lines = read_log(c, release_id)
        phases = c.execute(
            "SELECT seq, phase, created FROM pub_release_events WHERE release_id=? ORDER BY seq",
            (release_id,)).fetchall()
        job = c.execute("SELECT * FROM pub_upload_jobs WHERE release_id=?",
                        (release_id,)).fetchone()
        c.close()

        colors = {"error": "#c25a4e", "warn": "#e0a33e", "info": "var(--muted)"}
        rows = "".join(
            f'<tr><td style="width:120px;white-space:nowrap;color:var(--muted)">'
            f'{dt.datetime.fromtimestamp(r["ts"]).strftime("%d.%m %H:%M:%S")}</td>'
            f'<td style="width:110px;color:{colors.get(r["level"], "var(--muted)")}">'
            f'{e(r["step"])}</td><td>{e(r["message"])}</td></tr>'
            for r in lines) or ('<tr><td colspan=3><span class=muted>записей пока нет</span>'
                                '</td></tr>')

        chain = " → ".join(e(PHASE_LOOK.get(p["phase"], (p["phase"], ""))[0]) for p in phases)
        state, color = release_look(rel, "")
        up = ""
        if job:
            up = (f'<div class=fld><label class=cap>Заливка</label>'
                  f'{e(job["status"])}{" · " + e(job["verdict"]) if job["verdict"] else ""}'
                  f'{" · машина " + e(job["host"]) if job["host"] else ""}'
                  f'{" · видео " + e(job["video_id"]) if job["video_id"] else ""}</div>')

        body = _header(u, "channels") + PUB_CSS + f"""<div class=wrap>
<div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap">
<h1>Журнал ролика</h1><span class=muted style="font-size:13px">{e(rel["ch_name"])}</span>
<span style="flex:1"></span>
<a class="btn btn-ghost btn-sm" href="/channels/{rel["cid"]}">К каналу</a></div>
<div class=panel>
<div class=fld><label class=cap>Сейчас</label><span class="pub-{color if color != 'muted' else ''}">{e(state)}</span></div>
<div class=fld><label class=cap>Пройдено</label><span class=muted style="font-size:12.5px">{chain or "—"}</span></div>
{up}
<div class=fld><label class=cap>Номер выпуска</label><code style="font-size:12.5px">{e(release_id)}</code></div>
</div>
<div class=panel><div class=panel-title>Что происходило — новое сверху</div>
<table class=pub-tab>{rows}</table></div>
</div>"""
        return _page("Журнал ролика", body)

    # ── запись ──
    @app.post("/api/channels/create")
    async def channel_create(request: Request):
        u = require_web_user(request)
        form = await form_multi(request)
        try:
            uc = clean_uc(one(form, "uc"))
            name = one(form, "name")
            if not name:
                raise ChannelError("название канала обязательно")
        except ChannelError as exc:
            return _err_page(u, str(exc), "/channels/new")
        c = db()
        try:
            new_id = create_channel(c, _tid(u), uc, name, one(form, "lang"),
                                    one(form, "niche"), _now())
        except Exception as exc:
            c.close()
            if "UNIQUE" in str(exc):
                return _err_page(u, "Такой канал уже заведён.", "/channels/new")
            raise
        c.close()
        return RedirectResponse(f"/channels/{new_id}", 302)

    @app.post("/api/channels/{cid}/edit")
    async def channel_edit(cid: int, request: Request):
        u = require_web_user(request)
        row = _load(u, cid)
        if not row:
            return _err_page(u, "Канал не найден.")
        form = await form_multi(request)
        try:
            data = read_channel_form(form)
        except (ChannelError, ScheduleError) as exc:
            return _page("Ошибка", _card_html(u, row, str(exc)))
        now = _now()
        c = db()
        update_channel(c, _tid(u), cid, data, now)
        set_tags(c, _tid(u), cid, parse_tags(one(form, "tags")), now)
        c.commit()
        c.close()
        return RedirectResponse(f"/channels/{cid}", 302)

    @app.post("/api/channels/{cid}/status")
    async def channel_status(cid: int, request: Request):
        u = require_web_user(request)
        form = await form_multi(request)
        status = one(form, "status")
        if status not in STATUS_TITLES:
            raise HTTPException(400, "неизвестный статус канала")
        c = db()
        set_channel_status(c, _tid(u), cid, status, _now())
        c.commit()
        c.close()
        # Из меню в каталоге возвращаемся в тот же список; чужие адреса не принимаем,
        # иначе форма превращается в открытый редирект на любой сайт.
        back = one(form, "back") or ""
        if back.startswith("/") and not back.startswith("//"):
            return RedirectResponse(back, 302)
        return RedirectResponse(f"/channels/{cid}", 302)

    @app.post("/api/channels/{cid}/links")
    async def channel_links(cid: int, request: Request):
        u = require_web_user(request)
        row = _load(u, cid)
        if not row:
            return _err_page(u, "Канал не найден.")
        form = await form_multi(request)
        urls = parse_links(one(form, "links"))
        if not urls:
            return _page("Ошибка", _card_html(u, row, "В поле нет ни одной ссылки."))
        slot_raw = one(form, "slot")
        try:
            slot = float(slot_raw) if slot_raw else None
        except ValueError:
            slot = None
        # Названия доноров тянем в пуле потоков ДО открытия базы: сетевые вызовы в
        # обработчике заморозили бы весь кабинет на секунды за каждую ссылку.
        from starlette.concurrency import run_in_threadpool
        from pub_tags import donor_title as _dt_fetch
        titles = {}
        for u_ in urls:
            titles[u_] = await run_in_threadpool(_dt_fetch, u_)
        c = db()
        try:
            add_links(c, _tid(u), row, urls, _now(), slot=slot, titles=titles)
            c.commit()
        except ScheduleError as exc:
            c.close()
            return _page("Ошибка", _card_html(u, row, str(exc)))
        c.close()
        return RedirectResponse(f"/channels/{cid}", 302)

    @app.post("/api/plan/{pid}/move")
    async def plan_move(pid: int, request: Request):
        """Перетащили ссылку в другой выход."""
        u = require_web_user(request)
        form = await form_multi(request)
        try:
            target = float(one(form, "slot"))
        except ValueError:
            raise HTTPException(400, "не разобрал, куда перетащили")
        c = db()
        row = c.execute("SELECT channel_id FROM pub_plan_items WHERE id=? AND tenant_id=?",
                        (pid, _tid(u))).fetchone()
        if not row:
            c.close()
            return _err_page(u, "Строка не найдена.")
        cid = row["channel_id"]
        try:
            move_item(c, pid, target, _now())
            c.commit()
        except ValueError as exc:
            c.close()
            chan = _load(u, cid)
            return _page("Ошибка", _card_html(u, chan, str(exc)))
        c.close()
        return RedirectResponse(f"/channels/{cid}", 302)

    @app.post("/api/plan/{pid}/cancel")
    async def plan_cancel(pid: int, request: Request):
        u = require_web_user(request)
        c = db()
        row = c.execute("SELECT channel_id FROM pub_plan_items WHERE id=? AND tenant_id=?",
                        (pid, _tid(u))).fetchone()
        if not row:
            c.close()
            return _err_page(u, "Строка не найдена.")
        cid = row["channel_id"]
        try:
            cancel_item(c, pid, _now())
            c.commit()
        except ValueError as exc:
            c.close()
            return _page("Ошибка", _card_html(u, _load(u, cid), str(exc)))
        c.close()
        return RedirectResponse(f"/channels/{cid}", 302)

    # ── перенос списком ──
    @app.post("/api/channels/import")
    async def channel_import(request: Request):
        u = require_web_user(request)
        form = await form_multi(request)
        c = db()
        try:
            out = import_channels(c, _tid(u), one(form, "payload"), _now())
        except ChannelError as exc:
            c.close()
            return _err_page(u, str(exc), "/channels/import")
        c.close()
        created, updated, rejected = out["created"], out["updated"], out["rejected"]

        rej = ("".join(f"<div class=muted style='font-size:12.5px'>{e(r)}</div>"
                       for r in rejected[:50]) or "<span class=muted>нет</span>")
        body = _header(u, "channels") + f"""<div class=wrap>
<h1>Перенос завершён</h1>
<div class=card><b>Заведено новых: {created}</b><br>Обновлено: {updated}<br>
Отклонено: {len(rejected)}</div>
<div class=panel><div class=panel-title>Что отклонено и почему</div>{rej}</div>
<a class="btn btn-primary btn-sm" href="/channels">К каналам</a></div>"""
        return _page("Перенос завершён", body)
