"""Двухступенчатый vision-судья: дешёвый основной gate + точечный final check.

Вердикты: ok | soft (сомнительно — в галерею) | reject.
При strict_era=true в профиле soft приравнивается к reject.
JUDGE_VERSION входит в ключ кэша вердиктов: смена промпта инвалидирует кэш.
"""
import concurrent.futures as cf

from .llm import chat, extract_json, image_part

JUDGE_VERSION = "v13-watermark-reject"  # 17.07 C3b: канальная вотермарка/плашка подписки = reject (YouTube-CC майнинг)

PROMPT = """You verify a candidate visual for a scene of a {niche_desc} YouTube video.
The audience includes history buffs who publicly call out any mismatch in comments.

SCENE: {subject}
NARRATION (source of truth): {details}
ERA PINS: narrated year≈{year}, region: {region}, era {era_from}-{era_to}.
FOUND BY QUERY: {query}
SCENE CONTRACT: type={scene_type}; claim={claim_level}; person={person}; place={place}; country={country};
event={event}; exact_object={exact_object}; modern stock allowed={stock_ok}.

Decide if this visual can illustrate THIS scene without making a false historical claim.
The photo need not match artistic details (lighting, mood, composition). Documentary b-roll
standard: an authentic photo of the same subject, the same named people/places, or a closely
related moment of the SAME event — with PLAUSIBLE location and side for the narration.

HARD "reject" rules (any one triggers reject):
1. WRONG PERSON: a recognizable historical figure who is NOT the person the narration names
   (match FULL identity, not surname: Hans Frank ≠ Karl Hermann Frank; Heydrich ≠ Frank).
   If the narration names a specific person, an identifiable different person is the worst error.
   When type=named and person is set, that named person must also be clearly visible and a
   visually meaningful primary subject. A wide crowd/group where the person is tiny or cannot
   be located is reject even if metadata says they attended. For generic/reconstruction scenes
   with a resolved person, a contextual group/partial view is allowed; the wrong-person rule remains.
2. READABLE TEXT in the frame: newspaper print, captions, teletype strips, archive plates,
   signage or documents the viewer can read — especially if language/content contradicts
   the scene (Cyrillic in a Prague scene, etc.).
3. WRONG GEOGRAPHY: narration names a specific city/country and the photo is clearly another
   country/theater (architecture, uniforms, signage).
4. WRONG ERA/DECADE: a clearly different historical era is reject.
   ERA B-ROLL IS WELCOME: this is a fast-cut documentary, so a period-authentic photo of the
   same era AND the same war theater (soldiers, streets, ruins, equipment, crowds, landscapes)
   is at least "soft" even when its subject only loosely relates to the narration — successful
   channels cut era b-roll constantly. Judge it "ok" when the mood/subject family fits
   (combat scene ↔ combat b-roll, city scene ↔ period city shot).
   For an abstract time transition ANY atmospheric era-authentic shot passes as "soft";
   no clock/calendar motif is required.
   Exception: when metadata confirms the SAME named person, a NEUTRAL portrait/headshot from
   nearby years may be "soft" identity b-roll. A photo whose metadata or visible setting clearly
   depicts another specific event (trial, congress, funeral, ceremony) is NOT a neutral portrait
   and must be rejected for arrest, combat, meeting or other incompatible event narration.
5. COLOR or obviously modern photo, museum exhibit, mannequin, memorial with candles,
   reenactment, AI-look — UNLESS metadata identifies neutral Pexels/Pixabay stock and
   stock_ok=true. Even then, reject reenactments or anything pretending to be the exact event.
6. GRAPHIC GORE: piles of corpses, naked bodies, executions in progress — YouTube
   demonetization risk. Reject even if thematically related.
7. PAPER OBJECT (document, ticket, letter, form, page, map, ID, stamp): reject only when it
   clearly belongs to ANOTHER named case/person/country. An anonymous era-authentic document
   used as texture for a records/letters/orders narration is "soft".
8. Entirely unrelated subject or unreadable/broken image.
9. MODERN STOCK used as if it depicts a named person, exact historical event/place/document,
   or other claim where modern stock allowed=false. Neutral modern stock is acceptable only when
   the scene contract explicitly allows it.
10. CHANNEL WATERMARK / RE-UPLOAD OVERLAY: a channel logo watermark, subscribe button or
    plaque, like/subscribe call-to-action, or other modern on-screen UI graphics burned into
    the frame — reject (re-uploaded compilation, not clean archive footage).

- "soft" if plausible era photo but era/conflict adjacent, or subject related only loosely.
- "soft" for a metadata-confirmed NEUTRAL portrait/headshot of the exact named person used as an
  identity cutaway. Never use a visibly different named event as interchangeable identity b-roll.
- "ok" if era-consistent, location-plausible photo of the scene's subject or same event.

Use the archive metadata supplied after this instruction as hard evidence. A title naming another
person/place/event overrides visual similarity."""


FINAL_CHECK_PROMPT = """You are the FINAL quality gate for one already-chosen archive photo
in a {niche_desc} YouTube documentary. History-buff viewers publicly shame mismatches.

SCENE: {subject}
NARRATION: {details}
ERA: narrated year≈{year}, region: {region}.
SCENE CONTRACT: type={scene_type}; claim={claim_level}; person={person}; place={place}; country={country};
event={event}; exact_object={exact_object}; modern stock allowed={stock_ok}.
ARCHIVE METADATA: {candidate_meta}

FAIL this photo if ANY holds:
1. It shows a recognizable historical figure who is NOT the person the narration names
   (verify FULL identity: Hans Frank ≠ Karl Hermann Frank; Heydrich ≠ Frank).
   If type=named and person is set, the named person must be clearly visible, identifiable,
   and a visually meaningful primary subject. A tiny or unlocatable person in a wide crowd/group
   FAILS even when archive metadata says they attended. Multiple people are fine only when the
   named person remains clearly identifiable. For generic/reconstruction with a resolved person,
   prominence is not required, but a prominent different historical figure still FAILS.
2. Readable text in frame (signs, print, captions, archive plates) contradicts the scene
   (wrong language/country/date) or is prominent enough for viewers to read.
3. Clearly different country/theater than the narration implies (architecture, uniforms).
4. Obviously different historical era than year≈{year}. Era b-roll from the same war theater
   PASSES (this is a fast-cut documentary; loosely related period footage is standard practice).
   A metadata-confirmed NEUTRAL portrait/headshot of the SAME named person from nearby years may
   PASS as identity b-roll, but a photo explicitly depicting another event (trial/congress/
   funeral/ceremony) FAILS when that event conflicts with the narrated arrest/testimony/action.
5. Color/modern photo, museum, memorial, reenactment, unless metadata identifies allowed neutral
   stock; reenactments of the exact historical event still fail.
6. Graphic gore: corpses, naked bodies (YouTube demonetization).
7. Paper object (ID, document, form, ticket, letter, readable page) whose metadata names a
   DIFFERENT case/person/country; anonymous era documents pass for records/letters narration.
8. Entirely meaningless subject for this narration (era alone with zero topical or mood link).

Otherwise PASS (documentary b-roll standard: era-plausible related visual is fine).
Metadata naming another person/place/event is a hard FAIL. Reply ONLY JSON:
{{"pass":true|false,"reason":"<max 8 words>"}}"""


def _candidate_meta(cand) -> str:
    if cand is None:
        return "unavailable"
    fields = {
        "source": getattr(cand, "source", ""),
        "media": getattr(cand, "media_type", "image"),
        "title": getattr(cand, "title", ""),
        "date": getattr(cand, "date_text", ""),
        "author/provider": getattr(cand, "author", ""),
        "license": getattr(cand, "license_raw", "") or getattr(cand, "license", ""),
        "page": getattr(cand, "page_url", ""),
    }
    return "; ".join(f"{k}={str(v)[:240]}" for k, v in fields.items() if v)


def final_check(scene_ctx: dict, path: str, niche: dict, log=print, cand=None):
    """Подтверждение финального кадра сильной моделью. -> (ok: bool, reason: str).
    Поведение при ошибке задаёт final_check.fail_open (WW2-профиль fail-closed).

    D2: face-гейт ЗДЕСЬ, а не в router._confirmed_pick — у final_check ДВА
    call-site (router._confirmed_pick И parser_worker._verify_staged_video:
    видео идёт вторым путём из-за defer_video_to_contact_sheet), гейт в
    _confirmed_pick пропустил бы все клипы. Reject c reason «facebank: …»
    происходит ДО LLM-вызова — _confirmed_pick не считает его в tries."""
    cfg = (niche.get("judge", {}) or {}).get("final_check", {}) or {}
    fb_note = ""
    fb_person = str(scene_ctx.get("person") or "").strip()
    if fb_person and scene_ctx.get("type") == "named":
        try:
            from . import facebank
            fb = facebank.gate(path, fb_person, niche, log=log)
        except Exception as e:  # noqa: BLE001 — face-гейт не роняет контроль
            log(f"[facebank] гейт упал: {e} — решает LLM")
            fb = None
        if fb and fb.get("status") == "wrong":
            # чужое узнаваемое лицо: немедленный reject БЕЗ LLM (экономия)
            return False, f"facebank: wrong person {fb.get('person', '?')}"[:80]
        if fb and fb.get("status") == "match":
            fb_note = f"\nfacebank: face matches {fb_person}"
    era = niche.get("era", {})
    prompt = FINAL_CHECK_PROMPT.format(
        niche_desc=niche.get("description", ""),
        subject=scene_ctx.get("subject", ""),
        details=(scene_ctx.get("visual_prompt") or "")[:400],
        year=scene_ctx.get("year") or f'{era.get("from", "?")}-{era.get("to", "?")}',
        region=scene_ctx.get("region") or "?",
        scene_type=scene_ctx.get("type") or "generic",
        claim_level=scene_ctx.get("claim_level") or "context",
        person=scene_ctx.get("person") or "none",
        place=scene_ctx.get("place") or "none",
        country=scene_ctx.get("country") or "none",
        event=scene_ctx.get("event") or "none",
        exact_object=scene_ctx.get("exact_object") or "none",
        stock_ok=bool(scene_ctx.get("stock_ok")),
        candidate_meta=_candidate_meta(cand))
    prompt += fb_note  # D2: матч банка — судья принимает быстрее
    try:
        raw = chat(cfg.get("model", "gpt-5"),
                   [{"role": "user", "content": [{"type": "text", "text": prompt},
                                                 image_part(path)]}],
                   reasoning_effort=cfg.get("reasoning_effort", "low"),
                   max_tokens=int(cfg.get("max_completion_tokens", 2000)))
        v = extract_json(raw)
        return bool(v.get("pass")), str(v.get("reason", ""))[:80]
    except Exception as e:  # noqa: BLE001
        fail_open = bool(cfg.get("fail_open", False))
        log(f"[final-check] ошибка: {e} — {'пропускаю' if fail_open else 'отклоняю'} кадр")
        return fail_open, "check-error"


def judge_scene_candidates(scene_ctx: dict, cands: list, niche: dict, log=print) -> list:
    """cands: [(Candidate, local_path)] -> список dict {cand, path, verdict, reason}."""
    cfg = niche.get("judge", {})
    model = cfg.get("model", "gpt-5-mini")
    effort = cfg.get("reasoning_effort", "minimal")
    if (scene_ctx.get("person") or scene_ctx.get("type") == "named"
            or scene_ctx.get("stock_ok")):
        # Scanner may be minimal only where a selected historical identity/event gets
        # the stronger final gate, or where deterministic policy allows neutral stock.
        effort = cfg.get("screening_reasoning_effort", effort)
    threads = int(cfg.get("threads", 6))
    batch_size = max(1, int(cfg.get("batch_size", 1)))
    era = niche.get("era", {})
    strict = bool(niche.get("strict_era", False))

    prompt = PROMPT.format(
        niche_desc=niche.get("description", ""),
        subject=scene_ctx.get("subject", ""),
        details=(scene_ctx.get("visual_prompt") or "")[:400],
        year=scene_ctx.get("year") or "?",
        region=scene_ctx.get("region") or "?",
        era_from=era.get("from", "?"), era_to=era.get("to", "?"),
        query=scene_ctx.get("query", ""),
        scene_type=scene_ctx.get("type") or "generic",
        claim_level=scene_ctx.get("claim_level") or "context",
        person=scene_ctx.get("person") or "none",
        place=scene_ctx.get("place") or "none",
        country=scene_ctx.get("country") or "none",
        event=scene_ctx.get("event") or "none",
        exact_object=scene_ctx.get("exact_object") or "none",
        stock_ok=bool(scene_ctx.get("stock_ok")),
    )

    def one_batch(batch):
        many = len(batch) > 1
        try:
            if many:
                response_rule = ("\n\nEvaluate every numbered candidate independently. Reply ONLY "
                                 "a JSON array in the same order: "
                                 '[{"i":0,"verdict":"ok|soft|reject",'
                                 '"reason":"max 8 words"}, ...]')
            else:
                response_rule = ('\n\nReply ONLY JSON: {"verdict":"ok|soft|reject",'
                                 '"reason":"max 8 words"}')
            content = [{"type": "text", "text": prompt + response_rule}]
            for i, (cand, path) in enumerate(batch):
                meta = f"CANDIDATE {i} ARCHIVE METADATA: {_candidate_meta(cand)}"
                if getattr(cand, "media_type", "image") == "video":
                    meta += "\nThe supplied image is a preview frame from the candidate video."
                content.extend([{"type": "text", "text": meta}, image_part(path)])
            max_tokens = int(cfg.get("max_completion_tokens",
                                     2600 if many else 1500))
            raw = chat(model, [{"role": "user", "content": content}],
                       reasoning_effort=effort, max_tokens=max_tokens)
            parsed = extract_json(raw)
            if not many and isinstance(parsed, dict):
                parsed = [parsed]
            elif isinstance(parsed, dict):
                parsed = parsed.get("candidates") or parsed.get("results") or []
            if not isinstance(parsed, list):
                raise ValueError("judge batch is not a JSON array")
            indexed = {}
            for pos, value in enumerate(parsed):
                if not isinstance(value, dict):
                    continue
                try:
                    key = int(value.get("i", pos))
                except (TypeError, ValueError):
                    key = pos
                indexed[key] = value
            out = []
            for i, (cand, path) in enumerate(batch):
                v = indexed.get(i)
                if not v:
                    out.append({"cand": cand, "path": path, "verdict": "error",
                                "reason": "missing batch verdict"})
                    continue
                verdict = v.get("verdict", "reject")
                if verdict not in ("ok", "soft", "reject"):
                    verdict = "reject"
                if strict and verdict == "soft":
                    verdict = "reject"
                out.append({"cand": cand, "path": path, "verdict": verdict,
                            "reason": str(v.get("reason", ""))[:80]})
            return out
        except Exception as e:  # noqa: BLE001
            return [{"cand": cand, "path": path, "verdict": "error",
                     "reason": str(e)[:80]} for cand, path in batch]

    batches = [cands[i:i + batch_size] for i in range(0, len(cands), batch_size)]
    if not batches:
        return []
    with cf.ThreadPoolExecutor(min(threads, len(batches))) as ex:
        return [item for group in ex.map(one_batch, batches) for item in group]
