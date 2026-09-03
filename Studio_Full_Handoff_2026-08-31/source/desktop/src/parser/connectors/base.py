"""Базовый контракт коннектора источника."""
import hashlib
import ipaddress
import os
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

from ..config import UA

# нормализация лицензий -> ключ из licenses.allow профиля
_LICENSE_MAP = [
    (r"public domain|^pd\b|pdm|no known copyright|no known restrictions", "pd"),
    (r"cc0", "cc0"),
    (r"cc[- ]by[- ]nc|\bnc\b|noncommercial|non-commercial", "nc"),
    (r"cc[- ]by[- ]nd|\bnd\b|no derivatives|no-derivatives", "nd"),
    (r"cc[- ]by[- ]sa", "cc-by-sa"),
    (r"cc[- ]by(?![- ](?:sa|nc|nd))", "cc-by"),
    (r"pexels", "pexels"),
    (r"pixabay", "pixabay"),
]

# Только лицензии, при которых материал можно автоматически пускать в
# коммерческий ролик. Для видео коннекторы работают fail-closed: unknown/NC
# наружу не выходят.
SAFE_LICENSES = frozenset({"pd", "cc0", "cc-by", "cc-by-sa", "pexels", "pixabay"})

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".tif", ".tiff", ".bmp"}
_VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".webm", ".ogv", ".ogg", ".avi", ".mpeg", ".mpg"}
_MAX_IMAGE_BYTES = 64 * 1024 * 1024
_MAX_VIDEO_BYTES = 512 * 1024 * 1024
_STREAM_CHUNK = 1024 * 1024


# глобальный (на процесс) лимитер полноразмерных скачиваний — общий для всех коннекторов
_BIG_DL_LOCK = threading.Lock()
_BIG_DL_LAST = 0.0
# C4: не больше 2 параллельных полноразмеров — Wikimedia отдаёт 429 при parallel>3,
# и финал молча деградирует до тумбы (низкое качество в рендере)
_BIG_DL_SEMAPHORE = threading.BoundedSemaphore(2)


def _retry_after_seconds(value):
    """Заголовок Retry-After: секунды или HTTP-дата -> float секунд | None."""
    if not value:
        return None
    try:
        return max(0.0, float(str(value).strip()))
    except (TypeError, ValueError):
        pass
    try:
        import datetime
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(str(value))
        return max(0.0, (dt - datetime.datetime.now(datetime.timezone.utc))
                   .total_seconds())
    except Exception:  # noqa: BLE001 — нечитаемый заголовок = заголовка нет
        return None


def norm_license(raw: str) -> str:
    low = (raw or "").lower()
    for pat, key in _LICENSE_MAP:
        if re.search(pat, low):
            return key
    return "unknown"


@dataclass
class Candidate:
    source: str
    page_url: str
    thumb_url: str
    full_url: str
    title: str = ""
    license_raw: str = ""
    license: str = "unknown"
    author: str = ""
    date_text: str = ""
    query: str = ""
    clip_score: float = 0.0
    extra: dict = field(default_factory=dict)
    media_type: str = "image"       # image | video
    mime_type: str = ""
    width: int = 0
    height: int = 0
    duration: float = 0.0            # seconds, 0 = API did not provide it
    file_size: int = 0               # bytes, 0 = unknown
    fps: float = 0.0

    @property
    def uid(self) -> str:
        # Не меняем uid старых image-кандидатов: на нём сидят кэши/отчёты.
        suffix = "" if self.media_type == "image" else f"|{self.media_type}|{self.full_url}"
        return hashlib.sha1((self.source + self.page_url + self.thumb_url + suffix)
                            .encode()).hexdigest()[:16]

    def attribution(self) -> str:
        parts = [self.title or self.page_url]
        if self.author:
            parts.append(self.author)
        parts.append(self.license_raw or self.license)
        parts.append(self.page_url)
        return " · ".join(p for p in parts if p)


class BaseConnector:
    name = "base"
    supported_media_types = ("image",)

    def __init__(self, keys: dict, throttle: float = 0.7, dl_throttle: float = 0.15):
        self.keys = keys
        self.throttle = throttle        # поисковые API-запросы (вежливость к api.php)
        self.dl_throttle = dl_throttle  # скачивание тумбов: их отдаёт CDN, пауза мини
        self._lock = threading.Lock()
        self._last = 0.0
        self._dl_lock = threading.Lock()
        self._dl_last = 0.0
        self.http = requests.Session()
        self.http.headers["User-Agent"] = UA

    def _wait(self):
        with self._lock:
            delta = self.throttle - (time.time() - self._last)
            if delta > 0:
                time.sleep(delta)
            self._last = time.time()

    def _wait_dl(self):
        with self._dl_lock:
            delta = self.dl_throttle - (time.time() - self._dl_last)
            if delta > 0:
                time.sleep(delta)
            self._dl_last = time.time()

    def fetch_bytes(self, url: str, retries: int = 1) -> bytes:
        """Полноразмеры: глобальный семафор (max 2 одновременно, C4) + троттлинг
        ~1 req/s (upload.wikimedia.org банит бодрых ботов — проверено 15.07).
        На 429 читаем Retry-After и повторяем ОДИН раз после паузы; без
        Retry-After (или повторный 429) — сразу IOError, вызывающий честно
        откатывается на тумбу вместо слепых длинных ретраев."""
        for attempt in range(retries + 1):
            with _BIG_DL_SEMAPHORE:
                with _BIG_DL_LOCK:
                    global _BIG_DL_LAST
                    delta = 1.0 - (time.time() - _BIG_DL_LAST)
                    if delta > 0:
                        time.sleep(delta)
                    _BIG_DL_LAST = time.time()
                r = self.http.get(url, timeout=120)
            if r.status_code == 429:
                wait = _retry_after_seconds(r.headers.get("Retry-After"))
                if wait is not None and attempt < retries:
                    time.sleep(min(wait, 120.0))
                    continue
                raise IOError(f"429 (Retry-After={r.headers.get('Retry-After')!r}): {url}")
            r.raise_for_status()
            return r.content
        raise IOError(f"429 после {retries + 1} попыток: {url}")

    def _get_json(self, url: str, params: dict = None, headers: dict = None) -> dict:
        self._wait()
        r = self.http.get(url, params=params, headers=headers, timeout=30)
        r.raise_for_status()
        return r.json()

    def search(self, query: str, limit: int, media_type: str = "image") -> list:
        """Search source. Existing two-argument calls remain image-only."""
        raise NotImplementedError

    def search_videos(self, query: str, limit: int) -> list:
        if "video" not in self.supported_media_types:
            return []
        return self.search(query, limit, media_type="video")

    def download(self, url: str, path: str, *, media_type: str = None,
                 max_bytes: int = None, max_seconds: float = None):
        """Stream one media file to an atomic temp path.

        The old ``download(url, path)`` call remains valid. Type is inferred from the
        destination extension, while final video callers may pass ``media_type="video"``.
        HTML/JSON error pages, private/literal-local URLs, oversized and truncated files
        are rejected before they can replace a valid cache entry. ``max_seconds`` is a
        wall-clock budget: read-stall timeout не ловит живой, но медленный поток.
        """
        _validate_remote_url(url)
        expected = media_type or _infer_media_type(path) or _infer_media_type(url)
        if expected not in ("", "image", "video"):
            raise ValueError(f"неизвестный media_type: {expected}")
        cap = int(max_bytes or (_MAX_VIDEO_BYTES if expected == "video" else _MAX_IMAGE_BYTES))
        if cap <= 0:
            raise ValueError("max_bytes должен быть > 0")

        self._wait_dl()
        started = time.monotonic()
        r = self.http.get(url, timeout=(15, 180), stream=True, allow_redirects=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            r.raise_for_status()
            _validate_remote_url(getattr(r, "url", url) or url)
            content_type = (r.headers.get("Content-Type", "").split(";", 1)[0]
                            .strip().lower())
            raw_len = r.headers.get("Content-Length")
            content_len = int(raw_len) if raw_len and str(raw_len).isdigit() else 0
            if content_len > cap:
                raise IOError(f"файл больше лимита {cap} байт: {url}")

            total = 0
            head = bytearray()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=_STREAM_CHUNK):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > cap:
                        raise IOError(f"файл больше лимита {cap} байт: {url}")
                    if max_seconds and time.monotonic() - started > max_seconds:
                        raise IOError(
                            f"скачивание дольше {max_seconds:.0f}с ({total} байт): {url}")
                    if len(head) < 64:
                        head.extend(chunk[:64 - len(head)])
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
            if total == 0:
                raise IOError(f"пустой ответ: {url}")
            if (content_len and not r.headers.get("Content-Encoding")
                    and total != content_len):
                raise IOError(f"неполный файл: {total}/{content_len} байт: {url}")
            _validate_media_bytes(bytes(head), content_type, expected)
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        finally:
            close = getattr(r, "close", None)
            if close:
                close()

    def download_candidate(self, candidate: Candidate, path: str, *, thumbnail=False,
                           max_bytes: int = None):
        """Download a Candidate without accidentally treating a thumbnail as video."""
        url = candidate.thumb_url if thumbnail else candidate.full_url
        kind = "image" if thumbnail else candidate.media_type
        return self.download(url, path, media_type=kind, max_bytes=max_bytes)


def _infer_media_type(value: str) -> str:
    ext = os.path.splitext(urlparse(value or "").path.lower())[1]
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    return ""


def _validate_remote_url(url: str):
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
        raise ValueError(f"небезопасный URL: {url!r}")
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError(f"локальный URL запрещён: {url!r}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if not ip.is_global:
        raise ValueError(f"непубличный IP запрещён: {url!r}")


def _sniff_media(head: bytes) -> str:
    if head.startswith(b"\xff\xd8\xff") or head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image"
    if head.startswith((b"GIF87a", b"GIF89a", b"II*\x00", b"MM\x00*", b"BM")):
        return "image"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:16].lower()
        return "image" if any(x in brand for x in (b"avif", b"avis", b"heic", b"heif")) else "video"
    if head.startswith(b"\x1aE\xdf\xa3") or head.startswith(b"OggS"):
        return "video"
    if head.startswith(b"RIFF") and head[8:12] == b"AVI ":
        return "video"
    if head.startswith((b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3")):
        return "video"
    return ""


def _validate_media_bytes(head: bytes, content_type: str, expected: str):
    if (content_type.startswith("text/") or content_type in {
            "application/json", "application/xml", "text/xml",
            "application/javascript", "application/x-javascript"}):
        raise IOError(f"вместо медиа получен {content_type}")
    detected = _sniff_media(head)
    if not detected:
        raise IOError("формат файла не распознан")
    if expected and detected != expected:
        raise IOError(f"ожидался {expected}, получен {detected}")
    if content_type.startswith("image/") and detected != "image":
        raise IOError("заголовок image/* не совпадает с файлом")
    if content_type.startswith("video/") and detected != "video":
        raise IOError("заголовок video/* не совпадает с файлом")
