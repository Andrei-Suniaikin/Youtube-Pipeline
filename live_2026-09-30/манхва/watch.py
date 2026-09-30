"""Присмотр за очередью при 20 роликах в сутки: сводка за сутки, автоповтор временных сбоев, Telegram.

Ничего не задано — всё как в 623ae20: автоповтора нет, сообщений нет. Сводка на главной только читает очередь.

MANGA_AUTO_RETRY_MINUTES   пауза перед единственным автоповтором временного сбоя, 1–180 мин; 0 или пусто — выключен.
MANGA_TG_BOT_TOKEN         токен бота Telegram; вместе с MANGA_TG_CHAT_ID включает сообщения
MANGA_TG_CHAT_ID           «ролик готов» и «ролик упал: причина». Задан один из двух — сообщений нет, в журнале предупреждение.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("manga.watch")

DAY = 24 * 3600

# ------------------------------------------------------------------ временный или постоянный сбой

# Постоянные: повтор без человека не поможет или опасен (двойная покупка). Проверяются первыми.
_PERMANENT_TYPES = {
    "LimitError", "TokenError", "TokensRejected",          # лимит подписки Claude / токены не приняты
    "LumeanConfigError", "_Manual", "_Terminal",           # нет ключа/шаблона Lumean, заказ требует разбора
    "QuotaWait", "QuotaWaitError",                         # час FastGen исчерпан — ждать сброса, а не повторять
    "PendingImageError", "UncertainRequest", "FailedRequest",
    "ChannelsError", "KeyError", "FileNotFoundError", "PermissionError", "NotADirectoryError",
}
_PERMANENT_TEXT = (
    "лимит подписки", "упираются в лимит", "не принимает токен", "не принимает запрос", "не приняты",
    "квота часа", "quota", "rate limit", "unauthorized", "forbidden", "http 401", "http 403",
    "ключ отвергнут", "ключ fastgen не найден", "не найден в кабинете", "нет ключа", "ключ сменился",
    "not logged in", "login required", "codex_home", "command not found", "permission denied",
    "неверн", "задайте", "настройк", "ручного разбора", "мог принять запрос",
    "повторная покупка остановлена", "осталась без ответа", "квитанция", "нет субтитров",
    "отключены субтитры", "недоступен — проверьте ссылку", "возрастным ограничением", "не отдаёт субтитры",
    "длиннее", "превышает", "не найден (text/source.txt)", "no space left", "на диске нет места",
    # Lumean: отправка заказа оборвалась или получила 5xx — квитанция «creating» остаётся, и повтор
    # упрётся в неё («заказ мог создаться и оплатиться»). Повтор без человека бесполезен.
    "заказ не создан",
)
# Временные: сеть, таймаут, 5xx поставщика, прокси не отвечает, занятые слоты.
_TEMPORARY_TYPES = {
    "TimeoutError", "timeout", "ConnectionError", "ConnectionResetError", "ConnectionRefusedError",
    "ConnectionAbortedError", "BrokenPipeError", "LumeanNetworkError", "BusyError",
    "Timeout", "ReadTimeout", "ConnectTimeout", "ProxyError", "SOCKSError", "GeneralProxyError",
    "RemoteDisconnected", "ServerDisconnectedError", "ClientConnectorError", "NoAudioReceived",
    "WSServerHandshakeError", "IncompleteRead",
}
_TEMPORARY_TEXT = (
    "не отвечает", "не ответил вовремя", "timed out", "timeout", "таймаут", "не уложился",
    "повторите позже", "нет связи", "connection reset", "connection refused", "connection aborted",
    "remote end closed", "remotedisconnected", "temporarily unavailable", "bad gateway",
    "service unavailable", "gateway time", "internal server error", "все слоты заняты",
    "ещё не завершилась", "не готов за", "noaudioreceived", "server disconnected",
    "max retries exceeded", "name resolution", "network is unreachable", "ограничил частоту запросов",
)
# «не установлен» (пакет, CLI) — постоянный; «соединение не установлено» у прокси субтитров (r2-subs) — нет.
_NOT_INSTALLED = re.compile(r"не установлен(?![а-яё])")
_HTTP_5XX = re.compile(r"(?i)\b(?:http|post|get|usage|status|скачивание: http)\s*:?\s*5\d\d\b")


def _chain(exc):
    seen = []
    while exc is not None and exc not in seen and len(seen) < 8:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


def _text_kind(text):
    lower = str(text or "").lower()
    if not lower.strip():
        return None
    if any(t in lower for t in _PERMANENT_TEXT) or _NOT_INSTALLED.search(lower):
        return "permanent"
    if _HTTP_5XX.search(lower) or any(t in lower for t in _TEMPORARY_TEXT):
        return "temporary"
    return None


def classify(exc=None, text=None):
    """'temporary' — сбой сети или поставщика, один повтор через паузу разумен; 'permanent' — всё остальное.
    Неизвестное считается постоянным: лишний платный повтор хуже, чем ошибка на странице."""
    chain = _chain(exc) if exc is not None else []
    texts = [text] if text is not None else []
    texts += [str(e) for e in chain]
    names = {type(e).__name__ for e in chain}
    if names & _PERMANENT_TYPES or any(_text_kind(t) == "permanent" for t in texts):
        return "permanent"
    # «Не получены картинки: N» — повторять, только если у каждого несданного кадра причина временная.
    frames = getattr(exc, "frame_errors", None)
    if frames is not None:
        return "temporary" if frames and all(_text_kind(t) == "temporary" for t in frames) else "permanent"
    if names & _TEMPORARY_TYPES or any(_text_kind(t) == "temporary" for t in texts):
        return "temporary"
    # LumeanAPIError: 5xx и 429 поставщика временны, остальные коды — нет.
    status = next((getattr(e, "status", None) for e in chain if type(e).__name__ == "LumeanAPIError"), None)
    if isinstance(status, int) and (status >= 500 or status == 429):
        return "temporary"
    return "permanent"


def retry_seconds(env=None):
    """Пауза перед автоповтором в секундах; 0 — автоповтор выключен. Неверное значение останавливает запуск службы."""
    env = os.environ if env is None else env
    raw = str(env.get("MANGA_AUTO_RETRY_MINUTES", "") or "").strip()
    if not raw:
        return 0
    try:
        minutes = int(raw)
    except ValueError:
        raise ValueError(f"MANGA_AUTO_RETRY_MINUTES: целое число минут от 0 до 180, а не {raw!r}") from None
    if not 0 <= minutes <= 180:
        raise ValueError(f"MANGA_AUTO_RETRY_MINUTES: целое число минут от 0 до 180, а не {raw!r}")
    return minutes * 60


# ------------------------------------------------------------------ сводка за сутки

def summarize(projects, now=None):
    """Сводка для главной: за последние сутки готово/ошибок, сейчас идёт/ждёт, ссылки на упавшие."""
    now = time.time() if now is None else now
    since = now - DAY
    done = failed = running = waiting = retried = 0
    failures, older = [], 0
    for p in projects:
        status, finished = p.get("status"), p.get("finished_at")
        recent = isinstance(finished, (int, float)) and finished >= since
        if status == "running":
            running += 1
        elif status == "pending":
            waiting += 1
        elif status == "complete" and recent:
            done += 1
        elif status == "error":
            if recent or not isinstance(finished, (int, float)):
                failed += 1
                failures.append(dict(id=p["id"], title=p.get("title", ""), channel_name=p.get("channel_name") or "",
                                     reason=_first_line(p.get("error", "")), step=p.get("failed_step") or ""))
            else:
                older += 1
        auto = p.get("auto_retry") or {}
        if isinstance(auto.get("created"), (int, float)) and auto["created"] >= since:
            retried += 1
    return dict(done=done, running=running, waiting=waiting, failed=failed, retried=retried,
                failures=failures[:20], more_failures=max(0, len(failures) - 20), older_failures=older)


def _first_line(text):
    line = str(text or "").strip().split("\n", 1)[0]
    return line[:197] + "…" if len(line) > 200 else line


# ------------------------------------------------------------------ Telegram

_BOT_TOKEN = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")


def telegram_settings(env=None):
    """(токен, чат) или None — сообщения выключены."""
    env = os.environ if env is None else env
    token = str(env.get("MANGA_TG_BOT_TOKEN", "") or "").strip()
    chat = str(env.get("MANGA_TG_CHAT_ID", "") or "").strip()
    if not token and not chat:
        return None
    if not token or not chat:
        log.warning("Сообщения в Telegram выключены: задан только один из MANGA_TG_BOT_TOKEN и MANGA_TG_CHAT_ID")
        return None
    return token, chat


def mask(text, token=None):
    """Токен бота не должен попасть ни в журнал, ни в сообщение: маскируем по содержимому."""
    out = str(text or "")
    if token:
        out = out.replace(token, "[скрыто]")
    return _BOT_TOKEN.sub("[скрыто]", out)


def _post(url, payload, timeout):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:   # noqa: S310 — адрес постоянный
        return response.status


class Notifier:
    """Шлёт сообщения в фоне: сбой Telegram никогда не останавливает ролик."""

    def __init__(self, env=None, sender=None, background=True):
        self.settings = telegram_settings(env)
        self.sender = sender or _post
        self.background = background

    @property
    def enabled(self):
        return self.settings is not None

    def send(self, text):
        if not self.settings:
            return False
        token, chat = self.settings
        payload = {"chat_id": chat, "text": mask(text, token)[:3500], "disable_web_page_preview": True}
        url = f"https://api.telegram.org/bot{token}/sendMessage"

        def deliver():
            try:
                self.sender(url, payload, 15)
            except Exception as exc:  # noqa: BLE001 — уведомление вторично
                log.warning("Сообщение в Telegram не отправлено: %s", mask(f"{type(exc).__name__}: {exc}", token))

        if self.background:
            threading.Thread(target=deliver, name="manga-telegram", daemon=True).start()
        else:
            deliver()
        return True

    def ready(self, project):
        return self.send(f"Ролик готов: {_label(project)}")

    def failed(self, project, step, reason):
        where = f" на этапе «{step}»" if step else ""
        return self.send(f"Ролик упал{where}: {_label(project)}\nПричина: {_first_line(reason)}")


def _label(project):
    title = str(project.get("title") or project.get("id") or "")
    channel = project.get("channel_name")
    return f"«{title}»" + (f" ({channel})" if channel else "") + f", {project.get('minutes', '?')} мин"
