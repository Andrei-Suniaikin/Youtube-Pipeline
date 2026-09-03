"""Wikimedia Commons: bitmap/video search with metadata and licence. No key."""
import re

from .base import BaseConnector, Candidate, SAFE_LICENSES, norm_license

API = "https://commons.wikimedia.org/w/api.php"
THUMB_W = 640


class CommonsConnector(BaseConnector):
    name = "commons"
    supported_media_types = ("image", "video")

    def search(self, query: str, limit: int, media_type: str = "image") -> list:
        if media_type not in self.supported_media_types or limit <= 0:
            return []
        file_filter = "filetype:video" if media_type == "video" else "filetype:bitmap"
        params = {
            "action": "query", "format": "json", "generator": "search",
            "gsrsearch": f"{file_filter} {query}", "gsrnamespace": 6,
            "gsrlimit": min(limit * 3, 50),
            "prop": "imageinfo", "iiprop": "url|extmetadata|mime|size|mediatype",
            "iiurlwidth": THUMB_W,
        }
        data = self._get_json(API, params=params)
        out = []
        for page in (data.get("query", {}).get("pages", {}) or {}).values():
            infos = page.get("imageinfo")
            if not infos:
                continue
            ii = infos[0]
            mime = str(ii.get("mime") or "").lower()
            if media_type == "image" and mime not in ("image/jpeg", "image/png"):
                continue
            if media_type == "video" and not mime.startswith("video/"):
                continue
            if (ii.get("width") or 0) < 400:  # совсем мелочь не берём
                continue
            meta = ii.get("extmetadata", {}) or {}
            get = lambda k: _strip_html((meta.get(k, {}) or {}).get("value", ""))
            lic_raw = get("LicenseShortName")
            license_key = norm_license(lic_raw)
            if media_type == "video" and license_key not in SAFE_LICENSES:
                continue
            thumb = ii.get("thumburl") or (ii.get("url", "") if media_type == "image" else "")
            if not thumb or not ii.get("url"):
                continue
            out.append(Candidate(
                source=self.name,
                page_url=ii.get("descriptionurl", ""),
                thumb_url=thumb,
                full_url=ii.get("url", ""),
                title=(page.get("title", "") or "").removeprefix("File:"),
                license_raw=lic_raw, license=license_key,
                author=get("Artist")[:150], date_text=get("DateTimeOriginal")[:80],
                query=query,
                media_type=media_type, mime_type=mime,
                width=_as_int(ii.get("width")), height=_as_int(ii.get("height")),
                duration=_as_float(ii.get("duration")), file_size=_as_int(ii.get("size")),
            ))
            if len(out) >= limit:
                break
        return out


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
