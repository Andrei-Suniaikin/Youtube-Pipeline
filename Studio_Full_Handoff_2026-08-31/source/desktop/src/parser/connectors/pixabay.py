"""Pixabay photos and videos for modern/neutral stock scenes. Key: ``pixabay``."""
from .base import BaseConnector, Candidate

API_PHOTOS = "https://pixabay.com/api/"
API_VIDEOS = "https://pixabay.com/api/videos/"


class PixabayConnector(BaseConnector):
    name = "pixabay"
    supported_media_types = ("image", "video")

    def search(self, query: str, limit: int, media_type: str = "image") -> list:
        if getattr(self, "_rate_limited", False):
            return []
        if media_type not in self.supported_media_types or limit <= 0:
            return []
        key = self.keys.get("pixabay")
        if not key:
            return []
        # Pixabay requires per_page >= 3. We still return exactly caller's limit.
        params = {"key": key, "q": query, "per_page": max(3, min(limit, 200)),
                  "safesearch": "true"}
        endpoint = API_VIDEOS if media_type == "video" else API_PHOTOS
        try:
            data = self._get_json(endpoint, params=params)
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 429:
                self._rate_limited = True
            # requests' HTTPError includes the query string (and therefore API key).
            raise RuntimeError(f"Pixabay API HTTP {status or 'error'}") from None
        return (self._video_results(data, query, limit) if media_type == "video"
                else self._image_results(data, query, limit))

    def _image_results(self, data: dict, query: str, limit: int) -> list:
        out = []
        for hit in data.get("hits", []):
            thumb = hit.get("webformatURL") or hit.get("previewURL")
            full = hit.get("largeImageURL") or thumb
            if not thumb or not full:
                continue
            out.append(Candidate(
                source=self.name, page_url=hit.get("pageURL", ""),
                thumb_url=thumb, full_url=full,
                title=(hit.get("tags") or f"pixabay-{hit.get('id', '')}")[:150],
                license_raw="Pixabay Content License", license="pixabay",
                author=str(hit.get("user") or "")[:100], query=query,
                media_type="image", mime_type="image/jpeg",
                width=_as_int(hit.get("imageWidth")), height=_as_int(hit.get("imageHeight")),
                file_size=_as_int(hit.get("imageSize")),
                extra={"pixabay_id": hit.get("id")},
            ))
            if len(out) >= limit:
                break
        return out

    def _video_results(self, data: dict, query: str, limit: int) -> list:
        out = []
        for hit in data.get("hits", []):
            variant_name, video = _pick_video_variant(hit.get("videos") or {})
            if not video:
                continue
            picture_id = str(hit.get("picture_id") or "")
            thumb = video.get("thumbnail") or (
                f"https://i.vimeocdn.com/video/{picture_id}_640x360.jpg" if picture_id else "")
            if not thumb:
                continue
            out.append(Candidate(
                source=self.name, page_url=hit.get("pageURL", ""),
                thumb_url=thumb, full_url=video.get("url", ""),
                title=(hit.get("tags") or f"pixabay-video-{hit.get('id', '')}")[:150],
                license_raw="Pixabay Content License", license="pixabay",
                author=str(hit.get("user") or "")[:100], query=query,
                media_type="video", mime_type="video/mp4",
                width=_as_int(video.get("width")), height=_as_int(video.get("height")),
                duration=_as_float(hit.get("duration")),
                file_size=_as_int(video.get("size")),
                extra={"pixabay_id": hit.get("id"), "variant": variant_name},
            ))
            if len(out) >= limit:
                break
        return out


def _pick_video_variant(variants: dict):
    options = [(name, value) for name, value in variants.items()
               if isinstance(value, dict) and value.get("url")]
    if not options:
        return "", None

    def score(option):
        _name, value = option
        width, height = _as_int(value.get("width")), _as_int(value.get("height"))
        max_dim, pixels = max(width, height), width * height
        if 720 <= max_dim <= 1920:
            return 3, pixels
        if 0 < max_dim < 720:
            return 2, pixels
        if max_dim > 1920:
            return 1, -pixels
        return 0, 0

    return max(options, key=score)


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
