"""Deutsche Digitale Bibliothek API with per-binary open-license checks."""
import os
import re
from urllib.parse import quote, urlparse

from .base import BaseConnector, Candidate, SAFE_LICENSES

API = "https://api.deutsche-digitale-bibliothek.de"


class DdbConnector(BaseConnector):
    name = "ddb"
    supported_media_types = ("image", "video")

    def search(self, query: str, limit: int, media_type: str = "image") -> list:
        if media_type not in self.supported_media_types or limit <= 0:
            return []
        key = self.keys.get("ddb") or self.keys.get("deutsche_digitale_bibliothek")
        if not key:
            return []
        auth = {"oauth_consumer_key": key}
        try:
            data = self._get_json(f"{API}/search", params={
                **auth, "query": query, "rows": min(max(limit * 5, limit), 50),
                "offset": 0,
            })
        except Exception:  # noqa: BLE001
            return []

        results = data.get("results") or ((data.get("response") or {}).get("docs")) or []
        out = []
        for result in results:
            item = result.get("item") if isinstance(result.get("item"), dict) else result
            item_id = str(item.get("id") or result.get("id") or "").strip()
            if not item_id:
                continue
            try:
                detail = self._get_json(f"{API}/items/{quote(item_id, safe='')}", params=auth)
            except Exception:  # noqa: BLE001
                continue
            binaries = _binary_list(detail)
            if not binaries:
                try:
                    binaries = _binary_list(self._get_json(
                        f"{API}/items/{quote(item_id, safe='')}/binaries", params=auth))
                except Exception:  # noqa: BLE001
                    binaries = []
            selected = _pick_binary(binaries, media_type)
            if not selected:
                continue
            binary, full, mime, license_raw, license_key = selected
            thumb = _thumbnail([result, detail])
            if media_type == "video" and not thumb:
                continue
            title = (_text(item.get("title")) or _nested_text(detail, "title")
                     or f"DDB {item_id}")
            author = (_text(item.get("institution")) or _nested_text(detail, "provider")
                      or _nested_text(detail, "creator"))
            out.append(Candidate(
                source=self.name,
                page_url=f"https://www.deutsche-digitale-bibliothek.de/item/{quote(item_id, safe='')}",
                thumb_url=thumb or full,
                full_url=full,
                title=title[:150],
                license_raw=license_raw,
                license=license_key,
                author=author[:150],
                date_text=(_text(item.get("date")) or _nested_text(detail, "date"))[:80],
                query=query,
                media_type=media_type,
                mime_type=mime,
                extra={"ddb_id": item_id, "binary_kind": str(binary.get("kind") or "")},
            ))
            if len(out) >= limit:
                break
        return out


def _pick_binary(binaries: list, media_type: str):
    choices = []
    for binary in binaries:
        if not isinstance(binary, dict):
            continue
        raw_ref = binary.get("ref") or binary.get("href") or binary.get("url")
        full = _binary_url(raw_ref)
        mime = str(binary.get("mimetype") or binary.get("mime") or "").casefold()
        kind, inferred_mime = _media_kind(full, mime)
        if not full or kind != media_type:
            continue
        binary_rights = " · ".join(_rights_values(binary))
        # DDB metadata itself is CC0, but that says nothing about the binary. Never
        # inherit a record/metadata license: the selected binary needs its own grant.
        license_key = _open_license(binary_rights)
        if license_key not in SAFE_LICENSES:
            continue
        primary = 2 if binary.get("primary") is True else 1
        preferred = 2 if (media_type == "video" and inferred_mime == "video/mp4") else 1
        choices.append(((primary, preferred), binary, full, inferred_mime or mime,
                        binary_rights, license_key))
    if not choices:
        return None
    _, binary, full, mime, raw, key = max(choices, key=lambda x: x[0])
    return binary, full, mime, raw, key


def _binary_list(payload) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    binaries = payload.get("binaries") or payload
    if isinstance(binaries, dict):
        value = binaries.get("binary") or binaries.get("items") or []
        return value if isinstance(value, list) else [value] if isinstance(value, dict) else []
    return binaries if isinstance(binaries, list) else []


def _rights_values(payload) -> list:
    wanted = {"license", "licence", "licenseurl", "licenceurl", "licensegroup",
              "licencegroup", "rights", "edmrights", "usagerights", "rightsstatement"}
    out = []

    def visit(value, under_rights=False):
        if isinstance(value, dict):
            for key, child in value.items():
                norm = re.sub(r"[^a-z]", "", str(key).casefold())
                visit(child, under_rights or norm in wanted)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child, under_rights)
        elif under_rights and value not in (None, ""):
            out.append(str(value).strip())

    visit(payload)
    return [v for v in out if v]


def _open_license(raw: str) -> str:
    low = str(raw or "").casefold()
    if any(x in low for x in ("by-nc", "by-nd", "noncommercial", "nicht-kommerziell",
                              "keine weiterverbreitung", "in copyright", "rights reserved")):
        return "unknown"
    if "publicdomain/zero" in low or "cc0" in low:
        return "cc0"
    if "publicdomain/mark" in low or "public domain" in low or "/pdm/" in low:
        return "pd"
    if "licenses/by-sa" in low or "cc-by-sa" in low:
        return "cc-by-sa"
    if "licenses/by/" in low or re.search(r"\bcc[- ]by\b", low):
        return "cc-by"
    return "unknown"


def _thumbnail(payloads: list) -> str:
    keys = {"thumbnail", "thumbnailurl", "preview", "previewurl", "edmpreview"}
    urls = []

    def visit(value, wanted=False):
        if isinstance(value, dict):
            for key, child in value.items():
                norm = re.sub(r"[^a-z]", "", str(key).casefold())
                visit(child, wanted or norm in keys)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child, wanted)
        elif wanted and isinstance(value, str) and value.startswith(("http://", "https://", "/")):
            urls.append(value)

    visit(payloads)
    return _absolute(urls[0]) if urls else ""


def _binary_url(value) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    if value.startswith("/"):
        return API + value
    return f"{API}/binary/{quote(value, safe='/:._-')}"


def _absolute(value: str) -> str:
    return API + value if value.startswith("/") else value


def _media_kind(url: str, mime: str):
    ext = os.path.splitext(urlparse(url).path.casefold())[1]
    if mime.startswith("video/") or ext in {".mp4", ".m4v", ".mov", ".webm", ".ogv", ".mpeg", ".mpg"}:
        return "video", mime or {".mp4": "video/mp4", ".m4v": "video/mp4",
                                  ".mov": "video/quicktime", ".webm": "video/webm",
                                  ".ogv": "video/ogg", ".mpeg": "video/mpeg",
                                  ".mpg": "video/mpeg"}.get(ext, "")
    if mime.startswith("image/") or ext in {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}:
        return "image", mime or {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                                  ".png": "image/png", ".webp": "image/webp",
                                  ".tif": "image/tiff", ".tiff": "image/tiff"}.get(ext, "")
    return "", ""


def _text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return _text(value[0]) if value else ""
    if isinstance(value, dict):
        for key in ("value", "name", "label", "title"):
            if key in value and _text(value[key]):
                return _text(value[key])
    return ""


def _nested_text(payload, needle: str) -> str:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if needle in str(key).casefold() and _text(value):
                return _text(value)
            found = _nested_text(value, needle)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _nested_text(value, needle)
            if found:
                return found
    return ""
