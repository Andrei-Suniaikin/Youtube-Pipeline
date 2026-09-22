#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Телеграм как транспорт тревог. Только stdlib.

Что здесь есть и чего нет: здесь нет ни одного условия срабатывания — все одиннадцать поводов
живут в `pub_alerts.py` и проверяются без сети. Здесь только «отправить строку».

**Не настроен — значит выключен.** Нет `VS_TG_TOKEN` и `VS_TG_CHAT` — `send()` возвращает
False, и `pub_alerts.tick()` не считает тревогу произнесённой. Это важно: молчащий
транспорт не должен превращать аварию в «уже сообщили».

Лимиты Telegram Bot API (бесплатный): около 30 сообщений в секунду суммарно, не чаще
одного в секунду в один чат и не больше 20 в минуту в групповой чат. Наши тревоги —
десять поводов-состояний, не чаще раза в час каждый, плюс §7 — по одному
сообщению на упавшую задачу, с потолком на проход. В лимиты мы не упираемся даже
близко, но паузу в секунду между сообщениями и обработку 429 всё равно держим — очередь ошибок при развале системы
бывает длиннее ожидаемого.

    VS_TG_TOKEN=123456:AA...   токен от @BotFather
    VS_TG_CHAT=-1001234567890  куда слать (узнать: dev/alerts.py --whoami)
"""
from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.telegram.org"
TIMEOUT = 15
MIN_GAP = 1.0          # не чаще одного сообщения в секунду в один чат
LIMIT = 4096           # предел одного сообщения Telegram


def _ssl_context():
    """Доверенные корни: сначала certifi, если он есть, иначе системные.

    На Linux системного набора хватает, поэтому в бою это ничего не меняет. А вот
    framework-Python на macOS идёт БЕЗ заполненного хранилища, пока вручную не запущен
    «Install Certificates.command», и `urlopen` падает на «self-signed certificate in
    certificate chain». Выглядело это как «телеграм недоступен» — то есть тревоги
    молчали ровно там, где их и проверяют.
    """
    try:
        import certifi
    except ImportError:
        return None                     # системные корни: как было
    return ssl.create_default_context(cafile=certifi.where())


class Telegram:
    def __init__(self, token=None, chat=None, log=print):
        self.token = (token or os.environ.get("VS_TG_TOKEN") or "").strip()
        self.chat = (chat or os.environ.get("VS_TG_CHAT") or "").strip()
        self.log = log or (lambda *_a: None)
        self._last = 0.0
        self._warned = False

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat)

    def _call(self, method: str, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(f"{API}/bot{self.token}/{method}", data=data,
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")

    def send(self, text: str) -> bool:
        """Отправить. True — Telegram подтвердил приём, и только тогда.

        Никакого parse_mode: в текст попадают причины отказов провайдеров, то есть
        произвольные строки. Разметка означала бы, что чужой текст со звёздочкой
        ломает отправку — молча и в самый неподходящий момент.
        """
        if not self.enabled:
            if not self._warned:
                self.log("[тревоги] телеграм не настроен: нет VS_TG_TOKEN / VS_TG_CHAT")
                self._warned = True
            return False
        gap = MIN_GAP - (time.time() - self._last)
        if gap > 0:
            time.sleep(gap)
        body = str(text or "")[:LIMIT]
        for attempt in (1, 2):
            try:
                r = self._call("sendMessage", {"chat_id": self.chat, "text": body,
                                               "disable_web_page_preview": True})
                self._last = time.time()
                return bool(r.get("ok"))
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                wait = _retry_after(raw)
                if exc.code == 429 and wait and attempt == 1:
                    self.log(f"[тревоги] телеграм просит подождать {wait} с")
                    time.sleep(min(wait, 60))
                    continue
                self.log(f"[тревоги] телеграм отказал: HTTP {exc.code} {raw[:200]}")
                return False
            except Exception as exc:                               # noqa: BLE001
                # Причину печатаем целиком: «URLError» без текста одинаково выглядит
                # и когда нет интернета, и когда закрыт исходящий трафик, и когда
                # api.telegram.org не резолвится. Разбираться с этим будет человек.
                self.log(f"[тревоги] телеграм недоступен: {type(exc).__name__}: {exc}")
                return False
        return False

    def whoami(self) -> dict:
        """Кто этот бот и куда он может писать.

        Нужно ровно один раз — чтобы узнать chat_id. Telegram не даёт его иначе.
        Три случая, и они разные:

        * **личный чат** — написать боту любое сообщение;
        * **группа** — добавить бота и отправить туда `/start` (обычный текст он может
          не увидеть: у ботов по умолчанию включён privacy mode, и в группах им видны
          только команды и обращения);
        * **канал** — боту не пишут, его делают АДМИНИСТРАТОРОМ с правом публикации.
          В этот момент Telegram присылает `my_chat_member`, и канал появляется здесь.

        Для публичного канала chat_id вообще не нужен: годится `@имя_канала`.
        """
        out = {"token": bool(self.token), "me": None, "chats": [], "error": ""}
        if not self.token:
            out["error"] = "нет VS_TG_TOKEN"
            return out
        try:
            me = self._call("getMe", {})
            out["me"] = (me.get("result") or {}) if me.get("ok") else None
            upd = self._call("getUpdates", {"limit": 100, "timeout": 0})
        except urllib.error.HTTPError as exc:
            out["error"] = f"HTTP {exc.code}: {exc.read().decode('utf-8','replace')[:200]}"
            return out
        except Exception as exc:                                   # noqa: BLE001
            out["error"] = f"{type(exc).__name__}: {exc}"
            return out
        seen = {}
        for u in (upd.get("result") or []):
            msg = (u.get("message") or u.get("channel_post")
                   or u.get("my_chat_member") or u.get("chat_member") or {})
            chat = msg.get("chat") or {}
            if chat.get("id") is None:
                continue
            username = chat.get("username") or ""
            seen[chat["id"]] = {
                "id": chat["id"], "type": chat.get("type") or "",
                "username": username,
                # Публичный канал или группу можно адресовать по имени — это устойчивее
                # числового id и читается человеком.
                "as_name": f"@{username}" if username else "",
                "title": chat.get("title")
                         or " ".join(x for x in (chat.get("first_name"),
                                                 chat.get("last_name")) if x)
                         or username or ""}
        out["chats"] = sorted(seen.values(), key=lambda x: str(x["id"]))
        return out


def _retry_after(raw: str) -> int:
    try:
        return int(((json.loads(raw) or {}).get("parameters") or {}).get("retry_after") or 0)
    except (ValueError, AttributeError, TypeError):
        return 0


def sender(log=print):
    """Готовая функция для `pub_alerts.tick(c, send)`."""
    return Telegram(log=log).send
