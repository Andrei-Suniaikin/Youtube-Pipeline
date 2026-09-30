# -*- coding: utf-8 -*-
"""Шаг «Исходник»: текст ролика по ссылке YouTube или из вставленного текста.

Ролик по ссылке хранит source_url; шаг скачивает субтитры оригинала, пишет
text/source.txt и text/source_meta.json и превью в source/thumbnail.jpg.
Повторный запуск ничего не качает заново: готовый текст и превью остаются.
Ролик по тексту проходит шаг без сети.

Субтитры — через youtube-transcript-api (как в рерайт-студии, subs_fetch.py).
С адреса VPS YouTube их не отдаёт: нужен прокси MANGA_SUBS_PROXY (web.env).
Прокси может быть несколько — MANGA_SUBS_PROXIES, в порядке приоритета: шаг пробует их
по очереди; прокси, получивший отказ YouTube, отдыхает MANGA_SUBS_REST минут, недоступный
пропускается сразу (так же сделано у VideoStudio: VS_SUBS_PROXIES / VS_SUBS_REST).
Адрес прокси и его пароль не попадают ни в ошибки, ни в журнал, ни на страницы:
прокси везде называется по номеру в списке, схеме и порту.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

LINK_HOSTS = re.compile(r"(?i)^(?:https?://)?(?:[a-z0-9-]+\.)*(?:youtube\.com|youtube-nocookie\.com|youtu\.be)(?:[/?#]|$)")
_ID_PATTERNS = (
    re.compile(r"[?&]v=([\w-]{11})(?![\w-])"),
    re.compile(r"youtu\.be/([\w-]{11})(?![\w-])"),
    re.compile(r"/(?:embed|shorts|live|v)/([\w-]{11})(?![\w-])"),
)
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
TIMEOUT = 20.0
# Скелет получает исходник целиком. Через claude CLI окно ~200 тыс. токенов вместе с системной частью CLI;
# 400 000 символов английских субтитров ≈ 100–135 тыс. токенов. Длиннее — понятная ошибка до платных вызовов.
MAX_CHARS_DEFAULT = 400_000


class SourceError(RuntimeError):
    """Причина на русском, её можно показать владельцу как есть.

    kind: "blocked" — YouTube отказал этому адресу (IpBlocked/RequestBlocked/429), адрес отдыхает;
    "proxy" — прокси недоступен (отказ соединения, таймаут), берём следующий сразу;
    "address" — YouTube не принял именно этот адрес (согласие на cookie), берём следующий без отдыха;
    "" — причина в ролике или неизвестна, другой прокси её не исправит.
    video — True, если доказано, что причина в самом ролике (по классу ошибки библиотеки)."""

    def __init__(self, message, kind="", video=False):
        super().__init__(message)
        self.kind = kind
        self.video = video


def max_chars():
    raw = os.environ.get("MANGA_SOURCE_MAX_CHARS", "").strip()
    if not raw:
        return MAX_CHARS_DEFAULT
    if not raw.isdigit() or int(raw) < 1000:
        raise ValueError("MANGA_SOURCE_MAX_CHARS: целое число символов, не меньше 1000")
    return int(raw)


def looks_like_link(value):
    """Одно «слово» без пробелов, похожее на адрес: такое поле — ссылка, а не текст истории."""
    value = (value or "").strip()
    return bool(value) and not re.search(r"\s", value) and (
        value.lower().startswith(("http://", "https://", "www.")) or bool(LINK_HOSTS.match(value)))


def video_id(url):
    """Идентификатор ролика из ссылки YouTube; пустая строка — ссылка не на ролик YouTube."""
    value = (url or "").strip()
    if not LINK_HOSTS.match(value):
        return ""
    for pattern in _ID_PATTERNS:
        match = pattern.search(value)
        if match:
            return match.group(1)
    return ""


def canonical_url(vid):
    return f"https://www.youtube.com/watch?v={vid}"


def _proxy(value=None):
    value = (os.environ.get("MANGA_SUBS_PROXY", "") if value is None else value).strip()
    # socks5:// резолвит имена у нас, socks5h:// — у прокси; с адреса VPS нужно второе.
    if value.startswith("socks5://"):
        value = "socks5h://" + value[len("socks5://"):]
    return value


SUBS_REST_DEFAULT_MIN = 30   # как VS_SUBS_REST=1800 у VideoStudio
SUBS_STATE_FILE = "subs_proxies.json"
_state_lock = threading.Lock()


def proxies(value=None):
    """Прокси субтитров в порядке приоритета.

    MANGA_SUBS_PROXIES — через запятую (пробел, перенос строки) или JSON-список строк.
    Без него — старая MANGA_SUBS_PROXY как список из одного. Пусто — без прокси."""
    raw = (os.environ.get("MANGA_SUBS_PROXIES", "") if value is None else value).strip()
    if not raw:
        legacy = _proxy()
        return [legacy] if legacy else []
    if raw.startswith("["):
        try:
            items = json.loads(raw)
        except ValueError:
            raise ValueError("MANGA_SUBS_PROXIES: не разобран JSON-список — нужен [\"socks5h://…\", …]"
                             " или адреса через запятую") from None
        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            raise ValueError("MANGA_SUBS_PROXIES: JSON должен быть списком строк-адресов")
    else:
        items = re.split(r"[,\s]+", raw)
    result = []
    for number, item in enumerate((i.strip() for i in items if i and i.strip()), 1):
        if not re.match(r"(?i)^(socks5h?|socks4a?|https?)://[^/\s]+", item):
            raise ValueError(f"MANGA_SUBS_PROXIES: адрес №{number} без схемы или с неизвестной схемой —"
                             " нужен socks5h://…, socks5://… или http://…. Если в пароле есть запятая или пробел —"
                             " запишите список JSON-ом [\"…\", \"…\"] или замените запятую на %2C")
        item = _proxy(item)
        if item not in result:
            result.append(item)
    return result


def rest_minutes():
    """Сколько минут отдыхает прокси после отказа YouTube.

    По умолчанию 30 при списке MANGA_SUBS_PROXIES и 0 при одной старой MANGA_SUBS_PROXY:
    единственному прокси отдыхать не на кого, повтор шага пробует его сразу (как до списка)."""
    raw = os.environ.get("MANGA_SUBS_REST", "").strip()
    if not raw:
        return SUBS_REST_DEFAULT_MIN if os.environ.get("MANGA_SUBS_PROXIES", "").strip() else 0
    # Число без буквы — минуты. У VideoStudio VS_SUBS_REST в секундах (1800): его значение
    # переносится как «1800s», голое 1800 — ошибка с подсказкой, а не 30 часов отдыха.
    match = re.fullmatch(r"(?i)(\d+(?:[.,]\d+)?)\s*(s|m|h|с|м|ч)?", raw)
    value = -1.0
    if match:
        value = float(match.group(1).replace(",", "."))
        unit = (match.group(2) or "m").lower()
        value = value / 60 if unit in ("s", "с") else value * 60 if unit in ("h", "ч") else value
    if not value >= 0 or value > 24 * 60:
        raise ValueError("MANGA_SUBS_REST: отдых прокси после отказа YouTube в МИНУТАХ, от 0 до 1440"
                         " (например 30). Значение в секундах, как VS_SUBS_REST у VideoStudio, пишите"
                         " с буквой s: 1800s")
    return value


def proxy_label(number, proxy):
    """Имя прокси для людей: номер в списке, схема и порт — без адреса, логина и пароля."""
    try:
        parts = urlsplit(proxy)
        port = parts.port
        scheme = parts.scheme
    except ValueError:
        port, scheme = None, ""
    extra = ", ".join(x for x in (scheme, f"порт {port}" if port else "") if x)
    return f"прокси №{number}" + (f" ({extra})" if extra else "")


def _key(proxy):
    return hashlib.sha256(proxy.encode("utf-8")).hexdigest()[:16]


def state_path(store_root):
    return Path(store_root) / SUBS_STATE_FILE


def _load_state(path):
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _mark(path, proxy, **fields):
    """Записать, что случилось с прокси. В файле только хэш адреса и наши русские пометки."""
    if not path:
        return
    with _state_lock:
        data = _load_state(path)
        entry = data.get(_key(proxy)) if isinstance(data.get(_key(proxy)), dict) else {}
        entry.update(fields)
        data[_key(proxy)] = entry
        _write_json(path, data)


def _minutes_left(until, now):
    return max(1, int((until - now + 59) // 60))


def _fmt_time(ts):
    return time.strftime("%d.%m %H:%M", time.localtime(ts)) if ts else ""


def proxies_status(path, exits=None, now=None):
    """Состояние прокси для страницы: что было при последней попытке и последней проверке."""
    exits = proxies() if exits is None else exits
    now = time.time() if now is None else now
    data = _load_state(path)
    rows = []
    for number, proxy in enumerate(exits, 1):
        entry = data.get(_key(proxy)) or {}
        until = entry.get("rest_until") or 0
        resting = until > now
        rows.append({
            "label": proxy_label(number, proxy),
            "resting": resting,
            "rest_left_min": _minutes_left(until, now) if resting else 0,
            "last": entry.get("last") or "",
            "last_note": entry.get("last_note") or "",
            "last_at": _fmt_time(entry.get("last_at")),
            "alive": entry.get("alive"),
            "checked_at": _fmt_time(entry.get("checked_at")),
            "check_note": entry.get("check_note") or "",
        })
    return rows


def _probe(proxy):
    """Живой ли путь через прокси до YouTube: лёгкий запрос generate_204, без субтитров."""
    try:
        response = _session(proxy).get("https://www.youtube.com/generate_204", timeout=10, allow_redirects=False)
    except Exception as exc:
        kind = _kind(exc, proxy)
        return False, "YouTube не ответил вовремя" if kind == "timeout" else "не отвечает"
    if response.status_code in (200, 204):
        return True, "отвечает"
    if response.status_code == 429:
        return True, "прокси отвечает, YouTube ограничил частоту (429)"
    return True, f"прокси отвечает, YouTube вернул {response.status_code}"


def check_proxies(path, exits=None, probe=None):
    """Проверить все прокси сейчас (параллельно). Отдых после отказа проверка не снимает:
    generate_204 доступен и тому адресу, которому YouTube не отдаёт субтитры."""
    from concurrent.futures import ThreadPoolExecutor
    exits = proxies() if exits is None else exits
    probe = probe or _probe
    if exits:
        with ThreadPoolExecutor(max_workers=min(8, len(exits))) as pool:
            results = list(pool.map(probe, exits))
        now = time.time()
        for proxy, (alive, note) in zip(exits, results):
            _mark(path, proxy, alive=bool(alive), checked_at=now, check_note=str(note)[:120])
    return proxies_status(path, exits)


_BLOCK_NOTES = {"blocked": "YouTube отказал этому адресу", "rate": "YouTube ограничил частоту запросов (429)"}


def fetch_via_proxies(vid, fetch, path=None, exits=None, now=None):
    """Субтитры через первый пригодный прокси. Возвращает (результат, прокси).

    Отказ YouTube — прокси отдыхает rest_minutes() и берём следующий; недоступный — следующий
    сразу; ошибка без ясной причины (403, PoTokenRequired…) — следующий без отдыха; доказанная причина
    в ролике (нет субтитров, ролик удалён) — ошибка сразу, другой прокси не поможет."""
    exits = proxies() if exits is None else exits
    if not exits:
        return fetch(vid, ""), ""
    rest = rest_minutes() * 60
    # Один прокси без отдыха (старая MANGA_SUBS_PROXY) — файл состояния не заводим, как было до списка.
    keep = path if (len(exits) > 1 or rest) else None
    report, last, tried = [], None, 0
    for number, proxy in enumerate(exits, 1):
        name = proxy_label(number, proxy)
        moment = time.time() if now is None else now
        # Перечитываем перед каждым адресом: параллельный ролик мог только что получить отказ на нём.
        until = (_load_state(path).get(_key(proxy)) or {}).get("rest_until") or 0
        if until > moment:
            report.append(f"{name}: отдыхает после отказа YouTube, ещё {_minutes_left(until, moment)} мин")
            continue
        tried += 1
        try:
            got = fetch(vid, proxy)
        except SourceError as exc:
            kind = getattr(exc, "kind", "")
            moment = time.time() if now is None else now
            if kind in ("blocked", "rate"):
                _mark(keep, proxy, last="blocked", last_note=_BLOCK_NOTES[kind], last_at=moment,
                      rest_until=moment + rest if rest else 0)
                report.append(f"{name}: {_BLOCK_NOTES[kind]}" + (f", отдыхает {rest / 60:g} мин" if rest else ""))
                last = exc
                continue
            if kind == "address":
                note = "YouTube не принял этот адрес (страница согласия на cookie — выход в ЕС?)"
                _mark(keep, proxy, last="address", last_note=note, last_at=moment)
                report.append(f"{name}: {note}")
                last = exc
                continue
            if kind in ("proxy", "timeout"):
                note = "не отвечает (соединение не установлено)" if kind == "proxy" else "не ответил вовремя"
                _mark(keep, proxy, last="down", last_note=note, last_at=moment)
                report.append(f"{name}: {note}")
                last = exc
                continue
            if getattr(exc, "video", False):
                _mark(keep, proxy, last="ok", last_note="прокси работает; ошибка в самом ролике", last_at=moment)
                raise
            # Причина не ясна (403/5xx, PoTokenRequired, непонятный ответ): это ответ этому адресу, а не
            # доказанная ошибка ролика — пробуем следующий прокси без отдыха.
            _mark(keep, proxy, last="unknown", last_note="ошибка без ясной причины — см. причину шага", last_at=moment)
            report.append(f"{name}: ошибка без ясной причины ({_cause(exc)})")
            last = exc
            continue
        _mark(keep, proxy, last="ok", last_note="субтитры получены", last_at=time.time() if now is None else now,
              rest_until=0)
        return got, proxy
    if len(exits) == 1 and tried == 1 and last is not None:
        raise last   # один прокси — прежний текст ошибки, как до списка
    raise SourceError("Субтитры не получены ни через один прокси: " + "; ".join(report) + ". "
                      "Проверьте туннели (кнопка «Проверить прокси» на странице ролика) и нажмите"
                      " «Повторить незавершённое».", kind="all")


def _cause(exc):
    """Короткая причина без адресов: имя исходной ошибки библиотеки, если SourceError её обернул."""
    inner = exc.__cause__ or exc.__context__
    return type(inner).__name__ if inner is not None and not isinstance(inner, SourceError) else type(exc).__name__


def redact(text, proxy=""):
    """Убирает из текста ошибки адрес прокси, логин и пароль в любых адресах."""
    text = str(text or "")
    if proxy:
        text = text.replace(proxy, "[прокси]")
        rest = proxy.split("://", 1)[-1]
        host = rest.rsplit("@", 1)[-1]
        user = rest.rsplit("@", 1)[0] if "@" in rest else ""
        parts = {rest, host, host.rsplit(":", 1)[0], user, *user.split(":", 1)}
        for part in sorted((p for p in parts if len(p) >= 3), key=len, reverse=True):
            text = text.replace(part, "[прокси]")
    return re.sub(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@", r"\1[скрыто]@", text)


# Ошибки самого ролика: другой прокси не поможет. Проверяются по классу ДО поиска подстрок —
# в их тексте есть ссылка на ролик, а номер ролика может содержать «429», «proxy», «socks».
_VIDEO_ERRORS = ("TranscriptsDisabled", "NoTranscriptFound", "NoTranscriptAvailable", "VideoUnavailable",
                 "VideoUnplayable", "InvalidVideoId", "AgeRestricted", "NotTranslatable",
                 "TranslationLanguageNotAvailable")


def _text(exc, vid):
    """Текст ошибки для поиска слов — без номера ролика и без ссылок.

    В ссылке timedtext из YouTubeRequestFailed есть expire/signature с любыми цифрами («429» в том числе),
    а настоящий ответ 429 библиотека сама превращает в IpBlocked."""
    text = str(exc)
    if vid:
        text = text.replace(vid, " ")
    return re.sub(r"(?i)\bhttps?://\S+", " ", text).lower()


_TIMEOUTS = ("Timeout", "ReadTimeout", "ConnectTimeout", "TimeoutError")


def _reason(exc, vid=""):
    """Одна классификация на всё: что случилось (без учёта, был ли прокси).

    video — ошибка самого ролика (по классу, до поиска слов); blocked — YouTube отказал адресу;
    proxy — прокси не отвечает; rate — 429; consent — YouTube не отдал страницу согласия на cookie
    (зависит от страны выхода); timeout; refused — соединение отвергнуто; "" — неизвестно."""
    name = type(exc).__name__
    if name in _VIDEO_ERRORS:
        return "video"
    lower = _text(exc, vid)
    if name in ("IpBlocked", "RequestBlocked") or "ipblocked" in lower or "requestblocked" in lower:
        return "blocked"
    if name in ("ProxyError", "SOCKSError", "GeneralProxyError") or "proxy" in lower or "socks" in lower:
        return "proxy"
    if "too many requests" in lower or re.search(r"\b429\b", lower):
        return "rate"
    if name == "FailedToCreateConsentCookie":
        return "consent"
    if name in _TIMEOUTS or "timed out" in lower:
        return "timeout"
    if name == "ConnectionError" and ("refused" in lower or "unreachable" in lower):
        return "refused"
    return ""


def _kind(exc, proxy, vid=""):
    """К кому претензия: к адресу (blocked/rate/address), к прокси (proxy/timeout) или к ролику ("")."""
    reason = _reason(exc, vid)
    if reason in ("blocked", "rate", "proxy"):
        return reason
    if not proxy:
        return ""
    return {"consent": "address", "timeout": "timeout", "refused": "proxy"}.get(reason, "")


def _proxy_setting():
    """Как назвать прокси в тексте: при списке MANGA_SUBS_PROXIES — без имени старой настройки."""
    return "прокси субтитров" if os.environ.get("MANGA_SUBS_PROXIES", "").strip() else "прокси MANGA_SUBS_PROXY"


def _classify(exc, proxy, vid=""):
    """Русская причина для экрана — от той же классификации, что и перебор прокси."""
    name = type(exc).__name__
    reason = _reason(exc, vid)
    if reason == "blocked":
        if proxy:
            return f"YouTube не отдаёт субтитры и через {_proxy_setting()} — проверьте или смените прокси"
        return "YouTube не отдаёт субтитры с адреса сервера — задайте MANGA_SUBS_PROXY"
    if reason == "proxy":
        name = _proxy_setting()
        return f"{name[0].upper()}{name[1:]} не отвечает — проверьте его"
    if reason == "rate":
        return "YouTube ограничил частоту запросов — повторите позже"
    if reason == "video":
        if name == "TranscriptsDisabled":
            return "У ролика отключены субтитры — вставьте текст вместо ссылки"
        if name == "NoTranscriptFound":
            return "У ролика нет субтитров — вставьте текст вместо ссылки"
        if name in ("VideoUnavailable", "VideoUnplayable", "InvalidVideoId"):
            return "Ролик недоступен — проверьте ссылку"
        if name == "AgeRestricted":
            return "Ролик с возрастным ограничением: субтитры без входа не отдаются — вставьте текст"
    if reason == "timeout":
        return "YouTube не ответил вовремя — повторите позже"
    return f"Не удалось получить субтитры ({name}): {redact(str(exc), proxy)[:200]}"


def _session(proxy):
    import requests

    class TimeoutSession(requests.Session):
        # Без таймаута зависший сокет держит шаг вечно (как было с озвучкой 17.09).
        def request(self, *args, **kwargs):
            kwargs.setdefault("timeout", TIMEOUT)
            return super().request(*args, **kwargs)

    session = TimeoutSession()
    session.headers["User-Agent"] = _UA
    session.headers["Accept-Language"] = "en-US,en;q=0.9"
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


def _pick_track(tracks):
    """Язык оригинала — у автосубтитров (YouTube делает их только на языке речи).
    На этом языке ручные субтитры лучше автоматических; других языков — чаще переводы."""
    code = lambda t: (getattr(t, "language_code", "") or "").split("-")[0].lower()
    generated = next((t for t in tracks if t.is_generated), None)
    original = code(generated) if generated else code(tracks[0])
    same = [t for t in tracks if code(t) == original]
    return next((t for t in same if not t.is_generated), None) or (same[0] if same else tracks[0])


def subtitle_lines(snippets):
    """Строки субтитров без таймкодов; подряд идущие повторы (наложение фраз) схлопнуты."""
    lines = []
    for snippet in snippets:
        text = snippet.get("text", "") if isinstance(snippet, dict) else getattr(snippet, "text", "")
        text = re.sub(r"\s+", " ", text or "").strip()
        if text and (not lines or lines[-1] != text):
            lines.append(text)
    return lines


def normalize(lines):
    """Автосубтитры — строки по ~40 символов, предложения переходят через строку.
    Склеиваем в один поток текста: так их и режет скелет (\\s+ -> пробел)."""
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def fetch_youtube(vid, proxy):
    """Субтитры и название оригинала. Возвращает dict; при отказе — SourceError без адреса прокси."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api.proxies import GenericProxyConfig
    except ImportError:
        raise SourceError("На сервере не установлен пакет youtube-transcript-api") from None
    session = _session(proxy)
    kwargs = {"http_client": session}
    if proxy:
        kwargs["proxy_config"] = GenericProxyConfig(http_url=proxy, https_url=proxy)
    try:
        tracks = list(YouTubeTranscriptApi(**kwargs).list(vid))
        if not tracks:
            raise SourceError("У ролика нет субтитров — вставьте текст вместо ссылки", video=True)
        track = _pick_track(tracks)
        fetched = track.fetch()
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError(_classify(exc, proxy, vid), _kind(exc, proxy, vid),
                          video=_reason(exc, vid) == "video") from None
    snippets = list(fetched)
    lines = subtitle_lines(snippets)
    if not lines:
        raise SourceError("Субтитры ролика пустые — вставьте текст вместо ссылки", video=True)
    last = snippets[-1]
    end = (getattr(last, "start", 0) or 0) + (getattr(last, "duration", 0) or 0) if not isinstance(last, dict) \
        else (last.get("start") or 0) + (last.get("duration") or 0)
    title = ""
    try:
        response = session.get("https://www.youtube.com/oembed", params={"format": "json", "url": canonical_url(vid)})
        if response.status_code == 200:
            title = str(response.json().get("title") or "").strip()
    except Exception:
        title = ""  # название не обязательно: без него ролик назовётся по номеру оригинала
    return {"lines": lines, "language": (track.language_code or "").split("-")[0].lower(),
            "is_generated": bool(track.is_generated), "title": title,
            "duration_seconds": round(end) if end else None}


def fetch_thumbnail(vid, proxy, dest):
    """Превью оригинала: maxresdefault, при 404 или заглушке 120×90 (~1 КБ) — hqdefault."""
    session = _session(proxy)
    last = ""
    for name in ("maxresdefault", "hqdefault"):
        try:
            response = session.get(f"https://i.ytimg.com/vi/{vid}/{name}.jpg")
        except Exception as exc:
            last = _classify(exc, proxy, vid)
            continue
        if response.status_code == 200 and len(response.content) > 5000:
            dest = Path(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp")
            tmp.write_bytes(response.content)
            tmp.replace(dest)
            return name
        last = f"превью {name}: ответ {response.status_code}, {len(response.content)} байт"
    raise SourceError(f"Превью оригинала не скачано ({last})")


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_meta(root):
    try:
        data = json.loads((Path(root) / "text" / "source_meta.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _too_long(chars, limit, what):
    return SourceError(
        f"{what} слишком длинный для скелета: {chars:,} символов при пределе {limit:,}".replace(",", " ")
        + " (MANGA_SOURCE_MAX_CHARS). Модель получает исходник целиком; сократите текст"
        " или поднимите предел, если модель текста принимает больше.")


def run_step(root, data, fetch=None, thumbnail=None, proxy=None, state=None):
    """Выполнить шаг «Исходник». Возвращает {"note": ..., "title": название оригинала или None}.

    fetch/thumbnail подменяются в тестах; сеть трогается только у ролика по ссылке без готового текста.
    proxy — один прокси вместо настроек; state — файл состояния прокси (по умолчанию в корне очереди)."""
    root = Path(root)
    text_path = root / "text" / "source.txt"
    limit = max_chars()
    url = data.get("source_url")
    if not url:
        chars = len(text_path.read_text(encoding="utf-8")) if text_path.is_file() else 0
        if not chars:
            raise SourceError("Исходный текст ролика не найден (text/source.txt)")
        if chars > limit:
            raise _too_long(chars, limit, "Текст")
        return {"note": "Текст вставлен вручную: " + f"{chars:,}".replace(",", " ") + " символов.", "title": None}
    vid = data.get("video_id") or video_id(url)
    exits = proxies() if proxy is None else ([_proxy(proxy)] if _proxy(proxy) else [])
    # Проект лежит в <корень очереди>/projects/<id>: состояние прокси общее для всех роликов.
    state = state_path(root.parent.parent) if state is None else state
    rows = list(zip(exits, proxies_status(state, exits)))
    # Для превью — первый не отдыхающий и не отмеченный «не отвечает»; иначе первый не отдыхающий, иначе первый.
    proxy = next((p for p, row in rows if not row["resting"] and row["last"] != "down"),
                 next((p for p, row in rows if not row["resting"]), exits[0] if exits else ""))
    meta = read_meta(root)
    ready = bool(meta) and text_path.is_file() and text_path.stat().st_size > 0
    if not ready:
        got, proxy = fetch_via_proxies(vid, fetch or fetch_youtube, state, exits)
        text = normalize(got["lines"])
        meta = {"url": canonical_url(vid), "video_id": vid, "title": got.get("title") or "",
                "duration_seconds": got.get("duration_seconds"), "language": got.get("language") or "",
                "is_generated": bool(got.get("is_generated")), "lines": len(got["lines"]), "chars": len(text),
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "thumbnail": None}
        if len(text) > limit:
            raise _too_long(len(text), limit, "Текст оригинала")
        # Сначала текст, потом описание: описание без текста считается «не скачано».
        text_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = text_path.with_name("source.txt.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(text_path)
        _write_json(root / "text" / "source_meta.json", meta)
    elif meta.get("chars", 0) > limit:
        raise _too_long(meta["chars"], limit, "Текст оригинала")
    thumb = root / "source" / "thumbnail.jpg"
    thumb_note = ""
    if not thumb.is_file():
        try:
            meta["thumbnail"] = (thumbnail or fetch_thumbnail)(vid, proxy, thumb)
            _write_json(root / "text" / "source_meta.json", meta)
        except SourceError as exc:
            thumb_note = f" Внимание: {exc}. Текст получен, ролик идёт дальше; превью нужно только будущему шагу «Обложка»."
    kind = "автосубтитры" if meta.get("is_generated") else "субтитры"
    num = lambda n: f"{n or 0:,}".replace(",", " ")
    note = f"{kind} {meta.get('language') or '?'}: {num(meta.get('chars'))} символов, {num(meta.get('lines'))} строк."
    return {"note": note[:1].upper() + note[1:] + thumb_note, "title": meta.get("title") or None,
            "thumbnail_missing": bool(thumb_note)}
