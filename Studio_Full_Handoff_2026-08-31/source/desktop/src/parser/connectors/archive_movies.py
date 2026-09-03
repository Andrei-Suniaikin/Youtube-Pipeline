"""Кинохроника archive.org: полнометражные PD-фильмы/newsreels под нарезку на клипы.

Отличие от internet_archive: тот ищет короткие ролики целиком, этот — сырьё
(newsreel 9-13 мин, документалки до 80 мин), которое router режет через
parser.clips на клипы 3-15с. Поэтому здесь нет гейта max_source_seconds,
сортировка по downloads (прокси качества) и предпочтение лёгких mp4-дериватов
(512Kb MPEG4 / h.264), а не оригиналов MPEG2 по 4 ГБ.

Лицензии fail-closed: наружу выходит только явный PD/CC-safe licenseurl либо
официальная US-gov коллекция (PD by law). Community-загрузкам (youtube-зеркала,
opensource_movies) без licenseurl веры нет — режем.
"""
import os
import re
from urllib.parse import quote, urlparse

from .base import BaseConnector, Candidate, SAFE_LICENSES
from .internet_archive import _as_int, _duration, _first, _open_license, _values

SEARCH_API = "https://archive.org/advancedsearch.php"
METADATA_API = "https://archive.org/metadata"
MAX_MOVIE_BYTES = 500 * 1024 * 1024   # 17.07: синхронно с video.movies_max_download_mb (Moscow Strikes Back = 438МБ)

# Официальные коллекции, где PD гарантирован законом/передачей прав (разведка 17.07):
# universal_newsreels — Universal передал в NARA; FedFlix/usgovfilms — US gov works.
PD_COLLECTIONS = frozenset({
    "universal_newsreels", "fedflix", "usgovfilms", "cinemocracy", "prelinger",
})

# Негативные маркеры прав — режут кандидата даже из доверенной коллекции.
# «copyright» как подстрока ловит и «Copyright Universal Pictures…» (ревью 17.07);
# ложных срабатываний на PD-строках нет: "public domain"-тексты слова не содержат.
_NEGATIVE_RIGHTS = ("by-nc", "by-nd", "noncommercial", "non-commercial",
                    "no derivatives", "all rights reserved", "copyright")


class ArchiveMoviesConnector(BaseConnector):
    name = "archive_movies"
    supported_media_types = ("video",)

    def __init__(self, keys: dict, throttle: float = 1.0, dl_throttle: float = 0.15):
        # archive.org отдаёт 503 «please slow down» бодрым ботам — не чаще 1 rps
        super().__init__(keys, throttle=max(float(throttle), 1.0),
                         dl_throttle=dl_throttle)

    def search(self, query: str, limit: int, media_type: str = "image") -> list:
        if media_type != "video" or limit <= 0:
            return []
        params = {
            "fl[]": ["identifier", "title", "creator", "year", "date",
                     "licenseurl", "collection", "downloads", "item_size"],
            "rows": min(max(limit * 4, limit), 40),
            "page": 1,
            "output": "json",
            "sort[]": "downloads desc",   # прокси качества/популярности
        }
        # Каскад вариантов запроса (17.07): полнотекст archive.org AND-ит токены —
        # сценные запросы («Romanian soldiers Odessa 1941») дают 0 доков, а каскад
        # упрощения роутера не срабатывает, пока другие источники что-то находят.
        tokens = re.findall(r"[^\W_]+", str(query or ""), flags=re.UNICODE)
        year = next((t for t in tokens if re.fullmatch(r"19[34]\d", t)), None)
        variants = [query,
                    " ".join(tokens[:3] + ([year] if year and year not in tokens[:3] else [])),
                    " ".join(tokens[:2])]
        data = {}
        for v in dict.fromkeys(v for v in variants if v and v.strip()):
            params["q"] = _build_query(v)
            try:
                data = self._get_json(SEARCH_API, params=params)
            except Exception:  # noqa: BLE001
                return []
            if (data.get("response") or {}).get("docs"):
                break

        out = []
        for doc in ((data.get("response") or {}).get("docs") or []):
            identifier = str(doc.get("identifier") or "").strip()
            if not identifier:
                continue
            collections = {v.casefold() for v in _values(doc.get("collection"))}
            trusted = bool(collections & PD_COLLECTIONS)
            search_rights = _first(doc.get("licenseurl"))
            # 17.07: ранний гейт режет ТОЛЬКО явный негатив. Пустой licenseurl в
            # search-индексе — норма даже для PD-итемов (у Moscow Strikes Back
            # PD-mark лежит только в metadata) — финальное решение за вторым гейтом ниже.
            _low = str(search_rights or "").casefold()
            if any(neg in _low for neg in _NEGATIVE_RIGHTS):
                continue
            try:
                payload = self._get_json(f"{METADATA_API}/{quote(identifier, safe='')}")
            except Exception:  # noqa: BLE001
                continue
            meta = payload.get("metadata") or {}
            meta_rights = _first(meta.get("licenseurl")) or _first(meta.get("rights"))
            rights_raw = meta_rights or search_rights
            license_key = _license_key(rights_raw, trusted)
            if license_key is None:
                continue
            selected = _best_movie_file(payload.get("files") or [])
            if not selected:
                continue
            filename, mime, size = selected
            encoded_id = quote(identifier, safe="")
            out.append(Candidate(
                source=self.name,
                page_url=f"https://archive.org/details/{encoded_id}",
                thumb_url=f"https://archive.org/download/{encoded_id}/__ia_thumb.jpg",
                full_url=(f"https://archive.org/download/{encoded_id}/"
                          f"{quote(filename, safe='/')}"),
                title=(_first(meta.get("title")) or _first(doc.get("title"))
                       or identifier)[:150],
                license_raw=rights_raw or f"US-gov PD collection: "
                                          f"{','.join(sorted(collections & PD_COLLECTIONS))}",
                license=license_key,
                author=(_first(meta.get("creator")) or _first(doc.get("creator")))[:150],
                date_text=(_first(meta.get("date")) or _first(doc.get("year"))
                           or _first(doc.get("date")))[:80],
                query=query,
                media_type="video",
                mime_type=mime,
                duration=_duration(meta.get("runtime")),
                file_size=size,
                extra={"identifier": identifier, "filename": filename,
                       "downloads": _as_int(doc.get("downloads")),
                       "collection": _values(meta.get("collection")
                                             or doc.get("collection"))},
            ))
            if len(out) >= limit:
                break
        return out


def _build_query(query: str) -> str:
    """Токены AND'ом (как у internet_archive) + фильтр PD-recall на стороне поиска.

    Финальный гейт лицензий всё равно _license_key — search-фильтр лишь режет
    мусор youtube-зеркал ещё до metadata-обходов."""
    tokens = re.findall(r"[^\W_]+", str(query or ""), flags=re.UNICODE)[:10]
    if not tokens:
        tokens = ["world", "war", "newsreel"]
    words = " AND ".join(f'"{t}"' for t in tokens)
    pd_filter = ("(licenseurl:(*publicdomain*) OR licenseurl:(*creativecommons*) "
                 "OR collection:(" + " OR ".join(sorted(PD_COLLECTIONS)) + "))")
    return f"({words}) AND mediatype:movies AND {pd_filter}"


def _license_key(raw: str, trusted: bool):
    """-> ключ SAFE_LICENSES | None (reject). Негатив в правах бьёт доверие коллекции."""
    low = str(raw or "").casefold()
    if any(neg in low for neg in _NEGATIVE_RIGHTS):
        return None
    key = _open_license(raw)
    if key in SAFE_LICENSES:
        return key
    return "pd" if trusted else None


def _best_movie_file(files: list):
    """(name, mime, size) лучшего mp4-файла: h.264 > 512Kb MPEG4 > прочий mp4-дериват
    > оригинальный mp4; при равном приоритете — меньший размер. None = нет годного."""
    choices = []
    for item in files:
        if not isinstance(item, dict) or item.get("private") is True:
            continue
        name = str(item.get("name") or "").strip()
        if not name or name.startswith(("__ia_", "_meta")):
            continue
        size = _as_int(item.get("size"))
        if size <= 0 or size > MAX_MOVIE_BYTES:
            continue
        fmt = str(item.get("format") or "").casefold()
        ext = os.path.splitext(urlparse(name).path.casefold())[1]
        is_ogv = ext == ".ogv" or "ogg video" in fmt
        if (ext not in (".mp4", ".m4v") and "h.264" not in fmt
                and "mpeg4" not in fmt and not is_ogv):
            continue  # mkv/MPEG2-оригиналы не берём — ffmpeg-время и трафик
        if "h.264" in fmt:
            prio = 4
        elif "512kb" in fmt or "mpeg4" in fmt or "mpeg-4" in fmt:
            prio = 3
        elif str(item.get("source") or "").casefold() == "derivative" and not is_ogv:
            prio = 2
        elif not is_ogv:
            prio = 1
        else:
            prio = 0  # 17.07: ogv — последний приоритет, но лучше, чем потерять PD-фильм
                      # (у части итемов mp4 больше капа, ogv-дериват меньше; нарезка всё равно перекодирует)
        choices.append(((prio, -size), name, size))
    if not choices:
        return None
    _score, name, size = max(choices, key=lambda x: x[0])
    return name, ("video/ogg" if name.casefold().endswith(".ogv") else "video/mp4"), size
