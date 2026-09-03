"""D3: авто-аудит собранного ролика — «чужое лицо/страна/эпоха» в финале.

Контактные листы 4×5 из финальных googler/NNN.jpg (подпись: сцена/тип/subject),
на каждый лист ОДИН vision-вызов сильной модели (judge.final_check.model,
effort low): «trash только за чужое узнаваемое лицо/страну/эпоху, цвет в ч/б
ряду, нечитаемый кадр». Отчёт parser/audit.json + таблица в лог.
Стоимость: 181 сцена = 10 листов = 10 вызовов ≈ $0.03.

--fix: trash-сцены → router._cluster_broll_fill (named/person/exact — ТОЛЬКО
в ai: чужой кадр в named мимо face-гейта недопустим), не нашлось → статус ai
(+ A0-чистка кадра). После fix — merged/report/replace_scene_usage обновлены
и review пересобран (та же спецификация замены, что у C1). Идемпотентность:
повторный audit без trash — ноль замен.
"""
import json
import os
import time

from .llm import chat, extract_json, image_part

SHEET_COLS = 4
SHEET_ROWS = 5

AUDIT_PROMPT = """You audit the FINAL visual row of a {niche_desc} YouTube documentary.
The image is a contact sheet of chosen frames; each cell caption reads
"NNN type subject" where NNN is the scene number.
Scenes on this sheet: {scene_list}.

Mark a scene "trash" ONLY for one of:
- a recognizable historical figure who is clearly NOT the captioned person/subject;
- clearly wrong country/theater for the caption (architecture, uniforms, signage);
- clearly wrong historical era (different decade/war);
- a color frame inside the black-and-white archival row;
- an unreadable, broken or blank frame.
Anything else is "ok" — loose thematic era b-roll is standard practice in
fast-cut documentaries. When unsure, answer "ok".

Reply ONLY a JSON array covering EVERY listed scene:
[{{"scene": <number>, "verdict": "trash|ok", "reason": "<max 8 words>"}}, ...]"""


def _sheet_image(entries: list, out_path: str, cell_w: int = 320,
                 cell_h: int = 200) -> str:
    """Контактный лист 4×5: сетка кадров с подписью «NNN тип subject».
    Битая картинка лист не срывает (пустая ячейка с подписью)."""
    from PIL import Image, ImageDraw, ImageOps
    cap_h = 20
    rows_n = (len(entries) + SHEET_COLS - 1) // SHEET_COLS
    sheet = Image.new("RGB", (SHEET_COLS * cell_w, rows_n * (cell_h + cap_h)),
                      "black")
    draw = ImageDraw.Draw(sheet)
    for k, (num, img_path, caption) in enumerate(entries):
        x = (k % SHEET_COLS) * cell_w
        y = (k // SHEET_COLS) * (cell_h + cap_h)
        try:
            img = Image.open(img_path).convert("RGB")
            sheet.paste(ImageOps.fit(img, (cell_w, cell_h)), (x, y))
        except Exception:  # noqa: BLE001
            pass
        draw.text((x + 4, y + cell_h + 4), f"{num:03d} {caption}"[:52],
                  fill="white")
    tmp = out_path + ".tmp"
    sheet.save(tmp, "JPEG", quality=88)
    os.replace(tmp, out_path)
    return out_path


def build_sheets(entries: list, out_dir: str) -> list:
    """entries: [(num_1based, img_path, caption)] -> [(sheet_path, [num...])]."""
    os.makedirs(out_dir, exist_ok=True)
    per_sheet = SHEET_COLS * SHEET_ROWS
    out = []
    for s, start in enumerate(range(0, len(entries), per_sheet)):
        chunk = entries[start:start + per_sheet]
        path = _sheet_image(chunk, os.path.join(out_dir, f"sheet_{s + 1:02d}.jpg"))
        out.append((path, [num for num, _p, _c in chunk]))
    return out


def _judge_sheet(sheet_path: str, scene_nums: list, niche: dict, log=print) -> dict:
    """Один vision-вызов на лист. -> {num_1based: {"verdict","reason"}}.
    Ошибка вызова/парсинга = все сцены листа «ok» (аудит fail-open:
    не размечает мусором то, чего не видел)."""
    fc = (niche.get("judge", {}) or {}).get("final_check", {}) or {}
    prompt = AUDIT_PROMPT.format(
        niche_desc=niche.get("description", ""),
        scene_list=", ".join(str(n) for n in scene_nums))
    try:
        raw = chat(fc.get("model", "gpt-5-mini"),
                   [{"role": "user", "content": [
                       {"type": "text", "text": prompt},
                       image_part(sheet_path)]}],
                   reasoning_effort="low", max_tokens=3000)
        parsed = extract_json(raw)
        if isinstance(parsed, dict):
            parsed = parsed.get("scenes") or parsed.get("results") or []
        if not isinstance(parsed, list):
            raise ValueError("audit sheet reply is not a JSON array")
    except Exception as e:  # noqa: BLE001 — один лист не срывает аудит
        log(f"⚠ лист {os.path.basename(sheet_path)}: vision не ответил ({e}) "
            f"— сцены листа считаю ok")
        return {n: {"verdict": "ok", "reason": "audit-error"} for n in scene_nums}
    out = {n: {"verdict": "ok", "reason": ""} for n in scene_nums}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            num = int(item.get("scene"))
        except (TypeError, ValueError):
            continue
        if num not in out:
            continue
        verdict = "trash" if item.get("verdict") == "trash" else "ok"
        out[num] = {"verdict": verdict, "reason": str(item.get("reason", ""))[:80]}
    return out


def _apply_fixes(project: str, niche: dict, merged: dict, trash: list,
                 scenes: list, log=print) -> dict:
    """--fix по спецификации замены C1: merged/report обновляются вызывающим,
    usage через replace_scene_usage, review пересобирается после."""
    import parser_worker as pw  # ленивый: audit живёт в пакете parser
    from .classify import classify_scenes
    from .config import load_keys
    from .media_output import deactivate_parser_video
    from .router import Router
    lib = None
    try:
        lib = pw._open_library(niche)
    except Exception as e:  # noqa: BLE001 — fix работает и без библиотеки
        log(f"⚠ библиотека недоступна: {e} — cluster-замен не будет")
    # classify закэширован основным прогоном — здесь это бесплатное чтение
    cls_all = classify_scenes(scenes, niche, project, log=log)
    usage_project = os.path.basename(project)
    router = Router(niche, load_keys(), log=log, library=lib,
                    project=usage_project, no_usage_writes=True)
    fixed = {"broll": 0, "ai": 0}
    for idx in trash:
        rec = merged.setdefault(idx, {"scene": idx})
        scene = scenes[idx] if idx < len(scenes) else {}
        cls = cls_all[idx] if idx < len(cls_all) else {}
        out_img = os.path.join(project, "googler", f"{idx + 1:03d}.jpg")
        named = bool(cls.get("person") or cls.get("type") == "named"
                     or cls.get("claim_level") == "exact")
        replaced = False
        if not named and lib is not None:
            result = {"scene": idx, "judged": [], "chosen": None, "status": "none"}
            try:
                found = router._cluster_broll_fill(idx, scene, cls, None, result)
            except Exception as e:  # noqa: BLE001
                log(f"[{idx + 1:03d}] cluster-замена упала: {e}")
                found = False
            if found:
                saved = pw.save_final_media(
                    router, result["chosen"], project, idx, out_img,
                    scene=scene, cls=cls, queries=(cls.get("queries") or [])[:3],
                    niche=niche)
                if saved:
                    pw._commit_scene_usage(lib, True, result["chosen"],
                                           usage_project, idx)
                    rec["status"] = "soft"  # жёлтым в галерею — оператор видит
                    rec["chosen"] = result["chosen"]
                    rec["via"] = "audit_fix"
                    rec.setdefault("judged", []).extend(result.get("judged") or [])
                    fixed["broll"] += 1
                    replaced = True
                    log(f"[{idx + 1:03d}] trash → cluster b-roll "
                        f"{result['chosen'].get('uid')}")
                else:
                    router.release_pick(result, idx)
        if not replaced:
            # named — ТОЛЬКО в ai (чужой кадр мимо face-гейта недопустим);
            # либо замены не нашлось: статус ai + A0-чистка кадра
            deactivate_parser_video(project, idx)
            if os.path.exists(out_img):
                os.remove(out_img)
            pw._commit_scene_usage(lib, True, None, usage_project, idx)
            rec["status"], rec["chosen"] = "ai", None
            rec["via"] = "audit_fix"
            fixed["ai"] += 1
            log(f"[{idx + 1:03d}] trash → ai (кадр удалён"
                f"{', named' if named else ', замены нет'})")
    return fixed


def run_audit(project: str, niche: dict, fix: bool = False, log=print) -> int:
    """Команда `parser_worker audit --project P [--fix]`. -> exit code."""
    import parser_worker as pw
    project = os.path.abspath(project)
    parser_dir = os.path.join(project, "parser")
    googler_dir = os.path.join(project, "googler")
    rep_path = os.path.join(parser_dir, "report.json")
    if not os.path.exists(rep_path):
        log(f"❌ Нет {rep_path} — сначала прогон run")
        return 2
    report = json.load(open(rep_path))
    merged = {r["scene"]: r for r in report.get("scenes", [])}
    scenes_path = os.path.join(googler_dir, "scenes.json")
    scenes = json.load(open(scenes_path)) if os.path.exists(scenes_path) else []

    # аудитим весь финальный ряд: каждый существующий googler/NNN.jpg
    entries = []
    n_scenes = max(len(scenes), (max(merged) + 1) if merged else 0)
    for idx in range(n_scenes):
        img = os.path.join(googler_dir, f"{idx + 1:03d}.jpg")
        if not os.path.exists(img):
            continue
        rec = merged.get(idx) or {}
        subject = str((scenes[idx] if idx < len(scenes) else {}).get(
            "subject", ""))[:28]
        entries.append((idx + 1, img, f"{rec.get('type', '?')} {subject}"))
    if not entries:
        log("❌ Нет финальных кадров googler/NNN.jpg — аудитить нечего")
        return 2

    sheets = build_sheets(entries, os.path.join(parser_dir, "audit_sheets"))
    log(f"▶ Аудит: {len(entries)} кадров, листов {len(sheets)} "
        f"(1 vision-вызов на лист)")
    results, trash = {}, []
    for sheet_path, nums in sheets:
        verdicts = _judge_sheet(sheet_path, nums, niche, log=log)
        for num, v in sorted(verdicts.items()):
            idx = num - 1
            results[idx] = v
            if v["verdict"] == "trash":
                trash.append(idx)
                log(f"  [{num:03d}] TRASH: {v['reason']}")
    trash.sort()

    payload = {"project": project, "niche": niche.get("name"),
               "created_at": time.time(), "audited": len(entries),
               "trash": [{"scene": i, "reason": results[i]["reason"]}
                         for i in trash],
               "results": {str(i): v for i, v in sorted(results.items())}}
    fixed = None
    if fix and trash:
        fixed = _apply_fixes(project, niche, merged, trash, scenes, log=log)
        payload["fix"] = fixed
        report["scenes"] = [merged[k] for k in sorted(merged)]
        pw._rewrite_report_and_attrs(report, rep_path, parser_dir)
        try:
            from .gallery import write_review
            write_review(project, log=log)
        except Exception as e:  # noqa: BLE001 — review не критичен для fix
            log(f"⚠ review не пересобрался: {e}")
    elif fix:
        log("▶ trash-сцен нет — замен ноль (идемпотентно)")

    aud_path = os.path.join(parser_dir, "audit.json")
    tmp = aud_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, aud_path)

    log("\n===== АУДИТ =====")
    log(f"кадров: {len(entries)} | trash: {len(trash)}"
        + (f" | fix: {fixed['broll']} → cluster b-roll, {fixed['ai']} → ai"
           if fixed else ""))
    log(f"отчёт: {aud_path}")
    if fixed and fixed["ai"]:
        log(f"⚠ ОБЯЗАТЕЛЬНО перед рендером: fill-ai — {fixed['ai']} сцен "
            f"ушли в ai без кадра")
    return 0
