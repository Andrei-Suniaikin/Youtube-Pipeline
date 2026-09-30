#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Что именно уезжает на канал: название, описание, файл ролика, время выхода.

Правила заданы владельцем:

* **Название** — оригинальное название донора, переписанное на язык канала. Структура
  сохраняется, дословного перевода не делаем. Один короткий вызов модели, поэтому работает
  для любой ниши и языка, а не только для семи заготовленных брендов.
* **Описание** дублирует название. Если у канала включены главы (`auto_chapters` в пресете
  VideoStudio) и они собрались — снизу добавляется блок таймкодов.
* **Время выхода** — момент слота в часовом поясе канала, строкой с обязательным смещением:
  заливщик отказывается работать с временем без пояса, а без времени публикует немедленно.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re

import requests

MAX_TITLE = 95           # предел YouTube — 100, оставляем запас
MAX_DESCRIPTION = 4800   # предел 5000, запас на служебные строки

# Название пишет прокси triklz. Модель — необязательное поле `pub_title_model` серверного
# файла ключей; ключ прокси пускает не все модели из /v1/models (19.09.2026 разрешены
# claude-opus-5, claude-sonnet-5, gpt-5.6-sol/terra/luna; claude-opus-4-6 — 403).
DEFAULT_TITLE_MODEL = "claude-opus-5"
# Прокси всегда включает рассуждения, и они идут в max_tokens: при 120 семь из восьми
# ответов обрывались пустыми (finish=length). Замер 19.09 на opus-5 (16 названий):
# 466–1588 токенов на ответ, 11–71 с. Платятся только реально выданные токены, поэтому
# потолок с запасом ×2.5 ничего не стоит, а обрыв означал бы повтор и ещё один счёт.
TITLE_MAX_TOKENS = 4000
TITLE_TIMEOUT = 120

LANG_NAMES = {
    "pl": "польский", "cs": "чешский", "sk": "словацкий", "ro": "румынский",
    "hr": "хорватский", "el": "греческий", "hu": "венгерский", "ru": "русский",
    "de": "немецкий", "fr": "французский", "it": "итальянский", "es": "испанский",
    "pt": "португальский", "po": "португальский", "fi": "финский", "bg": "болгарский",
    "uk": "украинский", "en": "английский", "tr": "турецкий", "nl": "нидерландский",
    "gr": "греческий",   # так записан язык у одного канала в pub_channels
}

SYSTEM = (
    "Ты переписываешь названия видео для YouTube-канала на заданном языке. "
    "Сохраняй смысловую структуру исходного названия и его интригу, но НЕ переводи дословно: "
    "формулируй так, как назвал бы ролик носитель языка. "
    "Верни ТОЛЬКО готовое название одной строкой, без кавычек, без пояснений, "
    "без хештегов и без эмодзи. Не длиннее 90 символов."
)


class TitleError(RuntimeError):
    """Название сделать не удалось по вине ЭТОЙ строки (нет исходного названия и т.п.)."""


class TitleUnavailable(RuntimeError):
    """Генератор названий временно недоступен: нет ключа, лимит, сбой сети или сервиса.

    Это отказ общего ресурса — готовый ролик должен ЖДАТЬ и повториться, а не умирать
    в карантине из-за одного 429."""


def clean_title(text: str) -> str:
    """Убрать кавычки, переносы и служебный мусор из ответа модели."""
    line = " ".join((text or "").split())
    line = line.strip().strip('"').strip("«»").strip("'").strip()
    line = re.sub(r"^(название|title)\s*[:—-]\s*", "", line, flags=re.I).strip()
    if len(line) > MAX_TITLE:
        cut = line[:MAX_TITLE]
        if " " in cut:
            cut = cut[:cut.rfind(" ")]
        line = cut.rstrip(" ,;:—-")
    return line


_SECRET_RE = re.compile(r"(sk-|Bearer\s+)[A-Za-z0-9_\-.]{6,}")
_FILTER_WORDS = ("content_filter", "content_policy", "content policy", "moderation", "safety")

# Модель может отказаться словами, а не кодом: ответ приходит как обычный текст и без
# защиты уезжает на канал названием ролика. 19.09.2026 так вышло в эфир «I can't help
# rewrite titles that use sexual violence…». Ловим начало ответа — отказ всегда там.
_REFUSAL = re.compile(
    r"^\s*(?:i\s*(?:'|\u2019)?\s*(?:m\s+sorry|can(?:'|\u2019)?t|cannot|won(?:'|\u2019)?t)\b"
    r"|sorry\b|i\s+will\s+not\b|unable\s+to\s+(?:help|assist)\b|as\s+an\s+ai\b"
    r"|извин[а-я]*[,\s]+(?:но\s+)?(?:я\s+)?не\s+могу\b"
    r"|(?:я\s+)?не\s+могу\s+(?:помочь|переписать|этого|это\s+сделать)\b)", re.I)
# Вторая попытка: просим нейтральную формулировку вместо отказа.
RETRY_HINT = ("Ответ — ТОЛЬКО заголовок на языке канала, одной строкой. "
              "Если в оригинале есть жёсткие или откровенные формулировки, передай смысл "
              "нейтрально и без них. Не отказывайся и ничего не объясняй.")


def _brief(resp) -> str:
    """Код и текст ошибки прокси для журнала — без секретов и не длиннее строки."""
    try:
        err = resp.json().get("error") or {}
        if not isinstance(err, dict):
            err = {"message": str(err)}
        text = " ".join(str(err.get(k) or "") for k in ("code", "type", "message")).strip()
    except Exception:
        text = resp.text or ""
    return _SECRET_RE.sub(r"\1***", " ".join(text.split()))[:160]


def make_title(donor_title: str, lang: str, llm: dict) -> str:
    """Название донора → название на языке канала.

    Пишет прокси triklz (OpenAI-совместимый, семейство ключей alt_llm): `llm` — это
    {"base": адрес /v1, "key": ключ, "model": модель или пусто}. Прямой api.openai.com
    больше не зовём — у серверного ключа OpenAI нет денег (19.09.2026).

    Классы отказа те же, что у движка: всё, что лечится ожиданием или настройкой
    (сеть, 429/5xx, ключ не принят, модель не разрешена, пустой или оборванный ответ),
    — TitleUnavailable, ролик ЖДЁТ. TitleError (карантин строки) — только когда виновата
    сама строка: нет исходного названия, явный отказ фильтра содержимого, ответ короче
    10 символов.
    """
    source = " ".join((donor_title or "").split())
    if not source:
        raise TitleError("у донора нет названия — не из чего делать заголовок")
    llm = llm if isinstance(llm, dict) else {}
    base = str(llm.get("base") or "").strip().rstrip("/")
    key = str(llm.get("key") or "").strip()
    model = str(llm.get("model") or "").strip() or DEFAULT_TITLE_MODEL
    if not key or not base:
        raise TitleUnavailable("не задан ключ или адрес прокси названий (alt_llm) — ждём настройки")
    language = LANG_NAMES.get((lang or "").lower(), lang or "русский")

    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content":
                 f"Язык канала: {language}.\nНазвание оригинала: {source}"}]
    title = _title_once(base, key, model, messages)
    if _REFUSAL.match(title):
        # Отказ словами — не название. Одна попытка с просьбой о нейтральной формулировке.
        title = _title_once(base, key, model,
                            messages + [{"role": "assistant", "content": title},
                                        {"role": "user", "content": RETRY_HINT}])
        if _REFUSAL.match(title):
            raise TitleError(f"модель отказалась переписывать заголовок: {title[:120]}")
    if len(title) < 10:
        raise TitleError(f"модель вернула слишком короткое название: {title!r}")
    return title


def _title_once(base: str, key: str, model: str, messages: list) -> str:
    """Один заход к прокси названий. Возвращает очищённый ответ, классы отказа — как раньше."""
    try:
        resp = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "temperature": 0.7, "max_tokens": TITLE_MAX_TOKENS,
                  "messages": messages},
            timeout=TITLE_TIMEOUT)
    except requests.RequestException as exc:
        raise TitleUnavailable(f"модель не ответила: {type(exc).__name__}")
    if resp.status_code != 200:
        why = _brief(resp)
        if resp.status_code in (400, 422) and any(w in why.lower() for w in _FILTER_WORDS):
            raise TitleError(f"фильтр содержимого отверг название {resp.status_code}: {why}")
        # 401/403 (ключ, запрещённая модель), 404 (адрес), 429, 5xx и прочее — это
        # настройка или общий ресурс, а не вина строки: карантин здесь похоронил бы
        # весь готовый день из-за одной опечатки в модели.
        raise TitleUnavailable(f"модель {model} недоступна: {resp.status_code} {why}".strip())
    try:
        choice = resp.json()["choices"][0]
        text = (choice.get("message") or {}).get("content") or ""
        finish = str(choice.get("finish_reason") or "")
    except Exception:
        raise TitleUnavailable("не разобрал ответ прокси названий")
    if finish == "content_filter":
        raise TitleError("фильтр содержимого отверг название")
    if finish == "length" or not str(text).strip():
        raise TitleUnavailable(f"модель {model} вернула пустой или оборванный ответ ({finish or '—'})")
    return clean_title(text)


# ─────────────────────────── описание ───────────────────────────

def chapters_enabled(preset_settings) -> bool:
    """Включены ли главы в пресете VideoStudio канала."""
    if isinstance(preset_settings, str):
        try:
            preset_settings = json.loads(preset_settings or "{}")
        except Exception:
            return False
    if not isinstance(preset_settings, dict):
        return False
    value = preset_settings.get("auto_chapters")
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def read_chapters(project_dir: str) -> str:
    """Блок таймкодов, который сделала стадия глав. Нет файла — пустая строка."""
    path = os.path.join(project_dir or "", "chapters", "description.txt")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def make_description(title: str, chapters_text: str = "") -> str:
    """Описание = название, а таймкоды блоком ниже, если они есть."""
    parts = [(title or "").strip()]
    if chapters_text.strip():
        parts.append(chapters_text.strip())
    text = "\n\n".join(p for p in parts if p)
    return text[:MAX_DESCRIPTION]


# ─────────────────────────── файл ролика ───────────────────────────

def find_video(project_dir: str, render_job_id=None) -> str:
    """Путь к готовому mp4 внутри папки проекта.

    Сначала ищем ровно тот файл, который сделал наш этап рендера (`render/<job>.mp4`),
    и только если его нет — самый свежий mp4 в папке render. Брать «любой mp4» без
    попытки точного совпадения нельзя: в папке могут лежать прошлые версии.
    """
    render_dir = os.path.join(project_dir or "", "render")
    if render_job_id:
        exact = os.path.join(render_dir, f"{render_job_id}.mp4")
        if os.path.exists(exact):
            return exact
    try:
        found = [os.path.join(render_dir, n) for n in os.listdir(render_dir)
                 if n.lower().endswith(".mp4")]
    except Exception:
        return ""
    if not found:
        return ""
    return max(found, key=lambda p: os.path.getmtime(p))


# ─────────────────────────── время выхода ───────────────────────────

def schedule_iso(slot_at: float, tz_name: str) -> str:
    """Момент слота → строка с обязательным смещением, минутная точность.

    Заливщик принимает только такой формат; наивное время он отвергает, а без времени
    публикует немедленно — то есть ошибка здесь означала бы ролик, вышедший не тогда.
    """
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(tz_name or "Europe/Warsaw")
    except Exception:
        raise TitleError(f"неизвестный часовой пояс канала: {tz_name!r}")
    point = dt.datetime.fromtimestamp(float(slot_at), dt.timezone.utc).astimezone(zone)
    point = point.replace(second=0, microsecond=0)
    if point.utcoffset() is None:
        raise TitleError("не удалось определить смещение часового пояса")
    # Неоднозначный час (ночь перевода стрелок назад) — отказываемся ДО постановки задачи,
    # иначе ролик уйдёт на час раньше или позже и подтверждение не сойдётся.
    back = point.astimezone(dt.timezone.utc).astimezone(zone)
    if back.utcoffset() != point.utcoffset():
        raise TitleError("время выхода попало на перевод часов — сдвиньте расписание канала")
    return point.isoformat(timespec="minutes")
