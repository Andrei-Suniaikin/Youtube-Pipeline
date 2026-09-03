"""Wikimedia Commons: категорийный источник. Запрос → категории (ns=14, list=search),
валидация categoryinfo, выгреб файлов + спуск в подкатегории (depth 1). Без ключа.

Категории дают меньше кандидатов, но чище полнотекста (Siege of Odesa: 9/9 в тему
против 28 с марками/мемориалами). Полнотекст list=search обязателен как основной
резолвер: префиксный opensearch промахивается мимо переименований (Odessa→Odesa).
Маппинг запрос→категории кэшируется в памяти процесса (_CAT_CACHE).
"""
import re
import threading
import time

from .base import BaseConnector, Candidate, norm_license
from .commons import API, THUMB_W, _strip_html

_CAT_CACHE: dict = {}            # query.lower() -> [category titles]
_CACHE_LOCK = threading.Lock()

MAX_CATS = 2         # берём 1-2 лучшие категории на запрос
MAX_SUBCATS = 6      # спуск максимум в столько подкатегорий (depth 1)
MAX_CONT_PAGES = 3   # страниц континуации на категорию (по 50 файлов)
MIN_WIDTH = 400      # совсем мелочь не берём (как в commons.py)
_STOP = {"category", "categories", "images", "photographs", "photograph",
         "files", "media", "related", "pictures"}


def _tokens(s: str) -> set:
    """Значимые токены имени/запроса: слова от 4 букв + числа (годы)."""
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower())
            if (len(t) >= 4 or t.isdigit()) and t not in _STOP}


def _overlap(a: set, b: set) -> int:
    """Совпадение по 4-символьному префиксу терпит переименования/формы слова
    (Odessa/Odesa, Romanian/Romania)."""
    return sum(1 for x in a if any(x[:4] == y[:4] for y in b))


class CommonsCatConnector(BaseConnector):
    name = "commons_cat"

    # ---- низкий уровень: вызов API с maxlag-ретраем ----

    def _api(self, params: dict) -> dict:
        p = {"format": "json", "formatversion": 2, "maxlag": 5}
        p.update(params)
        data = {}
        for attempt in range(3):
            data = self._get_json(API, params=p)
            if (data.get("error") or {}).get("code") == "maxlag":
                time.sleep(2.0 * (attempt + 1))  # кластер перегружен — подождать
                continue
            break
        return data

    # ---- резолв запроса в категории ----

    def _cat_info(self, titles: list) -> dict:
        """title -> categoryinfo (батчами по 50). missing-категории опускаются."""
        out = {}
        for i in range(0, len(titles), 50):
            data = self._api({"action": "query", "prop": "categoryinfo",
                              "titles": "|".join(titles[i:i + 50])})
            for pg in (data.get("query", {}) or {}).get("pages", []) or []:
                if pg.get("missing"):
                    continue
                out[pg.get("title", "")] = pg.get("categoryinfo", {}) or {}
        return out

    def _resolve_categories(self, query: str) -> list:
        """1-2 живые категории под запрос. Кэш в памяти процесса."""
        key = query.strip().lower()
        with _CACHE_LOCK:
            if key in _CAT_CACHE:
                return list(_CAT_CACHE[key])
        data = self._api({"action": "query", "list": "search", "srsearch": query,
                          "srnamespace": 14, "srlimit": 10})
        hits = [h.get("title", "") for h in
                (data.get("query", {}) or {}).get("search", []) or []]
        hits = [t for t in hits if t]
        # переранжировка: пересечение токенов с запросом, при равенстве — relevance
        qtok = _tokens(query)
        hits.sort(key=lambda t: -_overlap(qtok, _tokens(t)))
        # пустышки и софт-редиректы (files=0, subcats=0) отсекаем по categoryinfo
        info = self._cat_info(hits)
        cats = [t for t in hits
                if (info.get(t, {}).get("files", 0) > 0
                    or info.get(t, {}).get("subcats", 0) > 0)][:MAX_CATS]
        with _CACHE_LOCK:
            _CAT_CACHE[key] = list(cats)
        return cats

    # ---- обход категории ----

    def _category_files(self, cat: str, need: int) -> list:
        """Файлы категории с imageinfo. gcmlimit строго 50 (иначе ii-батч режется)."""
        base = {"action": "query", "generator": "categorymembers",
                "gcmtitle": cat, "gcmtype": "file", "gcmlimit": 50,
                "prop": "imageinfo", "iiprop": "url|extmetadata|mime|size",
                "iiurlwidth": THUMB_W}
        pages, seen, cont = [], set(), {}
        for _ in range(MAX_CONT_PAGES):
            data = self._api({**base, **cont})
            for pg in (data.get("query", {}) or {}).get("pages", []) or []:
                pid = pg.get("pageid")
                if pid in seen:
                    continue
                seen.add(pid)
                pages.append(pg)
            if len(pages) >= need or "continue" not in data:
                break
            cont = dict(data["continue"])  # мержим ВСЕ поля continue как есть
        return pages

    def _pick_subcats(self, cat: str, query: str) -> list:
        """Подкатегории для спуска: keyword-фильтр против тематического дрейфа
        (Battle of Moscow под Barbarossa) + categoryinfo.files>0 против пустых."""
        data = self._api({"action": "query", "list": "categorymembers",
                          "cmtitle": cat, "cmtype": "subcat", "cmlimit": 200})
        subs = [m.get("title", "") for m in
                (data.get("query", {}) or {}).get("categorymembers", []) or []]
        toks = _tokens(query) | _tokens(cat)
        rel = [s for s in subs if s and _overlap(toks, _tokens(s)) > 0]
        if not rel:
            return []
        info = self._cat_info(rel[:50])
        return [s for s in rel if info.get(s, {}).get("files", 0) > 0][:MAX_SUBCATS]

    # ---- кандидаты ----

    def _to_candidate(self, page: dict, query: str, cat: str):
        infos = page.get("imageinfo")
        if not infos:
            return None
        ii = infos[0]
        if ii.get("mime") not in ("image/jpeg", "image/png"):
            return None
        if (ii.get("width") or 0) < MIN_WIDTH:
            return None
        meta = ii.get("extmetadata", {}) or {}
        get = lambda k: _strip_html((meta.get(k, {}) or {}).get("value", ""))
        lic_raw = get("LicenseShortName")
        return Candidate(
            source=self.name,
            page_url=ii.get("descriptionurl", ""),
            thumb_url=ii.get("thumburl") or ii.get("url", ""),
            full_url=ii.get("url", ""),
            title=(page.get("title", "") or "").removeprefix("File:"),
            license_raw=lic_raw, license=norm_license(lic_raw),
            author=get("Artist")[:150], date_text=get("DateTimeOriginal")[:80],
            query=query, extra={"category": cat},
        )

    def search(self, query: str, limit: int) -> list:
        cats = self._resolve_categories(query)
        out, seen = [], set()

        def take(pages, cat):
            for pg in pages:
                pid = pg.get("pageid")
                if pid in seen:
                    continue
                seen.add(pid)
                c = self._to_candidate(pg, query, cat)
                if c:
                    out.append(c)

        for cat in cats:                       # приоритет — корни категорий
            if len(out) >= limit:
                break
            take(self._category_files(cat, need=limit * 2), cat)
        if len(out) < limit:                   # добор: подкатегории depth 1
            for cat in cats:
                if len(out) >= limit:
                    break
                for sub in self._pick_subcats(cat, query):
                    if len(out) >= limit:
                        break
                    take(self._category_files(sub, need=limit), sub)
        return out[:limit]
