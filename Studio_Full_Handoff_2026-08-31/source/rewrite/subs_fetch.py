"""Субтитры YouTube → текст. Серверное ядро для вкладки «Субтитры».

Порт «Стратегии 0» из десктопной утилиты subtitles_to_txt.py: субтитры берутся
через youtube-transcript-api, без yt-dlp и без cookies. На сервере это
единственный путь — yt-dlp-фолбэки десктопа требуют cookies аккаунта и на VPS
всё равно упираются в «No video formats found».

Модуль сознательно не знает ни про FastAPI, ни про диск: он возвращает текст и
имя файла, а вызывающая сторона решает, отдать его как .txt или упаковать в zip.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional

# Кодов языков ISO 639-1/639-3 плюс региональный суффикс: ru, en, zh-Hans.
_LANG_RE = re.compile(r"^[a-zA-Z]{2,3}(?:-[A-Za-z0-9]{1,8})?$")

# Порядок важен: сначала самые частые формы ссылок. Хвостовой `(?![\w-])`
# обязателен: без него из более длинной последовательности берутся первые
# 11 символов, и разные ссылки схлопываются в один идентификатор.
_VIDEO_ID_PATTERNS = (
    re.compile(r"(?:v=)([\w-]{11})(?![\w-])"),
    re.compile(r"(?:youtu\.be/)([\w-]{11})(?![\w-])"),
    re.compile(r"(?:embed/)([\w-]{11})(?![\w-])"),
    re.compile(r"(?:shorts/)([\w-]{11})(?![\w-])"),
    re.compile(r"(?:live/)([\w-]{11})(?![\w-])"),
    re.compile(r"(?:/v/)([\w-]{11})(?![\w-])"),
)

# Голый идентификатор без ссылки — пользователи вставляют и такое.
_BARE_ID_RE = re.compile(r"^[\w-]{11}$")

# Порядок проб важен. Наивный поиск первого «"title":» в HTML ловит служебную
# строку плеера («Download unavailable») — из-за этого десктопная версия называла
# все файлы одинаково. Берём название из videoDetails, затем из мета-тегов.
_TITLE_PATTERNS = (
    re.compile(r'"videoDetails"\s*:\s*\{.*?"title"\s*:\s*"([^"]{1,200})"', re.S),
    re.compile(r'<meta\s+property="og:title"\s+content="([^"]{1,200})"'),
    re.compile(r'<meta\s+name="title"\s+content="([^"]{1,200})"'),
    re.compile(r"<title>(.{1,200}?)</title>", re.S),
)

# Символы, запрещённые в именах файлов Windows, плюс управляющие.
_UNSAFE_FILENAME_RE = re.compile(r'[\\/*?:"<>|\x00-\x1f]')

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


class SubtitlesError(Exception):
    """Ошибка, текст которой можно показать пользователю как есть."""


@dataclass
class SubtitleResult:
    """Результат по одной ссылке."""

    url: str
    video_id: str = ""
    ok: bool = False
    filename: str = ""
    text: str = ""
    language: str = ""
    is_generated: bool = False
    lines: int = 0
    error: str = ""
    blocked: bool = False
    available: List[str] = field(default_factory=list)

    def as_public_dict(self) -> dict:
        """То, что уходит в UI. Сам текст сюда не кладём — он может быть большим."""
        return {
            "url": self.url,
            "video_id": self.video_id,
            "ok": self.ok,
            "filename": self.filename,
            "language": self.language,
            "is_generated": self.is_generated,
            "lines": self.lines,
            "chars": len(self.text),
            "error": self.error,
            "available": self.available,
        }


def extract_video_id(raw: str) -> str:
    """Достаёт id видео из любой формы ссылки YouTube или из голого id."""
    value = (raw or "").strip()
    if not value:
        return ""
    if _BARE_ID_RE.match(value):
        return value
    for pattern in _VIDEO_ID_PATTERNS:
        match = pattern.search(value)
        if match:
            return match.group(1)
    return ""


def parse_urls(raw: str, limit: int) -> List[str]:
    """Разбирает многострочный ввод в список ссылок без повторов.

    Пользователь вставляет список как есть — по одной ссылке в строке, иногда с
    нумерацией или лишними пробелами. Строки, из которых не выуживается id,
    сохраняются как есть: пусть о них отчитается fetch, а не молча пропадут.
    """
    items: List[str] = []
    seen = set()
    for line in (raw or "").splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        # Срезаем ведущую нумерацию вида «1.», «12)», «- ».
        candidate = re.sub(r"^\s*(?:\d{1,3}[.)]|[-*•])\s+", "", candidate)
        if not candidate:
            continue
        key = extract_video_id(candidate) or candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(candidate)
        if len(items) >= limit:
            break
    return items


def normalize_lang(raw: str) -> str:
    """`auto`, пустая строка и мусор → `auto`; корректный код → нижний регистр."""
    value = (raw or "auto").strip().lower()
    if not value or value == "auto":
        return "auto"
    if not _LANG_RE.match(value):
        raise SubtitlesError(f"Некорректный код языка: {raw!r}")
    return value.split("-")[0]


def safe_filename(title: str, video_id: str, lang: str) -> str:
    """Имя файла как в десктопной версии: «Название_ID.язык.txt»."""
    cleaned = _UNSAFE_FILENAME_RE.sub("_", title or "").strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)[:80].strip()
    if not cleaned:
        cleaned = "subtitles"
    suffix = f".{lang}" if lang else ""
    return f"{cleaned}_{video_id}{suffix}.txt" if video_id else f"{cleaned}{suffix}.txt"


def fetch_title(video_id: str, timeout: int = 12, proxy: str = "") -> str:
    """Название видео нужно только для имени файла — ошибка здесь не фатальна.

    Спрашиваем oEmbed: ответ ~850 байт против 1,4 МБ HTML watch-страницы, и в нём
    название лежит отдельным полем, а не выуживается регуляркой.

    Ходить обязательно тем же путём, что и за субтитрами: запрос напрямую с
    заблокированного адреса возвращает не ошибку, а страницу-заглушку, из которой
    вытаскивается мусор вроде «11K» — и он уезжает в имя файла.
    """
    oembed = _fetch_title_oembed(video_id, timeout, proxy)
    if oembed:
        return oembed
    content = _http_get(
        f"https://www.youtube.com/watch?v={video_id}", timeout, proxy
    )
    if not content:
        return ""
    for pattern in _TITLE_PATTERNS:
        match = pattern.search(content)
        if not match:
            continue
        raw = match.group(1)
        try:
            title = json.loads('"' + raw + '"')
        except Exception:
            title = raw
        title = _unescape_html(title).strip()
        # У <title> в конце всегда « - YouTube» — в имя файла это не нужно.
        title = re.sub(r"\s*-\s*YouTube$", "", title).strip()
        if title:
            return title
    return ""


def _http_get(url: str, timeout: float, proxy: str = "") -> str:
    """Обычный GET тем же маршрутом, что и запросы за субтитрами."""
    import requests

    proxy = normalize_proxy(proxy)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        response = requests.get(
            url,
            headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=timeout,
            proxies=proxies,
        )
        if response.status_code != 200:
            return ""
        return response.text
    except Exception:
        return ""


def _fetch_title_oembed(video_id: str, timeout: int, proxy: str = "") -> str:
    url = (
        "https://www.youtube.com/oembed?format=json&url="
        f"https://www.youtube.com/watch%3Fv%3D{video_id}"
    )
    raw = _http_get(url, timeout, proxy)
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except Exception:
        return ""
    title = payload.get("title") if isinstance(payload, dict) else ""
    return _unescape_html(str(title or "")).strip()


def _unescape_html(text: str) -> str:
    import html

    return html.unescape(text)


def _track_code(track) -> str:
    return (getattr(track, "language_code", "") or "").split("-")[0].lower()


def _pick_track(tracks: list, lang: str):
    """Выбор дорожки. Внутри одного языка ручные субтитры всегда приоритетнее авто.

    В режиме `auto` язык оригинала определяем по авто-дорожке: YouTube генерирует
    её только для языка оригинала, тогда как ручные дорожки — это чаще всего
    переводы, и приходят они в алфавитном порядке (у английской лекции первым
    идёт арабский, так что «язык первой дорожки» — неверный признак).
    Сам текст при этом берём ручной, если он есть на том же языке: в нём
    расставлена пунктуация, которой у авто-субтитров нет.
    """
    if lang != "auto":
        return _best_of_language(tracks, lang)

    for track in tracks:
        if track.is_generated:
            return _best_of_language(tracks, _track_code(track)) or track
    return tracks[0] if tracks else None


def _best_of_language(tracks: list, lang: str):
    """Лучшая дорожка нужного языка: сначала ручная, потом авто."""
    for want_generated in (False, True):
        for track in tracks:
            if _track_code(track) == lang and bool(track.is_generated) == want_generated:
                return track
    return None


def _snippets_to_lines(data) -> List[str]:
    """Фразы → строки без таймкодов.

    Схлопываем только подряд идущие одинаковые строки: так лечится наложение
    фраз, но сохраняются повторы по тексту. Десктопная версия выбрасывала все
    повторы глобально и на живых роликах теряла от 0,5% (лекция) до 19% (песня)
    строк — припев и повторяющиеся реплики просто исчезали.
    """
    lines: List[str] = []
    for snippet in data:
        text = (
            snippet.get("text", "")
            if isinstance(snippet, dict)
            else (getattr(snippet, "text", "") or "")
        )
        text = re.sub(r"\s+", " ", text).strip()
        if text and (not lines or lines[-1] != text):
            lines.append(text)
    return lines


def build_http_client(timeout: float):
    """`requests.Session` с жёстким таймаутом на каждый запрос.

    Без него зависший сокет держит тред вечно: у самой библиотеки таймаута нет,
    а обернуть вызов снаружи нечем — `ThreadPoolExecutor` на выходе всё равно
    ждёт застрявшую задачу.
    """
    import requests

    class _TimeoutSession(requests.Session):
        def request(self, *args, **kwargs):
            kwargs.setdefault("timeout", timeout)
            return super().request(*args, **kwargs)

    return _TimeoutSession()


def build_api(timeout: float, proxy: str = ""):
    """Клиент youtube-transcript-api, при необходимости через прокси.

    YouTube блокирует запросы с адресов облачных провайдеров, поэтому на VPS
    прокси — не опция, а условие работы. Локально (домашний адрес) он не нужен.
    """
    from youtube_transcript_api import YouTubeTranscriptApi

    kwargs = {"http_client": build_http_client(timeout)}
    proxy = normalize_proxy(proxy)
    if proxy:
        from youtube_transcript_api.proxies import GenericProxyConfig

        kwargs["proxy_config"] = GenericProxyConfig(
            http_url=proxy, https_url=proxy
        )
    return YouTubeTranscriptApi(**kwargs)


def normalize_proxy(proxy: str) -> str:
    """`socks5://` → `socks5h://`: имена должен разрешать сам прокси.

    С обычной схемой requests резолвит хост локально и падает с невнятным
    ConnectionError — при этом сам прокси полностью исправен.
    """
    value = (proxy or "").strip()
    if value.startswith("socks5://"):
        return "socks5h://" + value[len("socks5://"):]
    if value.startswith("socks4://"):
        return "socks4a://" + value[len("socks4://"):]
    return value


def fetch_one(
    raw_url: str,
    lang: str = "auto",
    with_title: bool = True,
    timeout: float = 20.0,
    proxy: str = "",
) -> SubtitleResult:
    """Скачивает субтитры одного видео. Исключения наружу не выпускает."""
    result = SubtitleResult(url=raw_url)
    video_id = extract_video_id(raw_url)
    if not video_id:
        result.error = "Не похоже на ссылку YouTube"
        return result
    result.video_id = video_id

    try:
        import youtube_transcript_api  # noqa: F401
    except ImportError:
        raise SubtitlesError(
            "На сервере не установлен пакет youtube-transcript-api"
        )

    try:
        api = build_api(timeout, proxy)
        tracks = list(api.list(video_id))
    except Exception as exc:
        result.error, result.blocked = _classify_error(exc)
        return result

    if not tracks:
        result.error = "У видео нет субтитров"
        return result

    result.available = sorted({_track_code(t) for t in tracks if _track_code(t)})

    track = _pick_track(tracks, lang)
    if track is None:
        result.error = f"Языка «{lang}» у этого видео нет"
        return result

    try:
        data = track.fetch()
    except Exception as exc:
        result.error, result.blocked = _classify_error(exc)
        return result

    lines = _snippets_to_lines(data)
    if not lines:
        result.error = "Субтитры пустые"
        return result

    lang_code = _track_code(track) or "xx"
    title = fetch_title(video_id, timeout=timeout, proxy=proxy) if with_title else ""
    result.ok = True
    result.text = "\n".join(lines)
    result.lines = len(lines)
    result.language = lang_code
    result.is_generated = bool(track.is_generated)
    result.filename = safe_filename(title, video_id, lang_code)
    return result


def fetch_many(
    urls: List[str],
    lang: str = "auto",
    timeout: float = 20.0,
    parallel: int = 3,
    with_title: bool = True,
    proxy: str = "",
) -> List[SubtitleResult]:
    """Скачивает пачку. Порядок результатов совпадает с порядком ссылок.

    Идём волнами по `parallel` штук с паузой между волнами: чистый IP сервера —
    единственное, на чём держится вся схема без cookies и прокси, и терять его
    ради пары сэкономленных секунд нельзя. Если YouTube ответил блокировкой,
    остаток пачки не запрашиваем вовсе — помечаем как неотработанный.
    """
    import random
    from concurrent.futures import ThreadPoolExecutor

    results: List[SubtitleResult] = []
    wave = max(1, int(parallel))
    blocked = False

    for start in range(0, len(urls), wave):
        chunk = urls[start:start + wave]
        if blocked:
            for url in chunk:
                skipped = SubtitleResult(url=url, video_id=extract_video_id(url))
                skipped.error = "Пропущено: YouTube ограничил запросы с сервера"
                results.append(skipped)
            continue

        if start:
            time.sleep(0.2 + random.random() * 0.2)

        if wave == 1:
            chunk_results = [fetch_one(chunk[0], lang, with_title, timeout, proxy)]
        else:
            with ThreadPoolExecutor(max_workers=wave) as pool:
                chunk_results = list(
                    pool.map(
                        lambda url: fetch_one(url, lang, with_title, timeout, proxy),
                        chunk,
                    )
                )

        results.extend(chunk_results)
        blocked = any(r.blocked for r in chunk_results)

    return results


def _classify_error(exc: Exception) -> tuple:
    """Исключение пакета → (русская причина, признак «ограничили нас, а не видео»).

    Второй флаг важнее первого: при блокировке IP дальше по списку долбить
    YouTube бессмысленно и вредно — вызывающая сторона обрывает всю пачку.
    """
    name = type(exc).__name__
    lowered = str(exc).lower()
    if name in ("IpBlocked", "RequestBlocked") or "ipblocked" in lowered or "requestblocked" in lowered:
        # Не «попробуйте позже»: YouTube блокирует адреса облачных провайдеров
        # постоянно, и без прокси повторная попытка ничего не изменит.
        return "YouTube не отдаёт субтитры с адреса сервера — нужен прокси", True
    if "too many requests" in lowered or "429" in lowered:
        return "YouTube ограничил частоту запросов — попробуйте позже", True
    if name == "PoTokenRequired" or "po_token" in lowered or "potoken" in lowered:
        return "YouTube требует подтверждения — попробуйте позже", True
    if name == "TranscriptsDisabled" or "disabled" in lowered:
        return "У видео отключены субтитры", False
    if name in ("NoTranscriptFound", "NoTranscriptAvailable"):
        return "Субтитров на этом языке нет", False
    if name in ("VideoUnavailable", "VideoUnplayable"):
        return "Видео недоступно", False
    if name == "InvalidVideoId":
        return "Неверный идентификатор видео", False
    if name == "AgeRestricted" or ("age" in lowered and "restrict" in lowered):
        return "Видео с возрастным ограничением", False
    if name in ("TimeoutError", "ReadTimeout", "ConnectTimeout") or "timed out" in lowered:
        return "Таймаут запроса к YouTube", False
    return f"Не удалось получить субтитры ({name})", False
