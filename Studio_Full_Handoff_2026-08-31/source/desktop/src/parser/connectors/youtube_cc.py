"""YouTube CC-BY через yt-dlp: майнинг видео-пула ниши (решение юзера 17.07, C3b).

Поиск ytsearchN:<query> с пост-фильтром license из ПОЛНЫХ метаданных yt-dlp
(flat-выдача license не несёт): наружу выходит ТОЛЬКО
"Creative Commons Attribution license (reuse allowed)", длительность <= 30 мин,
кап размера как у movies. Скачивание — тоже yt-dlp (прямых файлов у YouTube нет).

⚠ CC-флаг НЕ гарантия прав: загрузивший мог не владеть материалом. Кандидат
помечается license=youtube-cc-by и extra.risk=contentid_possible — юзер осознанно
принимает риск ContentID-клеймов (его политика: апелляции отбивает сам).
Анти-мусор: (1) правило вотермарок в суде клипов (judge PROMPT v13);
(2) youtube_cc.trusted_channels в yaml опционально сужает до архивов/музеев.
"""
import json
import os
import shutil
import subprocess
import threading
from urllib.parse import urlparse

from .base import BaseConnector, Candidate

# лицензионная строка yt-dlp, единственная допустимая (fail-closed)
CC_LICENSE = "Creative Commons Attribution license (reuse allowed)"
LICENSE_KEY = "youtube-cc-by"       # свой ключ: юзер видит происхождение в отчётах
MAX_DURATION_SEC = 30 * 60          # длиннее 30 мин — не сырьё для нарезки
MAX_YT_BYTES = 500 * 1024 * 1024    # кап размера как у movies


class YoutubeCCConnector(BaseConnector):
    name = "youtube_cc"
    supported_media_types = ("video",)

    def __init__(self, keys: dict, throttle: float = 1.0, dl_throttle: float = 0.15):
        # YouTube не любит бодрых ботов так же, как archive.org — не чаще 1 rps
        super().__init__(keys, throttle=max(float(throttle), 1.0),
                         dl_throttle=dl_throttle)
        # задаёт вызывающий из yaml youtube_cc.trusted_channels (пусто = любые)
        self.trusted_channels = []

    @staticmethod
    def _ytdlp_bin() -> str:
        path = shutil.which("yt-dlp")
        if not path:
            raise RuntimeError("yt-dlp не найден (pip3 install yt-dlp)")
        return path

    def _search_json(self, query: str, n: int) -> list:
        """ytsearchN -> полные метаданные (по JSON-строке на видео)."""
        self._wait()
        proc = subprocess.run(
            [self._ytdlp_bin(), "-j", "--no-download", "--ignore-errors",
             "--no-warnings", f"ytsearch{n}:{query}"],
            capture_output=True, text=True, timeout=300)
        out = []
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def _channel_trusted(self, info: dict) -> bool:
        if not self.trusted_channels:
            return True
        allowed = {str(t).casefold() for t in self.trusted_channels}
        mine = {str(info.get(k) or "").casefold()
                for k in ("channel", "uploader", "channel_id", "uploader_id")}
        return bool(allowed & mine)

    def search(self, query: str, limit: int, media_type: str = "image") -> list:
        if media_type != "video" or limit <= 0:
            return []
        infos = self._search_json(query, min(max(limit * 4, limit), 20))
        out = []
        for info in infos:
            if not isinstance(info, dict):
                continue
            lic = str(info.get("license") or "")
            if lic != CC_LICENSE:
                continue  # fail-closed: только явный CC-BY (reuse allowed)
            duration = float(info.get("duration") or 0)
            if not duration or duration > MAX_DURATION_SEC:
                continue
            if not self._channel_trusted(info):
                continue
            size = int(info.get("filesize_approx") or info.get("filesize") or 0)
            if size and size > MAX_YT_BYTES:
                continue
            page = str(info.get("webpage_url") or "")
            if not page:
                continue
            channel = str(info.get("channel") or info.get("uploader") or "")
            out.append(Candidate(
                source=self.name,
                page_url=page,
                thumb_url=str(info.get("thumbnail") or ""),
                full_url=page,  # скачивание идёт yt-dlp по странице, не прямым URL
                title=str(info.get("title") or "")[:150],
                license_raw=lic, license=LICENSE_KEY,
                author=channel[:150],
                date_text=str(info.get("upload_date") or "")[:80],
                query=query, media_type="video", mime_type="video/mp4",
                duration=duration, file_size=size,
                extra={"youtube_id": str(info.get("id") or ""),
                       "channel": channel,
                       "channel_url": str(info.get("channel_url")
                                          or info.get("uploader_url") or ""),
                       "license_at_fetch": lic,       # лицензия на момент скачивания
                       "risk": "contentid_possible"},  # CC-флаг не гарантия прав
            ))
            if len(out) >= limit:
                break
        return out

    def download(self, url: str, path: str, *, media_type: str = None,
                 max_bytes: int = None, max_seconds: float = None):
        """YouTube-страницы качаются yt-dlp; остальное (тумбы) — базовым стримом."""
        host = (urlparse(url or "").hostname or "").lower()
        if not (host.endswith("youtube.com") or host.endswith("youtu.be")):
            return super().download(url, path, media_type=media_type,
                                    max_bytes=max_bytes, max_seconds=max_seconds)
        cap = int(max_bytes or MAX_YT_BYTES)
        self._wait_dl()
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        proc = subprocess.run(
            [self._ytdlp_bin(), "-f", "b[ext=mp4]/bv*[ext=mp4]+ba[ext=m4a]/b",
             "--max-filesize", str(cap), "--no-playlist", "--no-progress",
             "--no-warnings", "-o", tmp, url],
            capture_output=True, text=True, timeout=float(max_seconds or 900))
        # yt-dlp может дописать контейнерное расширение к шаблону
        got = next((p for p in (tmp, f"{tmp}.mp4", f"{tmp}.mkv", f"{tmp}.webm")
                    if os.path.exists(p)), None)
        if got is None:
            err = (proc.stderr or proc.stdout or "yt-dlp: файл не скачался")[-300:]
            raise IOError(f"yt-dlp не скачал {url}: {err}")
        os.replace(got, path)
