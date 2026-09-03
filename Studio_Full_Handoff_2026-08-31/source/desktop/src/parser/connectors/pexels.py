"""Pexels photos and videos for modern/neutral stock scenes. Key: ``pexels``."""
from .base import BaseConnector, Candidate

API_PHOTOS = "https://api.pexels.com/v1/search"
API_VIDEOS = "https://api.pexels.com/videos/search"
API = API_PHOTOS  # backward-compatible import


class PexelsConnector(BaseConnector):
    name = "pexels"
    supported_media_types = ("image", "video")

    def search(self, query: str, limit: int, media_type: str = "image") -> list:
        if media_type not in self.supported_media_types or limit <= 0:
            return []
        key = self.keys.get("pexels")
        if not key:
            return []
        endpoint = API_VIDEOS if media_type == "video" else API_PHOTOS
        data = self._get_json(endpoint,
                              params={"query": query, "per_page": min(limit, 30)},
                              headers={"Authorization": key})
        return (self._video_results(data, query, limit) if media_type == "video"
                else self._image_results(data, query, limit))

    def _image_results(self, data: dict, query: str, limit: int) -> list:
        out = []
        for p in data.get("photos", []):
            src = p.get("src") or {}
            thumb, full = src.get("medium"), src.get("large2x") or src.get("large")
            if not thumb or not full:
                continue
            out.append(Candidate(
                source=self.name,
                page_url=p.get("url", ""),
                thumb_url=thumb, full_url=full,
                title=(p.get("alt") or f"pexels-{p.get('id', '')}")[:150],
                license_raw="Pexels License", license="pexels",
                author=p.get("photographer", "")[:100],
                query=query,
                media_type="image", mime_type="image/jpeg",
                width=_as_int(p.get("width")), height=_as_int(p.get("height")),
                extra={"pexels_id": p.get("id")},
            ))
            if len(out) >= limit:
                break
        return out

    def _video_results(self, data: dict, query: str, limit: int) -> list:
        out = []
        for p in data.get("videos", []):
            video = _pick_video_file(p.get("video_files") or [])
            if not video:
                continue
            pictures = p.get("video_pictures") or []
            thumb = p.get("image") or ((pictures[0].get("picture") or "") if pictures else "")
            if not thumb:
                continue
            user = p.get("user") if isinstance(p.get("user"), dict) else {}
            tags = p.get("tags") or []
            if isinstance(tags, list):
                title = ", ".join(str(t) for t in tags[:8])
            else:
                title = str(tags)
            out.append(Candidate(
                source=self.name, page_url=p.get("url", ""), thumb_url=thumb,
                full_url=video.get("link", ""),
                title=(title or f"pexels-video-{p.get('id', '')}")[:150],
                license_raw="Pexels License", license="pexels",
                author=str(user.get("name") or "")[:100], query=query,
                media_type="video", mime_type=str(video.get("file_type") or "video/mp4"),
                width=_as_int(video.get("width") or p.get("width")),
                height=_as_int(video.get("height") or p.get("height")),
                duration=_as_float(p.get("duration")), fps=_as_float(video.get("fps")),
                extra={"pexels_id": p.get("id"), "video_file_id": video.get("id"),
                       "quality": video.get("quality", "")},
            ))
            if len(out) >= limit:
                break
        return out


def _pick_video_file(files: list):
    mp4 = [f for f in files if isinstance(f, dict) and f.get("link")
           and str(f.get("file_type") or "").lower() == "video/mp4"]
    if not mp4:
        return None

    def score(f):
        width, height = _as_int(f.get("width")), _as_int(f.get("height"))
        max_dim, pixels = max(width, height), width * height
        if 720 <= max_dim <= 1920:
            return 3, pixels                # largest render-ready HD
        if 0 < max_dim < 720:
            return 2, pixels                # usable preview if HD is absent
        if max_dim > 1920:
            return 1, -pixels               # smallest UHD, avoid 4K bandwidth
        return 0, 0

    return max(mp4, key=score)


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
