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

LANG_NAMES = {
    "pl": "польский", "cs": "чешский", "sk": "словацкий", "ro": "румынский",
    "hr": "хорватский", "el": "греческий", "hu": "венгерский", "ru": "русский",
    "de": "немецкий", "fr": "французский", "it": "итальянский", "es": "испанский",
    "pt": "португальский", "po": "португальский", "fi": "финский", "bg": "болгарский",
    "uk": "украинский", "en": "английский", "tr": "турецкий", "nl": "нидерландский",
}

SYSTEM = (
    "Ты переписываешь названия видео для YouTube-канала на заданном языке. "
    "Сохраняй смысловую структуру исходного названия и его интригу, но НЕ переводи дословно: "
    "формулируй так, как назвал бы ролик носитель языка. "
    "Верни ТОЛЬКО готовое название одной строкой, без кавычек, без пояснений, "
    "без хештегов и без эмодзи. Не длиннее 90 символов."
)


# Начало ответа, которым модель ОТКАЗЫВАЕТСЯ писать, а не пишет. 04.10.2026 на канал ВВ2
# «Ημερολόγια του Μετώπου» вышел ролик fMYByvfhsQU с названием «I can't help rewrite titles
# that use sexual violence…»: ответ был длиннее 10 знаков, и проверка длины его пропустила.
# Ищем только в НАЧАЛЕ строки: честное название может содержать «sorry» в середине.
TITLE_REFUSAL_RE = re.compile(
    r"^\s*(?:"
    r"i\s+(?:can['’]?t|cannot|can\s+not|won['’]?t|will\s+not|am\s+(?:not\s+able|unable|sorry))\b"
    r"|i['’]m\s+(?:not\s+able|unable|sorry)\b"
    r"|sorry\b|unfortunately,?\s+i\b|as\s+an\s+ai\b"
    r"|извини(?:те)?\b|простите\b|к\s+сожалению,?\s+(?:я|не)\b"
    r"|я\s+не\s+(?:могу|буду|стану)\s+(?:помочь|переписать|написать|выполнить|создать)\b"
    r"|не\s+могу\s+(?:помочь|переписать|написать|выполнить)\b"
    r")",
    re.I)


def refusal_in(title: str) -> bool:
    """Похоже ли «название» на отказ модели. Тогда это не название, а повод остановиться."""
    return bool(TITLE_REFUSAL_RE.search(title or ""))


def refused(title: str) -> "TitleError":
    return TitleError(f"модель отказалась писать название: «{title[:80]}» — нужен другой "
                      f"донор или название вручную")


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
    # После пометки «Название:» модель часто ставит кавычки ещё раз — снимаем и их.
    line = line.strip('"').strip("«»").strip("'").strip()
    if len(line) > MAX_TITLE:
        cut = line[:MAX_TITLE]
        if " " in cut:
            cut = cut[:cut.rfind(" ")]
        line = cut.rstrip(" ,;:—-")
    return line


def make_title(donor_title: str, lang: str, api_key: str, model: str = "gpt-4.1-mini") -> str:
    """Название донора → название на языке канала."""
    source = " ".join((donor_title or "").split())
    if not source:
        raise TitleError("у донора нет названия — не из чего делать заголовок")
    if not api_key:
        raise TitleUnavailable("нет ключа для генерации названия — ждём настройки")
    language = LANG_NAMES.get((lang or "").lower(), lang or "русский")

    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "temperature": 0.7, "max_tokens": 120,
                  "messages": [
                      {"role": "system", "content": SYSTEM},
                      {"role": "user", "content":
                       f"Язык канала: {language}.\nНазвание оригинала: {source}"}]},
            timeout=45)
    except requests.RequestException as exc:
        raise TitleUnavailable(f"модель не ответила: {type(exc).__name__}")
    if resp.status_code == 429 or resp.status_code >= 500:
        raise TitleUnavailable(f"модель временно недоступна: {resp.status_code}")
    if resp.status_code != 200:
        raise TitleError(f"модель отвергла запрос {resp.status_code}: {resp.text[:160]}")
    try:
        text = resp.json()["choices"][0]["message"]["content"]
    except Exception:
        raise TitleError("не разобрал ответ модели")
    title = clean_title(text)
    if refusal_in(title):
        raise refused(title)
    if len(title) < 10:
        raise TitleError(f"модель вернула слишком короткое название: {title!r}")
    return title


# Модель Codex для названий: та же, что пишет тексты рерайта (решение владельца 02.10).
TITLE_CODEX_MODEL = os.environ.get("VS_TITLE_CODEX_MODEL", "gpt-5.6-sol")
# Сколько ждать ответа сервиса текста. Он сам ждёт места в подписке до 5 минут
# (REWRITE_CHAT_ONCE_WAIT_SEC), плюс сам вызов: 04.10 название пришло за 244 с и было
# выброшено таймаутом в 240 с.
TITLE_TIMEOUT_SEC = float(os.environ.get("VS_TITLE_TIMEOUT_SEC", "480") or 480)


def make_title_codex(donor_title: str, lang: str, rewrite_key: str, base: str,
                     model: str = "") -> str:
    """Название на языке канала — подпиской Codex через сервис текста (chat-once).

    04.10.2026: 33 готовых ролика стояли перед заливкой с «модель временно недоступна:
    429». Названия писал OpenAI напрямую (`make_title`), а счёт OpenAI пуст с 21.09:
    ответ «нет денег» тоже 429, и выпуск ждал вечно. Владелец: «промпты должны улетать
    в подписку Codex». Подпиской распоряжается сервис текста (пулы, аренда, лимиты) —
    сюда, как и у обложек, ходим только его же входом `chat-once`.
    """
    source = " ".join((donor_title or "").split())
    if not source:
        raise TitleError("у донора нет названия — не из чего делать заголовок")
    if not rewrite_key:
        raise TitleUnavailable("нет ключа сервиса текста — ждём настройки")
    language = LANG_NAMES.get((lang or "").lower(), lang or "русский")
    payload = {"provider": "codex", "model": model or TITLE_CODEX_MODEL, "images": [],
               "messages": [{"role": "system", "content": SYSTEM},
                            {"role": "user", "content":
                             f"Язык канала: {language}.\nНазвание оригинала: {source}"}]}
    try:
        resp = requests.post(base.rstrip("/") + "/api/v1/chat-once", json=payload,
                             headers={"Authorization": f"Bearer {rewrite_key}"},
                             timeout=TITLE_TIMEOUT_SEC)
    except requests.RequestException as exc:
        raise TitleUnavailable(f"сервис текста не ответил: {type(exc).__name__}")
    if resp.status_code != 200:
        try:
            detail = str((resp.json() or {}).get("detail") or "")
        except ValueError:
            detail = resp.text[:160]
        # Любой отказ подписки — повод подождать, а не хоронить готовый ролик: лимит
        # окна, занятая учётка, сбой CLI проходят сами. Битым здесь может быть только
        # донор, а его мы проверили выше.
        raise TitleUnavailable(f"подписка Codex не дала название ({resp.status_code}): "
                               f"{detail[:160]}")
    try:
        text = str((resp.json() or {}).get("text") or "")
    except ValueError:
        raise TitleUnavailable("не разобрал ответ сервиса текста")
    title = clean_title(text)
    if refusal_in(title):
        raise refused(title)
    if len(title) < 10:
        raise TitleUnavailable(f"подписка вернула слишком короткое название: {title!r}")
    return title


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

class VideoAmbiguous(Exception):
    """В папке рендера несколько mp4, и какой из них наш — неизвестно."""


def find_video(project_dir: str, render_job_id=None, output_rel: str = "") -> str:
    """Путь к готовому mp4 внутри папки проекта — точный, а не угаданный.

    Порядок: путь, записанный САМИМ завершившимся рендером (`queue_tasks.output_rel`) →
    штатное имя `render/<job>.mp4` → единственный mp4 в папке.

    Чего здесь больше нет: выбора «самый свежий mp4». В папке проекта лежат прошлые
    версии ролика и результаты перерендеров, и «самый свежий по времени файла» — это
    догадка, ценой которой чужой ролик уходит на канал под нашим названием и в наш
    слот. Когда точного ответа нет, а кандидатов несколько, честный исход — отказ с
    перечислением файлов: пусть решит человек.
    """
    render_dir = os.path.join(project_dir or "", "render")
    if output_rel:
        # Путь относительный папке проекта — так его записывает приём результата.
        exact = os.path.join(project_dir or "", output_rel)
        if os.path.exists(exact):
            return exact
    if render_job_id:
        exact = os.path.join(render_dir, f"{render_job_id}.mp4")
        if os.path.exists(exact):
            return exact
    try:
        found = sorted(os.path.join(render_dir, n) for n in os.listdir(render_dir)
                       if n.lower().endswith(".mp4"))
    except Exception:
        return ""
    if not found:
        return ""
    if len(found) == 1:
        return found[0]
    raise VideoAmbiguous(
        "в папке рендера несколько готовых mp4 и не записано, какой из них наш: "
        + ", ".join(os.path.basename(p) for p in found)
        + ". Оставьте нужный файл или перезапустите рендер — угадывать «самый свежий» "
          "нельзя, на канал уйдёт чужой ролик")


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
