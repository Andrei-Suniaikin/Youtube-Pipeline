#!/usr/bin/env python3
"""parser_worker — подбор РЕАЛЬНОГО визуала (архивы/стоки) по scenes.json.

Кладёт выбранные картинки в <project>/googler/NNN.jpg (контракт рендера),
отчёт в <project>/parser/report.json, атрибуции в <project>/parser/attributions.txt.
Сцены без подобранного материала помечаются под AI-фолбэк (googler_worker images).

Примеры:
  python3 parser_worker.py run --project /path/to/p_xxx --niche ww2 --first_n 10
  python3 parser_worker.py run --project /path/to/p_xxx --overwrite
"""
import argparse
import concurrent.futures as cf
import io
import json
import os
import sys
import threading
import time
from types import SimpleNamespace

from parser import broll
from parser.classify import classify_scenes
from parser.config import load_keys, load_niche
from parser.judge import final_check, judge_scene_candidates
from parser.llm import merge_usage, reset_usage, usage_snapshot
from parser.media_output import (activate_video, deactivate_parser_video, discard_staged,
                                 scene_has_parser_video, stage_video)
from parser.placeholders import build_placeholder, placeholder_allowed
from parser.router import Router

MAX_EDGE = 2560  # ресайз финальных картинок (KB-апскейл рендера смотрит сам)


def log(msg):
    print(msg, flush=True)


def save_final_image(router: Router, chosen: dict, out_path: str) -> bool:
    """Качаем полноразмер (фолбэк — тумба), приводим к JPG<=MAX_EDGE."""
    from PIL import Image
    raw = None
    src = chosen.get("full_url")
    if src:
        try:
            conn = router._conns.get(chosen["source"])
            raw = conn.fetch_bytes(src)  # глобальный лимитер ~1 req/s + бэкофф на 429
        except Exception as e:  # noqa: BLE001
            log(f"    полноразмер не скачался ({e}) — беру тумбу")
            raw = None
    if raw is None:
        try:
            raw = open(chosen["thumb_path"], "rb").read()
        except Exception as e:  # noqa: BLE001
            log(f"    и тумба не читается: {e}")
            return False
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        w, h = img.size
        scale = MAX_EDGE / max(w, h)
        if scale < 1:
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        tmp = out_path + ".tmp"
        img.save(tmp, "JPEG", quality=88)
        os.replace(tmp, out_path)
        return True
    except Exception as e:  # noqa: BLE001
        log(f"    финал не сохранился: {e}")
        return False


def _candidate_from_chosen(chosen: dict):
    defaults = {"source": "", "page_url": "", "thumb_url": "", "full_url": "",
                "title": "", "license_raw": "", "license": "unknown", "author": "",
                "date_text": "", "query": "", "clip_score": 0.0, "extra": {},
                "media_type": "video", "mime_type": "", "duration": 0.0,
                "file_size": 0, "width": 0, "height": 0, "fps": 0.0}
    defaults.update(chosen)
    return SimpleNamespace(**defaults)


def _scene_context(scene: dict, cls: dict, queries: list) -> dict:
    return {"subject": cls.get("show") or scene.get("subject", ""),
            "visual_prompt": scene.get("text_segment", ""),
            "year": cls.get("year"), "region": cls.get("region"),
            "query": "; ".join(queries or []), "type": cls.get("type"),
            "person": cls.get("person"), "place": cls.get("place"),
            "country": cls.get("country"), "event": cls.get("event"),
            "claim_level": cls.get("claim_level", "context"),
            "exact_object": cls.get("exact_object"),
            "stock_ok": bool(cls.get("stock_ok"))}


def _video_needs_final_check(cls: dict, final_cfg: dict) -> bool:
    """Resolved people always need the strong identity check, regardless of scene type."""
    if not final_cfg.get("enabled"):
        return False
    check_types = set(final_cfg.get("types") or [])
    has_person = bool(str(cls.get("person") or "").strip())
    return has_person or not check_types or cls.get("type") in check_types


def _verify_staged_video(chosen: dict, check_path: str, scene: dict, cls: dict,
                         queries: list, niche: dict) -> tuple:
    """Judge a first/middle/last-frame sheet, not only the provider thumbnail."""
    if not (niche.get("video", {}) or {}).get("verify_contact_sheet", True):
        return True, "disabled"
    ctx = _scene_context(scene, cls, queries)
    cand = _candidate_from_chosen(chosen)
    final_cfg = (niche.get("judge", {}) or {}).get("final_check", {}) or {}
    if _video_needs_final_check(cls, final_cfg):
        return final_check(ctx, check_path, niche, log=log, cand=cand)
    judged = judge_scene_candidates(ctx, [(cand, check_path)], niche, log=log)[0]
    j_cfg = niche.get("judge", {}) or {}
    # Та же политика, что router._pick: глобальный accept_soft пускает soft в кадр,
    # иначе контакт-гейт детерминированно бракует клипы, которые роутер уже выбрал.
    allow_soft = bool(j_cfg.get("accept_soft", False)) or (
        bool(ctx.get("stock_ok")) and bool(j_cfg.get("accept_soft_stock", True)))
    accepted = judged.get("verdict") == "ok" or (
        judged.get("verdict") == "soft" and allow_soft)
    return accepted, judged.get("reason", "")


def save_final_media(router, chosen: dict, project: str, scene_idx: int,
                     out_img: str, scene: dict = None, cls: dict = None,
                     queries: list = None, niche: dict = None, manual: bool = False) -> bool:
    """Persist image or video and update renderer refs only after full validation."""
    if chosen.get("media_type", "image") != "video":
        if not save_final_image(router, chosen, out_img):
            return False
        deactivate_parser_video(project, scene_idx)
        chosen["local_path"] = out_img
        return True
    staged = None
    try:
        staged = stage_video(router, chosen, project, scene_idx, log=log)
        if not manual:
            ok, reason = _verify_staged_video(chosen, staged["check_path"], scene or {},
                                               cls or {}, queries or [], niche or {})
            if not ok:
                log(f"    video contact-sheet reject: {reason}")
                discard_staged(staged)
                return False
        final_path = activate_video(project, scene_idx, chosen, staged)
        chosen["local_path"] = final_path
        chosen["verification_path"] = staged["check_path"]
        chosen["duration"] = staged["facts"].get("duration", chosen.get("duration", 0))
        return True
    except Exception as e:  # noqa: BLE001
        if staged:
            discard_staged(staged)
        log(f"    видео не сохранилось: {e}")
        return False


def _usage_id_for_chosen(chosen: dict):
    """Return the durable material id; designed/synthetic cards are not reuse media."""
    chosen = chosen or {}
    extra = chosen.get("extra") or {}
    if (chosen.get("source") == "designed_placeholder"
            or chosen.get("placeholder_type") or extra.get("synthetic")):
        return None
    material_id = str(chosen.get("uid") or "").strip()
    return material_id or None


def _commit_scene_usage(library, saved: bool, chosen: dict, project: str, scene: int,
                        position: str = "final", log_fn=log) -> bool:
    """Commit usage only after media persistence; failures leave old usage intact."""
    if not saved or library is None:
        return False
    material_id = _usage_id_for_chosen(chosen)
    try:
        library.replace_scene_usage(material_id, project, scene, position)
        return True
    except Exception as e:  # noqa: BLE001 — usage telemetry must not break saved media
        log_fn(f"    usage не обновился для сцены {scene + 1:03d}: {e}")
        return False


def _load_report_state(rep_path: str) -> tuple:
    """Load scenes and the cumulative usage baseline exactly once per run."""
    merged = {}
    prior_usage = {}
    if os.path.exists(rep_path):
        try:
            with open(rep_path) as f:
                payload = json.load(f)
            for r in payload.get("scenes", []):
                merged[r["scene"]] = r
            # llm_usage was the only usage field in the legacy report schema.
            prior_usage = payload.get("llm_usage_total")
            if not isinstance(prior_usage, dict):
                prior_usage = payload.get("llm_usage") or {}
            if not isinstance(prior_usage, dict):
                prior_usage = {}
        except Exception as e:  # noqa: BLE001 — не терять улику молча
            corrupt = f"{rep_path}.corrupt.{int(time.time())}"
            os.replace(rep_path, corrupt)
            log(f"⚠ report.json битый ({e}) — сохранён как {corrupt}, начинаю чистый")
    return merged, prior_usage


def _load_merged(rep_path: str) -> dict:
    """Backward-compatible scenes-only report loader."""
    return _load_report_state(rep_path)[0]


def _write_merged(rep_path: str, parser_dir: str, project: str, niche_name: str,
                  merged: dict, report_id: str, prior_usage: dict = None):
    """Атомарная запись отчёта (stats пересчитаны из ВСЕХ сцен) + атрибуции."""
    merged_list = [merged[k] for k in sorted(merged)]
    stats = {"ok": 0, "soft": 0, "placeholder": 0,
             "ai": 0, "none": 0, "skipped": 0}
    for r in merged_list:
        key = "skipped" if r.get("status") == "skipped_exists" else r.get("status", "none")
        stats[key] = stats.get(key, 0) + 1
    run_usage = usage_snapshot()
    total_usage = merge_usage(prior_usage or {}, run_usage)
    tmp = rep_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"project": project, "niche": niche_name, "report_id": report_id,
                   "stats": stats,
                   # llm_usage remains as a cumulative alias for old readers.
                   "llm_usage": total_usage,
                   "llm_usage_run": run_usage,
                   "llm_usage_total": total_usage,
                   "scenes": merged_list}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, rep_path)
    attr_lines = [f"scene {r['scene'] + 1:03d}: {r['chosen']['attribution']}"
                  for r in merged_list if r.get("chosen")
                  and r["chosen"].get("attribution")]
    with open(os.path.join(parser_dir, "attributions.txt"), "w") as f:
        f.write("\n".join(attr_lines) + "\n")


def _cleanup_stale_ai_files(googler_dir: str, run_status: dict, merged: dict,
                            started_at: float, dry: bool = False, log_fn=log) -> int:
    """A0: удаление осиротевших кадров ai/none-сцен ЭТОГО прогона.

    `ai`-сцены НЕ пишут файл, поэтому в googler/NNN.jpg может остаться кадр
    СТАРОГО прогона (включая старые карточки) — рендер его молча подхватит.
    Кандидат на удаление обязан пройти ТРИ условия:
    (а) сцена реально обрабатывалась в этом прогоне — есть в run_status,
        т.е. входила в todo (не skipped и не вне --first_n);
    (б) итог этого прогона ai/none — кадра у сцены быть не должно;
    (в) файл записан ДО старта прогона (mtime < started_at), т.е. это хвост
        старого прогона, а не свежая запись.
    Сцены ВНЕ run_status не трогать НИКОГДА: fill-ai пишет googler/NNN.jpg,
    но статус в report.json остаётся `ai` — такой файл = валидный результат
    fill-ai, чистка по merged-статусу из старого отчёта запрещена.
    В --dry ничего не удаляется.
    """
    if dry:
        return 0
    removed = 0
    for i, status in sorted(run_status.items()):
        if status not in ("ai", "none"):
            continue
        out_img = os.path.join(googler_dir, f"{i + 1:03d}.jpg")
        try:
            if not os.path.exists(out_img) or os.path.getmtime(out_img) >= started_at:
                continue  # файла нет либо он записан этим прогоном — не трогать
            os.remove(out_img)
        except OSError as e:
            log_fn(f"[{i + 1:03d}] ⚠ осиротевший кадр не удалился: {e}")
            continue
        removed += 1
        if i in merged:
            merged[i]["stale_file_removed"] = True
        log_fn(f"[{i + 1:03d}] удалён осиротевший кадр старого прогона "
               f"(статус {status})")
    if removed:
        log_fn(f"▶ гигиена: удалено осиротевших кадров старых прогонов: {removed}")
    return removed


def _lib_row_chosen(row: dict, score: float) -> dict:
    """chosen-запись для клипа из библиотеки (та же форма, что router._chosen_rec)."""
    return {"uid": row["id"], "source": row["source"], "title": row["title"],
            "page_url": row["page_url"], "full_url": row["full_url"],
            "thumb_path": row["path"], "license": row["license"],
            "license_raw": row["license_raw"], "author": row["author"],
            "clip": round(float(score), 4),
            "media_type": row.get("media_type") or "video",
            "mime_type": row.get("mime_type") or "",
            "duration": float(row.get("duration") or 0),
            "file_size": int(row.get("file_size") or 0),
            "extra": row.get("extra") or {},
            "attribution": " · ".join(p for p in (
                row.get("title") or row.get("page_url"), row.get("author"),
                row.get("license_raw") or row.get("license"),
                row.get("page_url")) if p)}


def _insert_library_clip(router, lib, niche, project, idx, scene, cls, row,
                         score, merged, reason, via, log_fn=log) -> bool:
    """Общая спецификация замены кадра на клип (C1/C2, ревью плана):
    (а) статус/chosen обновляются в merged — report.json переписывает вызывающий;
    (б) usage через replace_scene_usage — старый кадр не остаётся навсегда
        «использованным» (дрейф reuse-лимитов);
    (в) googler/NNN.jpg НЕ удаляется: ref в googler_video_references его
        легитимно перекрывает для рендера;
    (г) contact-sheet суд, stage/activate и манифест — существующие механизмы
        save_final_media/stage_video, код вставки не дублируется."""
    chosen = _lib_row_chosen(row, score)
    out_img = os.path.join(project, "googler", f"{idx + 1:03d}.jpg")
    saved = save_final_media(router, chosen, project, idx, out_img,
                             scene=scene, cls=cls,
                             queries=(cls.get("queries") or [])[:3], niche=niche)
    if not saved:
        return False
    _commit_scene_usage(lib, True, chosen, os.path.basename(project), idx,
                        log_fn=log_fn)
    router._mark_used(row["id"], idx)
    chosen["reason"] = reason
    rec = merged.setdefault(idx, {"scene": idx})
    rec["status"] = "soft"  # всегда жёлтым в галерею — оператор видит замену
    rec["chosen"] = chosen
    rec["via"] = via
    return True


def _postfill_videos(router, lib, niche, project, scenes, cls_all, merged,
                     log_fn=log) -> int:
    """C1: пост-добор видео до video.min_videos_per_project.

    Хук-буст (video.hook_scenes) уже поднял видео-квоту библиотеки в резолве;
    здесь добираем клипы, если их в манифесте меньше минимума: motion-сцены
    БЕЗ видео в порядке убывания CLIP-близости к клипам библиотеки, каждый
    кандидат проходит существующий contact-sheet суд внутри save_final_media.
    Идемпотентность: при закрытом минимуме — 0 замен и 0 LLM-вызовов.
    Бюджет LLM: не больше min(deficit + 2, video.postfill_max_checks) судов."""
    vcfg = niche.get("video", {}) or {}
    min_videos = int(vcfg.get("min_videos_per_project", 4))
    if lib is None or min_videos <= 0:
        return 0
    have = {i for i in range(len(scenes)) if scene_has_parser_video(project, i)}
    deficit = min_videos - len(have)
    if deficit <= 0:
        return 0  # идемпотентность: повторный пасс на добранном проекте = 0 замен
    from parser.clip_rank import embed_text
    lib_cfg = niche.get("library", {}) or {}
    hook_scenes = int(vcfg.get("hook_scenes", 0) or 0)
    hook_target = int(vcfg.get("hook_video_target", 0) or 0)
    hook_have = sum(1 for i in have if i < hook_scenes)
    candidates = []
    for i, scene in enumerate(scenes):
        cls = cls_all[i]
        if i in have or not cls.get("motion"):
            continue
        if (cls.get("person") or cls.get("type") == "named"
                or cls.get("claim_level") == "exact"):
            continue  # чужой клип в named/exact мимо face-контроля недопустим
        if (merged.get(i) or {}).get("status") == "skipped_exists":
            continue  # сцену этот прогон не трогал — не наша
        show = cls.get("show") or scene.get("subject", "")
        queries = cls.get("queries") or []
        temb = embed_text(f'{show}. {queries[0] if queries else ""}. '
                          f'historical wartime photograph', niche)
        hits = [(row, s) for row, s in lib.search(
                    temb, top_k=int(lib_cfg.get("video_top_k", 3) or 3),
                    min_score=float(lib_cfg.get("video_min_score", 0.2)),
                    niche=niche.get("name"), media_type="video")
                if not router._used_elsewhere(row["id"], i)]
        if hits:
            candidates.append((i, scene, cls, hits[0], float(hits[0][1])))
    # хук-приоритет: пока в первых hook_scenes сценах видео меньше
    # hook_video_target, хук-сцены добираются первыми; внутри группы — по CLIP
    candidates.sort(key=lambda c: (
        0 if (c[0] < hook_scenes and hook_have < hook_target) else 1, -c[4]))
    inserted = attempts = 0
    max_attempts = min(deficit + 2, int(vcfg.get("postfill_max_checks", 6)))
    for i, scene, cls, (row, score), _s in candidates:
        if inserted >= deficit or attempts >= max_attempts:
            break
        attempts += 1
        if _insert_library_clip(router, lib, niche, project, i, scene, cls,
                                row, score, merged,
                                reason="video post-fill (хук/минимум клипов)",
                                via="video_postfill", log_fn=log_fn):
            inserted += 1
            if i < hook_scenes:
                hook_have += 1
            log_fn(f"[{i + 1:03d}] пост-добор видео: клип {row['id']} "
                   f"(clip={float(score):.2f})")
    if inserted or attempts:
        log_fn(f"▶ пост-добор видео: +{inserted} клипов "
               f"(дефицит был {deficit}, судов {attempts})")
    return inserted


def _rhythm_clip_for_scene(router, lib, niche, scene, cls, idx):
    """C2: клип библиотеки под кластер сцены с ok/soft-вердиктом из ЯВНОГО
    списка допустимых ctx (cached_verdict_any — старые версии судьи не воскресают).
    -> (row, score) | None."""
    from parser.clip_rank import embed_text
    from parser.clusters import scene_cluster
    from parser.judge import JUDGE_VERSION
    from parser.library import make_ctx_hash
    cl = scene_cluster(cls)
    if not cl:
        return None
    b_cfg = niche.get("broll", {}) or {}
    show = cls.get("show") or scene.get("subject", "")
    queries = cls.get("queries") or []
    temb = embed_text(f'{show}. {queries[0] if queries else ""}. '
                      f'historical wartime photograph', niche)
    allowed_ctx = [
        make_ctx_hash(f"__staple_era__|{JUDGE_VERSION}", ""),
        make_ctx_hash(f"__staple_video__|{JUDGE_VERSION}", ""),
        make_ctx_hash(f"{show}|{JUDGE_VERSION}", queries[0] if queries else ""),
    ]
    hits = lib.search(temb, top_k=int(b_cfg.get("cluster_video_top_k", 3)),
                      min_score=float(b_cfg.get("cluster_video_min_score", 0.20)),
                      niche=niche.get("name"), media_type="video")
    for row, score in hits:
        if cl not in ((row.get("extra") or {}).get("clusters") or []):
            continue
        if router._used_elsewhere(row["id"], idx):
            continue
        cached = lib.cached_verdict_any(row["id"], allowed_ctx)
        if not cached or cached["verdict"] not in ("ok", "soft"):
            continue
        return row, float(score)
    return None


def _rhythm_swap_videos(router, lib, niche, project, scenes, cls_all, merged,
                        log_fn=log) -> int:
    """C2: ритм-контроль — рвать серии статичных финалов живым клипом.

    Скользящее окно по итоговой раскладке: при video.rhythm_static_window
    (дефолт 5) статичных сцен подряд заменяется ОДНА сцена окна — средняя
    по позиции, не named/person/exact — на клип библиотеки с ok/soft-вердиктом
    кластера сцены. Вставка — тот же путь, что C1 (contact-sheet суд, манифест,
    replace_scene_usage). Кап замен на ролик: video.rhythm_max_swaps.
    Бюджет LLM: не больше video.rhythm_max_checks (дефолт 2×max_swaps)
    попыток вставки (каждая = stage+contact-sheet суд); забракованная судом
    пара (сцена, клип) запоминается и не пересуживается в соседних окнах."""
    vcfg = niche.get("video", {}) or {}
    max_swaps = int(vcfg.get("rhythm_max_swaps", 6))
    window = max(2, int(vcfg.get("rhythm_static_window", 5)))
    if lib is None or max_swaps <= 0 or not hasattr(lib, "cached_verdict_any"):
        return 0
    max_checks = max(max_swaps, int(vcfg.get("rhythm_max_checks",
                                             2 * max_swaps)))
    rejected = set()  # (scene_idx, clip_id) — contact-sheet reject, не пересуживать
    swaps = attempts = 0
    while swaps < max_swaps and attempts < max_checks:
        swap_done = False
        streak = []
        for i in range(len(scenes)):
            if scene_has_parser_video(project, i):
                streak = []
                continue
            streak.append(i)
            if len(streak) < window:
                continue
            win = streak[-window:]  # окно: последние window статичных подряд
            mid = win[len(win) // 2]
            for j in sorted(win, key=lambda x: (abs(x - mid), x)):
                cls = cls_all[j]
                if (cls.get("person") or cls.get("type") == "named"
                        or cls.get("claim_level") == "exact"):
                    continue  # named/exact не трогаем (спецификация C2)
                pick = _rhythm_clip_for_scene(router, lib, niche, scenes[j],
                                              cls, j)
                if not pick:
                    continue
                row, score = pick
                if (j, row["id"]) in rejected:
                    continue  # уже забракован судом — не жечь LLM повторно
                attempts += 1
                if _insert_library_clip(router, lib, niche, project, j,
                                        scenes[j], cls, row, score, merged,
                                        reason="rhythm clip swap (ритм-контроль)",
                                        via="rhythm_swap", log_fn=log_fn):
                    log_fn(f"[ритм] сцена {j + 1}: статика → клип {row['id']}")
                    swaps += 1
                    swap_done = True
                    break
                rejected.add((j, row["id"]))
                if attempts >= max_checks:
                    break  # бюджет судов исчерпан
            if swap_done or attempts >= max_checks:
                break  # замена меняет раскладку / бюджет кончился — скан заново
        if not swap_done:
            break
    if swaps or attempts:
        log_fn(f"▶ ритм-контроль: {swaps} замен статики на клипы "
               f"(судов {attempts})")
    return swaps


def cmd_run(args):
    reset_usage()
    project = os.path.abspath(args.project)
    googler_dir = os.path.join(project, "googler")
    scenes_path = os.path.join(googler_dir, "scenes.json")
    if not os.path.exists(scenes_path):
        log(f"❌ Нет {scenes_path} — сначала нужен план сцен (googler_worker prompts)")
        return 2

    parser_dir = os.path.join(project, "parser")
    rep_path = os.path.join(parser_dir, "report.json")
    merged, prior_usage = _load_report_state(rep_path)

    niche = load_niche(args.niche)
    keys = load_keys()
    lib = None
    lib_cfg = niche.get("library", {})
    if lib_cfg.get("enabled"):
        from parser.library import Library
        try:
            lib = Library(lib_cfg.get("path", "~/VideoStudio/parser_library"))
            log(f"▶ Библиотека: {lib.root} ({lib.stats()['materials']} материалов)")
            # G: диагностика кластерного пула — «мало real» чаще всего = «пул пустой»
            pool = lib.cluster_pool_stats(niche.get("name"))
            log(f"▶ Кластерный пул '{niche.get('name')}': {pool['images']} фото + "
                f"{pool['videos']} клипов"
                + ("" if pool["total"] else
                   f" — ПУСТО, запусти: parser_worker.py warm --niche {niche.get('name')}"))
        except Exception as e:  # noqa: BLE001
            log(f"⚠ Библиотека недоступна: {e} — работаем без реюза")
    scenes = json.load(open(scenes_path))
    if args.first_n:
        scenes_slice = scenes[: args.first_n]
    else:
        scenes_slice = scenes
    log(f"▶ Проект: {project}")
    log(f"▶ Ниша: {niche['name']} | сцен: {len(scenes_slice)}/{len(scenes)}")

    # 1. Классификация (кэшируется). Тестовый --first_n не должен оплачивать и ждать
    # классификацию всего ролика; полный запуск сам инвалидирует короткий cache hash.
    cls_all = classify_scenes(scenes_slice, niche, project, log=log,
                              force=args.force_classify)

    # 2. Каскад по сценам
    usage_project = os.path.basename(project)
    # Router still reserves picks in memory for parallel anti-repeat, while durable
    # usage is committed below only after save_final_media succeeds.
    router = Router(niche, keys, log=log, library=lib,
                    project=usage_project, no_usage_writes=True)
    cand_dir = os.path.join(parser_dir, "candidates")
    os.makedirs(cand_dir, exist_ok=True)
    report_id = f"{int(time.time())}-{os.getpid()}"

    # 2b. Тематический B-roll пул кампании (последняя ступень перед AI-фолбэком)
    router.broll_pool = broll.build_pool(
        niche, cls_all, keys, os.path.join(cand_dir, "_broll"), log=log)

    # 2c. Одноразовый прогрев библиотеки staple-клипами кинохроники (archive_movies).
    # B4: основной путь прогрева — команда `warm`; run греет только пока маркер
    # <library.root>/staple_video_done.json отсутствует И broll.warm_on_run
    # не выключен (дефолт true — первый прогон ведёт себя как раньше).
    if lib is not None and not args.dry:
        warm_on_run = bool((niche.get("broll", {}) or {}).get("warm_on_run", True))
        marker = os.path.join(lib.root, "staple_video_done.json")
        if warm_on_run and not os.path.exists(marker):
            try:
                broll.warm_staple_videos(niche, keys, lib,
                                         os.path.join(cand_dir, "_staple_movies"),
                                         log=log)
            except Exception as e:  # noqa: BLE001 — прогрев не роняет прогон
                log(f"⚠ staple-видео прогрев не удался: {e}")
        elif warm_on_run:
            log("▶ staple-видео уже прогреты (маркер есть) — добор: "
                "parser_worker.py warm --niche " + niche["name"])

    stats, t0, aborted = {"ok": 0, "soft": 0, "placeholder": 0,
                          "ai": 0, "none": 0, "skipped": 0}, time.time(), False
    ai_scenes = []
    run_status = {}  # A0: итоговые статусы сцен ИМЕННО этого прогона (только todo)
    workers = args.workers or int(niche.get("run", {}).get("scene_workers", 4))
    state_lock = threading.Lock()

    todo = []
    for i, scene in enumerate(scenes_slice):
        out_img = os.path.join(googler_dir, f"{i + 1:03d}.jpg")
        if (os.path.exists(out_img) or scene_has_parser_video(project, i)) and not args.overwrite:
            stats["skipped"] += 1
            merged.setdefault(i, {"scene": i, "status": "skipped_exists"})
        else:
            todo.append((i, scene))
    # Reserve scarce exact/person archive assets before generic contextual scenes.
    # Output filenames/report ordering still use the original scene index.
    def allocation_priority(item):
        i, _scene = item
        contract = cls_all[i]
        if contract.get("type") == "named":
            event = str(contract.get("event") or "").casefold()
            identity_only = (not event or any(token in event for token in (
                "official", "identity", "identification", "leadership", "authority",
                "historical figure", "person")))
            return (1 if identity_only else 0, i)
        if contract.get("person") and contract.get("claim_level") == "exact":
            return (2, i)
        if contract.get("claim_level") == "exact":
            return (3, i)
        return (4, i)

    todo.sort(key=allocation_priority)
    log(f"▶ В работу: {len(todo)} сцен (пропущено готовых: {stats['skipped']}), "
        f"параллельно: {workers}")

    def process_scene(i, scene):
        out_img = os.path.join(googler_dir, f"{i + 1:03d}.jpg")
        try:
            res = router.resolve_scene(i, scene, cls_all[i],
                                       os.path.join(cand_dir, f"{i:03d}"))
        except Exception as e:  # noqa: BLE001 — одна сцена не роняет прогон
            log(f"[{i + 1:03d}] ❌ сцена упала: {type(e).__name__}: {e} → AI-фолбэк")
            res = {"scene": i, "type": cls_all[i].get("type", "?"), "queries": [],
                   "n_candidates": 0, "judged": [], "chosen": None, "status": "ai"}
        ph_cfg = niche.get("placeholders", {}) or {}
        if (not res.get("chosen") and not args.dry
                and placeholder_allowed(cls_all[i], ph_cfg)):
            ph_path = os.path.join(parser_dir, "placeholders", f"{i + 1:03d}.jpg")
            placeholder = build_placeholder(cls_all[i], ph_path)
            if placeholder:
                res["chosen"], res["status"] = placeholder, "placeholder"
        if res["chosen"] and not args.dry:
            saved = save_final_media(router, res["chosen"], project, i, out_img,
                                     scene=scene, cls=cls_all[i],
                                     queries=res.get("queries") or [], niche=niche)
            if not saved:
                router.release_pick(res, i)  # кадр не встал — снять пометку/usage
                if os.path.exists(out_img) or scene_has_parser_video(project, i):
                    # старый файл на месте (--overwrite): не затирать его запись
                    log(f"[{i + 1:03d}] сохранение не удалось — оставляю старый кадр")
                    return i, {"scene": i, "status": "skipped_exists"}, True
                # Видеокандидат может пройти дешёвый shortlist, но не пройти
                # полноразмерный contact-sheet gate. Не оставляем после этого
                # дыру/AI-задачу, если для сцены можно честно собрать context card.
                ph_cfg = niche.get("placeholders", {}) or {}
                if placeholder_allowed(cls_all[i], ph_cfg):
                    ph_path = os.path.join(parser_dir, "placeholders", f"{i + 1:03d}.jpg")
                    placeholder = build_placeholder(cls_all[i], ph_path)
                    if placeholder:
                        res["chosen"], res["status"] = placeholder, "placeholder"
                        saved = save_final_media(
                            router, placeholder, project, i, out_img,
                            scene=scene, cls=cls_all[i],
                            queries=res.get("queries") or [], niche=niche)
                        if saved:
                            log(f"  [{i}] rejected media → designed fallback")
                if not saved:
                    res["status"] = "ai"
                    res["chosen"] = None
            if saved:
                _commit_scene_usage(lib, True, res["chosen"], usage_project, i)
        return i, res, False

    done_n = 0
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(process_scene, i, sc): i for i, sc in todo}
        for fut in cf.as_completed(futs):
            i, res, kept_old = fut.result()
            done_n += 1
            with state_lock:
                if kept_old:
                    stats["skipped"] += 1
                    merged.setdefault(i, res)
                else:
                    stats[res["status"]] = stats.get(res["status"], 0) + 1
                    if res["status"] == "ai":
                        ai_scenes.append(str(i + 1))
                    merged[i] = res
                    run_status[i] = res["status"]
                if not args.dry:
                    _write_merged(rep_path, parser_dir, project, niche["name"],
                                  merged, report_id, prior_usage)  # checkpoint per scene
            ch = (res.get("chosen") or {}) if not kept_old else {}
            log(f"[{done_n}/{len(todo)}] сцена {i + 1:03d}: {res['status']:4s} "
                f"тип={res.get('type', '?'):14s} канд={res.get('n_candidates', 0):2d} "
                f"{('← ' + ch.get('source', '') + '/' + ch.get('media_type', 'image') +
                    ' clip=' + str(ch.get('clip', ''))) if ch else ''}")
            if router.judge_errors >= 12 and router.judge_oks == 0:
                log("❌ Судья не вернул ни одного вердикта (ключ/сеть/квота OpenAI?) — "
                    "останавливаюсь, чтобы не слить весь прогон в AI-фолбэк")
                aborted = True
                for f in futs:
                    f.cancel()
                break

    # A0: осиротевшие кадры старых прогонов у ai/none-сцен этого прогона —
    # до финальной записи отчёта, чтобы флаг stale_file_removed попал в report.json.
    _cleanup_stale_ai_files(googler_dir, run_status, merged, t0, dry=args.dry)

    # C1/C2: видео-пост-пассы (минимум клипов + ритм-контроль) — до финальной
    # записи отчёта, чтобы статусы замен попали в report.json. Стоят LLM
    # (contact-sheet суды) — бюджеты в video.postfill_max_checks и
    # video.rhythm_max_swaps/rhythm_max_checks.
    if not args.dry and not aborted and lib is not None:
        try:
            _postfill_videos(router, lib, niche, project, scenes_slice, cls_all,
                             merged)
        except Exception as e:  # noqa: BLE001 — пост-добор не роняет прогон
            log(f"⚠ пост-добор видео не удался: {e}")
        try:
            _rhythm_swap_videos(router, lib, niche, project, scenes_slice,
                                cls_all, merged)
        except Exception as e:  # noqa: BLE001
            log(f"⚠ ритм-контроль не удался: {e}")

    if not args.dry:
        # Also persist classifier-only/fully-skipped runs and the final usage snapshot.
        _write_merged(rep_path, parser_dir, project, niche["name"],
                      merged, report_id, prior_usage)

    took = time.time() - t0
    total_done = sum(v for k, v in stats.items() if k != "skipped") or 1
    log("\n===== ИТОГ =====")
    log(f"ok={stats['ok']} soft={stats['soft']} placeholder={stats['placeholder']} ai={stats['ai']} "
        f"none={stats['none']} skipped={stats['skipped']} | {took / 60:.1f} мин"
        f"{' | DRY (ничего не записано)' if args.dry else ''}")
    log(f"доля реального материала: {(stats['ok'] + stats['soft']) / total_done * 100:.0f}% | "
        f"визуально закрыто: {(stats['ok'] + stats['soft'] + stats['placeholder']) / total_done * 100:.0f}%")
    if not args.dry:
        log(f"отчёт: {rep_path}")
        log(f"ревью: python3 parser_worker.py review --project {project}")
    if ai_scenes:
        log(f"под AI-фолбэк ({len(ai_scenes)} сцен): {','.join(ai_scenes[:20])}"
            f"{' …' if len(ai_scenes) > 20 else ''}")
        log(f"  → добить: python3 parser_worker.py fill-ai --project {project}")
    if not args.dry:
        # A1: fill-ai — обязательный шаг перед рендером; считаем по ВСЕМУ отчёту,
        # а не только по сценам этого прогона (--first_n/resume оставляют хвосты).
        ai_no_file = [k for k, r in sorted(merged.items())
                      if r.get("status") == "ai" and not os.path.exists(
                          os.path.join(googler_dir, f"{k + 1:03d}.jpg"))]
        if ai_no_file:
            log(f"⚠ ОБЯЗАТЕЛЬНО перед рендером: fill-ai — {len(ai_no_file)} ai-сцен "
                f"без кадра, без него рендер получит дыры вместо этих сцен")
    return 1 if aborted else 0


class _DirectConns(dict):
    """Шим под save_final_image: любой source качается напрямую (BaseConnector:
    UA + глобальный лимитер полноразмеров + бэкофф на 429)."""

    def __init__(self):
        super().__init__()
        from parser.connectors.base import BaseConnector
        self._conn = BaseConnector(keys={})

    def get(self, _name, _default=None):
        return self._conn


class _DirectRouter:
    """Мини-роутер для apply: без niche/keys, только скачивание."""

    def __init__(self):
        self._conns = _DirectConns()


def _guess_full_url(source: str, page_url: str):
    """Полноразмер без API: у Commons Special:FilePath редиректит на оригинал.
    Для остальных источников None → save_final_image возьмёт локальную тумбу."""
    if source in ("commons", "commons_cat") and "/wiki/File:" in (page_url or ""):
        fname = page_url.split("/wiki/File:", 1)[1]
        return "https://commons.wikimedia.org/wiki/Special:FilePath/" + fname
    return None


def _rewrite_report_and_attrs(report: dict, rep_path: str, parser_dir: str):
    """Пересчёт stats + атомарная запись отчёта + пересборка attributions.txt."""
    stats = {"ok": 0, "soft": 0, "placeholder": 0,
             "ai": 0, "none": 0, "skipped": 0}
    for r in report.get("scenes", []):
        key = "skipped" if r.get("status") == "skipped_exists" else r.get("status", "none")
        stats[key] = stats.get(key, 0) + 1
    report["stats"] = stats
    tmp = rep_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    os.replace(tmp, rep_path)
    attr_lines = [f"scene {r['scene'] + 1:03d}: {r['chosen']['attribution']}"
                  for r in report.get("scenes", []) if r.get("chosen")]
    with open(os.path.join(parser_dir, "attributions.txt"), "w") as f:
        f.write("\n".join(attr_lines) + "\n")


def cmd_apply(args):
    """Применяет decisions.json из review.html: pick/ai по сценам."""
    import shutil
    project = os.path.abspath(args.project)
    parser_dir = os.path.join(project, "parser")
    googler_dir = os.path.join(project, "googler")
    rep_path = os.path.join(parser_dir, "report.json")
    dec_path = args.decisions or os.path.join(parser_dir, "decisions.json")
    if not os.path.exists(rep_path):
        log(f"❌ Нет {rep_path}")
        return 2
    if not os.path.exists(dec_path):
        log(f"❌ Нет {dec_path} (экспортируй из review.html)")
        return 2
    report = json.load(open(rep_path))
    scenes = {r["scene"]: r for r in report.get("scenes", [])}
    usage_project = os.path.basename(project)
    niche_name = str(report.get("niche") or "ww2")
    apply_lib = None
    try:
        apply_niche = load_niche(niche_name)
        lib_cfg = apply_niche.get("library", {}) or {}
        if lib_cfg.get("enabled"):
            from parser.library import Library
            apply_lib = Library(lib_cfg.get("path", "~/VideoStudio/parser_library"))
            log(f"▶ apply library: {apply_lib.root}")
    except Exception as e:  # noqa: BLE001 — ручной apply работает и без библиотеки
        log(f"⚠ apply library недоступна: {e} — usage не обновится")
    raw_dec = json.load(open(dec_path))
    if isinstance(raw_dec, dict):  # новый формат: {"report_id":..., "decisions":[...]}
        decisions = raw_dec.get("decisions", [])
        dec_rid, rep_rid = raw_dec.get("report_id", ""), report.get("report_id", "")
        if dec_rid and rep_rid and dec_rid != rep_rid and not args.force:
            log(f"❌ decisions.json от ДРУГОЙ версии отчёта ({dec_rid} != {rep_rid}) — "
                f"после нового прогона перегенери review.html и разметь заново "
                f"(или --force, если уверен)")
            return 2
    else:  # легаси-формат: голый список
        decisions = raw_dec
        log("⚠ decisions.json без report_id (старый формат) — версию отчёта не проверяю")
    bak = f"{rep_path}.bak.{int(time.time())}"  # не затирать прошлые бэкапы
    shutil.copy2(rep_path, bak)
    log(f"▶ apply: {len(decisions)} решений из {dec_path} (бэкап: {bak})")

    router = _DirectRouter()
    applied = errors = 0
    interrupted = None
    try:
        for d in decisions:
            idx, action = d.get("scene"), d.get("action")
            r = scenes.get(idx)
            if r is None:
                log(f"[?] сцены {idx} нет в отчёте — пропуск")
                errors += 1
                continue
            out_img = os.path.join(googler_dir, f"{idx + 1:03d}.jpg")
            if action == "keep":
                continue
            if action == "ai":
                deactivate_parser_video(project, idx)
                if os.path.exists(out_img):
                    os.remove(out_img)
                _commit_scene_usage(apply_lib, True, None, usage_project, idx)
                r["status"], r["chosen"] = "ai", None
                applied += 1
                log(f"[{idx + 1:03d}] → AI (кадр удалён)")
            elif action == "pick":
                j = next((x for x in r.get("judged", []) if x["uid"] == d.get("uid")), None)
                if not j:
                    log(f"[{idx + 1:03d}] uid {d.get('uid')!r} не найден в judged — пропуск")
                    errors += 1
                    continue
                thumb = j.get("path") or os.path.join(
                    parser_dir, "candidates", f"{idx:03d}", j["uid"] + ".jpg")
                author = j.get("author", "")
                lic_raw = j.get("license_raw") or j["license"]
                chosen = {"uid": j["uid"], "source": j["source"], "title": j["title"],
                          "page_url": j["page_url"],
                          "full_url": j.get("full_url")
                                      or _guess_full_url(j["source"], j["page_url"]),
                          "thumb_path": thumb, "license": j["license"],
                          "license_raw": lic_raw, "author": author,
                          "clip": j.get("clip"),
                          "media_type": j.get("media_type", "image"),
                          "mime_type": j.get("mime_type", ""),
                          "duration": j.get("duration", 0.0),
                          "file_size": j.get("file_size", 0),
                          "extra": j.get("extra") or {},
                          "attribution": " · ".join(p for p in (
                              j["title"] or j["page_url"], author, lic_raw,
                              j["page_url"]) if p)}
                if not save_final_media(router, chosen, project, idx, out_img, manual=True):
                    log(f"[{idx + 1:03d}] не удалось сохранить кадр {j['uid']} — пропуск")
                    errors += 1
                    continue
                if apply_lib is not None:
                    try:
                        material_id = str(j.get("uid") or "").strip()
                        if not material_id or apply_lib.get(material_id) is None:
                            material_id = apply_lib.add(j, thumb, niche=niche_name)
                        chosen["uid"] = material_id
                        _commit_scene_usage(
                            apply_lib, True, chosen, usage_project, idx)
                    except Exception as e:  # noqa: BLE001 — media already saved; fail-soft
                        log(f"[{idx + 1:03d}] ⚠ library/usage не обновились: {e}")
                r["status"] = "ok" if j.get("verdict") == "ok" else "soft"
                r["chosen"] = chosen
                applied += 1
                log(f"[{idx + 1:03d}] → {j['uid']} ({j['source']}, {j.get('verdict')})")
                time.sleep(0.7)  # троттлинг скачиваний полноразмеров
            else:
                log(f"[{idx + 1:03d}] неизвестное действие {action!r} — пропуск")
                errors += 1
    except BaseException as e:  # noqa: BLE001 — отчёт обязан сесть на диск в любом случае
        interrupted = e
        import traceback
        traceback.print_exc()  # полный стек в лог — «тип без стека» недиагностируем (17.07)

    _rewrite_report_and_attrs(report, rep_path, parser_dir)
    if interrupted is not None:
        log(f"⚠ apply прерван ({type(interrupted).__name__}) — отчёт синхронизирован "
            f"с фактическим состоянием файлов, бэкап: {bak}")
        return 1
    log(f"Готово: применено {applied}, ошибок {errors}. "
        f"report.json и attributions.txt обновлены (бэкап: {bak})")
    ai_left = sum(1 for r in report["scenes"] if r.get("status") == "ai")
    if ai_left:
        log(f"AI-сцены ({ai_left}) добить: python3 parser/aifill.py --project {project}")
    return 0 if not errors else 1


def cmd_repair(args):
    """Перекачивает финалы, деградировавшие до тумбы (ширина < min_width) —
    например после 429 от Wikimedia на полноразмерах."""
    from PIL import Image
    project = os.path.abspath(args.project)
    rep_path = os.path.join(project, "parser", "report.json")
    if not os.path.exists(rep_path):
        log(f"❌ Нет {rep_path}")
        return 2
    report = json.load(open(rep_path))
    router = _DirectRouter()
    fixed = small = 0
    for r in report.get("scenes", []):
        ch = r.get("chosen")
        if not ch or not ch.get("full_url") or ch.get("media_type", "image") == "video":
            continue
        out_img = os.path.join(project, "googler", f"{r['scene'] + 1:03d}.jpg")
        if not os.path.exists(out_img):
            continue
        try:
            w = Image.open(out_img).size[0]
        except Exception:  # noqa: BLE001
            w = 0
        if w >= args.min_width:
            continue
        small += 1
        log(f"[{r['scene'] + 1:03d}] {w}px — перекачиваю оригинал")
        if save_final_image(router, ch, out_img):
            fixed += 1
    log(f"Деградировавших: {small}, перекачано: {fixed}")
    return 0


def cmd_review(args):
    """Генерирует parser/review.html для ручного ревью подбора."""
    from parser.gallery import write_review
    path = write_review(os.path.abspath(args.project), log=log)
    log(f"Ревью: {path}")
    log(f"Открыть: file://{path}  (или python3 -m http.server из <project>/parser/)")
    return 0


def _open_library(niche: dict):
    """Библиотека из профиля ниши; None если выключена/недоступна."""
    lib_cfg = niche.get("library", {}) or {}
    if not lib_cfg.get("enabled"):
        return None
    from parser.library import Library
    return Library(lib_cfg.get("path", "~/VideoStudio/parser_library"))


def cmd_warm(args):
    """B4: прогрев пула ниши БЕЗ прогона проекта: staple-клипы кинохроники
    (archive_movies) + фото по кластерным запросам (broll.cluster_queries).
    Цель пула: >=400 фото + >=40 клипов на нишу."""
    reset_usage()
    niche = load_niche(args.niche)
    keys = load_keys()
    try:
        lib = _open_library(niche)
    except Exception as e:  # noqa: BLE001
        log(f"❌ Библиотека недоступна: {e}")
        return 2
    if lib is None:
        log("❌ library.enabled=false в профиле ниши — прогревать некуда")
        return 2
    log(f"▶ Прогрев ниши {niche['name']}: {lib.root} "
        f"({lib.stats()['materials']} материалов)")
    workdir = os.path.join(lib.root, "warm_tmp")
    added_clips = 0
    if args.films != 0:  # --films 0 = пропустить фильмы (только фото)
        warm_niche = niche
        extra_queries = []
        if args.films < 0:  # C3b: очередь Universal Newsreels — только полный прогрев
            try:
                extra_queries = broll.newsreel_warm_queries(niche, keys, lib, log=log)
            except Exception as e:  # noqa: BLE001
                log(f"⚠ очередь newsreels не собралась: {e}")
        if args.films > 0 or extra_queries:
            import copy
            warm_niche = copy.deepcopy(niche)
            qs = (warm_niche.get("broll", {}) or {}).get(
                "staple_video_queries") or []
            if args.films > 0:  # кап: 1 запрос = 1 фильм (см. warm_staple_videos)
                qs = qs[: args.films]
            warm_niche.setdefault("broll", {})["staple_video_queries"] = \
                qs + extra_queries
        try:
            added_clips = broll.warm_staple_videos(
                warm_niche, keys, lib, os.path.join(workdir, "movies"), log=log)
        except Exception as e:  # noqa: BLE001 — фото-прогрев всё равно идёт
            log(f"⚠ staple-видео прогрев не удался: {e}")
        # C3b: YouTube CC-BY (yt-dlp) — тот же конвейер клипов, свой done-маркер
        try:
            added_clips += broll.warm_youtube_cc(
                niche, keys, lib, os.path.join(workdir, "youtube"), log=log)
        except Exception as e:  # noqa: BLE001
            log(f"⚠ youtube-cc прогрев не удался: {e}")
    added_photos = broll.warm_cluster_photos(
        niche, keys, lib, os.path.join(workdir, "photos"),
        max_images=args.images, log=log)
    st = lib.stats()
    log(f"▶ Прогрев готов: +{added_photos} фото, +{added_clips} клипов | "
        f"библиотека: {st['materials']} материалов")
    return 0


def cmd_reindex_clusters(args):
    """B2: назначить кластеры существующим материалам библиотеки (merge_extra;
    extra movie-клипов с тайм-кодами не перезаписывается)."""
    from parser.clusters import reindex_library
    niche = load_niche(args.niche)
    try:
        lib = _open_library(niche)
    except Exception as e:  # noqa: BLE001
        log(f"❌ Библиотека недоступна: {e}")
        return 2
    if lib is None:
        log("❌ library.enabled=false в профиле ниши")
        return 2
    log(f"▶ reindex-clusters: {lib.root} (ниша {niche['name']})")
    total, with_clusters = reindex_library(lib, niche=niche,
                                           only_missing=not args.force, log=log)
    if total and with_clusters / total < 0.9:
        log("⚠ с кластерами <90% материалов — проверь regex-таблицу/эмбеддинги")
        return 1
    return 0


def _gold_contact_sheet(rows: list, out_path: str, cols: int = 5,
                        cell_w: int = 320, cell_h: int = 200) -> str:
    """C3: контактный лист золотого фонда — сетка тумб с подписью uid
    (для ручного отбора юзером). Битая тумба лист не срывает."""
    from PIL import Image, ImageDraw, ImageOps
    cap_h = 20
    rows_n = (len(rows) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell_w, rows_n * (cell_h + cap_h)), "black")
    draw = ImageDraw.Draw(sheet)
    for k, row in enumerate(rows):
        x = (k % cols) * cell_w
        y = (k // cols) * (cell_h + cap_h)
        try:
            img = Image.open(row["path"]).convert("RGB")
            sheet.paste(ImageOps.fit(img, (cell_w, cell_h)), (x, y))
        except Exception:  # noqa: BLE001
            pass
        draw.text((x + 4, y + cell_h + 4),
                  f"{row['id']} {row.get('media_type') or 'image'}", fill="white")
    tmp = out_path + ".tmp"
    sheet.save(tmp, "JPEG", quality=88)
    os.replace(tmp, out_path)
    return out_path


def cmd_gold(args):
    """C3: золотой фонд — ручная пометка лучших материалов юзером (НЕ автоматика).

    gold: true кладётся в extra_json через merge_extra; буст +0.05 применяется
    ровно в двух точках скоринга: ранжирование кандидатов кластерного b-roll
    (router._cluster_broll_fill) и CLIP-ранг пула (broll.pick_from_pool)."""
    niche = load_niche(args.niche)
    try:
        lib = _open_library(niche)
    except Exception as e:  # noqa: BLE001
        log(f"❌ Библиотека недоступна: {e}")
        return 2
    if lib is None:
        log("❌ library.enabled=false в профиле ниши")
        return 2
    if args.add:
        n = 0
        for uid in [u.strip() for u in args.add.split(",") if u.strip()]:
            if lib.get(uid) is None:
                log(f"⚠ {uid}: материала нет в библиотеке — пропуск")
                continue
            lib.merge_extra(uid, {"gold": True})
            n += 1
            log(f"★ gold: {uid}")
        log(f"Помечено gold: {n}")
        return 0
    rows = [r for r, _e in lib.all_rows(niche=niche.get("name"))
            if (r.get("extra") or {}).get("gold")]
    if args.sheet:
        if not rows:
            log("Золотой фонд пуст — листа не будет")
            return 1
        path = _gold_contact_sheet(rows, os.path.abspath(args.sheet))
        log(f"Контактный лист gold ({len(rows)} материалов): {path}")
        return 0
    if not rows:
        log("Золотой фонд пуст: parser_worker.py gold --add uid1,uid2")
        return 0
    for r in rows:
        log(f"★ {r['id']} | {r.get('media_type') or 'image':5s} | "
            f"{(r.get('title') or '')[:60]} | {r.get('source')}")
    log(f"Всего gold: {len(rows)}")
    return 0


def cmd_audit(args):
    """D3: авто-аудит финального ряда (контактные листы + vision), --fix чинит
    trash-сцены: cluster b-roll, named → только ai (см. parser.audit)."""
    reset_usage()
    from parser.audit import run_audit
    niche = load_niche(args.niche)
    return run_audit(os.path.abspath(args.project), niche, fix=args.fix, log=log)


def cmd_fill(args):
    """Доливка AI-картинок для сцен, оставшихся под фолбэком (status=ai)."""
    from parser.aifill import fill_missing
    res = fill_missing(os.path.abspath(args.project), log=log, dry=args.dry,
                       style=args.style or None)
    log(f"Итог: недоставало {len(res['missing'])}, сгенерировано {len(res['generated'])}, "
        f"ошибок {len(res['errors'])}")
    return 0 if not res["errors"] else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="подобрать визуал по scenes.json")
    run.add_argument("--project", required=True, help="папка проекта (с googler/scenes.json)")
    run.add_argument("--niche", default="ww2")
    run.add_argument("--first_n", type=int, default=0, help="только первые N сцен (тест)")
    run.add_argument("--overwrite", action="store_true", help="перезаписывать существующие NNN.jpg")
    run.add_argument("--dry", action="store_true", help="не писать финальные картинки")
    run.add_argument("--force-classify", action="store_true", help="переклассифицировать сцены")
    run.add_argument("--workers", type=int, default=0,
                     help="сцен параллельно (дефолт: run.scene_workers из профиля ниши)")

    rev = sub.add_parser("review", help="сгенерировать parser/review.html для ручного ревью")
    rev.add_argument("--project", required=True, help="папка проекта (с parser/report.json)")
    app_ = sub.add_parser("apply", help="применить decisions.json из review.html")
    app_.add_argument("--project", required=True, help="папка проекта (с parser/report.json)")
    app_.add_argument("--decisions", default="",
                      help="путь к decisions.json (дефолт: <project>/parser/decisions.json)")
    app_.add_argument("--force", action="store_true",
                      help="применить решения даже от другой версии отчёта")
    fil = sub.add_parser("fill-ai", help="догенерить AI-картинки для сцен под фолбэком")
    fil.add_argument("--project", required=True)
    fil.add_argument("--dry", action="store_true", help="только показать недостающие")
    fil.add_argument("--style", default="", help="стиль исходного прогона (для seed)")
    rep = sub.add_parser("repair", help="перекачать финалы, деградировавшие до тумбы")
    rep.add_argument("--project", required=True)
    rep.add_argument("--min-width", type=int, default=800)
    wrm = sub.add_parser("warm", help="прогреть пул ниши: staple-клипы + фото по кластерам")
    wrm.add_argument("--niche", default="ww2")
    wrm.add_argument("--images", type=int, default=0,
                     help="кап добавленных фото (0 = без капа)")
    wrm.add_argument("--films", type=int, default=-1,
                     help="кап фильмов (~= запросов); 0 = только фото, -1 = все")
    rix = sub.add_parser("reindex-clusters",
                         help="назначить кластеры материалам библиотеки")
    rix.add_argument("--niche", default="ww2")
    rix.add_argument("--force", action="store_true",
                     help="пересчитать и уже размеченные материалы")
    gld = sub.add_parser("gold", help="золотой фонд: пометка/список/контактный лист")
    gld.add_argument("--niche", default="ww2")
    gld.add_argument("--add", default="", help="uid1,uid2 — пометить gold (руками)")
    gld.add_argument("--list", action="store_true", help="список gold-материалов")
    gld.add_argument("--sheet", default="", help="контактный лист gold в JPG")
    aud = sub.add_parser("audit",
                         help="авто-аудит финального ряда: листы 4×5 + vision")
    aud.add_argument("--project", required=True)
    aud.add_argument("--niche", default="ww2")
    aud.add_argument("--fix", action="store_true",
                     help="trash-сцены: cluster b-roll, named → только ai")

    args = ap.parse_args()
    if args.cmd == "run":
        sys.exit(cmd_run(args))
    elif args.cmd == "review":
        sys.exit(cmd_review(args))
    elif args.cmd == "apply":
        sys.exit(cmd_apply(args))
    elif args.cmd == "fill-ai":
        sys.exit(cmd_fill(args))
    elif args.cmd == "repair":
        sys.exit(cmd_repair(args))
    elif args.cmd == "warm":
        sys.exit(cmd_warm(args))
    elif args.cmd == "reindex-clusters":
        sys.exit(cmd_reindex_clusters(args))
    elif args.cmd == "gold":
        sys.exit(cmd_gold(args))
    elif args.cmd == "audit":
        sys.exit(cmd_audit(args))


if __name__ == "__main__":
    main()
