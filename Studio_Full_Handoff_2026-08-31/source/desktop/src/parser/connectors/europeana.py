"""Europeana Search API. ``reusability=open`` is mandatory for every mode."""
import re
from urllib.parse import urlparse

from .base import BaseConnector, Candidate, SAFE_LICENSES, norm_license

API = "https://api.europeana.eu/record/v2/search.json"


class EuropeanaConnector(BaseConnector):
    name = "europeana"
    supported_media_types = ("image", "video")

    def search(self, query: str, limit: int, media_type: str = "image") -> list:
        if media_type not in self.supported_media_types or limit <= 0:
            return []
        key = self.keys.get("europeana")
        if not key:
            return []
        params = {
            "wskey": key, "query": query, "media": "true",
            "reusability": "open", "qf": f"TYPE:{media_type.upper()}",
            "rows": min(limit, 24), "profile": "rich",
        }
        try:
            data = self._get_json(API, params=params)
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            # requests' default HTTPError contains the full URL, including wskey.
            raise RuntimeError(f"Europeana API HTTP {status or 'error'}") from None
        out = []
        for it in data.get("items", []):
            thumb = _first(it.get("edmPreview"))
            if not thumb:
                continue
            full = (_pick_video_url(it) if media_type == "video"
                    else (_first(it.get("edmIsShownBy")) or thumb))
            if not full:
                continue
            rights = _first(it.get("rights"))
            license_key = _map_rights(rights)
            # Open reusability is a search hint, not a substitute for item rights.
            if media_type == "video" and license_key not in SAFE_LICENSES:
                continue
            title = _first(it.get("title"))
            provider = _first(it.get("dataProvider"))
            mime = _mime_from_url(full) if media_type == "video" else ""
            out.append(Candidate(
                source=self.name,
                page_url=(it.get("guid") or it.get("link") or ""),
                thumb_url=thumb, full_url=full, title=title[:150],
                license_raw=rights, license=license_key,
                author=provider[:150],
                date_text=_first(it.get("year"))[:80],
                query=query,
                media_type=media_type, mime_type=mime,
                width=_as_int(_first(it.get("videoWidth"))),
                height=_as_int(_first(it.get("videoHeight"))),
                duration=_as_float(_first(it.get("videoDuration"))),
                file_size=_as_int(_first(it.get("videoSize"))),
                extra={"europeana_id": str(it.get("id") or "")},
            ))
            if len(out) >= limit:
                break
        return out


def _map_rights(rights_url: str) -> str:
    low = (rights_url or "").lower()
    if "publicdomain/mark" in low or "publicdomain/zero" in low:
        return "pd" if "mark" in low else "cc0"
    if "by-nc" in low or "/inc" in low or "noc-nc" in low:
        return "nc"
    if "licenses/by-sa" in low:
        return "cc-by-sa"
    if "licenses/by/" in low:
        return "cc-by"
    return norm_license(rights_url)


_VIDEO_RE = re.compile(r"\.(?:mp4|m4v|mov|webm|ogv|ogg)(?:$|[?#])", re.I)


def _values(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if isinstance(v, (str, int, float)) and str(v)]
    return [str(value)] if isinstance(value, (str, int, float)) and str(value) else []


def _first(value) -> str:
    vals = _values(value)
    return vals[0] if vals else ""


def _pick_video_url(item: dict) -> str:
    # edmIsShownAt/guid are landing pages. Only a direct downloadable file is safe
    # for an automated renderer; ambiguous players/HLS are deliberately skipped.
    urls = _values(item.get("edmIsShownBy")) + _values(item.get("edmHasView"))
    return next((u for u in urls if u.startswith(("http://", "https://"))
                 and _VIDEO_RE.search(u)), "")


def _mime_from_url(url: str) -> str:
    ext = urlparse(url).path.lower().rsplit(".", 1)[-1]
    return {"mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime",
            "webm": "video/webm", "ogv": "video/ogg", "ogg": "video/ogg"}.get(ext, "")


def _as_int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _as_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
