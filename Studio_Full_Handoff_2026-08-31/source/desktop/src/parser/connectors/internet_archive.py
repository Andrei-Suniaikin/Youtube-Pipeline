"""Internet Archive Advanced Search + Metadata APIs with strict license gates."""
import os
import re
from urllib.parse import quote, urlparse

from .base import BaseConnector, Candidate, SAFE_LICENSES

SEARCH_API = "https://archive.org/advancedsearch.php"
METADATA_API = "https://archive.org/metadata"
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_VIDEO_BYTES = 256 * 1024 * 1024


class InternetArchiveConnector(BaseConnector):
    name = "internet_archive"
    supported_media_types = ("image", "video")

    def search(self, query: str, limit: int, media_type: str = "image") -> list:
        if media_type not in self.supported_media_types or limit <= 0:
            return []
        mediatype = "movies" if media_type == "video" else "image"
        params = {
            "q": f'({_quoted_query(query)}) AND mediatype:{mediatype} AND licenseurl:(*)',
            "fl[]": ["identifier", "title", "creator", "date", "licenseurl",
                     "rights", "collection", "mediatype"],
            "rows": min(max(limit * 5, limit), 50),
            "page": 1,
            "output": "json",
        }
        try:
            data = self._get_json(SEARCH_API, params=params)
        except Exception:  # noqa: BLE001
            return []

        out = []
        docs = ((data.get("response") or {}).get("docs") or [])
        for doc in docs:
            identifier = str(doc.get("identifier") or "").strip()
            search_rights = _first(doc.get("licenseurl")) or _first(doc.get("rights"))
            if not identifier or _open_license(search_rights) not in SAFE_LICENSES:
                continue
            try:
                payload = self._get_json(f"{METADATA_API}/{quote(identifier, safe='')}")
            except Exception:  # noqa: BLE001
                continue
            meta = payload.get("metadata") or {}
            meta_rights = _first(meta.get("licenseurl")) or _first(meta.get("rights"))
            rights_raw = meta_rights or search_rights
            license_key = _open_license(rights_raw)
            if license_key not in SAFE_LICENSES:
                continue
            selected = _best_file(payload.get("files") or [], media_type)
            if not selected:
                continue
            filename, mime, size, width, height = selected
            encoded_id = quote(identifier, safe="")
            full = f"https://archive.org/download/{encoded_id}/{quote(filename, safe='/')}"
            thumb = f"https://archive.org/download/{encoded_id}/__ia_thumb.jpg"
            out.append(Candidate(
                source=self.name,
                page_url=f"https://archive.org/details/{encoded_id}",
                thumb_url=thumb,
                full_url=full,
                title=(_first(meta.get("title")) or _first(doc.get("title"))
                       or identifier)[:150],
                license_raw=rights_raw,
                license=license_key,
                author=(_first(meta.get("creator")) or _first(doc.get("creator")))[:150],
                date_text=(_first(meta.get("date")) or _first(doc.get("date")))[:80],
                query=query,
                media_type=media_type,
                mime_type=mime,
                width=width,
                height=height,
                duration=_duration(meta.get("runtime")),
                file_size=size,
                extra={"identifier": identifier, "filename": filename,
                       "collection": _values(meta.get("collection") or doc.get("collection"))},
            ))
            if len(out) >= limit:
                break
        return out


def _best_file(files: list, media_type: str):
    choices = []
    cap = MAX_VIDEO_BYTES if media_type == "video" else MAX_IMAGE_BYTES
    for item in files:
        if not isinstance(item, dict) or item.get("private") is True:
            continue
        name = str(item.get("name") or "").strip()
        if not name or name.startswith(("__ia_", "_meta")):
            continue
        size = _as_int(item.get("size"))
        if size <= 0 or size > cap:
            continue
        fmt = str(item.get("format") or item.get("mimetype") or "").casefold()
        ext = os.path.splitext(urlparse(name).path.casefold())[1]
        kind, mime = _file_kind(fmt, ext)
        if kind != media_type:
            continue
        source = str(item.get("source") or "").casefold()
        width, height = _dimensions(item)
        if media_type == "video":
            codec = 4 if ext in {".mp4", ".m4v"} else 2
            derived = 2 if source == "derivative" else 1
            sane = 2 if 480 <= max(width, height) <= 1920 else 1
            score = (codec, derived, sane, min(size, 96 * 1024 * 1024))
        else:
            codec = 3 if ext in {".jpg", ".jpeg", ".png"} else 1
            score = (codec, min(width * height, 20_000_000), -size)
        choices.append((score, name, mime, size, width, height))
    if not choices:
        return None
    _, name, mime, size, width, height = max(choices, key=lambda x: x[0])
    return name, mime, size, width, height


def _file_kind(fmt: str, ext: str):
    if ext in {".mp4", ".m4v", ".mov", ".webm", ".ogv", ".mpeg", ".mpg"}:
        mime = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
                ".webm": "video/webm", ".ogv": "video/ogg", ".mpeg": "video/mpeg",
                ".mpg": "video/mpeg"}[ext]
        return "video", mime
    if any(x in fmt for x in ("h.264", "mpeg4", "mpeg-4", "ogg video")):
        return "video", "video/mp4" if "ogg" not in fmt else "video/ogg"
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        return "image", {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                         ".png": "image/png", ".webp": "image/webp"}[ext]
    return "", ""


def _open_license(raw: str) -> str:
    low = str(raw or "").casefold()
    if any(x in low for x in ("by-nc", "by-nd", "noncommercial", "non-commercial",
                              "no derivatives", "all rights reserved", "in copyright")):
        return "unknown"
    if "publicdomain/zero" in low or "creativecommons.org/publicdomain/zero" in low or "cc0" in low:
        return "cc0"
    if ("publicdomain/mark" in low or "licenses/publicdomain" in low
            or "public domain" in low):
        return "pd"
    if "licenses/by-sa" in low or "cc-by-sa" in low:
        return "cc-by-sa"
    if "licenses/by/" in low or re.search(r"\bcc[- ]by\b", low):
        return "cc-by"
    return "unknown"


def _quoted_query(query: str) -> str:
    # An exact quoted sentence destroys recall in IA (archive titles rarely preserve
    # the classifier's whole phrase). Requiring the individual words keeps precision
    # while still finding catalog records with a different word order.
    tokens = re.findall(r"[^\W_]+", str(query or ""), flags=re.UNICODE)[:10]
    if not tokens:
        tokens = ["world", "war", "ii"]
    return " AND ".join(f'"{token}"' for token in tokens)


def _values(value) -> list:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v)]
    return [str(value)] if value not in (None, "") else []


def _first(value) -> str:
    vals = _values(value)
    return vals[0] if vals else ""


def _dimensions(item: dict) -> tuple:
    width, height = _as_int(item.get("width")), _as_int(item.get("height"))
    if not (width and height):
        match = re.search(r"(\d{2,5})\s*[xX]\s*(\d{2,5})", str(item.get("resolution") or ""))
        if match:
            width, height = int(match.group(1)), int(match.group(2))
    return width, height


def _duration(value) -> float:
    text = str(value or "")
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    parts = text.split(":")
    try:
        return sum(float(part) * (60 ** i) for i, part in enumerate(reversed(parts)))
    except (TypeError, ValueError):
        return 0.0


def _as_int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0
