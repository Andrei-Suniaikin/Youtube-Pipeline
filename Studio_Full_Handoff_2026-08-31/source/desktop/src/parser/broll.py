"""Тематический B-roll пул кампании — ПОСЛЕДНЯЯ ступень перед AI-фолбэком.

build_pool(): один раз на проект. Берёт 2-4 самые частые темы (region+year)
из классификации сцен, выгребает кандидатов через commons_cat (категории — чище)
и commons (полнотекст — шире), фильтрует лицензии профиля, качает тумбы на диск.
pick_from_pool(): лучшие по CLIP кандидаты пула для конкретной сцены
(generic/abstract, у которых каскад источников ничего не дал).
"""
import collections
import concurrent.futures as cf
import os

from .connectors import get_connector

MAX_THEMES = 4
MIN_THEME_COUNT = 2   # тема должна встретиться хотя бы в 2 сценах


def top_themes(classify_results: list, max_themes: int = MAX_THEMES) -> list:
    """-> [(query, count)] — 2-4 самые частые (region+year) кампании.

    Составные регионы («Black Sea / Odessa») режем по «/» и считаем части
    отдельно; год для темы — самый частый ненулевой год этого региона."""
    per_region = collections.defaultdict(collections.Counter)
    display = {}
    for r in classify_results or []:
        parts = {p.strip() for p in str(r.get("region") or "").split("/") if p.strip()}
        for p in parts:
            k = p.lower()
            display.setdefault(k, p)
            per_region[k][r.get("year")] += 1
    themes = []
    for k, ycnt in sorted(per_region.items(), key=lambda kv: -sum(kv[1].values())):
        years = [y for y, _ in ycnt.most_common() if y]
        q = f"{display[k]} {years[0]}" if years else display[k]
        themes.append((q, sum(ycnt.values())))
    strong = [t for t in themes if t[1] >= MIN_THEME_COUNT][:max_themes]
    return strong if len(strong) >= 2 else themes[:2]


def build_pool(niche: dict, classify_results: list, keys: dict,
               out_dir: str, log=print) -> list:
    """-> [(Candidate, thumb_path)] — общий пул кампании (лицензии уже отфильтрованы).

    Пустой список, если broll.enabled=false в профиле или темы не извлеклись."""
    cfg = niche.get("broll", {}) or {}
    if not cfg.get("enabled", False):
        return []
    max_pool = int(cfg.get("max_pool", 60))
    throttle = float((niche.get("search", {}) or {}).get("throttle_sec", 0.7))
    lic = niche.get("licenses", {}) or {}
    allow, inc_nc = set(lic.get("allow", [])), bool(lic.get("include_nc"))

    themes = top_themes(classify_results, int(cfg.get("max_themes", MAX_THEMES)))
    staples = [str(s).strip() for s in (cfg.get("staples") or []) if str(s).strip()]
    themes += [(s, 0) for s in staples]  # дежурные темы ниши — всегда в пуле
    if not themes:
        log("[broll] темы не извлеклись и staples пусты — пул пуст")
        return []
    log(f"[broll] темы: {', '.join(f'{q!r}×{n}' for q, n in themes)}")

    conns = []
    for name in ("commons_cat", "commons"):
        try:
            conns.append(get_connector(name, keys, throttle))
        except Exception as e:  # noqa: BLE001
            log(f"[broll] источник {name} недоступен: {e}")
    if not conns:
        return []

    per = max(5, max_pool // max(1, len(themes) * len(conns)))
    cands, seen = [], set()
    for q, _n in themes:
        for conn in conns:
            if len(cands) >= max_pool:
                break
            try:
                found = conn.search(q, per)
            except Exception as e:  # noqa: BLE001
                log(f"[broll] {conn.name}:{q!r} ошибка поиска: {e}")
                continue
            for c in found:
                if c.uid in seen:
                    continue
                if not (c.license in allow or (c.license == "nc" and inc_nc)):
                    continue
                seen.add(c.uid)
                cands.append(c)
    cands = cands[:max_pool]

    os.makedirs(out_dir, exist_ok=True)
    by_name = {conn.name: conn for conn in conns}

    def dl(c):
        p = os.path.join(out_dir, c.uid + ".jpg")
        if not os.path.exists(p):
            try:
                by_name.get(c.source, conns[0]).download(c.thumb_url, p)
            except Exception:  # noqa: BLE001
                return None
        return (c, p)

    pool = []
    with cf.ThreadPoolExecutor(6) as ex:
        for r in ex.map(dl, cands):
            if r:
                pool.append(r)
    log(f"[broll] пул готов: {len(pool)}/{len(cands)} кандидатов -> {out_dir}")
    return pool


def warm_staple_videos(niche: dict, keys: dict, library, workdir: str,
                       log=print) -> int:
    """Одноразовый прогрев библиотеки клипами кинохроники (archive_movies).

    По каждому запросу из broll.staple_video_queries берётся 1 лучший PD-фильм,
    режется на клипы (parser.clips), клипы судятся era-судьёй по contact-sheet
    и ok/soft садятся в библиотеку с вечным вердиктом ctx '__staple_video__'.
    Каждый фильм обрабатывается один раз на всю сетку — маркер
    <library.root>/staple_video_done.json. -> сколько клипов добавлено."""
    cfg = niche.get("broll", {}) or {}
    queries = [str(q).strip() for q in (cfg.get("staple_video_queries") or [])
               if str(q).strip()]
    if not queries or library is None:
        return 0
    import json
    import re as _re
    import time

    from . import clips as clips_mod
    from .clip_rank import embed_images
    from .judge import JUDGE_VERSION, judge_scene_candidates
    from .library import make_ctx_hash
    from .router import _movie_clip_candidate

    throttle = max(1.0, float((niche.get("search", {}) or {}).get("throttle_sec", 0.7)))
    try:
        conn = get_connector("archive_movies", keys, throttle)
    except Exception as e:  # noqa: BLE001
        log(f"[staple-video] источник archive_movies недоступен: {e}")
        return 0
    vcfg = niche.get("video", {}) or {}
    max_bytes = int(float(vcfg.get("movies_max_download_mb", 300)) * 1024 * 1024)
    max_clips = int(vcfg.get("max_clips_per_film", 6))
    dl_budget = float(vcfg.get("movies_download_timeout_sec", 600))
    marker_path = os.path.join(library.root, "staple_video_done.json")
    try:
        with open(marker_path, encoding="utf-8") as f:
            done = json.load(f)
    except (OSError, ValueError):
        done = {}
    ctx = make_ctx_hash(f"__staple_video__|{JUDGE_VERSION}", "")
    staple_ctx = {"subject": "documentary era wartime newsreel footage: soldiers, "
                             "vehicles, battlefields, ruined streets (not titles, "
                             "not intertitles, not blank frames)",
                  "visual_prompt": "", "year": None, "region": "",
                  "query": "staple video b-roll"}
    added = 0
    for q in queries:
        try:
            found = conn.search(q, 5, media_type="video")
        except Exception as e:  # noqa: BLE001
            log(f"[staple-video] {q!r}: ошибка поиска: {e}")
            continue
        # 17.07: сортировка downloads desc тащит популярный мусор (на «Moscow Strikes
        # Back» приезжал другой фильм) — предпочитаем максимум токенов запроса в title.
        q_tokens = {t for t in _re.findall(r"[^\W_]+", q.casefold()) if len(t) > 2}
        def _title_hits(f):
            title = str(f.title or "").casefold()
            return sum(1 for t in q_tokens if t in title)
        found.sort(key=_title_hits, reverse=True)
        films = found[:1]
        for film in films:
            ident = str((film.extra or {}).get("identifier") or film.uid)
            if ident in done:
                continue
            safe = _re.sub(r"[^\w.-]+", "_", ident)[:80]
            movie_dir = os.path.join(workdir, safe)
            os.makedirs(movie_dir, exist_ok=True)
            film_path = os.path.join(movie_dir, "source.mp4")
            infos, failed = [], False
            try:
                log(f"[staple-video] {q!r}: качаю {film.title[:50]!r} "
                    f"(~{(film.file_size or 0) / 1e6:.0f}МБ)")
                if not os.path.exists(film_path):
                    conn.download(film.full_url, film_path,
                                  media_type="video", max_bytes=max_bytes,
                                  max_seconds=dl_budget)
                infos = clips_mod.split_video(film_path, movie_dir,
                                              max_clips=max_clips, log=log)
            except Exception as e:  # noqa: BLE001 — прогрев не роняет прогон
                failed = True
                log(f"[staple-video] {ident}: скачивание/нарезка не удалась: {e} "
                    f"— done-маркер не пишу, ретрай в следующем прогоне")
            finally:
                try:
                    os.remove(film_path)
                except OSError:
                    pass
            if failed:
                continue  # транзитный 503/timeout не должен навсегда пометить фильм
            pairs = []
            for info in infos:
                frame = clips_mod.mid_frame(info["path"])
                if not frame:
                    continue
                try:
                    sheet = clips_mod.contact_sheet(info["path"])
                except Exception:  # noqa: BLE001
                    continue
                pairs.append((_movie_clip_candidate(film, info), sheet, frame))
            n_ok = 0
            if pairs:
                embs = embed_images([f for _c, _s, f in pairs], niche)
                judged = judge_scene_candidates(
                    staple_ctx, [(c, s) for c, s, _f in pairs], niche, log=log)
                for (cand, _sheet, frame), j in zip(pairs, judged):
                    if j.get("verdict") not in ("ok", "soft"):
                        continue
                    try:
                        mid = library.add(cand, frame, embs.get(frame),
                                          niche=niche.get("name"))
                        library.put_verdict(mid, ctx, j["verdict"], j["reason"])
                        n_ok += 1
                    except Exception as e:  # noqa: BLE001
                        log(f"[staple-video] library.add: {e}")
            done[ident] = {"at": time.time(), "query": q, "clips_ok": n_ok}
            tmp = f"{marker_path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(done, f, ensure_ascii=False, indent=1)
            os.replace(tmp, marker_path)
            added += n_ok
            log(f"[staple-video] {ident}: в библиотеку {n_ok}/{len(pairs)} клипов")
    if added:
        log(f"[staple-video] прогрев готов: +{added} клипов кинохроники")
    return added


def newsreel_warm_queries(niche: dict, keys: dict, library, log=print) -> list:
    """C3b: систематический проход Universal Newsreels 1941–44.

    advancedsearch по collection:universal_newsreels + date range; в очередь
    прогрева идут итемы с Russia/front/war-токенами в title (префикс-матч,
    warm.newsreel_title_match). Кап очереди — warm.max_films_per_night
    (дефолт 20, 0 = выключено). Уже обработанные итемы (маркер
    staple_video_done.json) пропускаются. -> список запросов-заголовков:
    title-матч warm_staple_videos найдёт сам итем без нового кода скачивания."""
    w_cfg = niche.get("warm", {}) or {}
    cap = int(w_cfg.get("max_films_per_night", 20))
    if cap <= 0 or library is None:
        return []
    import json
    import re as _re

    from .connectors.archive_movies import SEARCH_API

    throttle = max(1.0, float((niche.get("search", {}) or {}).get("throttle_sec", 0.7)))
    try:
        conn = get_connector("archive_movies", keys, throttle)
    except Exception as e:  # noqa: BLE001
        log(f"[newsreels] источник archive_movies недоступен: {e}")
        return []
    params = {
        "q": ("collection:(universal_newsreels) AND mediatype:(movies) "
              "AND year:[1941 TO 1944]"),
        "fl[]": ["identifier", "title", "year"],
        "rows": 300, "page": 1, "output": "json", "sort[]": "date asc",
    }
    try:
        data = conn._get_json(SEARCH_API, params=params)
    except Exception as e:  # noqa: BLE001
        log(f"[newsreels] advancedsearch не ответил: {e}")
        return []
    marker_path = os.path.join(library.root, "staple_video_done.json")
    try:
        with open(marker_path, encoding="utf-8") as f:
            done = json.load(f)
    except (OSError, ValueError):
        done = {}
    terms = [str(t).casefold() for t in
             (w_cfg.get("newsreel_title_match") or ["russia", "soviet", "front", "war"])]
    out = []
    for doc in ((data.get("response") or {}).get("docs") or []):
        if len(out) >= cap:
            break
        ident = str(doc.get("identifier") or "").strip()
        raw_title = doc.get("title")
        if isinstance(raw_title, list):
            raw_title = raw_title[0] if raw_title else ""
        title = str(raw_title or "").strip()
        if not ident or not title or ident in done:
            continue
        tokens = _re.findall(r"[a-z0-9]+", title.casefold())
        if not any(tok.startswith(term) for term in terms for tok in tokens):
            continue
        out.append(title)
    if out:
        log(f"[newsreels] очередь прогрева: {len(out)} итемов (кап {cap})")
    return out


def warm_youtube_cc(niche: dict, keys: dict, library, workdir: str,
                    log=print) -> int:
    """C3b: майнинг видео-пула с YouTube (CC-BY через yt-dlp, решение юзера 17.07).

    Скачанное идёт в ТОТ ЖЕ конвейер, что staple-видео: clips.split_video ->
    contact-sheet суд era-судьёй -> библиотека (вечный вердикт ctx
    '__staple_video__') с provenance {source: youtube_cc, url, канал, лицензия
    на момент скачивания}. license=youtube-cc-by + risk contentid_possible
    в extra — юзер осознанно принимает риск клеймов. -> сколько клипов добавлено."""
    cfg = niche.get("broll", {}) or {}
    queries = [str(q).strip() for q in (cfg.get("youtube_cc_queries") or [])
               if str(q).strip()]
    if not queries or library is None:
        return 0
    import json
    import re as _re
    import time

    from . import clips as clips_mod
    from .clip_rank import embed_images
    from .judge import JUDGE_VERSION, judge_scene_candidates
    from .library import make_ctx_hash
    from .router import _movie_clip_candidate

    y_cfg = niche.get("youtube_cc", {}) or {}
    throttle = max(1.0, float((niche.get("search", {}) or {}).get("throttle_sec", 0.7)))
    try:
        conn = get_connector("youtube_cc", keys, throttle)
    except Exception as e:  # noqa: BLE001
        log(f"[youtube-cc] источник недоступен: {e}")
        return 0
    conn.trusted_channels = [str(c) for c in (y_cfg.get("trusted_channels") or [])]
    vcfg = niche.get("video", {}) or {}
    max_bytes = int(float(vcfg.get("movies_max_download_mb", 300)) * 1024 * 1024)
    max_clips = int(vcfg.get("max_clips_per_film", 6))
    dl_budget = float(vcfg.get("movies_download_timeout_sec", 600))
    per_query = int(y_cfg.get("max_per_query", 2))
    marker_path = os.path.join(library.root, "staple_video_done.json")
    try:
        with open(marker_path, encoding="utf-8") as f:
            done = json.load(f)
    except (OSError, ValueError):
        done = {}
    ctx = make_ctx_hash(f"__staple_video__|{JUDGE_VERSION}", "")
    staple_ctx = {"subject": "documentary era wartime newsreel footage: soldiers, "
                             "vehicles, battlefields, ruined streets (not titles, "
                             "not intertitles, not blank frames, no channel "
                             "watermarks or subscribe overlays)",
                  "visual_prompt": "", "year": None, "region": "",
                  "query": "youtube cc staple video b-roll"}
    added = 0
    for q in queries:
        try:
            found = conn.search(q, per_query, media_type="video")
        except Exception as e:  # noqa: BLE001
            log(f"[youtube-cc] {q!r}: ошибка поиска: {e}")
            continue
        for video in found:
            ident = f"youtube:{(video.extra or {}).get('youtube_id') or video.uid}"
            if ident in done:
                continue
            safe = _re.sub(r"[^\w.-]+", "_", ident)[:80]
            movie_dir = os.path.join(workdir, safe)
            os.makedirs(movie_dir, exist_ok=True)
            video_path = os.path.join(movie_dir, "source.mp4")
            infos, failed = [], False
            try:
                log(f"[youtube-cc] {q!r}: качаю {video.title[:50]!r} "
                    f"({(video.duration or 0) / 60:.0f} мин, канал "
                    f"{(video.extra or {}).get('channel', '')!r})")
                if not os.path.exists(video_path):
                    conn.download(video.full_url, video_path,
                                  media_type="video", max_bytes=max_bytes,
                                  max_seconds=dl_budget)
                infos = clips_mod.split_video(video_path, movie_dir,
                                              max_clips=max_clips, log=log)
            except Exception as e:  # noqa: BLE001 — прогрев не роняет прогон
                failed = True
                log(f"[youtube-cc] {ident}: скачивание/нарезка не удалась: {e} "
                    f"— done-маркер не пишу, ретрай в следующем прогоне")
            finally:
                try:
                    os.remove(video_path)
                except OSError:
                    pass
            if failed:
                continue  # транзитный сбой не должен навсегда пометить видео
            pairs = []
            for info in infos:
                frame = clips_mod.mid_frame(info["path"])
                if not frame:
                    continue
                try:
                    sheet = clips_mod.contact_sheet(info["path"])
                except Exception:  # noqa: BLE001
                    continue
                pairs.append((_movie_clip_candidate(video, info), sheet, frame))
            n_ok = 0
            if pairs:
                embs = embed_images([f for _c, _s, f in pairs], niche)
                judged = judge_scene_candidates(
                    staple_ctx, [(c, s) for c, s, _f in pairs], niche, log=log)
                for (cand, _sheet, frame), j in zip(pairs, judged):
                    if j.get("verdict") not in ("ok", "soft"):
                        continue
                    try:
                        mid = library.add(cand, frame, embs.get(frame),
                                          niche=niche.get("name"))
                        library.put_verdict(mid, ctx, j["verdict"], j["reason"])
                        n_ok += 1
                    except Exception as e:  # noqa: BLE001
                        log(f"[youtube-cc] library.add: {e}")
            done[ident] = {"at": time.time(), "query": q, "clips_ok": n_ok,
                           "source": "youtube_cc"}
            tmp = f"{marker_path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(done, f, ensure_ascii=False, indent=1)
            os.replace(tmp, marker_path)
            added += n_ok
            log(f"[youtube-cc] {ident}: в библиотеку {n_ok}/{len(pairs)} клипов")
    if added:
        log(f"[youtube-cc] прогрев готов: +{added} клипов")
    return added


def warm_cluster_photos(niche: dict, keys: dict, library, workdir: str,
                        max_images: int = 0, log=print) -> int:
    """B4: прогрев ФОТО-пула ниши по кластерным запросам (broll.cluster_queries).

    Для каждого кластера — свои запросы (yaml: {cluster: [queries]});
    источники commons/commons_cat/europeana; суд era-судьёй батчами
    (batch_size судьи); ok/soft садятся в библиотеку с вечным вердиктом
    ctx '__staple_era__' и кластером запроса в extra (merge_extra —
    НЕ перезапись). max_images — общий кап добавленных фото (0 = без капа).
    -> сколько фото добавлено."""
    cfg = niche.get("broll", {}) or {}
    cluster_queries = cfg.get("cluster_queries") or {}
    if not cluster_queries or library is None:
        return 0
    from .clip_rank import embed_images
    from .judge import JUDGE_VERSION, judge_scene_candidates
    from .library import make_ctx_hash

    throttle = float((niche.get("search", {}) or {}).get("throttle_sec", 0.7))
    lic = niche.get("licenses", {}) or {}
    allow, inc_nc = set(lic.get("allow", [])), bool(lic.get("include_nc"))
    conns = []
    for name in ("commons", "commons_cat", "europeana"):
        try:
            conns.append(get_connector(name, keys, throttle))
        except Exception as e:  # noqa: BLE001
            log(f"[warm-фото] источник {name} недоступен: {e}")
    if not conns:
        return 0
    by_name = {conn.name: conn for conn in conns}
    ctx = make_ctx_hash(f"__staple_era__|{JUDGE_VERSION}", "")
    per = int(cfg.get("warm_per_query", 8))
    era = niche.get("era", {}) or {}
    os.makedirs(workdir, exist_ok=True)
    added = 0
    for cluster, queries in cluster_queries.items():
        if max_images and added >= max_images:
            break
        pairs, seen = [], set()
        for q in [str(x).strip() for x in (queries or []) if str(x).strip()]:
            for conn in conns:
                try:
                    found = conn.search(q, per)
                except Exception as e:  # noqa: BLE001
                    log(f"[warm-фото] {conn.name}:{q!r} ошибка поиска: {e}")
                    continue
                for c in found:
                    if c.uid in seen:
                        continue
                    if not (c.license in allow or (c.license == "nc" and inc_nc)):
                        continue
                    seen.add(c.uid)
                    p = os.path.join(workdir, c.uid + ".jpg")
                    if not os.path.exists(p):
                        try:
                            by_name.get(c.source, conns[0]).download(c.thumb_url, p)
                        except Exception:  # noqa: BLE001
                            continue
                    pairs.append((c, p))
        if not pairs:
            log(f"[warm-фото] {cluster}: кандидатов нет")
            continue
        staple_ctx = {"subject": f"documentary era b-roll "
                                 f"({cluster.replace('_', ' ')}): authentic "
                                 f"wartime photograph of the era",
                      "visual_prompt": "", "year": era.get("from"),
                      "region": "", "query": f"staple b-roll {cluster}"}
        embs = embed_images([p for _c, p in pairs], niche)
        judged = judge_scene_candidates(staple_ctx, pairs, niche, log=log)
        n_ok = 0
        for (cand, path), j in zip(pairs, judged):
            if max_images and added >= max_images:
                break
            if j.get("verdict") not in ("ok", "soft"):
                continue
            try:
                mid = library.add(cand, path, embs.get(path),
                                  niche=niche.get("name"))
                library.put_verdict(mid, ctx, j["verdict"], j["reason"])
                # кластер запроса гарантированно в extra (add мог дать иные)
                row = library.get(mid) or {}
                cur = set((row.get("extra") or {}).get("clusters") or [])
                if cluster not in cur:
                    library.merge_extra(mid, {"clusters": sorted(cur | {cluster})})
                added += 1
                n_ok += 1
            except Exception as e:  # noqa: BLE001
                log(f"[warm-фото] library.add: {e}")
        log(f"[warm-фото] {cluster}: +{n_ok}/{len(pairs)} фото")
    if added:
        log(f"[warm-фото] прогрев готов: +{added} фото")
    return added


def pick_from_pool(pool: list, scene_text: str, clip_rank_fn,
                   top_k: int = 3, min_clip: float = 0.22) -> list:
    """Лучшие по CLIP кандидаты пула для сцены -> [(Candidate, path, score)].

    clip_rank_fn(text, [paths]) -> [(path, score)] — обёртка clip_rank.rank
    с профилем ниши (передаёт вызывающий, чтобы не тянуть torch сюда)."""
    if not pool:
        return []
    from .library import GOLD_BOOST  # C3: gold-буст к CLIP-рангу пула
    by_path = {p: c for c, p in pool}
    scored = []
    for p, s in clip_rank_fn(scene_text, list(by_path.keys())):
        c = by_path.get(p)
        gold = bool((getattr(c, "extra", {}) or {}).get("gold")) if c else False
        scored.append((p, float(s) + (GOLD_BOOST if gold else 0.0)))
    ranked = sorted(scored, key=lambda kv: -kv[1])
    out = []
    for p, s in ranked:
        if s < min_clip or len(out) >= top_k:
            break
        c = by_path.get(p)
        if c is None:
            continue
        c.clip_score = round(float(s), 4)
        out.append((c, p, float(s)))
    return out
