"""Czech Digital Library (Kramerius 7): open, reusable image records only."""
import re
from urllib.parse import quote

from .base import BaseConnector, Candidate, SAFE_LICENSES

DEFAULT_API = "https://api.ceskadigitalniknihovna.cz/search/api/client/v7.0"


class KrameriusConnector(BaseConnector):
    name = "kramerius"
    supported_media_types = ("image",)

    def search(self, query: str, limit: int, media_type: str = "image") -> list:
        if media_type != "image" or limit <= 0:
            return []
        base = str(self.keys.get("kramerius_base") or DEFAULT_API).rstrip("/")
        terms = _solr_terms(query)
        params = {
            "q": "*:*",
            "fq": [
                "accessibility:public",
                "ds.img_full.mime:*",
                f"(title.search:({terms}) OR root.title:({terms}) OR own_parent.title:({terms}))",
            ],
            "rows": min(max(limit * 8, limit), 80),
            "wt": "json",
        }
        try:
            data = self._get_json(f"{base}/search", params=params)
        except Exception:  # noqa: BLE001
            return []

        out = []
        for doc in ((data.get("response") or {}).get("docs") or []):
            if str(doc.get("accessibility") or "").casefold() != "public":
                continue
            mime = str(doc.get("ds.img_full.mime") or "").casefold()
            if not mime.startswith("image/"):
                continue
            rights = _rights(doc)
            license_raw = " · ".join(dict.fromkeys(rights))
            license_key = _open_license(license_raw)
            # Kramerius' token "public" only means anonymous access, not copyright
            # permission. It is deliberately insufficient without CC/PD metadata.
            if license_key not in SAFE_LICENSES:
                continue
            pid = str(doc.get("pid") or "").strip()
            if not pid:
                continue
            encoded_pid = quote(pid, safe=":")
            leader = str(doc.get("cdk.leader") or "cdk").strip()
            title = str(doc.get("own_parent.title") or doc.get("root.title")
                        or doc.get("title.search") or pid)
            authors = doc.get("authors") or doc.get("authors.search") or []
            author = str(authors[0] if isinstance(authors, list) and authors else authors or "")
            out.append(Candidate(
                source=self.name,
                page_url=f"https://www.digitalniknihovna.cz/{quote(leader, safe='')}/view/{encoded_pid}",
                thumb_url=f"{base}/items/{encoded_pid}/image/preview",
                full_url=f"{base}/items/{encoded_pid}/image",
                title=title[:150],
                license_raw=license_raw,
                license=license_key,
                author=author[:150],
                date_text=str(doc.get("date.str") or doc.get("date_range_start.year") or "")[:80],
                query=query,
                media_type="image",
                mime_type=mime,
                extra={"pid": pid, "model": str(doc.get("model") or ""),
                       "collection": doc.get("cdk.collection") or [],
                       "accessibility": "public"},
            ))
            if len(out) >= limit:
                break
        return out


def _solr_terms(query: str) -> str:
    tokens = re.findall(r"[^\W_]+", str(query or ""), flags=re.UNICODE)[:8]
    if not tokens:
        return '"world" AND "war"'
    return " AND ".join(f'"{token}"' for token in tokens)


def _rights(doc: dict) -> list:
    fields = ("licenses", "licenses_of_ancestors", "cdk.licenses",
              "cdk.licenses_of_ancestors", "cdk.k5.license.translated",
              "rights", "license_url")
    out = []
    for field in fields:
        value = doc.get(field)
        if isinstance(value, (list, tuple)):
            out.extend(str(v).strip() for v in value if str(v).strip())
        elif value not in (None, ""):
            out.append(str(value).strip())
    return out


def _open_license(raw: str) -> str:
    low = str(raw or "").casefold()
    if any(x in low for x in ("by-nc", "by-nd", "noncommercial", "all rights reserved",
                              "dnnt", "onsite", "private", "in copyright")):
        return "unknown"
    if "publicdomain/zero" in low or "cc0" in low:
        return "cc0"
    if "publicdomain/mark" in low or "public domain" in low or "public-domain" in low:
        return "pd"
    if "licenses/by-sa" in low or "cc-by-sa" in low:
        return "cc-by-sa"
    if "licenses/by/" in low or re.search(r"\bcc[- ]by\b", low):
        return "cc-by"
    return "unknown"
