"""US National Archives Catalog API v2: rights-checked images and videos."""
import os
from urllib.parse import urlparse

from .base import BaseConnector, Candidate

API = "https://catalog.archives.gov/api/v2/records/search"
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_VIDEO_BYTES = 512 * 1024 * 1024


class NaraConnector(BaseConnector):
    name = "nara"
    supported_media_types = ("image", "video")

    def search(self, query: str, limit: int, media_type: str = "image") -> list:
        if media_type not in self.supported_media_types or limit <= 0:
            return []
        key = self.keys.get("nara") or self.keys.get("nara_catalog")
        if not key:
            return []
        params = {
            "q": query,
            "availableOnline": "true",
            "typeOfMaterials": ("Moving Images" if media_type == "video"
                                else "Photographs and other Graphic Materials"),
            "limit": min(max(limit * 4, limit), 100),
            "sourceIncludes": ("naId,title,useRestriction,creators,productionDates,"
                               "inclusiveStartDate,digitalObjects"),
        }
        try:
            data = self._get_json(API, params=params, headers={"x-api-key": key})
        except Exception:  # noqa: BLE001 - an optional archive must not stop a run
            return []

        hits = (((data.get("body") or data).get("hits") or {}).get("hits") or [])
        out = []
        for hit in hits:
            record = ((hit.get("_source") or {}).get("record")
                      or hit.get("record") or {})
            rights = record.get("useRestriction")
            if not _unrestricted(rights):
                continue
            rights_raw = _rights_text(rights)
            naid = str(record.get("naId") or "").strip()
            if not naid:
                continue
            for obj in record.get("digitalObjects") or []:
                if not isinstance(obj, dict) or _media_type(obj) != media_type:
                    continue
                full = _http_url(obj.get("objectUrl"))
                thumb = _http_url(obj.get("thumbnailUrl"))
                if not full or (media_type == "video" and not thumb):
                    continue
                size = _as_int(obj.get("objectFileSize"))
                cap = MAX_VIDEO_BYTES if media_type == "video" else MAX_IMAGE_BYTES
                if size < 0 or size > cap:
                    continue
                mime = _mime(obj, full, media_type)
                out.append(Candidate(
                    source=self.name,
                    page_url=f"https://catalog.archives.gov/id/{naid}",
                    thumb_url=thumb or full,
                    full_url=full,
                    title=str(record.get("title") or obj.get("objectDescription")
                              or f"NARA {naid}")[:150],
                    license_raw=rights_raw,
                    # NARA's item-level "Unrestricted" is a reuse status, not a blanket
                    # public-domain declaration. Keep the distinction in reports/policy.
                    license="nara-unrestricted",
                    author=_creator(record)[:150],
                    date_text=_date_text(record)[:80],
                    query=query,
                    media_type=media_type,
                    mime_type=mime,
                    file_size=size,
                    extra={"naid": naid, "object_id": str(obj.get("objectId") or "")},
                ))
                if len(out) >= limit:
                    return out
        return out


def _unrestricted(value) -> bool:
    if not isinstance(value, dict):
        return False
    if str(value.get("status") or "").strip().casefold() != "unrestricted":
        return False
    text = " ".join(_scalars(value)).casefold()
    blocked = ("copyright held", "subject to copyright", "permission required",
               "rights reserved", "restricted -", "license required")
    return not any(token in text for token in blocked)


def _rights_text(value) -> str:
    if not isinstance(value, dict):
        return ""
    parts = [str(value.get("status") or "").strip(), str(value.get("note") or "").strip()]
    return " · ".join(p for p in parts if p)


def _media_type(obj: dict) -> str:
    url = str(obj.get("objectUrl") or "")
    ext = os.path.splitext(urlparse(url).path.casefold())[1]
    label = str(obj.get("objectType") or "").casefold()
    if ext in {".mp4", ".m4v", ".mov", ".webm", ".ogv", ".mpeg", ".mpg"}:
        return "video"
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".tif", ".tiff"}:
        return "image"
    if any(x in label for x in ("moving image", "video", "mpeg", "mp4")):
        return "video"
    if any(x in label for x in ("jpeg", "jpg", "png", "image", "photograph", "tiff")):
        return "image"
    return ""


def _mime(obj: dict, url: str, media_type: str) -> str:
    label = str(obj.get("objectType") or "").casefold()
    ext = os.path.splitext(urlparse(url).path.casefold())[1]
    if media_type == "video":
        return {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
                ".webm": "video/webm", ".ogv": "video/ogg", ".mpeg": "video/mpeg",
                ".mpg": "video/mpeg"}.get(ext, "video/mpeg" if "mpeg" in label else "")
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp", ".gif": "image/gif", ".tif": "image/tiff",
            ".tiff": "image/tiff"}.get(ext, "image/jpeg" if "jpeg" in label else "")


def _creator(record: dict) -> str:
    for value in _walk(record.get("creators") or []):
        if isinstance(value, dict):
            for key in ("heading", "name", "termName", "title"):
                if isinstance(value.get(key), str) and value[key].strip():
                    return value[key].strip()
    return "National Archives and Records Administration"


def _date_text(record: dict) -> str:
    for root in (record.get("productionDates"), record.get("inclusiveStartDate")):
        for value in _walk(root):
            if isinstance(value, str) and any(ch.isdigit() for ch in value):
                return value
    return ""


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk(child)
    else:
        yield value


def _scalars(value) -> list:
    return [str(v) for v in _walk(value) if isinstance(v, (str, int, float, bool))]


def _http_url(value) -> str:
    value = str(value or "").strip()
    return value if value.startswith(("https://", "http://")) else ""


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
