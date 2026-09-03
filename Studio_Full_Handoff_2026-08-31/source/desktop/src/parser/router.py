"""Каскад-роутер одной сцены: библиотека → источники по типу → лицензии → CLIP →
судья → выбор → (B-roll пул кампании) → AI-фолбэк.

Возврат: {"status": "ok"|"soft"|"ai"|"none", "chosen": {...}|None, "judged": [...],
          "n_candidates": int} — всё для report/галереи.
"""
import concurrent.futures as cf
import os
import re
import threading
import unicodedata

from .broll import pick_from_pool
from .clip_rank import embed_images, embed_text, rank
from .connectors import get_connector
from .judge import JUDGE_VERSION, final_check, judge_scene_candidates
from .library import make_ctx_hash


def _words(value: str) -> list:
    text = "".join(ch for ch in unicodedata.normalize("NFKD", str(value or ""))
                   if not unicodedata.combining(ch)).casefold()
    return re.findall(r"[a-z0-9]+", text)


def _person_title_match(person: str, title: str) -> bool:
    """Match full identity or initial+surname; never surname alone."""
    pwords, twords = _words(person), _words(title)
    if len(pwords) < 2:
        return False
    tset = set(twords)
    if all(word in tset for word in pwords):
        return True
    return (pwords[-1] in tset
            and all(word[0] in tset for word in pwords[:-1]))


_DOCUMENT_TITLE_RE = re.compile(
    r"\b(?:ausweis|identity\s+card|passport|paper\s+document|official\s+document|"
    r"ticket|admission\s+pass|printed\s+form|průkaz|doklad|legitimace)\b",
    re.IGNORECASE)
_EVENT_FAMILIES = {
    "court": ("court", "trial", "tribunal", "soud", "proces"),
    "detention": ("arrest", "detention", "detained", "checkpoint", "patrol",
                  "disarmament", "identity check", "captur", "zadrž"),
    "congress": ("congress", "party rally", "party conference", "sjezd"),
    "execution": ("execution", "executed", "poprav", "shooting", "massacre"),
    "funeral": ("funeral", "burial", "memorial service", "pohřeb"),
}


def _event_families(value: str) -> set:
    low = str(value or "").casefold()
    return {family for family, terms in _EVENT_FAMILIES.items()
            if any(term in low for term in terms)}


def _metadata_hard_reject(scene_ctx: dict, cand):
    """Cheap fail-closed metadata rules before the expensive final vision check."""
    title = str(getattr(cand, "title", "") or "")
    if scene_ctx.get("exact_object") != "document" and _DOCUMENT_TITLE_RE.search(title):
        return "metadata: document not allowed"
    scene_event = " ".join(str(scene_ctx.get(key) or "")
                           for key in ("event", "subject", "query"))
    wanted, shown = _event_families(scene_event), _event_families(title)
    # A neutral portrait has no event family and remains a valid identity cutaway.
    # Explicit metadata for another event is not interchangeable footage.
    if wanted and shown and wanted.isdisjoint(shown):
        return "metadata: conflicting event"
    return None


def _relax_queries(cls: dict, scene: dict) -> list:
    """Механическое упрощение: первые 3 слова запроса + год; регион + год."""
    out = []
    year = str(cls.get("year") or "")
    for q in cls.get("queries", [])[:2]:
        toks = [t for t in q.split() if t]
        short = " ".join(toks[:3])
        if year and year not in short:
            short = f"{short} {year}".strip()
        if short and short.lower() not in {o.lower() for o in out}:
            out.append(short)
    region = str(cls.get("region") or "").split("—")[0].split("(")[0].strip()
    if region:
        rq = f"{' '.join(region.split()[:2])} {year}".strip()
        if rq.lower() not in {o.lower() for o in out}:
            out.append(rq)
    return out[:3]


def _content_key(path: str) -> str:
    """Контент-ключ файла — та же формула, что Library.add (sha256[:16] байт)."""
    import hashlib
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return ""


def _jrec(j, uid=None, cached=None) -> dict:
    """Единая judged-запись для report.json (галерея/apply читают эти поля)."""
    c = j["cand"]
    rec = {"uid": uid or c.uid, "source": c.source, "title": c.title,
           "page_url": c.page_url, "full_url": c.full_url,
           "license": c.license, "license_raw": c.license_raw, "author": c.author,
           "clip": c.clip_score, "verdict": j["verdict"], "reason": j["reason"],
           "path": j["path"], "media_type": getattr(c, "media_type", "image"),
           "mime_type": getattr(c, "mime_type", ""),
           "duration": getattr(c, "duration", 0.0),
           "file_size": getattr(c, "file_size", 0),
           "extra": getattr(c, "extra", {}) or {}}
    if cached is not None:
        rec["cached"] = cached
    return rec


def _chosen_rec(c, path: str, uid: str = None) -> dict:
    return {"uid": uid or c.uid, "source": c.source, "title": c.title,
            "page_url": c.page_url, "full_url": c.full_url,
            "thumb_path": path, "license": c.license,
            "license_raw": c.license_raw, "author": c.author,
            "clip": c.clip_score, "attribution": c.attribution(),
            "media_type": getattr(c, "media_type", "image"),
            "mime_type": getattr(c, "mime_type", ""),
            "duration": getattr(c, "duration", 0.0),
            "file_size": getattr(c, "file_size", 0),
            "extra": getattr(c, "extra", {}) or {}}


def _movie_clip_candidate(film, info: dict):
    """Candidate-клип из фильма archive_movies: full_url остаётся фильмом,
    in/out тайм-коды и локальный вырезанный файл едут в extra (stage_video
    поймёт и врезку -ss, и готовый local_path)."""
    from .connectors.base import Candidate
    extra = dict(getattr(film, "extra", {}) or {})
    extra.update({"movie_clip": True, "film_uid": film.uid,
                  "start_time": round(float(info["start"]), 3),
                  "end_time": round(float(info["end"]), 3),
                  "local_path": info["path"]})
    return Candidate(
        source=film.source, page_url=film.page_url, thumb_url=film.thumb_url,
        full_url=film.full_url,
        title=f"{film.title} [{info['start']:.0f}-{info['end']:.0f}s]",
        license_raw=film.license_raw, license=film.license, author=film.author,
        date_text=film.date_text, query=film.query, media_type="video",
        mime_type=film.mime_type or "video/mp4",
        duration=float(info.get("duration") or (info["end"] - info["start"])),
        extra=extra)


def _scene_ctx(scene: dict, cls: dict, queries: list) -> dict:
    return {"subject": cls.get("show") or scene.get("subject", ""),
            "visual_prompt": scene.get("text_segment", ""),
            "year": cls.get("year"), "region": cls.get("region"),
            "query": "; ".join(queries), "type": cls.get("type"),
            "person": cls.get("person"), "place": cls.get("place"),
            "country": cls.get("country"), "event": cls.get("event"),
            "claim_level": cls.get("claim_level", "context"),
            "exact_object": cls.get("exact_object"),
            "stock_ok": bool(cls.get("stock_ok"))}


class Router:
    def __init__(self, niche: dict, keys: dict, log=print, library=None,
                 project: str = "", no_usage_writes: bool = False):
        self.niche = niche
        self.keys = keys
        self.log = log
        self.lib = library          # parser.library.Library | None
        self.project = project      # имя проекта для usage-учёта
        self.no_usage_writes = no_usage_writes  # --dry: не писать usage в БД
        self.broll_pool = []        # [(Candidate, path)] — задаёт parser_worker после classify
        self._proj_used = None      # lazy: {material_id/uid: set(scene)} — кадр 1 раз на ролик
        self._used_lock = threading.RLock()   # атомарный pick+mark при параллельных сценах
        self._broll_lock = threading.Lock()   # pick_from_pool мутирует clip_score кандидатов
        self._movie_locks = {}                # uid фильма -> Lock: скачать/нарезать один раз
        self._movie_failed = set()            # транзитный сбой скачивания: без вечного негатив-кэша
        self._films_lock = threading.Lock()   # кап скачиваний фильмов на прогон
        self._films_fetched = 0
        self.judge_errors = 0       # error-вердикты подряд/всего — для аварийного стопа
        self.judge_oks = 0          # хоть один невырожденный вердикт был
        throttle = float(niche.get("search", {}).get("throttle_sec", 0.7))
        self._conns = {}
        configured_sources = {s for r in niche.get("routes", {}).values()
                              for s in r.get("sources", [])}
        configured_sources.update(
            s for p in (niche.get("source_packs", {}) or {}).values()
            for s in (p.get("sources") or []))
        for name in configured_sources:
            try:
                self._conns[name] = get_connector(name, keys, throttle)
            except Exception as e:  # noqa: BLE001
                log(f"[router] источник {name} недоступен: {e}")

    def _allowed(self, cand) -> bool:
        lic = self.niche.get("licenses", {})
        allow = set(lic.get("allow", []))
        if cand.license in allow:
            return True
        if cand.license == "nc" and lic.get("include_nc"):
            return True
        return False

    def _allowed_for_scene(self, cand, cls: dict) -> bool:
        if not self._allowed(cand):
            return False
        stock_sources = set((self.niche.get("search", {}) or {}).get(
            "stock_sources", ["pexels", "pixabay"]))
        if cand.source in stock_sources and not cls.get("stock_ok"):
            return False
        search_cfg = self.niche.get("search", {}) or {}
        stock_only = (search_cfg.get("stock_only_safe", False)
                      or (search_cfg.get("stock_only_metaphor", False)
                          and cls.get("type") == "abstract"
                          and cls.get("claim_level") == "metaphor"))
        if cls.get("stock_ok") and stock_only and cand.source not in stock_sources:
            return False
        if getattr(cand, "media_type", "image") == "video":
            vcfg = self.niche.get("video", {}) or {}
            max_bytes = int(float(vcfg.get("max_download_mb", 120)) * 1024 * 1024)
            file_size = int(getattr(cand, "file_size", 0) or 0)
            duration = float(getattr(cand, "duration", 0) or 0)
            extra = getattr(cand, "extra", {}) or {}
            if cand.source == "archive_movies" and not extra.get("movie_clip"):
                # полнометражка идёт под нарезку (parser.clips): длительность
                # источника не гейт, лимит трафика — свой
                movies_max = int(float(vcfg.get("movies_max_download_mb", 300))
                                 * 1024 * 1024)
                if file_size and file_size > movies_max:
                    return False
                if duration and duration < float(vcfg.get("min_source_seconds", 1.5)):
                    return False
                return True
            if file_size and file_size > max_bytes:
                return False
            if duration and duration < float(vcfg.get("min_source_seconds", 1.5)):
                return False
            if duration and duration > float(vcfg.get("max_source_seconds", 90)):
                return False
        return True

    def _sources_for_scene(self, route: dict, cls: dict) -> list:
        """Base route + matching country/source packs, deterministic and deduplicated."""
        base = list(route.get("sources", []))
        hay = " ".join(str(cls.get(k) or "") for k in
                       ("region", "country", "place", "event", "show"))
        hay += " " + " ".join(cls.get("queries") or [])
        hay = hay.casefold()
        preferred = []
        for pack in (self.niche.get("source_packs", {}) or {}).values():
            matches = [str(x).casefold() for x in (pack.get("match") or [])]
            if any(token and token in hay for token in matches):
                preferred.extend(pack.get("sources") or [])
        return list(dict.fromkeys(preferred + base))

    def _active_sources(self, route: dict, cls: dict) -> list:
        """Resolve available sources and hard-gate stock before any API request."""
        sources = [s for s in self._sources_for_scene(route, cls) if s in self._conns]
        search_cfg = self.niche.get("search", {}) or {}
        stock_sources = set(search_cfg.get("stock_sources", ["pexels", "pixabay"]))
        if not cls.get("stock_ok"):
            sources = [s for s in sources if s not in stock_sources]
        elif (search_cfg.get("stock_only_safe", False)
              or (search_cfg.get("stock_only_metaphor", False)
                  and cls.get("type") == "abstract"
                  and cls.get("claim_level") == "metaphor")):
            # A time jump needs an actual clock/calendar motif, not a random 1942
            # archive photo that happens to share the year.
            sources = [s for s in sources if s in stock_sources]
        return sources

    def _skip_search_for_contract(self, cls: dict) -> bool:
        skipped = set((self.niche.get("search", {}) or {}).get(
            "skip_exact_objects", ["document"]))
        return cls.get("exact_object") in skipped

    # ---------- правило «кадр один раз на ролик» ----------

    def _used_elsewhere(self, key: str, idx: int) -> bool:
        """True, если кадр НЕЛЬЗЯ ставить в сцену idx по правилам реюза.

        Конкуренты повторяют кадры внутри ролика — разрешаем, но с капом
        (reuse.max_per_video, дефолт 2) и дистанцией (reuse.min_gap сцен, дефолт 60)."""
        if not key:
            return False
        with self._used_lock:
            if self._proj_used is None:
                self._proj_used = (self.lib.usage_map(self.project or "unknown")
                                   if self.lib else {})
            scenes = self._proj_used.get(key)
            if not scenes or idx in scenes:
                return False
            r_cfg = self.niche.get("reuse", {}) or {}
            if len(scenes) >= int(r_cfg.get("max_per_video", 2)):
                return True
            gap = int(r_cfg.get("min_gap", 60))
            return any(abs(idx - s) < gap for s in scenes)

    def _mark_used(self, key: str, idx: int):
        if not key:
            return
        with self._used_lock:
            if self._proj_used is None:
                self._proj_used = {}
            self._proj_used.setdefault(key, set()).add(idx)

    def _record_usage(self, material_id: str, idx: int):
        """usage в БД (переживает перезапуски) + пометка в памяти."""
        if self.lib and material_id and not self.no_usage_writes:
            self.lib.record_usage(material_id, self.project or "unknown", idx, "final")
        self._mark_used(material_id, idx)

    def release_pick(self, result: dict, idx: int):
        """Откат выбора (финал не сохранился): снять пометку и usage-запись."""
        ch = (result or {}).get("chosen") or {}
        key = ch.get("uid")
        if not key:
            return
        if self._proj_used and key in self._proj_used:
            self._proj_used[key].discard(idx)
        if self.lib and not self.no_usage_writes:
            try:
                self.lib.remove_usage(key, self.project or "unknown", idx)
            except Exception:  # noqa: BLE001
                pass

    def _tally_judge(self, judged: list):
        for j in judged:
            if j["verdict"] == "error":
                self.judge_errors += 1
            else:
                self.judge_oks += 1

    def _pick(self, judged: list, idx: int, key_field: str = "material_id",
              allow_soft: bool = False, force_ok: bool = False):
        """Первый ok, иначе первый soft — пропуская уже использованные кадры.
        17.07 «видео — имба» (решение юзера): внутри одного вердикта живой клип
        приоритетнее фото — реальная хроника в сцене ценнее статичного кадра."""
        wants = ("ok",) if force_ok else (
            ("ok", "soft") if (allow_soft or (self.niche.get("judge", {}) or {}).get(
                "accept_soft", False)) else ("ok",))
        for want in wants:
            ordered = sorted(
                judged, key=lambda j: str(getattr(j["cand"], "media_type", "image") or
                                          "image") != "video")
            for j in ordered:
                key = j.get(key_field) or j["cand"].uid
                if j["verdict"] == want and not self._used_elsewhere(key, idx):
                    return j
        return None


    def _confirmed_pick(self, judged, idx, scene_ctx, result=None, check_state=None,
                        force_ok=False):
        """Выбор + подтверждение сильной моделью (двухступенчатый суд).
        mini-судья слеп к лицам/географии на тумбе — контролёр ловит (ревью 16.07)."""
        fc = (self.niche.get("judge", {}) or {}).get("final_check", {}) or {}
        check_types = set(fc.get("types") or [])
        need_check = bool(fc.get("enabled")) and (
            not check_types or scene_ctx.get("type") in check_types
            or bool(scene_ctx.get("person")))
        max_checks = max(0, int(fc.get("max_checks", 3)))
        state = check_state if check_state is not None else {}
        tries = int(state.get("tries", 0))
        while True:
            with self._used_lock:
                allow_soft = bool(scene_ctx.get("stock_ok")) and bool(
                    (self.niche.get("judge", {}) or {}).get("accept_soft_stock", True))
                allow_soft = allow_soft or (bool(scene_ctx.get("person")) and bool(
                    (self.niche.get("judge", {}) or {}).get("accept_soft_identity", False)))
                pick = self._pick(judged, idx, allow_soft=allow_soft, force_ok=force_ok)
                if pick:
                    key = pick.get("material_id") or pick["cand"].uid
                    self._record_usage(key, idx)
            if not pick:
                return None
            hard_reason = _metadata_hard_reject(scene_ctx, pick["cand"])
            if hard_reason:
                self.log(f"  [{idx}] metadata отклонила "
                         f"{(pick['cand'].title or '')[:40]!r}: {hard_reason}")
                key = pick.get("material_id") or pick["cand"].uid
                with self._used_lock:
                    if self._proj_used and key in self._proj_used:
                        self._proj_used[key].discard(idx)
                pick["verdict"], pick["reason"] = "reject", hard_reason
                if result is not None:
                    for rec in result.get("judged", []):
                        if rec["uid"] in (key, pick["cand"].uid):
                            rec["verdict"], rec["reason"] = "reject", hard_reason
                continue
            if not need_check:
                return pick
            if (getattr(pick["cand"], "media_type", "image") == "video"
                    and fc.get("defer_video_to_contact_sheet", True)):
                # Provider thumbnail is insufficient; parser_worker validates 3 real frames
                # after downloading and before publishing renderer refs.
                return pick
            if tries >= max_checks:
                state["exhausted"] = True
                key = pick.get("material_id") or pick["cand"].uid
                with self._used_lock:
                    if self._proj_used and key in self._proj_used:
                        self._proj_used[key].discard(idx)
                if self.lib and not self.no_usage_writes:
                    try:
                        self.lib.remove_usage(key, self.project or "unknown", idx)
                    except Exception:  # noqa: BLE001
                        pass
                self.log(f"  [{idx}] лимит контролёра исчерпан — безопасный fallback")
                return None
            tries += 1
            state["tries"] = tries
            ok, reason = final_check(scene_ctx, pick["path"], self.niche, log=self.log,
                                     cand=pick["cand"])
            if ok:
                return pick
            if str(reason).startswith("facebank:"):
                # D2: face-гейт отклонил ДО LLM-вызова — бюджет контролёра
                # (max_checks) не тратится; остальная бухгалтерия ниже — та же,
                # что у _metadata_hard_reject (discard из _proj_used, пометка
                # reject в result["judged"]) + откат usage.
                tries -= 1
                state["tries"] = tries
            self.log(f"  [{idx}] контролёр отклонил {(pick['cand'].title or '')[:40]!r}: {reason}")
            key = pick.get("material_id") or pick["cand"].uid
            with self._used_lock:
                if self._proj_used and key in self._proj_used:
                    self._proj_used[key].discard(idx)
            if self.lib and not self.no_usage_writes:
                try:
                    self.lib.remove_usage(key, self.project or "unknown", idx)
                except Exception:  # noqa: BLE001
                    pass
            pick["verdict"] = "reject"
            pick["reason"] = f"контролёр: {reason}"[:80]
            if result is not None:  # синхронизируем запись в отчёте для галереи
                for rec in result.get("judged", []):
                    if rec["uid"] in (key, pick["cand"].uid):
                        rec["verdict"], rec["reason"] = "reject", pick["reason"]

    def _judge_progressively(self, scene_ctx, pairs, idx, result, prepare=None,
                             candidate_limit=None, out_judged=None):
        """Judge ranked candidates in small waves and stop at the first final pick.

        ``max_candidates`` remains the quality ceiling; ``wave_size`` only avoids
        paying for lower-ranked candidates when an earlier wave already succeeds.
        ``prepare`` persists verdicts/material ids for each actually judged wave.
        ``out_judged`` collects every judged item — caller may re-pick among them
        (например, если развёртка выбранного фильма в клипы не удалась).
        """
        j_cfg = self.niche.get("judge", {}) or {}
        configured_limit = (j_cfg.get("max_candidates", 6)
                            if candidate_limit is None else candidate_limit)
        limit = max(0, int(configured_limit))
        if not limit:
            return None
        wave_size = max(1, min(limit, int(j_cfg.get("wave_size") or limit)))
        limited = list(pairs[:limit])
        check_state = {"tries": 0, "exhausted": False}
        prefer_ok = bool(j_cfg.get("prefer_ok_across_waves", False))
        all_judged = []
        for start in range(0, len(limited), wave_size):
            wave = limited[start:start + wave_size]
            judged = judge_scene_candidates(scene_ctx, wave, self.niche, log=self.log)
            self._tally_judge(judged)
            if prepare is not None:
                prepare(judged)
            for j in judged:
                if not j.get("material_id") and j["verdict"] != "error":
                    j["material_id"] = _content_key(j["path"]) or j["cand"].uid
            all_judged.extend(judged)
            if out_judged is not None:
                out_judged.extend(judged)
            result.setdefault("judged", []).extend(
                _jrec(j, uid=j.get("material_id")) for j in judged)
            pick = self._confirmed_pick(
                judged, idx, scene_ctx, result, check_state=check_state,
                force_ok=prefer_ok)
            if pick:
                return pick
            if check_state.get("exhausted"):
                break
        if prefer_ok and not check_state.get("exhausted"):
            return self._confirmed_pick(
                all_judged, idx, scene_ctx, result, check_state=check_state)
        return None

    # ---------- слой кинохроники: archive_movies -> клипы ----------

    def _movie_clips(self, film, conn, workdir) -> list:
        """Скачать фильм и нарезать один раз на прогон (кэш в _movies/<uid>).

        -> [{path, start, end, duration, frame}]; [] кэшируется тоже, чтобы
        не перекачивать 300МБ после неудачной нарезки."""
        import json as _json
        from . import clips as clips_mod
        from .media_output import FILM_DL_SEMAPHORE
        vcfg = self.niche.get("video", {}) or {}
        max_bytes = int(float(vcfg.get("movies_max_download_mb", 300)) * 1024 * 1024)
        max_clips = int(vcfg.get("max_clips_per_film", 6))
        max_films = int(vcfg.get("max_films_per_run", 6))
        dl_budget = float(vcfg.get("movies_download_timeout_sec", 600))
        shared = os.path.join(os.path.dirname(os.path.abspath(workdir)),
                              "_movies", film.uid)
        lock = self._movie_locks.setdefault(film.uid, threading.Lock())
        with lock:
            plan_path = os.path.join(shared, "clips.json")
            if os.path.exists(plan_path):
                try:
                    with open(plan_path, encoding="utf-8") as f:
                        infos = _json.load(f)
                    return [i for i in infos
                            if os.path.exists(i.get("path", ""))
                            and os.path.exists(i.get("frame", ""))]
                except (OSError, ValueError):
                    pass
            if film.uid in self._movie_failed:
                return []  # сеть уже моргнула в этом прогоне — не долбим archive.org
            os.makedirs(shared, exist_ok=True)
            film_path = os.path.join(shared, "source.mp4")
            infos, failed = [], False
            try:
                if not os.path.exists(film_path):
                    with self._films_lock:  # бюджет трафика: кап фильмов на прогон
                        if self._films_fetched >= max_films:
                            self.log(f"  [movies] кап {max_films} фильмов на прогон "
                                     f"исчерпан — {film.title[:40]!r} пропущен")
                            return []
                        self._films_fetched += 1
                    self.log(f"  [movies] качаю {film.title[:50]!r} "
                             f"(~{(film.file_size or 0) / 1e6:.0f}МБ)")
                    with FILM_DL_SEMAPHORE:  # не больше 2 параллельных 300МБ-загрузок
                        conn.download(film.full_url, film_path,
                                      media_type="video", max_bytes=max_bytes,
                                      max_seconds=dl_budget)
                infos = clips_mod.split_video(film_path, shared,
                                              max_clips=max_clips, log=self.log)
            except Exception as e:  # noqa: BLE001 — фильм не роняет сцену
                failed = True
                self.log(f"  [movies] {film.title[:40]!r}: скачивание/нарезка "
                         f"не удалась: {e}")
            finally:
                try:
                    os.remove(film_path)  # сырьё 300МБ не храним, клипы остаются
                except OSError:
                    pass
            if failed:
                # транзитный сбой (503/timeout) не равен «фильм не режется»:
                # пустой clips.json не пишем, ретрай — в следующем прогоне
                self._movie_failed.add(film.uid)
                return []
            for info in infos:
                info["frame"] = clips_mod.mid_frame(info["path"])
            infos = [i for i in infos if i.get("frame")]
            tmp = f"{plan_path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(infos, f, ensure_ascii=False, indent=1)
            os.replace(tmp, plan_path)
            return infos

    def _expand_movie_pick(self, idx, scene_ctx, film_pick, result, workdir,
                           scene_text, ctx_hash):
        """Фильм archive_movies -> клипы -> CLIP по средним кадрам -> лучшие 2
        на contact-sheet суд. Все клипы садятся в библиотеку с in/out тайм-кодами
        (source=archive_movies) — сетка реюзает их без перекачки фильма."""
        from . import clips as clips_mod
        film = film_pick["cand"]
        conn = self._conns.get(film.source)
        if conn is None:
            return None
        infos = self._movie_clips(film, conn, workdir)
        if not infos:
            return None
        temb = embed_text(scene_text, self.niche)
        embs = embed_images([i["frame"] for i in infos], self.niche)
        scored = sorted(((i, float(embs[i["frame"]] @ temb)) for i in infos
                         if i["frame"] in embs), key=lambda t: -t[1])
        if not scored:
            return None
        # все клипы -> библиотека (provenance: film page_url + start/end)
        clip_recs = []
        for info, score in scored:
            cand = _movie_clip_candidate(film, info)
            cand.clip_score = round(score, 4)
            mid = None
            if self.lib is not None:
                try:
                    mid = self.lib.add(cand, info["frame"], embs.get(info["frame"]),
                                       niche=self.niche.get("name"))
                except Exception as e:  # noqa: BLE001
                    self.log(f"  [{idx}] library.add клипа: {e}")
            clip_recs.append((cand, info, mid or _content_key(info["frame"]) or cand.uid))
        # лучшие 2 неиспользованных — на существующий видео-суд по contact-sheet
        judged, fresh_pairs = [], []
        for cand, info, mid in clip_recs:
            if len(judged) + len(fresh_pairs) >= 2:
                break
            if self._used_elsewhere(mid, idx):
                continue
            cached = self.lib.get_verdict(mid, ctx_hash) if self.lib else None
            if cached:
                judged.append({"cand": cand, "path": info["frame"], "material_id": mid,
                               "verdict": cached["verdict"], "reason": cached["reason"],
                               "cached": True})
                continue
            try:
                sheet = clips_mod.contact_sheet(info["path"])
            except Exception as e:  # noqa: BLE001
                self.log(f"  [{idx}] contact-sheet клипа не собрался: {e}")
                continue
            fresh_pairs.append((cand, sheet, info, mid))
        if fresh_pairs:
            fresh = judge_scene_candidates(
                scene_ctx, [(c, s) for c, s, _i, _m in fresh_pairs],
                self.niche, log=self.log)
            self._tally_judge(fresh)
            for (cand, _sheet, info, mid), j in zip(fresh_pairs, fresh):
                j["material_id"] = mid
                j["path"] = info["frame"]  # в галерею — средний кадр, не простыня
                if self.lib is not None and j["verdict"] != "error":
                    try:
                        self.lib.put_verdict(mid, ctx_hash, j["verdict"], j["reason"])
                    except Exception:  # noqa: BLE001
                        pass
                judged.append(j)
        if not judged:
            return None
        result.setdefault("judged", []).extend(
            _jrec(j, uid=j["material_id"]) for j in judged)
        with self._used_lock:
            allow_soft = bool((self.niche.get("judge", {}) or {}).get(
                "accept_soft", False)) or bool(scene_ctx.get("stock_ok"))
            pick = self._pick(judged, idx, allow_soft=allow_soft)
            if pick:
                self._record_usage(pick["material_id"], idx)
        if pick:
            self.log(f"  [{idx}] кинохроника: клип "
                     f"{pick['cand'].extra.get('start_time')}–"
                     f"{pick['cand'].extra.get('end_time')}s из "
                     f"{(film.title or '')[:40]!r} ({pick['verdict']})")
        return pick

    # ---------- ступени фолбэка ----------

    def _try_broll(self, idx, scene, cls, queries, result, temb=None) -> bool:
        """Ступень пула: тематический CLIP-матч -> судья; иначе кластерный b-roll
        из библиотеки (B3), иначе staple-заполнение (дежурный кадр эпохи —
        паттерн конкурентов; AI = супер-крайний случай)."""
        b_cfg = self.niche.get("broll", {}) or {}
        if not (b_cfg.get("enabled") and self.broll_pool) or cls["type"] == "stats":
            return False
        if (not b_cfg.get("exact_enabled", False)
                and (cls.get("person") or cls.get("claim_level") == "exact"
                     or cls.get("type") == "named")):
            return False
        text = (f'{cls.get("show") or scene.get("subject", "")}. '
                f'{queries[0] if queries else ""}. historical wartime photograph')
        with self._broll_lock:  # pick_from_pool мутирует clip_score общих кандидатов
            top = pick_from_pool(self.broll_pool, text,
                                 lambda t, ps: rank(t, ps, self.niche, log=self.log),
                                 top_k=3, min_clip=float(b_cfg.get("min_clip", 0.22)))
        if not top:
            # B3: кластерный матч из библиотеки ДО staple — иначе случайный
            # «дежурный кадр» победит тематически близкий кластерный.
            if self._cluster_broll_fill(idx, scene, cls, temb, result):
                return True
            if b_cfg.get("staple_fill", True):
                return self._staple_fill(idx, cls, result)
            return False
        self.log(f"  [{idx}] broll-пул: {len(top)} кандидатов на суд")
        scene_ctx = _scene_ctx(scene, cls, queries)
        ctx_hash = make_ctx_hash(
            f"{cls.get('show') or scene.get('subject', '')}|{JUDGE_VERSION}",
            queries[0] if queries else "")

        # унификация ключа анти-повтора: контент-ключ (= формула Library.add),
        # иначе один и тот же кадр из пула и из каскада не сматчится
        def prepare(judged):
            for j in judged:
                if j["verdict"] == "error":
                    continue
                j["material_id"] = None
                if self.lib is not None:
                    try:
                        j["material_id"] = self.lib.add(
                            j["cand"], j["path"], None,
                            niche=self.niche.get("name"))
                        self.lib.put_verdict(j["material_id"], ctx_hash,
                                             j["verdict"], j["reason"])
                    except Exception:  # noqa: BLE001
                        pass

        pick = self._judge_progressively(
            scene_ctx, [(c, p) for c, p, _s in top], idx, result, prepare=prepare,
            candidate_limit=(self.niche.get("judge", {}) or {}).get(
                "broll_max_candidates", 1))
        if not pick:
            # B3: кластерный матч из библиотеки ДО staple (см. выше)
            if self._cluster_broll_fill(idx, scene, cls, temb, result):
                return True
            if b_cfg.get("staple_fill", True):
                return self._staple_fill(idx, cls, result)
            return False
        c = pick["cand"]
        result["status"] = pick["verdict"]
        result["chosen"] = _chosen_rec(
            c, pick["path"], uid=pick.get("material_id") or c.uid)
        return True

    def _cluster_broll_fill(self, idx, scene, cls, temb, result) -> bool:
        """B3: кластерный b-roll из библиотеки — «похожий кадр» вместо заглушки.

        Кандидаты: CLIP-поиск по библиотеке + фильтр «кластер сцены есть в
        extra.clusters» + видео-квота для motion-сцен. Вердикты — ТОЛЬКО из
        кэша по ЯВНОМУ списку допустимых ctx (cached_verdict_any): материал
        без вердикта в допустимых ctx пропускается — нулевые LLM-вызовы.
        Named/person/exact — мимо ступени: чужой combat-кадр в named-сцену
        мимо face-контроля недопустим (тот же гейт, что у staple)."""
        if self.lib is None or not hasattr(self.lib, "cached_verdict_any"):
            return False  # guard: _ai_or_none может звать без библиотеки
        b_cfg = self.niche.get("broll", {}) or {}
        if not b_cfg.get("cluster_fill", True):
            return False
        if (cls.get("person") or cls.get("claim_level") == "exact"
                or cls.get("type") == "named"):
            return False
        from .clusters import scene_cluster
        cl = scene_cluster(cls)
        if not cl:
            return False
        show = cls.get("show") or scene.get("subject", "")
        queries = cls.get("queries") or []
        if temb is None:  # вызов из _ai_or_none: пересчитать лениво
            text = (f'{show}. {queries[0] if queries else ""}. '
                    f'historical wartime photograph')
            temb = embed_text(text, self.niche)
        hits = list(self.lib.search(
            temb, top_k=8, min_score=float(b_cfg.get("cluster_min_score", 0.22)),
            niche=self.niche.get("name")))
        if cls.get("motion"):  # видео-квота: кадры клипов скорят к тексту ниже фото
            seen_v = {row["id"] for row, _s in hits}
            hits += [h for h in self.lib.search(
                temb, top_k=int(b_cfg.get("cluster_video_top_k", 3)),
                min_score=float(b_cfg.get("cluster_video_min_score", 0.20)),
                niche=self.niche.get("name"), media_type="video")
                if h[0]["id"] not in seen_v]
        if not hits:
            return False
        # C3: золотой фонд — ручная пометка юзера бустит ранжирование (+0.05)
        from .library import GOLD_BOOST
        hits = [(row, score + (GOLD_BOOST if (row.get("extra") or {}).get("gold")
                               else 0.0)) for row, score in hits]
        # допустимые ctx вердиктов: era-staple, video-staple (клипы прогрева
        # несут ИМЕННО его) и контекстный ctx текущей сцены. Голый
        # verdict IN (ok,soft) запрещён — воскресил бы старые версии судьи.
        allowed_ctx = [
            make_ctx_hash(f"__staple_era__|{JUDGE_VERSION}", ""),
            make_ctx_hash(f"__staple_video__|{JUDGE_VERSION}", ""),
            make_ctx_hash(f"{show}|{JUDGE_VERSION}", queries[0] if queries else ""),
        ]
        from .connectors.base import Candidate
        # «видео — имба» (17.07): при прочих равных живой клип раньше фото
        hits.sort(key=lambda h: ((h[0].get("media_type") or "image") != "video",
                                 -h[1]))
        for row, score in hits:
            if cl not in ((row.get("extra") or {}).get("clusters") or []):
                continue
            c = Candidate(source=row["source"], page_url=row["page_url"],
                          thumb_url="", full_url=row["full_url"], title=row["title"],
                          license_raw=row["license_raw"], license=row["license"],
                          author=row["author"], date_text=row["date_text"],
                          query=row["query"],
                          media_type=row.get("media_type") or "image",
                          mime_type=row.get("mime_type") or "",
                          width=int(row.get("width") or 0),
                          height=int(row.get("height") or 0),
                          duration=float(row.get("duration") or 0),
                          file_size=int(row.get("file_size") or 0),
                          fps=float(row.get("fps") or 0),
                          extra=row.get("extra") or {})
            if not self._allowed_for_scene(c, cls):
                continue
            if (c.media_type or "image") == "video" and not cls.get("motion"):
                continue  # та же политика, что у library-хитов основного каскада
            key = row["id"]
            if self._used_elsewhere(key, idx):
                continue
            cached = self.lib.cached_verdict_any(key, allowed_ctx)
            if not cached or cached["verdict"] not in ("ok", "soft"):
                continue  # без вердикта в допустимых ctx — не судим, пропускаем
            with self._used_lock:
                if self._used_elsewhere(key, idx):
                    continue
                self._record_usage(key, idx)
            c.clip_score = round(float(score), 4)
            reason = f"cluster b-roll ({cl})"
            rec = {"uid": key, "source": c.source, "title": c.title,
                   "page_url": c.page_url, "full_url": c.full_url,
                   "license": c.license, "license_raw": c.license_raw,
                   "author": c.author, "clip": c.clip_score, "verdict": "soft",
                   "reason": reason, "path": row["path"],
                   "media_type": c.media_type, "mime_type": c.mime_type,
                   "duration": c.duration, "file_size": c.file_size,
                   "extra": c.extra}
            result.setdefault("judged", []).append(rec)
            result["status"] = "soft"  # всегда жёлтым в галерею — оператор видит
            result["via"] = "cluster_broll"
            result["chosen"] = _chosen_rec(c, row["path"], uid=key)
            result["chosen"]["reason"] = reason  # фильтр «Сомнительные» в галерее
            self.log(f"  [{idx}] cluster b-roll ({cl}): "
                     f"{(c.title or '')[:50]!r} ({c.media_type})")
            return True
        return False

    def _staple_fill(self, idx, cls, result) -> bool:
        if cls.get("type") == "named":
            return False  # named-сцене случайный кадр эпохи хуже AI (ревью 16.07)
        """Дежурный кадр эпохи из пула вместо AI. Era-проверка судьёй кэшируется
        в библиотеке НАВСЕГДА (ctx '__staple_era__') — один кадр судится один раз."""
        from .library import make_ctx_hash
        ctx = make_ctx_hash(f"__staple_era__|{JUDGE_VERSION}", "")
        fresh_checked = 0
        # ранжируем пул по CLIP к сцене: staple с нулевой связью хуже AI
        b_cfg = self.niche.get("broll", {}) or {}
        min_clip = float(b_cfg.get("staple_min_clip", 0.14))
        text = f'{cls.get("show") or ""}. historical wartime photograph'
        with self._broll_lock:
            ranked = pick_from_pool(self.broll_pool, text,
                                    lambda t, ps: rank(t, ps, self.niche, log=self.log),
                                    top_k=len(self.broll_pool), min_clip=min_clip)
        for c, p, _s in ranked:
            key = None
            if self.lib is not None:
                try:
                    key = self.lib.add(c, p, None, niche=self.niche.get("name"))
                except Exception:  # noqa: BLE001
                    pass
            key = key or _content_key(p) or c.uid
            if self._used_elsewhere(key, idx):
                continue
            cached = self.lib.get_verdict(key, ctx) if self.lib else None
            if cached is None:
                if fresh_checked >= 3:  # не больше 3 свежих судов на сцену
                    continue
                fresh_checked += 1
                staple_ctx = {"subject": "documentary era b-roll: people, soldiers, "
                                         "streets or buildings (not random objects)",
                              "visual_prompt": "", "year": cls.get("year"),
                              "region": cls.get("region"), "query": "staple b-roll"}
                j = judge_scene_candidates(staple_ctx, [(c, p)], self.niche,
                                           log=self.log)[0]
                self._tally_judge([j])
                if j["verdict"] == "error":
                    continue
                if self.lib is not None:
                    self.lib.put_verdict(key, ctx, j["verdict"], j["reason"])
                cached = {"verdict": j["verdict"], "reason": j["reason"]}
            if cached["verdict"] not in ("ok", "soft"):
                continue
            with self._used_lock:
                if self._used_elsewhere(key, idx):
                    continue
                self._record_usage(key, idx)
            c.clip_score = 0.0
            rec = {"uid": key, "source": c.source, "title": c.title,
                   "page_url": c.page_url, "full_url": c.full_url,
                   "license": c.license, "license_raw": c.license_raw,
                   "author": c.author, "clip": 0.0, "verdict": "soft",
                   "reason": "staple b-roll (дежурный кадр эпохи)", "path": p}
            result["judged"].append(rec)
            result["status"] = "soft"  # всегда жёлтым в галерею — оператор видит
            result["chosen"] = _chosen_rec(c, p, uid=key)
            result["chosen"]["reason"] = rec["reason"]  # фильтр «Сомнительные»
            self.log(f"  [{idx}] staple b-roll: {(c.title or '')[:50]!r}")
            return True
        return False

    def _ai_or_none(self, idx, scene, cls, route, queries, result, temb=None):
        """Финал сцены без выбора: сперва broll-пул, иначе ai/none."""
        if not self._try_broll(idx, scene, cls, queries, result, temb=temb):
            result["status"] = "ai" if route.get("ai_fallback") else "none"
        return result

    def _cached_library_or_cold(self, idx, scene, cls, hits, ctx_hash, queries):
        """Use context-cached verdicts immediately; return all cold hits unsjudged.

        Cold library candidates are merged with fresh archive results later and share
        one vision batch.  The old design judged them here, discarded every failed
        verdict from the report, then paid again for external and B-roll candidates.
        """
        from .connectors.base import Candidate
        cached, cold = [], []
        for row, score in hits:
            if self._used_elsewhere(row["id"], idx):
                continue
            c = Candidate(source=row["source"], page_url=row["page_url"],
                          thumb_url="", full_url=row["full_url"], title=row["title"],
                          license_raw=row["license_raw"], license=row["license"],
                          author=row["author"], date_text=row["date_text"],
                          query=row["query"], media_type=row.get("media_type") or "image",
                          mime_type=row.get("mime_type") or "",
                          width=int(row.get("width") or 0), height=int(row.get("height") or 0),
                          duration=float(row.get("duration") or 0),
                          file_size=int(row.get("file_size") or 0),
                          fps=float(row.get("fps") or 0), extra=row.get("extra") or {})
            if not self._allowed_for_scene(c, cls):
                continue
            if (c.media_type or "image") == "video" and not cls.get("motion"):
                continue  # видео из библиотеки — только motion-сцены (та же политика,
                          # что media-фильтр внешнего поиска в resolve_scene)
            c.clip_score = round(float(score), 4)
            verdict = self.lib.get_verdict(row["id"], ctx_hash)
            if verdict:
                cached.append({"cand": c, "path": row["path"],
                               "material_id": row["id"],
                               "verdict": verdict["verdict"],
                               "reason": verdict["reason"], "cached": True})
            else:
                cold.append((c, row["path"], row["id"]))
        cached.sort(key=lambda j: -(j["cand"].clip_score or 0))
        cached_result = {"judged": [
            _jrec(j, uid=j["material_id"], cached=True) for j in cached]}
        if cached:
            scene_ctx = _scene_ctx(scene, cls, queries)
            pick = self._confirmed_pick(cached, idx, scene_ctx, cached_result)
            if pick:
                c = pick["cand"]
                return ({"scene": idx, "type": cls["type"], "queries": queries,
                         "n_candidates": len(hits), "from_library": True,
                         "judged": cached_result["judged"], "status": pick["verdict"],
                         "chosen": _chosen_rec(c, pick["path"], uid=pick["material_id"])},
                        cold, cached_result["judged"])
        return None, cold, cached_result["judged"]

    def _resolve_from_library(self, idx, scene, cls, hits, ctx_hash, queries):
        """Хиты библиотеки -> судья с кэшем вердиктов. None = идти во внешний каскад."""
        from .connectors.base import Candidate
        j_cfg = self.niche.get("judge", {})
        hits = [(row, s) for row, s in hits
                if not self._used_elsewhere(row["id"], idx)]  # кадр 1 раз на ролик
        if not hits:
            return None
        pairs, judged = [], []
        for row, score in hits:
            c = Candidate(source=row["source"], page_url=row["page_url"],
                          thumb_url="", full_url=row["full_url"], title=row["title"],
                          license_raw=row["license_raw"], license=row["license"],
                          author=row["author"], date_text=row["date_text"],
                          query=row["query"], media_type=row.get("media_type") or "image",
                          mime_type=row.get("mime_type") or "",
                          width=int(row.get("width") or 0), height=int(row.get("height") or 0),
                          duration=float(row.get("duration") or 0),
                          file_size=int(row.get("file_size") or 0),
                          fps=float(row.get("fps") or 0), extra=row.get("extra") or {})
            # Library is shared across scene types. Apply the same hard policy as for
            # fresh API hits so cached stock can never leak into an exact/named scene.
            if not self._allowed_for_scene(c, cls):
                continue
            if (c.media_type or "image") == "video" and not cls.get("motion"):
                continue  # видео из библиотеки — только motion-сцены
            c.clip_score = round(float(score), 4)
            cached = self.lib.get_verdict(row["id"], ctx_hash)
            if cached:
                judged.append({"cand": c, "path": row["path"], "material_id": row["id"],
                               "verdict": cached["verdict"], "reason": cached["reason"],
                               "cached": True})
            else:
                pairs.append((c, row["path"], row["id"]))
        scene_ctx = _scene_ctx(scene, cls, queries)
        if pairs and not judged:
            # A JUDGE_VERSION bump makes the warm material library verdict-cold.
            # Use the same waves as external search, while leaving mixed/cached
            # selection below byte-for-byte compatible with the old global sort.
            material_by_path = {p: mid for _c, p, mid in pairs}
            progressive = {"judged": []}

            def prepare(fresh):
                for j in fresh:
                    mid = material_by_path[j["path"]]
                    j["material_id"] = mid
                    if j["verdict"] != "error":
                        self.lib.put_verdict(mid, ctx_hash, j["verdict"], j["reason"])

            pick = self._judge_progressively(
                scene_ctx, [(c, p) for c, p, _mid in pairs], idx, progressive,
                prepare=prepare, candidate_limit=j_cfg.get(
                    "library_max_candidates", j_cfg.get("max_candidates", 6)))
            if not pick:
                return None
            progressive["judged"].sort(key=lambda rec: -(rec.get("clip") or 0))
            for rec in progressive["judged"]:
                rec["cached"] = False
            c = pick["cand"]
            return {"scene": idx, "type": cls["type"], "queries": queries,
                    "n_candidates": len(hits), "from_library": True,
                    "judged": progressive["judged"], "status": pick["verdict"],
                    "chosen": _chosen_rec(c, pick["path"], uid=pick["material_id"])}
        if pairs:
            lim = int(j_cfg.get("max_candidates", 6))
            fresh = judge_scene_candidates(scene_ctx, [(c, p) for c, p, _m in pairs[:lim]],
                                           self.niche, log=self.log)
            self._tally_judge(fresh)
            for (c, p, mid), j in zip(pairs, fresh):
                j["material_id"] = mid
                if j["verdict"] != "error":
                    self.lib.put_verdict(mid, ctx_hash, j["verdict"], j["reason"])
                judged.append(j)
        judged.sort(key=lambda j: -(j["cand"].clip_score or 0))
        pick = self._confirmed_pick(judged, idx, scene_ctx)
        if not pick:
            return None
        c = pick["cand"]
        return {"scene": idx, "type": cls["type"], "queries": queries,
                "n_candidates": len(hits), "from_library": True,
                "judged": [_jrec(j, uid=j["material_id"], cached=bool(j.get("cached")))
                           for j in judged],
                "status": pick["verdict"],
                "chosen": _chosen_rec(c, pick["path"], uid=pick["material_id"])}

    # ---------- основной каскад ----------

    def resolve_scene(self, idx: int, scene: dict, cls: dict, workdir: str) -> dict:
        s_cfg = self.niche.get("search", {})
        c_cfg = self.niche.get("clip", {})
        j_cfg = self.niche.get("judge", {})
        # A resolved person is routed through identity-safe archives even when the
        # visual grammar was initially labelled generic/reconstruction.
        route_key = "named" if cls.get("person") else cls["type"]
        route = self.niche.get("routes", {}).get(route_key,
                                                 {"sources": [], "ai_fallback": True})
        sources = self._active_sources(route, cls)
        media_modes = list(route.get("media") or ["image"])
        if not cls.get("motion"):
            media_modes = [m for m in media_modes if m != "video"] or ["image"]
        queries = cls.get("queries", [])[:3]

        if self._skip_search_for_contract(cls):
            self.log(f"  [{idx}] exact {cls.get('exact_object')} → designed fallback")
            return {"scene": idx, "type": cls["type"], "queries": queries,
                    "n_candidates": 0, "judged": [], "chosen": None, "status": "ai"}

        # 0. Библиотека прежде внешнего поиска (реюз + кэш вердиктов).
        #    «Что показать» решает классификатор по наррации (cls["show"]) —
        #    AI-шный subject сцен-плана документы выдумывает, ему веры нет.
        show = cls.get("show") or scene.get("subject", "")
        text = (f'{show}. {queries[0] if queries else ""}. '
                f'historical wartime photograph')
        ctx_hash = make_ctx_hash(f"{show}|{JUDGE_VERSION}", queries[0] if queries else "")
        lib_cfg = self.niche.get("library", {})
        lib_on = self.lib is not None and lib_cfg.get("enabled", True)
        temb = None
        cold_library, cached_judged = [], []
        if lib_on:
            temb = embed_text(text, self.niche)
            top_k = int(lib_cfg.get("top_k", 8))
            hits = []
            if hasattr(self.lib, "search_metadata"):
                hits.extend(self.lib.search_metadata(
                    person=cls.get("person") or "", event=cls.get("event") or "",
                    place=cls.get("place") or "", year=cls.get("year"),
                    top_k=int(lib_cfg.get("metadata_top_k", 6)),
                    niche=self.niche.get("name")))
            vector_hits = self.lib.search(
                temb, top_k=top_k, min_score=float(lib_cfg.get("min_score", 0.25)),
                niche=self.niche.get("name"))
            seen_hits = {row["id"] for row, _score in hits}
            hits.extend((row, score) for row, score in vector_hits if row["id"] not in seen_hits)
            hits = hits[:top_k + int(lib_cfg.get("metadata_top_k", 6))]
            # Отдельная видео-квота (17.07): кадры клипов скорят к тексту ниже картинок
            # (факт: релевантный клип 0.30, порог 0.32) и вытесняются из общего top_k.
            # Motion-гейт для library-хитов стоит в _cached_library_or_cold/_resolve_from_library.
            if "video" in media_modes and cls.get("motion"):
                video_top_k = int(lib_cfg.get("video_top_k", 2))
                # C1: хук-буст — в первых video.hook_scenes сценах видео-квота
                # библиотеки поднимается до 4 (живой клип в хуке держит зрителя)
                if idx < int((self.niche.get("video", {}) or {})
                             .get("hook_scenes", 0) or 0):
                    video_top_k = max(video_top_k, 4)
                seen_v = {row["id"] for row, _score in hits}
                hits.extend(h for h in self.lib.search(
                    temb, top_k=video_top_k,
                    min_score=float(lib_cfg.get("video_min_score", 0.24)),
                    niche=self.niche.get("name"), media_type="video")
                    if h[0]["id"] not in seen_v)
            if hits:
                res, cold_library, cached_judged = self._cached_library_or_cold(
                    idx, scene, cls, hits, ctx_hash, queries)
                if res:
                    self.log(f"  [{idx}] из библиотеки: {res['chosen']['uid']} "
                             f"({res['status']}, хитов {len(hits)})")
                    return res

        # 1. Сбор кандидатов со всех источников маршрута (параллельно по источникам)
        def search_source(name):
            found = []
            stock_sources = set(s_cfg.get("stock_sources") or [])
            query_set = ((cls.get("stock_queries") or queries)
                         if name in stock_sources else queries)
            # E2: национальные архивы (kramerius/ddb) индексируют свой язык —
            # localized_query из контракта идёт ПЕРВЫМ, EN-запросы следом
            localized_sources = set(s_cfg.get("localized_sources") or [])
            lq = cls.get("localized_query")
            if (name in localized_sources and name not in stock_sources
                    and isinstance(lq, str) and lq.strip()):
                lq = lq.strip()
                query_set = [lq] + [q for q in query_set if q != lq]
            allowed_modes = (s_cfg.get("source_media", {}) or {}).get(name)
            source_modes = [m for m in media_modes
                            if not allowed_modes or m in set(allowed_modes)]
            source_query_count = int((s_cfg.get("source_query_count", {}) or {}).get(
                name, len(query_set)))
            source_limit = int((s_cfg.get("source_limit", {}) or {}).get(
                name, s_cfg.get("per_query_limit", 10)))
            for q in query_set[:max(1, source_query_count)]:
                for media_type in source_modes:
                    try:
                        found += self._conns[name].search(
                            q, source_limit,
                            media_type=media_type)
                    except TypeError:
                        if media_type == "image":  # legacy/image-only connector
                            try:
                                found += self._conns[name].search(
                                    q, source_limit)
                            except Exception as e:  # noqa: BLE001
                                self.log(f"  [{idx}] {name}:{q!r} ошибка поиска: {e}")
                    except Exception as e:  # noqa: BLE001
                        self.log(f"  [{idx}] {name}:{q!r}/{media_type} ошибка поиска: {e}")
            return found

        def collect(qs):
            found, seen_local = [], set()
            nonlocal queries
            queries = qs
            if sources:
                with cf.ThreadPoolExecutor(len(sources)) as ex:
                    for lst in ex.map(search_source, sources):
                        for c in lst:
                            if c.uid not in seen_local and self._allowed_for_scene(c, cls):
                                seen_local.add(c.uid)
                                found.append(c)
            return found

        cands = collect(queries)
        if not cands:  # каскад упрощения: полнотекст архивов ANDит слова
            relaxed = _relax_queries(cls, scene)
            if relaxed:
                self.log(f"  [{idx}] 0 кандидатов, упрощаю запросы: {relaxed}")
                cands = collect(relaxed)
        if not cands and cls.get("fallback_query"):
            self.log(f"  [{idx}] фолбэк-запрос архива: {cls['fallback_query']!r}")
            cands = collect([cls["fallback_query"]])
        cands = cands[: int(s_cfg.get("max_candidates_per_scene", 40))]

        result = {"scene": idx, "type": cls["type"], "queries": queries,
                  "n_candidates": len(cands) + len(cold_library),
                  "judged": list(cached_judged), "chosen": None,
                  "status": "none"}
        if not cands and not cold_library:
            return self._ai_or_none(idx, scene, cls, route, queries, result,
                                     temb=temb)

        # 2. Скачиваем тумбы
        os.makedirs(workdir, exist_ok=True)
        by_path = {}

        def dl(c):
            p = os.path.join(workdir, c.uid + ".jpg")
            if not os.path.exists(p):
                try:
                    self._conns[c.source].download(c.thumb_url, p)
                except Exception:  # noqa: BLE001
                    return None
            return (p, c)

        with cf.ThreadPoolExecutor(6) as ex:
            for r in ex.map(dl, cands):
                if r:
                    by_path[r[0]] = r[1]
        library_id_by_path = {}
        for c, path, material_id in cold_library:
            if os.path.exists(path):
                by_path.setdefault(path, c)
                library_id_by_path[path] = material_id
        if not by_path:
            return self._ai_or_none(idx, scene, cls, route, queries, result,
                                     temb=temb)

        # 3. CLIP: короткий фактический текст (длинный кино-промпт душит скоры);
        #    эмбеддинги считаются один раз — и в ранжирование, и в library.add.
        #    Для resolved-person сцен буст только по полному имени из контракта.
        person = (cls.get("person") or "").strip().lower()
        if temb is None:
            temb = embed_text(text, self.niche)
        embs = embed_images(list(by_path.keys()), self.niche)
        ranked = sorted(((p, float(e @ temb)) for p, e in embs.items()),
                        key=lambda kv: -kv[1])
        ranked += [(p, -1.0) for p in by_path if p not in embs]
        min_score = float(c_cfg.get("min_score", 0.18))
        scored = []
        for p, s in ranked:
            c = by_path[p]
            name_hit = _person_title_match(person, c.title or "") if person else False
            if s >= min_score or name_hit:
                scored.append((c, p, s + (0.5 if name_hit else 0.0)))
        scored.sort(key=lambda t: -t[2])
        # Commons search/category/library frequently return the identical bytes under
        # different UIDs. Do not spend several final identity checks on one photo.
        unique_scored, seen_content = [], set()
        for item in scored:
            key = _content_key(item[1]) or str(item[0].full_url or item[0].uid)
            if key in seen_content:
                continue
            seen_content.add(key)
            unique_scored.append(item)
        scored = unique_scored
        top = scored[: int(c_cfg.get("top_k", 10))]
        for c, _p, s in top:
            c.clip_score = round(s, 4)
        if not top:
            return self._ai_or_none(idx, scene, cls, route, queries, result,
                                     temb=temb)

        # 4. Судья по лучшим: волнами, пока не найден финал
        scene_ctx = _scene_ctx(scene, cls, queries)
        def prepare(judged):
            if not lib_on:
                return
            for j in judged:
                if j["verdict"] == "error":
                    continue
                if (j["cand"].source == "archive_movies"
                        and not (j["cand"].extra or {}).get("movie_clip")):
                    continue  # в библиотеку идут нарезанные клипы, не тумбы фильмов
                try:
                    mid = library_id_by_path.get(j["path"])
                    if not mid:
                        mid = self.lib.add(j["cand"], j["path"], embs.get(j["path"]),
                                           niche=self.niche.get("name"))
                    self.lib.put_verdict(mid, ctx_hash, j["verdict"], j["reason"])
                    j["material_id"] = mid
                except Exception as e:  # noqa: BLE001
                    self.log(f"  [{idx}] library.add: {e}")

        # 5. Выбор: первый ok в порядке CLIP; следующая волна
        #    запускается только если текущая не дала финал.
        judged_items = []
        pick = self._judge_progressively(
            scene_ctx, [(c, p) for c, p, _s in top], idx, result, prepare=prepare,
            candidate_limit=j_cfg.get(
                "external_max_candidates", j_cfg.get("max_candidates", 6)),
            out_judged=judged_items)
        # 5b. Слой кинохроники: выбранный фильм archive_movies разворачивается
        #     в конкретный клип (скачать -> нарезать -> CLIP -> contact-sheet суд).
        #     Сломанный фильм не топит сцену: пробуем остальных судёных кандидатов
        #     той же волны, а не сразу AI-фолбэк.
        while (pick and getattr(pick["cand"], "media_type", "image") == "video"
                and pick["cand"].source == "archive_movies"
                and not (getattr(pick["cand"], "extra", {}) or {}).get("movie_clip")):
            film_pick = pick
            pick = self._expand_movie_pick(idx, scene_ctx, film_pick, result,
                                           workdir, text, ctx_hash)
            if pick:
                break
            # клипы не прошли суд/не нарезались — освобождаем пометку фильма
            key = film_pick.get("material_id") or film_pick["cand"].uid
            with self._used_lock:
                if self._proj_used and key in self._proj_used:
                    self._proj_used[key].discard(idx)
            judged_items = [j for j in judged_items
                            if (j.get("material_id") or j["cand"].uid) != key]
            if not judged_items:
                break
            pick = self._confirmed_pick(judged_items, idx, scene_ctx, result)
        if not pick:
            return self._ai_or_none(idx, scene, cls, route, queries, result,
                                     temb=temb)
        c = pick["cand"]
        result["status"] = pick["verdict"]
        result["chosen"] = _chosen_rec(
            c, pick["path"], uid=pick.get("material_id") or c.uid)
        return result
