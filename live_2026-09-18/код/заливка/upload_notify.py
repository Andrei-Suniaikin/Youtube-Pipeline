#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Короткий пинг оператору в Telegram с машины заливки.

Зачем отдельно от сторожа: сторож живёт на VPS и смотрит на конвейер снаружи, а
это сообщение рождается прямо в момент заливки на Минске или Маке — когда вход в
аккаунт упёрся в то, что может решить только человек (капча, телефон, отклонённый
пароль). Формат конфига тот же, что у сторожа, чтобы бот и чат были одни и те же.

Конфиг (0600, только владельцу):
    {"telegram": {"enabled": true, "botToken": "...", "chatId": "...", "repeatMinutes": 30}}
Путь берётся из VIDEO_UPLOAD_TELEGRAM_CONFIG, иначе ~/.config/video-ops/telegram.json.

``repeatMinutes`` (необязательный): «Заливка остановлена» по той же паре
(профиль, канал) с той же причиной повторно не уходит, пока не пройдёт столько
минут. Ключа нет или 0 — как раньше, каждое обращение шлётся. Первое сообщение и
смена причины уходят всегда. Память дедупа — файл на пару (профиль, канал) в
~/.local/state/video-studio/upload-notify/ (отдельно от account-recovery: чистка
остывания восстановления дедуп не сбрасывает); переопределяется
VIDEO_UPLOAD_NOTIFY_STATE_DIR — только для тестов.

Отправка НИКОГДА не ломает заливку: любая ошибка возвращается как False.
Секреты в текст не попадают — вызывающий передаёт только профиль, канал и причину.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG_ENV = "VIDEO_UPLOAD_TELEGRAM_CONFIG"
DEFAULT_CONFIG = Path.home() / ".config/video-ops/telegram.json"
MAX_CHARS = 3500
TIMEOUT = 15.0
REPEAT_KEY = "repeatMinutes"
STATE_DIR_ENV = "VIDEO_UPLOAD_NOTIFY_STATE_DIR"
DEFAULT_STATE_DIR = Path.home() / ".local/state/video-studio/upload-notify"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]")

# В лог не попадают ни пути, ни токен, ни текст сообщения — только факт.
log = logging.getLogger(__name__)


def _telegram_section():
    path = Path(os.getenv(CONFIG_ENV) or DEFAULT_CONFIG)
    try:
        if path.stat().st_mode & 0o077:
            # Файл с токеном бота не должен быть читаем посторонними.
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    telegram = raw.get("telegram") if isinstance(raw, dict) else None
    if not isinstance(telegram, dict) or not telegram.get("enabled"):
        return None
    return telegram


def _config():
    telegram = _telegram_section()
    if telegram is None:
        return None
    token, chat = telegram.get("botToken"), telegram.get("chatId")
    if not isinstance(token, str) or not token or chat in (None, ""):
        return None
    return token, str(chat)


def _repeat_window_seconds():
    """Окно дедупа в секундах; 0 — дедуп выключен (нет ключа, 0 или мусор)."""
    telegram = _telegram_section()
    if telegram is None:
        return 0
    value = telegram.get(REPEAT_KEY)
    if value in (None, ""):
        return 0
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        minutes = -1
    if minutes < 0:
        log.warning("telegram.%s — не число минут; дедуп повторов выключен", REPEAT_KEY)
        return 0
    return minutes * 60


def available():
    """Есть ли рабочая настройка — чтобы вызывающий мог сказать это в логе."""
    return _config() is not None


def _safe_name(value):
    """Имя файла из любого значения (в ручном вызове профиль бывал «—»)."""
    text = str(value)
    safe = _SAFE_NAME.sub("_", text)[:40] or "_"
    if safe != text:
        # Разные «странные» значения не должны сливаться в один файл.
        safe += "-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return safe


def _state_path(profile, channel_uc):
    directory = Path(os.getenv(STATE_DIR_ENV) or DEFAULT_STATE_DIR)
    return directory / f"{_safe_name(profile)}_{_safe_name(channel_uc)}.json"


def _read_last(profile, channel_uc):
    """Последний ушедший пинг по паре; нет записи или она битая — {} (шлём)."""
    try:
        value = json.loads(_state_path(profile, channel_uc).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_last(profile, channel_uc, reason, when):
    """Атомарная запись; свой .tmp на процесс — параллельные заливки не мешают."""
    path = _state_path(profile, channel_uc)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"schema_version": 1, "reason": reason, "sent_at": when},
                                  ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        # Не только OSError: например, суррогат в причине даёт UnicodeEncodeError.
        # Сообщение к этому моменту уже ушло — падать поверх него нельзя.
        log.warning("дедуп пинга не сохранён (%s): повтор может уйти ещё раз",
                    type(exc).__name__)


def send(text):
    """Отправить одно сообщение. True — ушло, False — нет (без исключений)."""
    settings = _config()
    if settings is None or not isinstance(text, str) or not text.strip():
        return False
    token, chat = settings
    body = urllib.parse.urlencode({
        "chat_id": chat,
        "text": text[:MAX_CHARS],
        "disable_web_page_preview": "true",
    }).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return isinstance(payload, dict) and payload.get("ok") is True
    except Exception:
        # Ни текст ошибки, ни URL наружу не отдаём: в URL лежит токен бота.
        return False


def owner_help(profile, channel_uc, reason, *, host="", label="", hint="", deliver=None):
    """Сообщение «нужен человек» одной понятной строкой, без секретов.

    Повтор по той же паре (профиль, канал) с той же причиной внутри окна
    ``repeatMinutes`` глушится (False, строка в логе). Запись делается только
    после удачной отправки: сбой сети не запирает следующую попытку.
    ``deliver`` — подстановка отправки для тестов; по умолчанию :func:`send`.
    """
    where = f" ({host})" if host else ""
    name = f"\nКанал: {label}" if label else ""
    lines = [
        "🔴 Заливка остановлена: нужен ты",
        f"Профиль AdsPower: {profile}{where}{name}",
        f"Канал: {channel_uc}" if not label else f"UC: {channel_uc}",
        f"Причина: {reason}",
    ]
    if hint:
        lines.append(hint)
    deliver = deliver or send
    key = " ".join(str(reason).split())
    window = 0
    try:
        window = _repeat_window_seconds()
        if window > 0:
            last = _read_last(profile, channel_uc)
            try:
                age = time.time() - float(last.get("sent_at") or 0)
            except (TypeError, ValueError):
                age = -1.0
            if last.get("reason") == key and 0 <= age < window:
                log.info("повтор пинга подавлен: тот же профиль, канал и причина, %d мин назад",
                         age // 60)
                return False
    except Exception as exc:
        # Дедуп «мягкий»: любая неожиданная ошибка состояния — шлём как раньше.
        # Потерянное первое сообщение хуже лишнего повтора.
        log.warning("проверка повтора пинга не удалась (%s): отправляю без неё",
                    type(exc).__name__)
    sent = bool(deliver("\n".join(lines)))
    if sent and window > 0:
        _write_last(profile, channel_uc, key, time.time())
    return sent
