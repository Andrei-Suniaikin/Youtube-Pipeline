#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Клиенты соседних сервисов: рерайт текста и генератор обложек.

Оба живут на той же машине, поэтому ходим на localhost — мимо внешнего домена и Caddy.
Ключи доступа хранятся в таблице `admin_state` кабинета: там же, где переживает рестарт
пауза очереди, и не в файле с вендорскими секретами.

Разделение отказов — главное, ради чего здесь свои обёртки:

* **отказ этой строки** — битый донор, нет субтитров, отказ модерации. Строка в карантин;
* **отказ общего ресурса** — сосед не отвечает, кончились креды канала (у рерайта это 503),
  превышен лимит. Ждёт вся очередь, ни одна строка не ломается.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import threading
import time

import requests

from pub_engine import ItemFailed, ResourceDown

REWRITE_BASE = "http://127.0.0.1:8899"
COVER_BASE = "http://127.0.0.1:8900"

TIMEOUT = 30
# YouTube не отдаёт субтитры адресам облачных провайдеров, а каждый домашний адрес
# держит лишь несколько десятков скачиваний в час и потом отвечает 429. Поэтому выходов
# несколько (туннели с наших машин), и на отказ мы отвечаем сменой выхода, а не ожиданием.
SUBS_PROXIES = [p.strip() for p in os.environ.get(
    "VS_SUBS_PROXIES", "socks5h://127.0.0.1:1080").split(",") if p.strip()]
# Cookies владельца: с ними YouTube отвечает как обычному зрителю, а не как анониму.
SUBS_COOKIES = os.environ.get("VS_SUBS_COOKIES", "/root/.vs_yt_cookies.txt")
# Выход, ответивший отказом, отдыхает — но очередь идёт дальше через остальные.
SUBS_REST = float(os.environ.get("VS_SUBS_REST", "1800"))
_subs_rest_until = {}          # выход → до какого времени отдыхает
_subs_gate = threading.Lock()

YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")

# Русские названия языков — их ждут оба соседа.
LANG_TO_RU = {
    "pl": "Польский", "cs": "Чешский", "sk": "Словацкий", "ro": "Румынский",
    "hr": "Хорватский", "el": "Греческий", "hu": "Венгерский", "ru": "Русский",
    "de": "Немецкий", "fr": "Французский", "it": "Итальянский", "es": "Испанский",
    "pt": "Португальский", "po": "Португальский", "fi": "Финский", "bg": "Болгарский",
    "uk": "Украинский", "en": "Английский", "tr": "Турецкий", "nl": "Нидерландский",
}


def lang_ru(code: str) -> str:
    return LANG_TO_RU.get((code or "").lower(), "Русский")


# ─────────────────────────── ключи ───────────────────────────

def get_key(c, name: str) -> str:
    """Ключ доступа к соседу. Нет таблицы или записи — пустая строка, а не падение:
    отсутствие ключа должно читаться как «ждём настройки», а не как сбой строки."""
    try:
        row = c.execute("SELECT v FROM admin_state WHERE k=?", (f"pub_key_{name}",)).fetchone()
    except Exception:
        return ""
    return (row[0] if row else "") or ""


def set_key(c, name: str, value: str, now: float) -> None:
    c.execute("INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES(?,?,?)",
              (f"pub_key_{name}", value, now))


# ─────────────────────────── общее ───────────────────────────

def _call(method: str, url: str, key: str, service: str, **kw):
    """Запрос к соседу с переводом его ответа в наши два класса отказа."""
    headers = kw.pop("headers", {}) or {}
    headers["Authorization"] = f"Bearer {key}"
    try:
        resp = requests.request(method, url, headers=headers, timeout=TIMEOUT, **kw)
    except requests.RequestException as exc:
        raise ResourceDown(service, f"{service} не отвечает: {type(exc).__name__}")

    if resp.status_code in (401, 403):
        raise ResourceDown(service, f"{service}: ключ доступа не принят")
    if resp.status_code == 429:
        raise ResourceDown(service, f"{service}: превышен лимит запросов")
    if resp.status_code == 503:
        # У рерайта 503 означает «нет кредов канала», а не «сервис лёг»: ретраить строкой
        # бессмысленно, но и в карантин её вести нельзя — ждёт вся очередь.
        raise ResourceDown(service, f"{service}: нет доступных ключей провайдера")
    if resp.status_code >= 500:
        raise ResourceDown(service, f"{service}: внутренняя ошибка {resp.status_code}")
    if resp.status_code == 422:
        raise ItemFailed(f"{service} не принял запрос: {_detail(resp)}")
    if resp.status_code >= 400:
        raise ItemFailed(f"{service} ответил {resp.status_code}: {_detail(resp)}")
    return resp


def _detail(resp) -> str:
    try:
        body = resp.json()
    except Exception:
        return (resp.text or "")[:200]
    if isinstance(body, dict):
        for key in ("detail", "message", "error"):
            if body.get(key):
                return str(body[key])[:200]
    return json.dumps(body, ensure_ascii=False)[:200]


# ─────────────────────────── субтитры донора ───────────────────────────

def video_id(url: str) -> str:
    m = YT_ID_RE.search(url or "")
    if m:
        return m.group(1)
    tail = (url or "").rstrip("/").rsplit("/", 1)[-1]
    return tail if re.fullmatch(r"[A-Za-z0-9_-]{11}", tail) else ""



def _subs_live_exits() -> list:
    """Выходы, которые сейчас не отдыхают. Пусто — значит отдыхают все."""
    now = time.time()
    with _subs_gate:
        return [p for p in SUBS_PROXIES if _subs_rest_until.get(p, 0) <= now]


def _subs_rest(proxy: str) -> None:
    """Отправить выход отдыхать: его квота на этот час исчерпана."""
    with _subs_gate:
        _subs_rest_until[proxy] = time.time() + SUBS_REST


def _subs_wait_left() -> int:
    """Сколько минут до пробуждения ближайшего выхода — для понятного сообщения."""
    with _subs_gate:
        if not _subs_rest_until:
            return 0
        return max(0, int((min(_subs_rest_until.values()) - time.time()) / 60) + 1)


def _subs_session(proxy: str):
    """Сессия к YouTube: заданный выход наружу и cookies владельца внутри.

    Файл cookies читаем на каждый запрос — обновили его, и новые подхватятся сами,
    перезапуск сервиса для этого не нужен.
    """
    session = requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    if SUBS_COOKIES and os.path.exists(SUBS_COOKIES):
        try:
            jar = http.cookiejar.MozillaCookieJar(SUBS_COOKIES)
            jar.load(ignore_discard=True, ignore_expires=True)
            for cookie in jar:
                session.cookies.set_cookie(cookie)
        except Exception:
            pass          # битый или чужой формат — идём анонимом, это не повод падать
    return session


def _transcript_api(proxy: str):
    from youtube_transcript_api import YouTubeTranscriptApi
    return YouTubeTranscriptApi(http_client=_subs_session(proxy))


def _no_subtitles(exc) -> bool:
    """У ролика действительно нет субтитров — это приговор строке, а не нашей стороне.

    Список закрытый: только то, чем библиотека сообщает об отсутствии дорожек. Всё
    остальное (наши опечатки, обрывы, неожиданные ответы) строку убивать не вправе.
    """
    name = type(exc).__name__
    return name in ("NoTranscriptFound", "TranscriptsDisabled", "NoTranscriptAvailable",
                    "VideoUnavailable", "VideoUnplayable", "AgeRestricted")


def _is_exit_fault(exc) -> bool:
    """Виноват выход, а не ролик: исчерпанная квота либо неработающий туннель.

    Оба случая лечатся сменой выхода. Записать их в «у ролика нет субтитров» значило бы
    похоронить исправный донор из-за упавшего туннеля — этой ошибкой уже было убито
    сорок с лишним выпусков, прежде чем её нашли.
    """
    name = type(exc).__name__
    if ("TooManyRequests" in name or "IpBlocked" in name
            or "RequestBlocked" in name or "YouTubeRequestFailed" in name):
        return True
    return isinstance(exc, (requests.exceptions.ConnectionError,
                            requests.exceptions.ProxyError,
                            requests.exceptions.Timeout))


def _subs_cache_db():
    """Соединение с базой кабинета. Кэш субтитров лежит там же, где остальная работа."""
    import sqlite3
    path = os.environ.get("VS_DB", "/root/VideoStudio/server/vsgen.db")
    conn = sqlite3.connect(path, timeout=20)
    conn.execute("CREATE TABLE IF NOT EXISTS subs_cache("
                 "video_id TEXT PRIMARY KEY, text TEXT NOT NULL, created REAL NOT NULL)")
    return conn


def _subs_cache_get(vid: str) -> str:
    """Ранее скачанные субтитры этого ролика.

    Один сюжет идёт на несколько языковых каналов, и без кэша каждый канал качал бы
    один и тот же ролик заново — на нашей волне это 330 лишних скачиваний из 574,
    то есть ровно та нагрузка, из-за которой YouTube и закрывает адрес.
    """
    try:
        conn = _subs_cache_db()
        try:
            row = conn.execute("SELECT text FROM subs_cache WHERE video_id=?", (vid,)).fetchone()
        finally:
            conn.close()
        return (row[0] if row else "") or ""
    except Exception:
        return ""          # кэш — ускорение, а не условие работы


def _subs_cache_put(vid: str, text: str) -> None:
    try:
        conn = _subs_cache_db()
        try:
            conn.execute("INSERT OR REPLACE INTO subs_cache(video_id,text,created) VALUES(?,?,?)",
                         (vid, text, time.time()))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _track_code(track) -> str:
    return str(getattr(track, "language_code", "") or "")


def _pick_track(tracks: list):
    """Дорожка субтитров донора: язык оригинала, ручная версия предпочтительнее.

    Авто-дорожку YouTube делает только для языка оригинала — по ней и опознаём язык.
    Ручные дорожки чаще переводы и приходят по алфавиту, поэтому «первая» ничего не
    значит. Текст берём ручной того же языка: в нём есть пунктуация.
    """
    for track in tracks:
        if getattr(track, "is_generated", False):
            code = _track_code(track)
            for want_generated in (False, True):
                for other in tracks:
                    if (_track_code(other) == code
                            and bool(getattr(other, "is_generated", False)) == want_generated):
                        return other
            return track
    return tracks[0]


def donor_text(url: str) -> str:
    """Текст донорского ролика из субтитров YouTube.

    Пробуем выходы по очереди: отказ по адресу — это исчерпанная квота ЭТОГО выхода,
    а не беда ролика. Кончились все выходы — ждёт вся очередь, строка остаётся живой.
    """
    vid = video_id(url)
    if not vid:
        raise ItemFailed(f"не разобрал ссылку на ролик: {url[:80]}")
    cached = _subs_cache_get(vid)
    if cached:
        return cached          # этот ролик уже качали для соседнего канала

    try:
        from youtube_transcript_api import YouTubeTranscriptApi   # noqa: F401
    except ImportError:
        raise ResourceDown("субтитры", "на сервере нет youtube-transcript-api")

    exits = _subs_live_exits()
    if not exits:
        raise ResourceDown("субтитры",
                           f"все выходы исчерпали квоту, ближайший через {_subs_wait_left()} мин")

    last_block = None
    for proxy in exits:
        api = _transcript_api(proxy)
        try:
            tracks = list(api.list(vid))
            if not tracks:
                raise ItemFailed("у ролика нет субтитров")
            fetched = _pick_track(tracks).fetch()
            parts = fetched.to_raw_data() if hasattr(fetched, "to_raw_data") else list(fetched)
        except (ItemFailed, ResourceDown):
            raise
        except Exception as exc:                       # noqa: BLE001
            if _is_exit_fault(exc):
                _subs_rest(proxy)      # этот выход исчерпан — пробуем следующий
                last_block = exc
                continue
            if _no_subtitles(exc):
                raise ItemFailed(f"у ролика нет субтитров ({type(exc).__name__})")
            # Всё прочее — наша поломка, а не свойство ролика. Одна опечатка в коде
            # успела похоронить 48 исправных выпусков как «без субтитров»: строка живёт,
            # ждёт и пойдёт снова, когда поломку починят.
            raise ResourceDown("субтитры", f"сбой на нашей стороне: {type(exc).__name__}")

        text = " ".join((p.get("text") if isinstance(p, dict) else getattr(p, "text", "")) or ""
                        for p in parts).strip()
        if len(text) < 200:
            raise ItemFailed("субтитры донора слишком короткие или пустые")
        _subs_cache_put(vid, text)
        return text

    raise ResourceDown("субтитры",
                       f"все выходы исчерпали квоту, ближайший через {_subs_wait_left()} мин")


# ─────────────────────────── рерайт ───────────────────────────

class RewriteClient:
    def __init__(self, key: str, base: str = REWRITE_BASE):
        self.key = key
        self.base = base.rstrip("/")

    def _url(self, path: str) -> str:
        return f"{self.base}/api/v1{path}"

    def upload_text(self, text: str, filename: str = "donor.txt") -> str:
        """Имя файла становится именем результата и режется до 50 символов: одинаковые
        имена у двух задач затирают друг друга, поэтому имя обязано быть уникальным."""
        resp = _call("POST", self._url("/files"), self.key, "рерайт",
                     files={"file": (filename, text.encode("utf-8"), "text/plain")})
        return resp.json()["file_id"]

    def preset_language(self, preset: dict, lang_code: str) -> dict:
        """Языковой набор пресета для нашего языка: там лежат имя ведущего и стиль."""
        want = (lang_code or "").lower()
        ru = lang_ru(lang_code).lower()
        for entry in (preset or {}).get("languages") or []:
            if not isinstance(entry, dict):
                continue
            for field in ("code", "display"):
                if str(entry.get(field) or "").lower() in (want, ru):
                    return entry
        return {}

    def create_job(self, file_id: str, lang_code: str, preset: dict, channel_name: str = "") -> str:
        preset = preset or {}
        lang_entry = self.preset_language(preset, lang_code)
        body = {
            "files": [file_id],
            # В languages допустим ТОЛЬКО code: сервис запрещает лишние поля,
            # название канала передаётся отдельным полем channel_name.
            "languages": [{"code": lang_ru(lang_code)}],
            "model": preset.get("model") or "gpt-4.1-nano",
            "chunk_size": int(preset.get("chunk") or 3500),
            "preserve_paragraphs": bool(preset.get("preserve_paragraphs", True)),
            "address_style": (preset.get("address") or "")[:8],
            "channel_name": (channel_name or lang_entry.get("channel") or "")[:80],
            # Имя ведущего живёт в языковом наборе пресета; без него рерайт подставит
            # своё умолчание, и в intro/outro поедет чужое имя.
            "host_name": str(lang_entry.get("host") or "")[:80],
            "safe_text": bool(preset.get("safe_text", False)),
            "max_threads": int(preset.get("threads") or 1),
        }
        resp = _call("POST", self._url("/jobs"), self.key, "рерайт", json=body)
        return resp.json()["job_id"]

    def job(self, job_id: str) -> dict:
        return _call("GET", self._url(f"/jobs/{job_id}"), self.key, "рерайт").json()

    def result_code(self, info: dict, lang_code: str) -> str:
        """Под каким кодом сервис положил результат.

        Короткие коды у него свои и местами неожиданные (португальский — `po`,
        хорватский — `cr`), поэтому код берём из ответа задачи, а не строим сами.
        """
        files = (info or {}).get("output_files") or {}
        if not files:
            raise ItemFailed("рерайт отметил задачу готовой, но файла результата нет")
        want = (lang_code or "").lower()
        for code in files:
            if code.lower() == want:
                return code
        return list(files)[0]

    def result_text(self, job_id: str, code: str) -> str:
        resp = _call("GET", self._url(f"/jobs/{job_id}/files/{code}"), self.key, "рерайт")
        return resp.text


# ─────────────────────────── обложки ───────────────────────────

class CoverClient:
    def __init__(self, key: str, base: str = COVER_BASE):
        self.key = key
        self.base = base.rstrip("/")

    def _url(self, path: str) -> str:
        return f"{self.base}/api/v1{path}"

    def create_job(self, preset_name: str, lang_code: str, donor_url: str) -> str:
        """Обложка по донорской ссылке.

        Свой заголовок не придумываем: сервис сам скачивает превью донора, разбирает
        текст на нём и переводит на язык канала. Режим `title` потребовал бы готового
        заголовка, которого на этой стадии ещё нет.
        """
        body = {"mode": "url", "preset_name": preset_name,
                "languages": [lang_ru(lang_code)], "urls": [donor_url]}
        resp = _call("POST", self._url("/jobs"), self.key, "обложки", json=body)
        return resp.json()["job_id"]

    def job(self, job_id: str) -> dict:
        return _call("GET", self._url(f"/jobs/{job_id}"), self.key, "обложки").json()

    def download(self, job_id: str, lang_code: str) -> bytes:
        resp = _call("GET", self._url(f"/jobs/{job_id}/files/{lang_ru(lang_code)}"),
                     self.key, "обложки")
        return resp.content


def cover_degraded(job: dict) -> bool:
    """Обложка вышла бракованной: текста на ней нет вовсе либо он нарисован с ошибками.

    Сервис делает несколько попыток нарисовать текст и проверяет результат; не вышло —
    помечает файл как fallback. Смотреть только на статус задачи значит принять такой
    брак за нормальную обложку.

    Оговорка: если у пресета выключена проверка текста, брак не помечается вообще —
    тогда этот признак ничего не поймает, и качество остаётся на совести пресета.
    """
    if not isinstance(job, dict):
        return False
    if job.get("fallback_count"):
        return True
    return bool(job.get("fallback_files"))
