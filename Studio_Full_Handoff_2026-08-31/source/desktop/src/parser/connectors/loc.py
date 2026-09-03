"""Library of Congress JSON API: photographs and direct downloadable videos."""
import re
from urllib.parse import urlparse

from .base import BaseConnector, Candidate, SAFE_LICENSES, norm_license

API_PHOTOS = "https://www.loc.gov/photos/"
API_VIDEOS = "https://www.loc.gov/film-and-videos/"
API = API_PHOTOS  # backward-compatible import


class LocConnector(BaseConnector):
    name = "loc"
    supported_media_types = ("image", "video")

    def search(self, query: str, limit: int, media_type: str = "image") -> list:
        if media_type not in self.supported_media_types or limit <= 0:
            return []
        params = {"q": query, "fo": "json", "c": min(limit, 20),
                  "fa": ("digitized:true|access-restricted:false" if media_type == "video"
                         else "access-restricted:false")}
        try:
            data = self._get_json(API_VIDEOS if media_type == "video" else API_PHOTOS,
                                  params=params)
        except Exception:  # noqa: BLE001 — LoC бывает 5xx, источник не критичный
            return []
        out = []
        for it in data.get("results", []):
            if media_type == "video":
                cand = _video_candidate(it, query, self.name)
                # Search results often contain only a resource summary. The official
                # item endpoint is where LoC exposes resources[].files/download URLs.
                if not cand:
                    detail_url = _item_api_url(it)
                    if detail_url:
                        try:
                            detail = self._get_json(
                                detail_url, params={"fo": "json", "at": "item,resources"})
                            cand = _video_candidate(_merge_detail(it, detail), query, self.name)
                        except Exception:  # noqa: BLE001 -- one opaque item is not fatal
                            cand = None
                if cand:
                    out.append(cand)
                if len(out) >= limit:
                    break
                continue
            imgs = it.get("image_url") or []
            if not imgs:
                continue
            thumb = imgs[1] if len(imgs) > 1 else imgs[0]  # [0] бывает 150px — мал для CLIP
            full = imgs[-1]
            # LoC photos в основном PD ("no known restrictions"); точный статус — на странице
            rights = "restricted" if it.get("access_restricted") else "no known restrictions"
            out.append(Candidate(
                source=self.name,
                page_url=it.get("url", "") if str(it.get("url", "")).startswith("http")
                else "https://www.loc.gov" + str(it.get("url", "")),
                thumb_url=thumb if thumb.startswith("http") else "https:" + thumb,
                full_url=full if full.startswith("http") else "https:" + full,
                title=str(it.get("title", ""))[:150],
                license_raw=rights, license="pd" if "no known" in rights else "unknown",
                author=str((it.get("contributor") or [""])[0])[:150],
                date_text=str(it.get("date", ""))[:80],
                query=query,
                media_type="image", mime_type="image/jpeg",
            ))
            if len(out) >= limit:
                break
        return out


_VIDEO_RE = re.compile(r"\.(?:mp4|m4v|mov|webm|ogv|ogg|avi|mpeg|mpg)(?:$|[?#])", re.I)


def _video_candidate(item: dict, query: str, source: str):
    if _is_restricted(item.get("access_restricted")):
        return None
    rights = _rights_text(item)
    license_key = norm_license(rights)
    if license_key not in SAFE_LICENSES:
        return None

    resources = list(_walk_resources(item.get("resources") or []))
    options = []
    for res in resources:
        if _is_restricted(res.get("download_restricted")):
            continue
        mime = _first_text(res.get("mimetype") or res.get("mime_type")
                           or res.get("mime") or res.get("format")).lower()
        urls = []
        for key in ("url", "download_url", "download", "file", "video", "video_stream"):
            urls.extend(_url_values(res.get(key)))
        for url in urls:
            full = _direct_video_url(url)
            if full and (mime.startswith("video/") or _VIDEO_RE.search(full)):
                options.append((res, full, (_mime_from_url(full) or mime)))
    # Some item responses expose a top-level video_url outside resources.
    for url in _url_values(item.get("video_url")):
        full = _direct_video_url(url)
        if full and _VIDEO_RE.search(full):
            options.append((item, full, _mime_from_url(full)))
    if not options:
        return None
    res, full, mime = max(options, key=_resource_score)

    thumb = _pick_thumb(item, res)
    if not thumb:  # judge/CLIP cannot evaluate an opaque player
        return None
    page = _absolute_url(_first_text(item.get("url") or item.get("id")))
    title = _first_text(item.get("title"))[:150]
    author = _first_text(item.get("contributor") or item.get("creator"))[:150]
    return Candidate(
        source=source, page_url=page, thumb_url=thumb, full_url=full,
        title=title, license_raw=rights, license=license_key,
        author=author, date_text=_first_text(item.get("date"))[:80], query=query,
        media_type="video", mime_type=mime,
        width=_as_int(res.get("width")), height=_as_int(res.get("height")),
        duration=_as_float(res.get("duration") or item.get("duration")),
        file_size=_as_int(res.get("filesize") or res.get("file_size") or res.get("size")),
        extra={"loc_id": _first_text(item.get("id"))},
    )


def _walk_resources(value):
    if isinstance(value, dict):
        yield value
        for key in ("files", "resources", "media", "streams"):
            if key in value:
                yield from _walk_resources(value[key])
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_resources(child)


def _rights_text(item: dict) -> str:
    nested = item.get("item") if isinstance(item.get("item"), dict) else {}
    values = []
    for obj in (item, nested):
        for key in ("rights", "rights_advisory", "rights_information", "restrictions"):
            values.extend(_text_values(obj.get(key)))
    # De-duplicate while preserving the source wording for attribution/audit.
    return " · ".join(dict.fromkeys(v.strip() for v in values if v.strip()))


def _item_api_url(item: dict) -> str:
    value = _first_text(item.get("id") or item.get("url"))
    if not value:
        return ""
    value = value.replace("http://www.loc.gov/", "https://www.loc.gov/", 1)
    if not value.startswith("https://www.loc.gov/item/"):
        return ""
    return value if value.endswith("/") else value + "/"


def _merge_detail(search_item: dict, detail: dict) -> dict:
    merged = dict(search_item)
    if detail.get("resources"):
        merged["resources"] = detail["resources"]
    nested = detail.get("item") if isinstance(detail.get("item"), dict) else {}
    merged["item"] = nested
    for key in ("rights", "rights_advisory", "rights_information", "restrictions",
                "title", "date", "contributor", "creator", "image_url", "image"):
        if not merged.get(key) and nested.get(key):
            merged[key] = nested[key]
    return merged


def _pick_thumb(item: dict, resource: dict) -> str:
    vals = (_url_values(item.get("image_url")) + _url_values(item.get("image"))
            + _url_values(resource.get("image")) + _url_values(resource.get("thumbnail")))
    # LoC image_url commonly contains [150px, 300px, ..., largest]. A middle/larger
    # preview is much better for the visual judge without downloading the master.
    return _absolute_url(vals[-1]) if vals else ""


def _resource_score(option) -> tuple:
    res, url, mime = option
    width, height = _as_int(res.get("width")), _as_int(res.get("height"))
    max_dim = max(width, height)
    codec = 3 if (mime == "video/mp4" or ".mp4" in url.lower()) else 2
    sane_size = 1 if 720 <= max_dim <= 1920 else 0
    return codec, sane_size, min(max_dim, 1920)


def _text_values(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out = []
        for v in value:
            out.extend(_text_values(v))
        return out
    if isinstance(value, dict):
        return []
    return [str(value)]


def _url_values(value) -> list:
    return [v for v in _text_values(value) if v.startswith(("http://", "https://", "//", "/"))]


def _first_text(value) -> str:
    vals = _text_values(value)
    return vals[0] if vals else ""


def _absolute_url(value: str) -> str:
    value = str(value or "")
    if value.startswith("//"):
        return "https:" + value
    if value.startswith("/"):
        return "https://www.loc.gov" + value
    return value if value.startswith(("http://", "https://")) else ""


def _direct_video_url(value: str) -> str:
    url = _absolute_url(value)
    parsed = urlparse(url)
    # LoC's own IIIF A/V service offers both HLS and static MP4 at the same path.
    # Do not rewrite arbitrary third-party playlists.
    if (parsed.hostname == "tile.loc.gov" and "/streaming-services/" in parsed.path
            and parsed.path.lower().endswith(".m3u8")):
        return url[:url.lower().rfind(".m3u8")] + ".mp4"
    return url


def _mime_from_url(url: str) -> str:
    ext = urlparse(url).path.lower().rsplit(".", 1)[-1]
    return {"mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime",
            "webm": "video/webm", "ogv": "video/ogg", "ogg": "video/ogg",
            "avi": "video/x-msvideo", "mpeg": "video/mpeg", "mpg": "video/mpeg"}.get(ext, "")


def _is_restricted(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "restricted"}


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
