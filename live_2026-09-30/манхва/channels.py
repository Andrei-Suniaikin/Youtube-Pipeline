# -*- coding: utf-8 -*-
"""Пресеты каналов и пачка ссылок «ссылка ; канал».

Пресет — строка таблицы каналов: ключ, название, язык, голос, стиль, длительность.
Таблица лежит файлом channels.json в папке данных студии (рядом с queue.sqlite3,
на VPS — /root/manga_rewrite/projects_web/channels.json) и читается заново при каждом
открытии страницы: правка файла действует без перезапуска службы. Нет файла — нет
каналов, остальная студия работает как раньше.

Формат файла: {"channels": [{"key": "randkazmanhwa", "name": "Randka z Manhwą",
"language": "Polish", "voice": "pl-PL-MarekNeural", "style": "S4", "minutes": 70}, ...]}.
Язык можно писать ключом (Polish) или по-русски (Польский). Без voice — первый голос
языка, без style — S4 «Вебтун», без minutes — 70.

Необязательные поля для шага «Название» (publish.py): description — блок канала под описанием
ролика (до 3000 знаков), keywords — ключевые слова канала через запятую (до 500 знаков), они
идут в теги ролика. Без них шаг работает, только без блока канала и его тегов.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

try:
    from . import languages as LG
    from . import source as SRC
except ImportError:
    import languages as LG
    import source as SRC

FILE_NAME = "channels.json"
DEFAULT_STYLE = "S4"
DEFAULT_MINUTES = 70
STYLES = ("S0", "S1", "S2", "S3", "S4")
MAX_LINES = 30
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,59}$")
_LABELS = {spec["label"].casefold(): key for key, spec in LG.LANGS.items()}


class ChannelsError(ValueError):
    """Файл каналов не читается или в нём ошибка; текст — для владельца, по-русски."""


def path(root) -> Path:
    return Path(root) / FILE_NAME


def _language(value):
    value = str(value or "").strip()
    if value in LG.LANGS:
        return value
    return _LABELS.get(value.casefold())


def validate(data) -> list:
    """Проверенный список пресетов. Ошибка — ChannelsError с номером канала и причиной."""
    items = data.get("channels") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ChannelsError("нужен список каналов: {\"channels\": [ … ]}")
    out, keys, names = [], {}, {}
    for n, item in enumerate(items, 1):
        where = f"канал {n}"
        if not isinstance(item, dict):
            raise ChannelsError(f"{where}: нужна запись вида {{\"key\": …, \"name\": …, \"language\": …}}")
        name = str(item.get("name") or "").strip()
        if name:
            where = f"канал {n} «{name}»"
        key = str(item.get("key") or "").strip().lstrip("@").lower()
        if not KEY_RE.fullmatch(key):
            raise ChannelsError(f"{where}: ключ — латиница, цифры, точка, дефис или _, до 60 знаков "
                                f"(например хендл канала без @), а сейчас {item.get('key')!r}")
        if not 1 <= len(name) <= 80 or ";" in name or "\t" in name:
            raise ChannelsError(f"{where}: название — от 1 до 80 знаков, без «;» и табуляции")
        language = _language(item.get("language"))
        if not language:
            raise ChannelsError(f"{where}: язык {item.get('language')!r} студия не знает; есть: "
                                + ", ".join(f"{k} ({s['label']})" for k, s in LG.LANGS.items()))
        voices = [code for code, _ in LG.LANGS[language]["voices"]]
        voice = str(item.get("voice") or voices[0]).strip()
        if voice not in voices:
            raise ChannelsError(f"{where}: голос {voice!r} не подходит к языку {language}; есть: {', '.join(voices)}")
        style = str(item.get("style") or DEFAULT_STYLE).strip().upper()
        if style not in STYLES:
            raise ChannelsError(f"{where}: стиль {item.get('style')!r}; есть: {', '.join(STYLES)}")
        minutes = item.get("minutes", DEFAULT_MINUTES)
        if type(minutes) is not int or not 1 <= minutes <= 180:
            raise ChannelsError(f"{where}: длительность — целое число минут от 1 до 180, а сейчас {minutes!r}")
        if key in keys:
            raise ChannelsError(f"{where}: ключ {key!r} уже есть у канала {keys[key]}")
        if name.casefold() in names:
            raise ChannelsError(f"{where}: такое же название уже у канала {names[name.casefold()]}")
        extra = {}
        for field, limit in (("description", 3000), ("keywords", 500)):
            value = item.get(field)
            if value is None:
                continue
            if field == "keywords" and isinstance(value, list) and all(isinstance(v, str) for v in value):
                value = ", ".join(v.strip() for v in value if v.strip())
            if not isinstance(value, str) or len(value.strip()) > limit:
                raise ChannelsError(f"{where}: {field} — текст до {limit} знаков"
                                    + (" (или список строк)" if field == "keywords" else ""))
            if value.strip():
                extra[field] = value.strip()
        keys[key], names[name.casefold()] = n, n
        out.append(dict(key=key, name=name, language=language, voice=voice, style=style, minutes=minutes, **extra))
    return out


def load(root) -> list:
    """Пресеты из <root>/channels.json; нет файла — пустой список, битый файл — ChannelsError."""
    p = path(root)
    try:
        text = p.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ChannelsError(f"не удалось прочитать {p}: {exc.strerror or exc}") from None
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ChannelsError(f"{p} — не JSON: {exc}") from None
    try:
        return validate(data)
    except ChannelsError as exc:
        raise ChannelsError(f"{p}: {exc}") from None


def find(channels, ref):
    """Канал по ключу (можно с @) или по названию без учёта регистра; None — такого нет."""
    ref = str(ref or "").strip()
    if not ref:
        return None
    key = ref.lstrip("@").lower()
    for ch in channels:
        if ch["key"] == key or ch["name"].casefold() == ref.casefold():
            return ch
    return None


def request_token(channel, video_id) -> str:
    """Один ролик на пару «канал + ролик-донор»: повторная отправка пачки находит его, а не создаёт второй."""
    return f"batch:{channel['key']}:{video_id}"


def parse_batch(text, channels, default=None):
    """Строки пачки → (годные, ошибки, повторы).

    Строка: «ссылка ; канал» (или через табуляцию, как из таблицы) либо одна ссылка —
    тогда канал берётся общий для пачки (default). Пустые строки и строки с # пропускаются.
    Годная: {"line", "url", "video_id", "channel"}. Ошибка: {"line", "text", "error"}.
    Повтор той же пары в пачке: {"line", "text", "same_as"}."""
    rows = [(n, raw.strip()) for n, raw in enumerate(str(text or "").splitlines(), 1)]
    rows = [(n, s) for n, s in rows if s and not s.startswith("#")]
    if not rows:
        raise ValueError("Вставьте хотя бы одну строку: ссылка на ролик YouTube ; канал.")
    if len(rows) > MAX_LINES:
        raise ValueError(f"В пачке {len(rows)} строк, можно не больше {MAX_LINES}. Разбейте её на две.")
    good, errors, repeats, seen = [], [], [], {}
    for n, line in rows:
        parts = re.split(r"\s*[;\t]\s*", line, maxsplit=1)
        url = parts[0].strip()
        ref = parts[1].strip() if len(parts) > 1 else ""
        vid = SRC.video_id(url) if SRC.looks_like_link(url) else None
        if not vid:
            errors.append(dict(line=n, text=line, error="ссылка не похожа на ролик YouTube "
                                                        "(нужен адрес вида https://www.youtube.com/watch?v=…)"))
            continue
        channel = find(channels, ref) if ref else default
        if channel is None:
            errors.append(dict(line=n, text=line, error=(
                f"канал «{ref}» не найден в таблице каналов" if ref else
                "не указан канал: допишите «; канал» или выберите канал для всей пачки")))
            continue
        pair = (channel["key"], vid)
        if pair in seen:
            repeats.append(dict(line=n, text=line, same_as=seen[pair]))
            continue
        seen[pair] = n
        good.append(dict(line=n, url=url, video_id=vid, channel=channel))
    return good, errors, repeats
