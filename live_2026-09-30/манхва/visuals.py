# -*- coding: utf-8 -*-
"""Заполнение бланка кадра: один вызов на сцену отдаёт все графы её кадров.

Пачками по сценам — потому что на ролик это 14 вызовов вместо 493.

Что изменилось 08.09.2026. Раньше модель возвращала два поля: описание и ракурс.
Всё остальное — свет, одежда, поза, взгляд, эмоция, где кто стоит, откуда снимаем —
она решала заново внутри каждого описания, то есть по сорок раз за сцену и каждый
раз иначе. Теперь постоянные сцены приходят готовыми из паспорта сцены, а модель
заполняет только то, что действительно своё у каждого кадра.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from llm import call_json, LLMError
import claude_pool as CP
# общий предел одновременных вызовов модели на службу (MANGA_CLAUDE_CONCURRENCY); без него — прямой вызов
call_json = CP.limited(call_json)
import languages as LG
from sheets import plain   # 29.09.2026: поле модели строкой (список/словарь → «; », None → "")

# 29.09.2026: текстовые графы ответа модели на кадр — их plain() приводит к строке при разборе
_STR_KEYS = frozenset(("what", "shot_size", "angle", "camera_pos", "staging", "pose", "contact",
                       "distance", "gaze", "emotion", "focus", "place", "panel", "quote",
                       "quote_by", "sfx", "layer", "changed"))

def batch_prompt(lang: str = "Russian") -> str:
    """Для русского — BATCH_PROMPT побайтно. Для остальных языков две пометки разработчика
    («Замер: …») переведены на английский: в польском промпте не остаётся кириллицы."""
    if lang == "Russian":
        return BATCH_PROMPT
    out = BATCH_PROMPT
    for ru, en in BATCH_NOTES_EN:
        assert ru in out, ru[:40]
        out = out.replace(ru, en)
    return out


BATCH_PROMPT = """You are the storyboard artist of a manhwa recap video.
You get consecutive shots of ONE scene and you fill in a form for each shot.
Everything you leave empty will be invented by the illustrator, differently every time.

CAST — these looks are fixed, never invent appearance:
{cast}

THIS SCENE
  place: {where}
  light and time: {light}
  camera positions available (pick one per shot, by its exact wording):
{anchors}
  fixed sides of the frame: {sides}

Return ONE JSON object, nothing else:
{{"shots": [{{
   "n": <shot number as given>,
   "what": "<English, 30-50 words: ONE frozen moment. Who does what, with hands and
             objects. No style words, no lighting words, no camera words — those are
             separate fields below.>",
   "shot_size": "extreme_close"|"close"|"medium"|"full"|"wide"|"insert",
   "angle": "eye"|"low"|"high"|"over_shoulder",
   "camera_pos": "<one of the camera positions listed above, copied word for word>",
   "staging": "<where each visible person stands relative to the FURNITURE and to each
                other: 'he stands left of the counter, two steps from camera; she is
                further back by the shelves'>",
   "posture": {{"<character id>": "sitting"|"standing"|"crouching"|"lying"|"leaning"}},
   "pose": "<what the body and hands DO, given that posture — never contradict it>",
   "contact": "<if a hand, arm or shoulder enters the frame from outside, say WHOSE it is,
                from which edge it enters and what it holds or touches; empty if none>",
   "distance": "<how far apart the people are and how far from the camera, in steps:
                 'both at the same desk, an arm's length apart, three steps from camera'>",
   "gaze": "<where each person looks, in frame terms: 'he looks to the right of frame',
             'she looks straight at him'>",
   "emotion": "<one word for the main character's face: calm, angry, afraid, hurt,
                shocked, sad, determined, blank. ONE emotion per shot, at the strength
                the text gives it, never amplified. 'shocked', 'afraid', 'angry' ONLY when
                the text names that reaction outright (cried out, trembled, shouted,
                flinched, went pale). Inner tension, a held breath, a hard decision,
                a swallowed reply = 'determined', 'hurt' or 'calm' with pressed lips —
                not a scream, not tears.>",
   "face_visible": true|false,
   "focus": "<character id whose face matters most, or empty>",
   "on_screen": ["<character id from CAST who is actually VISIBLE in this shot>"],
   "place": "<where THIS shot happens, if it is not the place given above; else empty>",
   "state_change": {{"<character id>": "<what changed about this person in THIS shot and
                       stays changed afterwards — clothes ('jacket off, draped over the chair'),
                       hair ('ponytail undone, hair loose'), injury or dirt ('fresh cut on the
                       lip', 'snow brushed off'), what they hold ('now holds the letter', 'puts
                       the phone down'). When the person was already changed earlier in this
                       scene, write the FULL current state, not only the new delta: 'jacket off,
                       sleeves rolled up', not just 'sleeves rolled up'. Leave the object empty
                       when nothing persists — which is most shots.>"}},
   "panel": "establishing"|"reaction"|"face"|"detail"|"impact"|"dialogue",
   "quote": "<ONLY if the narration reports that someone speaks in this shot ('he said',
             'she shouted', 'answered', 'whispered'): the line itself in {lang}, 3-12 words,
             as the character would say it. Otherwise empty string.>",
   "quote_by": "<character id who says the quote, or empty>",
   "sfx": "<ONLY for a physical impact or sudden motion in this shot: ONE short sound word in
            {lang} as manhwa lettering — {sfx_examples}. Otherwise empty.>",
   "layer": "now"|"flashback"|"dream",
   "props_seen": ["<names of the scene objects visible in this shot>"],
   "changed": "<what is different from the PREVIOUS shot, said ABSOLUTELY: 'now stands
                half-turned facing the window', never 'he turned'. Empty for the first
                shot of the scene.>"
 }}]}}

RULES
- One entry per shot, same "n" values, nothing skipped.
- Paper, letters, forms and screens are drawn ALMOST BLANK: a few faint ruled lines or
  two short scribbles at most, never a page filled with writing. A "handwritten letter"
  becomes a page of dense pseudo-text (frame 364 — the whole sheet was covered in fake
  handwriting). Write "a folded sheet with a few faint lines", never "a letter full of text".
- NEVER name a document, sign, screen or book by the words printed on it. Write
  "a folded sheet of paper", not "the consent form"; "a form on the desk", not
  "the medical questionnaire". The illustrator draws what you name, letters included,
  and letters are forbidden in the finished picture.
- layer: "flashback" whenever the text remembers something that happened earlier —
  words like {memory_words}. This changes who is on
  screen and how old they look, so getting it wrong ruins the shot.
- a flashback shot shows ONLY the remembered moment: the remembered place and the people who
  were there then. Never a dissolve, overlay or split with the present room or the present
  people — they belong to a separate "now" shot before or after it.
- posture is PER CHARACTER, one entry for each person in the shot. In one frame the father
  may stand while the son sits — a single value for the whole frame is wrong and gets the
  seated person drawn standing.
- posture first, pose second: pose must not contradict posture. If the person sits,
  "leans forward" means leaning while seated.
- panel is the manhwa panel type: establishing (place + people, wide), reaction (a face
  reacting), face (emotional close-up), detail (hands/object), impact (the blow, the fall,
  the slam — with motion), dialogue (two people talking). Vary them like a manhwa page.
- quote: a bubble frame will be drawn by code with this exact text, so keep it short and
  natural. Most shots have NO quote — narration is not speech.
- sfx: at most one word, only where something physically happens — a blow, a fall, a slam,
  glass breaking, a door banging. Not for feelings, not for looks, not for speech. In a
  scene of 50 shots there are usually 3–6 such moments, not 16. When in doubt, leave empty.
  {sfx_match}
- state_change is for the MOMENT of change only: the shot where the text says someone
  takes a coat off, lets their hair down, gets hurt, picks something up or puts it down.
  From that shot on the software carries the new state forward by itself. Do not repeat
  it in later shots, and never use it to restate what is already true.
  Замер: в одной сцене герой снял куртку в одном кадре, а паспорт продолжал требовать
  куртку — и она мигала то есть, то нет в 14 кадрах подряд.
- when two people are at one piece of furniture, say it plainly: "both at the SAME single
  desk, no second desk". A phrase like "shared desk" is drawn as two desks side by side.
- distance: give it always when two or more people are in frame. Without it the
  main character is drawn as a giant in the foreground and the other as a speck.
- "what" describes a single instant a camera could freeze. Never a sequence.
- Call people by their NAME only. Never re-describe a face, hair, age, build or
  clothing: the illustrator already has a reference picture of every character,
  and words that disagree with the picture make the face drift. Write "Fang Shu
  grips the shelf", not "a young man of 17 with dark hair grips the shelf".
- Vary shot_size between neighbours. Never three shots in a row with the same one.
  Wide shots re-establish the room; use one when the place changes or after four
  close shots in a row.
- face_visible is false when the shot is hands, an object, a back view, or a face
  hidden by hair or shadow. Be honest: a close-up of clasped hands is face_visible false.
- camera_pos: move the camera between shots. Do not shoot four shots in a row from
  the same position.
- Keep the fixed sides: a character assigned to the left stays on the left all scene.
- The narration often names nobody and nowhere. The "place" given to you is a guess too:
  it is the place of the WHOLE scene, and a scene often walks through several places. If the
  text of a shot happens somewhere else — another room, a street, a forge, a battlefield,
  a memory — write that place in "place" of that shot, in plain words of this language.
  Repeat the same wording for every shot that happens there, so the place stays one place.
  The "on_screen" given to you is only a GUESS made by matching names: it never works out
  who "he", "she" or "him" is, and where the text names nobody it simply keeps the last
  person it saw, sometimes for twenty shots in a row. Decide from the MEANING of the text
  who is visible in each shot and return the corrected list in "on_screen". The "what" of
  the shot must show the right person doing the right thing.
  EVERY person who does something in your "what" — holds, carries, reaches, speaks, walks in —
  must be listed in "on_screen" of that shot. A person who is not in "on_screen" gets no
  reference picture, so the illustrator invents their face, and it comes out as a copy of
  someone else's. If you do not want them in the shot, do not give them an action: write the
  action without them, or put the object alone in frame.
- When the text is a thought or a feeling with no visible action, the shot is a close-up
  of that character's face carrying the emotion. Never invent a new place for it.
- TIME AND PLACE OF EACH SENTENCE. Draw what is true at this moment of the story, where it is
  true. A sentence about the FUTURE or about what WOULD happen — a threat, a plan, a prophecy:
  "later the maid will poison the old lady", "tomorrow he will be executed" — is NOT happening
  now. Never draw that act as already being done, and never put it in the room of this scene
  just because the scene is there. Draw EITHER the place where it will happen, as it is now
  (its own "place"; the thing that will be used is there, nobody is doing it yet), OR the face
  of the person it threatens or concerns, here and now. Such a shot is layer "now": the
  software has no layer for the future. A sentence that happens in another place gets that
  place in "place". For the past nothing changes: layer follows the flashback rule above.
- emotion is read from the words, not from the drama of the moment. Замер 10.09 (335 кадров):
  ~14 кадров ушли в крик и слёзы там, где текст даёт сжатые губы и молчание. A tense
  silence is 'determined' or 'calm', not 'shocked'; a hard word received is 'hurt', not
  'afraid'. Use 'shocked'/'afraid'/'angry' only for a reaction the text spells out.

If the first entry is marked "previous shot, for continuity only", it is the shot that
came immediately BEFORE this batch. Use it to fill "changed" for the first real shot and
to avoid repeating its camera position and shot size. Do NOT return an entry for it.

SHOTS:
{shots}
"""

BATCH_NOTES_EN = (
    ("  Замер: в одной сцене герой снял куртку в одном кадре, а паспорт продолжал требовать\n"
     "  куртку — и она мигала то есть, то нет в 14 кадрах подряд.",
     "  Measured: in one scene a character took a jacket off in one shot while the passport kept\n"
     "  demanding the jacket, and it flickered on and off for 14 shots in a row."),
    ("Замер 10.09 (335 кадров):\n"
     "  ~14 кадров ушли в крик и слёзы там, где текст даёт сжатые губы и молчание.",
     "Measured on 335 shots:\n"
     "  ~14 shots went to shouting and tears where the text gives pressed lips and silence."),
)


# Добавка к кускам раскадровки, где есть первые кадры ролика (хук). Проба 27.09: модель
# открыла ролик общим планом гостиной с эмоцией «спокойствие» под фразу «Старую госпожу
# убьют ежедневными дозами яда», а «гости замерли, услышав бесстыдную мысль» нарисовала
# людьми с чашками и спокойными лицами. Нормальная грамматика начала сцены — сначала
# покажи комнату, эмоцию не раздувай — в хуке убивает первые секунды.
HOOK_SHOTS = 14
HOOK_BATCH_NOTE = """
THE OPENING SHOTS OF THE VIDEO — THE HOOK. The viewer decides in these seconds whether to
keep watching. THESE RULES APPLY ONLY TO SHOTS {hook_shots}. Every other shot in this batch
follows the normal rules above — calm faces, wide shots and establishing shots are fine there.

- SHOT 1 IS NEVER AN ESTABLISHING SHOT. Do not open on a room, a building or a landscape
  with small people in it. Open on the single most striking thing the first sentence names:
  the poison in the cup, the wound, the weapon, the letter, the face in terror or rage —
  shot_size "close", "extreme_close" or "insert", panel "face", "detail" or "impact".
  The room can be shown later, after the viewer is caught.
- EMOTION AT FULL STRENGTH. In these shots, whenever the text threatens, hurts, humiliates or
  shocks someone, their face shows it clearly: afraid, shocked, angry, hurt, sad. "calm" and
  "blank" are NOT allowed for a character the text puts in danger or in shock. "Froze",
  "went still", "fell silent" after something shameful or terrible means shocked, not blank.
  Full strength means the FACES, not special effects: panel "impact" — speed lines, burst,
  flying objects — is ONLY for a physical blow, fall, collision or slam. A quiet action —
  pouring, adding poison, handing over, touching, writing — is panel "detail", drawn plainly.
- Never use the same camera position twice in a row among these shots.
- A REACTION IS NEVER A WIDE SHOT. When someone froze, gasped, went pale, stared, heard or saw
  something shocking, the shot is about their FACES: shot_size "close" or "medium", panel
  "reaction" or "face", at most three faces, each large enough to read the emotion. A wide
  shot makes faces twenty pixels tall — the shock is lost. If more people react, show the
  two or three who matter most.
- ONE FACE CARRIES EACH REACTION. In a reaction or face shot with two or three people, put in
  "focus" the one whose reaction the sentence is about, and "emotion" is that face's emotion.
  BEGIN "staging" with that person: in the foreground, nearest to the camera, face the largest
  in the frame; only then the others: behind, smaller, at the edge — second plan. Two equal
  faces side by side split the eye, and the shock reads as nobody's.
- A THREAT IS NOT YET AN ACT ("will be killed", "later the maid will poison her"): follow
  TIME AND PLACE above — the threatened face in fear, or the poison waiting where it will be
  used. Never the act already happening in this room.
"""


# Добавка к BATCH_PROMPT только для переписи кадров-воспоминаний (memory_text задан).
# Замер 18.09 (часовой ромком, сцена 1, кадры 12–15): в тексте «он закрыл её собой
# от удара ножом» имени нет, состав кадра угадан по «последнему названному мужчине» —
# отцу героини из кадра 9, и BATCH_PROMPT велит верить on_screen больше текста.
# Итог: ножом закрывается и умирает отец, а не герой. Кто в кадре, решает модель
# по смыслу, видя текст всей сцены; место кадра — место воспоминания, а не зал.
MEMORY_BATCH_NOTE = """
THESE SHOTS ARE A MEMORY. Two fields above work differently here:
- "place" is the place of this memory; draw it there, not in the room of the present.
- "on_screen" is only a guess made by matching names in the text: it never works out who
  "he", "she" or "him" is, and in a memory it is often wrong. Decide who is visible in each
  shot from the MEANING of its text, reading the scene text below to know who "he" and
  "she" are. The "what" of the shot must show the right person doing the right thing.

SCENE TEXT (for who is who; draw only the shots listed):
{scene_text}
"""


def build_prompts(frames, beats, src: str, sk, style_line: str,
                  scenes: Optional[Dict[int, object]] = None,
                  lang: str = "Russian", model: str = "claude:sonnet",
                  scene_of_frame=None, workers: int = 4, log=print,
                  cast_text: Optional[str] = None, balanced: bool = False,
                  only_empty: bool = False,
                  memory_text: Optional[str] = None,
                  memory_ids: Optional[List[str]] = None) -> List[str]:
    """Заполняет графы бланка у frames. Возвращает список проблем.

    balanced — порядок и нарезка под много потоков (MANGA_TEXT_THREADS): сцены с самой
    длинной цепочкой кусков стартуют первыми, а куски одной сцены равные (19 кадров =
    10+9, а не 18+1: кусок из одного кадра — лишний вызов модели в цепочке сцены).

    only_empty — дозаполнение: спрашивать только куски, где есть кадр без описания.
    Готовые куски сцены не пересылаются, их последний кадр идёт стыком в следующий, а первый
    кадр готового куска после спрошенного идёт в запрос как «next shot» (стык вперёд).

    cast_text — подменить список героев. Нужно для воспоминаний: там другой
    состав и другой возраст, и бланк должен заполняться в ТОМ мире.
    memory_text — текст сцены: кадры — воспоминание. Место кадра = место сцены
    (паспорт воспоминания), состав кадра решает модель (on_screen), из memory_ids.
    Без него промпт побайтно прежний.
    """
    cast = cast_text or "\n".join(
        f"  - {c.get('id')}: {c.get('name')} — {c.get('appearance_en','')}"
        for c in (sk.characters or [])) or f"  {LG.labels(lang)['none']}"

    все_id = {str(c.get("id")) for c in (sk.characters or []) if c.get("id")}
    англ_имена = cast_names(sk)

    groups: Dict[int, List[int]] = {}
    for i, f in enumerate(frames):
        s = scene_of_frame(f) if scene_of_frame else 1
        groups.setdefault(s, []).append(i)

    problems: List[str] = []
    scenes = scenes or {}

    # Одна сцена — это до 54 кадров, а бланк вырос до одиннадцати граф. Ответ
    # перестал влезать в отведённое время: 09.09.2026 четыре сцены из четырнадцати
    # отвалились по таймауту и 150 кадров остались без описания вовсе. Поэтому
    # режем сцену на куски по CHUNK кадров и передаём в следующий кусок последний
    # кадр предыдущего — иначе графа «что изменилось» на стыке теряет опору.
    CHUNK = 18

    def one(job):
        scene_num, idxs, tail, nxt = (*job, None) if len(job) == 3 else job
        sc = scenes.get(scene_num)
        anchors = getattr(sc, "anchors", None) or [
            "a wide view of the whole room", "closer in, from the middle of the room"]
        sides = ", ".join(f"{k}: {v}" for k, v in (getattr(sc, "sides", {}) or {}).items()) or "(not set)"
        shots = []
        if tail is not None:
            t = frames[tail]
            shots.append({"n": t.idx, "text": "(previous shot, for continuity only — "
                                              "do not return it)",
                          "on_screen": t.in_frame or [], "place": t.location or "",
                          "already_done": {"what": t.prompt_en, "shot_size": t.shot_size,
                                           "camera_pos": t.camera_pos, "staging": t.staging}})
        for i in idxs:
            f = frames[i]
            shots.append({"n": f.idx,
                          "text": f.text(src, beats).strip()[:400],
                          "on_screen": f.in_frame or [],
                          "place": (getattr(sc, "where", "") or f.location or "")
                          if memory_text is not None else (f.location or "")})
        if nxt is not None:
            # Дозаполнение: следующий кусок сцены готов и не переспрашивается, а его первый
            # кадр описан как продолжение прежнего варианта нашего последнего кадра. Даём его
            # модели, чтобы новый последний кадр вёл в него, а не расходился на стыке.
            t = frames[nxt]
            shots.append({"n": t.idx, "text": "(next shot, already drawn, for continuity only — "
                                              "do not return it; the last shot above must lead "
                                              "into it)",
                          "on_screen": t.in_frame or [],
                          "place": (getattr(sc, "where", "") or t.location or "")
                          if memory_text is not None else (t.location or ""),
                          "already_done": {"what": t.prompt_en, "shot_size": t.shot_size,
                                           "camera_pos": t.camera_pos, "staging": t.staging,
                                           "changed": t.changed}})
        prompt = batch_prompt(lang).format(
            cast=cast, lang=lang, sfx_examples=LG.sfx(lang),
            memory_words=LG.memory_words(lang), sfx_match=LG.sfx_match(lang),
            where=getattr(sc, "where", "") or (frames[idxs[0]].location or ""),
            light=(getattr(sc, "constants_block", lambda: "")() or "(not set)"),
            anchors="\n".join(f"    - {a}" for a in anchors),
            sides=sides,
            shots=json.dumps(shots, ensure_ascii=False, indent=1))
        if memory_text is not None:
            prompt += MEMORY_BATCH_NOTE.format(scene_text=memory_text[:6000])
        # хук: в куске есть первые кадры ролика — открывать сильным кадром, эмоцию в полную силу
        хук = [int(frames[i].idx) for i in idxs if int(frames[i].idx) <= HOOK_SHOTS]
        if хук:
            # номера перечисляем явно: кусок бывает до 18 кадров, и без списка правила хука
            # получали кадры 15–18 (второй аудит Codex 27.09, R1)
            prompt += HOOK_BATCH_NOTE.replace("{hook_shots}", ", ".join(str(n) for n in хук))
        try:
            data, ans = call_json(prompt, model=model, timeout=900, retries=2)
        except LLMError as e:
            problems.append(f"сцена {scene_num}, кадры {frames[idxs[0]].idx}-{frames[idxs[-1]].idx}: {e}")
            return False
        # 29.09.2026: 28.09 раскадровка 54706a7d упала целиком на pose-не-строке (visuals.py:622) —
        # одно кривое поле роняло все сцены ролика. Текстовые графы — строкой здесь, один раз;
        # строку plain() отдаёт как есть, нормальный ответ побайтно прежний. Элемент без
        # номера или не объект — кадр «без описания» (его доберёт дозаполнение), а не падение шага.
        by_n = {}
        for s in (data.get("shots") or []):
            try:
                by_n[int(s.get("n", -1))] = {k: plain(v or "") if k in _STR_KEYS else v for k, v in s.items()}
            except (AttributeError, TypeError, ValueError):
                pass
        miss = 0
        for i in idxs:
            f = frames[i]
            s = by_n.get(f.idx)
            if not s or not (s.get("what") or "").strip():
                miss += 1
                continue
            f.prompt_en = strip_clothes(s["what"].strip())
            f.shot_size = s.get("shot_size", "") or "medium"
            f.angle = s.get("angle", "") or "eye"
            f.camera_pos = s.get("camera_pos", "")
            f.staging = s.get("staging", "")
            f.pose = s.get("pose", "")
            f.gaze = s.get("gaze", "")
            f.emotion = s.get("emotion", "")
            _p = s.get("posture")
            f.posture = _p if isinstance(_p, dict) else ({} if not _p else {"*": str(_p)})
            f.contact = s.get("contact", "")
            f.distance = s.get("distance", "")
            f.layer = s.get("layer", "now") or "now"
            # 27.09.2026 (раскадровка, хук): слоя «будущее» софт не знает — всё, что не «now»,
            # рисуется воспоминанием со своим паспортом. Будущее и угроза — «now» со своим местом
            if str(f.layer).strip().lower() in ("future", "later", "plan", "threat"):
                f.layer = "now"
            f.panel = s.get("panel", "") or ""
            f.quote = (s.get("quote") or "").strip()
            f.quote_by = (s.get("quote_by") or "").strip()
            f.sfx = (s.get("sfx") or "").strip()
            f.props_seen = s.get("props_seen") or []
            _cc = s.get("state_change") or s.get("costume_change")
            f.costume_change = _cc if isinstance(_cc, dict) else {}
            f.face_visible = bool(s.get("face_visible", True))
            f.focus = s.get("focus", "")
            f.changed = s.get("changed", "")
            f.camera = f.shot_size
            # состав кадра решает модель на всех кадрах, а не только на воспоминаниях:
            # догадка по именам врала в десятках кадров подряд (замер 25.09)
            можно = (set(memory_ids or []) if memory_text is not None else все_id)
            if isinstance(s.get("on_screen"), list):
                ok = [c for c in dict.fromkeys(str(x) for x in s["on_screen"]) if c in можно]
                if ok:
                    # тех, кого её же описание называет видимыми, не выбрасываем: иначе
                    # бланк разойдётся сам с собой и дозаполнение сотрёт описание кадра
                    свои = [c for c in cast_mentioned(
                        (f.prompt_en or "") + "\n" + (f.staging or ""), англ_имена)
                        if c in можно]
                    слито = list(dict.fromkeys(ok + свои))
                    # Предел четыре. Прежние три были тесны: четвёртый герой молча выпадал
                    # из состава, а потом его же описывали в осанке и мизансцене — 151 кадр
                    # из 221 с таким расхождением (замер 26.09). Лишний лист бесплатен:
                    # по 78 775 покупкам картинка стоит 4 единицы квоты и при нуле листов,
                    # и при четырёх.
                    f.in_frame = слито[:max(4, len(f.in_frame or []))]
                elif not [x for x in s["on_screen"] if str(x or "").strip()]:
                    # явный пустой on_screen [] — ответ модели «в кадре никого», а не молчание.
                    # Раньше он фильтровался в пустой ok и не присваивался: кадр
                    # сохранял прежний состав, и пустой лагерь рисовался с двумя
                    # героями (аудит F05, d5ab1d90… #457). Замер 27.09: в 78 240
                    # сохранённых кадрах всего 23 с пустым составом. Отсутствие поля
                    # и список из одних чужих id по-прежнему оставляют прежний состав.
                    свои = [c for c in cast_mentioned(
                        (f.prompt_en or "") + "\n" + (f.staging or ""), англ_имена)
                        if c in можно]
                    # Осанка с героем — значит, кто-то в кадре есть. Кадры кроме
                    # establishing/detail опустошаем только при фразе «никого не видно»
                    # и невидимом лице: там герой часто назван лишь «он/она», и face
                    # 417ae437 #362 («no one present to interrupt», а Кален стоит в
                    # кадре) без проверки лица терял героя (замер 27.09).
                    поза = f.posture if isinstance(f.posture, dict) else {}
                    if not свои and not any(k in можно for k in поза) and (
                            f.panel == "establishing" or
                            # деталь с видимым лицом — в кадре человек (аудит V2-04, 27.09.2026)
                            (f.panel == "detail" and not f.face_visible) or
                            (not f.face_visible and _НИКОГО.search(
                                (f.prompt_en or "") + "\n" + (f.staging or "")))):
                        f.in_frame = []   # focus снимет проверка «главный внутри состава» ниже
            # место кадра решает модель: сцена — это глава сюжета, и внутри неё герои
            # переходят из комнаты в комнату. Место берём как написано, списком скелета
            # не ограничиваем: в хуке нужных мест там просто нет.
            место = str(s.get("place") or "").strip()
            if место and место.lower() not in ("none", "-", "—"):
                f.location = место[:120]
            # главный в кадре обязан быть внутри состава
            if (f.focus or "") not in (f.in_frame or []):
                f.focus = (f.in_frame or [""])[0]
        if miss:
            problems.append(f"сцена {scene_num}: без описания {miss} из {len(idxs)}")
        log(f"  сцена {scene_num}, кадры {frames[idxs[0]].idx}-{frames[idxs[-1]].idx}: "
            f"{len(idxs)-miss}/{len(idxs)}, {ans.seconds:.0f}s")
        return miss < len(idxs)

    # Куски ОДНОЙ сцены идут последовательно, сцены — параллельно. Раньше все
    # куски всех сцен лежали в одном пуле вперемешку, и второй кусок сцены
    # стартовал, пока первый ещё ждал модель: «previous shot, for continuity
    # only» читался из кадра с пустыми prompt_en/staging, и стык терял опору
    # (проверено 10.09.2026 заглушкой вместо модели: при общем пуле второй кусок
    # получал пустой already_done, при пуле по сценам — описание первого куска).
    # Если кусок отвалился (таймаут или кадр-стык без описания) — остаток сцены
    # не блокируем: идём дальше с tail=None и записываем проблему.
    def parts_of(idxs):
        if not balanced:
            return [idxs[k:k + CHUNK] for k in range(0, len(idxs), CHUNK)]
        n = -(-len(idxs) // CHUNK)                 # столько же кусков, сколько при CHUNK
        size, extra = divmod(len(idxs), n)
        out, k = [], 0
        for j in range(n):
            step = size + (1 if j < extra else 0)
            out.append(idxs[k:k + step])
            k += step
        return out

    def one_scene(scene_num):
        idxs = groups[scene_num]
        tail = None
        parts = parts_of(idxs)
        done = lambda p: all((frames[i].prompt_en or "").strip() for i in p)
        for k, part in enumerate(parts):
            if only_empty and done(part):
                tail = part[-1]
                continue
            # Следующий кусок останется как есть (он готов) — его первый кадр идёт стыком вперёд.
            nxt = (parts[k + 1][0] if only_empty and k + 1 < len(parts) and done(parts[k + 1])
                   else None)
            if tail is not None and not frames[tail].prompt_en:
                problems.append(f"сцена {scene_num}: кадр-стык {frames[tail].idx} без описания, "
                                f"кусок с {frames[part[0]].idx} идёт без опоры")
                tail = None
            one((scene_num, part, tail, nxt))
            # Стык берём всегда: если кусок упал, проверка выше сама увидит
            # пустой prompt_en и пойдёт без опоры. А при дозаполнении
            # (pipeline: повторный прогон по уже заполненному бланку) у кадра-стыка
            # описание есть с прошлого раза — терять его незачем.
            tail = part[-1]

    from concurrent.futures import ThreadPoolExecutor
    order = sorted(groups)
    if balanced:
        # Шаг длится не меньше самой длинной цепочки: её запускаем первой, чтобы она
        # не стартовала последней, когда остальные потоки уже свободны.
        order = sorted(groups, key=lambda s: (-len(parts_of(groups[s])), -len(groups[s]), s))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(CP.carry(one_scene), order))

    empty = [f.idx for f in frames if not f.prompt_en]
    if empty:
        problems.append(f"кадров без описания: {len(empty)} (например {empty[:8]})")
    carry_costume(frames, log=log)
    fix_camera_runs(frames, log=log)
    problems.extend(check_blanks(frames))
    problems.extend(check_quality(frames, scenes))
    problems.extend(check_form(frames, scenes, names=cast_names(sk)))
    return problems


def check_blanks(frames) -> List[str]:
    """Пустая графа = будущий гуляющий параметр. Ловим ДО генерации.

    Это главная проверка всего софта: она не смотрит ни одной картинки и
    стоит миллисекунды, а находит ровно те дефекты, которые иначе всплывут
    на готовом ролике по одному.
    """
    out = []
    need = ["prompt_en", "shot_size", "angle", "camera_pos", "staging", "gaze", "emotion"]
    for g in need:
        n = sum(1 for f in frames if not getattr(f, g, ""))
        if n:
            out.append(f"пустая графа «{g}»: {n} кадров из {len(frames)}")
    # три одинаковые крупности подряд внутри сцены
    trip = 0
    for i in range(len(frames) - 2):
        a, b, c = frames[i], frames[i + 1], frames[i + 2]
        if a.scene_num == b.scene_num == c.scene_num and \
           a.shot_size and a.shot_size == b.shot_size == c.shot_size:
            trip += 1
    if trip:
        out.append(f"троек подряд с одной крупностью: {trip}")
    # четыре подряд с одной точки съёмки
    quad = 0
    for i in range(len(frames) - 3):
        w = frames[i:i + 4]
        if len({f.scene_num for f in w}) == 1 and w[0].camera_pos and \
           len({f.camera_pos for f in w}) == 1:
            quad += 1
    if quad:
        out.append(f"четвёрок подряд с одной точки съёмки: {quad}")
    return out


# --- второй уровень приёмки бланка ------------------------------------------
# check_blanks ловит только ПУСТУЮ графу. Заполненная, но негодная — тридцать
# слов вместо пятидесяти, кириллица в английском поле, «он дерётся с ней»
# вместо позы — проходит насквозь и доезжает до готового ролика.
import re

_CYR = re.compile(r"[а-яё]", re.IGNORECASE)
_VAGUE = re.compile(r"\b(fights?|argues?|talks?|interacts?|reacts?|confronts?|"
                    r"struggles?|discusses?|communicates?)\b", re.IGNORECASE)
_EMOTIONS = {"calm", "angry", "afraid", "hurt", "shocked", "sad", "determined", "blank"}
_SHOT_SCALE = ["extreme_close", "close", "medium", "full", "wide"]

# Описание кадра прямо говорит, что людей в нём нет. Нужен для явного on_screen=[] на
# кадрах, которые не establishing/detail (удар, реакция на предмет). «No person is fully
# visible» (видны руки/сапоги) и «no one visible beside her» сюда НЕ попадают — там человек
# в кадре есть (замер 27.09, e5bc86c5 #76, fba0bea7 #551).
_НИКОГО = re.compile(
    r"\b(?:no (?:one|people|person|figures?|characters?|humans?|human figures?)|nobody)"
    r"(?: (?:is|are))? (?:visible|present|in (?:the )?(?:frame|shot|sight|view))\b"
    r"(?! (?:beside|besides|next to|near|except|but|other than|apart from|around))"
    r"|\bempty of (?:people|figures)\b",
    re.IGNORECASE)

# Одежда описывается уже в паспорте сцены и лежит на приложенной картинке.
# Третье описание в тексте кадра создаёт спор трёх источников — а спор модель
# решает усреднением, то есть новой одеждой в каждом кадре.
_CLOTHES = re.compile(
    r"[,;]?\s*(?:wearing|dressed in|clad in|in (?:a|an|his|her|their))\s+"
    r"[^,.;]{0,60}?(?:uniform|jacket|coat|dress|shirt|blazer|apron|scarf|hoodie|"
    r"sweater|trousers|skirt|robe|suit|clothes|outfit)[^,.;]{0,25}",
    re.IGNORECASE)


def strip_clothes(text: str) -> str:
    out = _CLOTHES.sub("", text or "")
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\s+([,.;])", r"\1", out)
    return out.strip().strip(",").strip()


def check_form(frames, scenes=None, names=None) -> List[str]:
    """Проверки, которые видны в самом бланке — до единой картинки и без кредитов.

    На прогоне 08.09 автопроверка выдала три замечания на 493 кадра, а глазами
    нашли восемьсот. Всё перечисленное ниже было видно в storyboard.json.
    """
    out = []
    scenes = scenes or {}
    # 1. герой в кадре, о котором в паспорте сцены не сказано ничего
    naked = []
    for f in frames:
        sc = scenes.get(getattr(f, "scene_num", 0))
        if not sc:
            continue
        for cid in (f.in_frame or []):
            if cid not in (getattr(sc, "costume", None) or {}):
                naked.append((f.idx, cid))
    if naked:
        who = sorted({c for _, c in naked})
        out.append(f"герой в кадре без строки костюма в паспорте сцены: "
                   f"{len(naked)} кадров, кто именно: {who[:6]}")
    # 2. поза противоречит осанке
    bad = []
    for f in frames:
        post = f.posture if isinstance(f.posture, dict) else {}
        if any(v == "sitting" for v in post.values()) and \
           re.search(r"\b(stands|standing) up\b", plain(f.pose), re.I):   # 29.09.2026: pose бывает словарём
            bad.append(f.idx)
    if bad:
        out.append(f"поза спорит с осанкой: {len(bad)} кадров {bad[:6]}")
    # 3. рука из-за края кадра не описана
    hands = [f.idx for f in frames
             if re.search(r"\b(hand|arm|shoulder|fingers)\b", f.staging or "", re.I)
             and not (f.contact or "").strip()
             and len(f.in_frame or []) < 2]
    if hands:
        out.append(f"конечность из-за края кадра не описана графой contact: "
                   f"{len(hands)} кадров {hands[:6]}")
    # 4. документ назван печатным именем — модель напишет эти буквы
    doc = [f.idx for f in frames
           if re.search(r"\b(consent form|medical form|questionnaire|certificate|"
                        r"the form|the report|the letter|the note reading|sign reading)\b",
                        f.prompt_en or "", re.I)]
    if doc:
        out.append(f"предмет назван печатным именем (будут буквы): {len(doc)} кадров {doc[:6]}")
    # 5. двое и больше без дистанции
    nod = [f.idx for f in frames if len(f.in_frame or []) > 1 and not (f.distance or "").strip()]
    if nod:
        out.append(f"двое и больше в кадре без графы distance: {len(nod)} кадров {nod[:6]}")
    # 6. слой времени
    fb = [f.idx for f in frames if getattr(f, "layer", "now") != "now"]
    if fb:
        out.append(f"кадров-воспоминаний: {len(fb)} — им нужен свой паспорт {fb[:8]}")
    # 7. бланк расходится сам с собой: описание про одних, in_frame — про другого
    if names:
        out.extend(check_cast(frames, names))
    return out


# --- бланк расходится сам с собой --------------------------------------------
# Замер 10.09 (run_ceo, 335 кадров): кадры 131–137 — in_frame=[male_reporter], а
# prompt_en описывает рукопожатие Эммы и Эдриана. В генератор ушёл эталон лица
# репортёра, и 7 из 7 кадров — портрет репортёра вместо сцены. Столбец «кто в
# кадре» и столбец «что происходит» заполняются разными шагами и до сих пор
# никто не сверял их между собой.
#
# Правило: английское имя героя, встреченное в prompt_en или staging БЕЗ пометки
# «за кадром», обязано быть в in_frame. Иначе frame.form_error = "cast_mismatch",
# frame.form_missing = [id, ...] — и pipeline.step_refill считает такой кадр пустым.
_OFF_FRAME = re.compile(
    r"\b(?:beyond|outside|past|from|over|off)\s+(?:the\s+)?(?:\w+\s+)?(?:edge|frame|framing|crop)s?\b"
    r"|\boff[\s-]?(?:frame|screen)\b"
    r"|\bunseen\b|\bcropped\b"
    r"|\bnot (?:in|inside) (?:the )?frame\b"
    r"|\bout of (?:the )?frame\b"
    # «represented only by her hand», «with only his arm visible», «only her hand and
    # forearm are visible» — в кадре часть тела, самого героя нет
    r"|\brepresented (?:only )?by\b|\bwith only\b|\bonly (?:by )?(?:her|his|their) \w+",
    re.IGNORECASE)
# Считаем героя ВИДИМЫМ только когда он подлежащее своего полупредложения:
# «Emma stands on the left», «Adrian stands on the right behind it». Первая
# версия правила брала любое упоминание имени — и на run_ceo ловила 114 кадров
# из 335, из них девять десятых ложные: «Adrian's hand enters from the right»
# (в кадре рука, не Эдриан), «gives Emma a nod», «toward Adrian», «avoiding
# Adrian» (Эмма смотрит на него, его в кадре нет). Притяжательное «Adrian's …»
# не считается: в кадре предмет или рука, а не сам герой.
# Пометка «за кадром» действует на всё своё предложение (до «;» или точки):
# «Adrian and Emma remain outside the frame» гасит обоих, «Adrian remains to her
# right but outside the frame» — Эдриана. А подлежащее ищем в более мелких
# полупредложениях: «Emma stands on the left, Adrian stands on the right».
_SENT = re.compile(r"[;]|\.\s+|\n")
_CLAUSE = re.compile(r"[;]|\.\s+|,\s+|\n|\s+(?:while|as|and|but|then)\s+")
_HEAD = r"^\W*(?:(?:while|as|and|but|then|when|the|now|only|both|young|little|old)\s+)*"
# Точка в «Mr. Hayes» / «Ms. Carter» — это ещё и разделитель предложений в _SENT
# и _CLAUSE: такой вариант имени резался пополам и не мог совпасть никогда
# (проверка скептика 10.09: «Mr. Hayes stands at the window» -> никто). Точку
# после обращения снимаем и в тексте, и в вариантах имени.
_ABBR = re.compile(r"\b(Mr|Mrs|Ms|Dr)\.(?=\s)", re.IGNORECASE)
# Ушедший и спрятанный (аудит V2-04, 27.09.2026): «Ilchui fled», «Charlotte Esther Atorian remains
# completely concealed beneath the table», «Montlair and Melson have already disappeared». Имя
# стоит подлежащим, а человека в кадре нет: в состав его не возвращаем и в in_frame не требуем.
# «Makoto left, Shinozuka right» — сторона кадра, не уход: «left» только после has/have/had.
# Замер 27.09 по 78 264 кадрам: видимые меняются в 33, только вычёркиванием; все 33 просмотрены.
_УШЁЛ = (r"\s+(?:(?:has|have|had|is|are|was|were|already|now|long|just|still|remains?|stays?|"
         r"lies?|hides?|crouche?s?|sits?|kneels?)\s+){0,3}"
         r"(?:fled|departed|vanished|disappeared|absent|nowhere to be seen"
         r"|gone(?=\s*(?:[.,;:!?)]|$)|\s+(?:from|away|off|out|into|through|by|already|now|long|without)\b)"
         r"|(?<=has |ave |had )(?:already |just )?left(?=\s*(?:[.,;:!?)]|$)|\s+(?:the\s+)?(?:room|frame|"
         r"shot|house|hall|scene|building|clearing|camp|palace|office|study|chamber|corridor|courtyard)\b"
         r"|\s+(?:through|by|without|already|earlier|long)\b)"
         r"|out of (?:sight|view)(?!\s+of\b)"
         r"|(?:completely|fully|entirely|wholly|totally)\s+(?:hidden|concealed|out of (?:sight|view))"
         r"(?!\s+from\s+(?!view\b|sight\b)))(?![\w-])")


def cast_names(sk) -> Dict[str, List[str]]:
    """id героя -> как его зовут по-английски в описаниях кадров.

    В промпт герой уходит как «id: русское имя», а описание модель пишет
    латиницей: «Emma», «Adrian», «the company director». Берём то же, что
    frameprompt.en_name (id без подчёркиваний, с заглавных) плюс варианты из
    скелета, в которых есть заглавная буква («Emma», «Mr. Hayes», «Adrian's
    grandfather»). Варианты без заглавной («the reporter», «someone») — общие
    слова, по ним не сверяем: лучше пропустить, чем ловить ложное.
    """
    out: Dict[str, List[str]] = {}
    for c in (getattr(sk, "characters", None) or []):
        cid = c.get("id") or ""
        if not cid:
            continue
        al = {cid.replace("_", " ").title()}
        # adrians_grandfather -> «Adrian's grandfather»: чтобы «Adrian» внутри не
        # засчитался как сам Эдриан
        m = re.match(r"^([a-z]+)s_([a-z_]+)$", cid)
        if m:
            al.add(f"{m.group(1).title()}'s {m.group(2).replace('_', ' ')}")
        for v in (c.get("variants") or []):
            v = (v or "").strip()
            if v and re.search(r"[A-Z]", v) and not _CYR.search(v):
                al.add(v)
        out[cid] = sorted(al, key=len, reverse=True)
    return out


def cast_mentioned(text: str, names: Dict[str, List[str]]) -> set:
    """id героев, названных в тексте как ВИДИМЫЕ.

    Видимый = подлежащее полупредложения и не в притяжательной форме
    («Adrian's hand» — в кадре рука). Герой, про которого хоть где-то в тексте
    сказано «за кадром», видимым не считается вовсе: «…as Emma insists that
    he…; Emma stays outside frame» — Эмма говорит из-за кадра, а не стоит в нём.
    """
    text = _ABBR.sub(r"\1", text or "")
    order = sorted(((_ABBR.sub(r"\1", a), cid) for cid, als in names.items() for a in als),
                   key=lambda t: len(t[0]), reverse=True)
    seen, off = set(), set()
    if order:
        # ушедших и спрятанных вычёркиваем до поиска подлежащих; «Charlotte Esther Atorian»
        # длиннее варианта «Charlotte Esther» — хвост из слов с заглавной входит в имя.
        # Имя берётся одним куском без перебора (заглядывание + обратная ссылка): у «Charlotte /
        # Charlotte Esther / Charlotte Esther Atorian» перебор разборов перечисления рос как 3^n —
        # 15 имён через запятую считались 2 минуты (проверка 27.09.2026)
        имя = ("(?:" + "|".join(re.escape(a) for a, _ in order) + r")(?![\w'’-])"
               r"(?:\s+(?-i:[A-Z])[\w'’-]*){0,2}")
        text = re.sub(r"(?<![A-Za-z])((?=(" + имя + r"))\2(?:(?:\s*,\s*|\s*,?\s+and\s+)(?=(" + имя
                      + r"))\3)*)(?=" + _УШЁЛ + ")", lambda m: " " * len(m.group(1)), text,
                      flags=re.IGNORECASE)
    for sent in _SENT.split(text or ""):
        if _OFF_FRAME.search(sent):
            for alias, cid in order:   # длинные первыми и вычёркиваем: «Adrian's
                sent, n = re.subn(      # grandfather» не делает Эдриана «за кадром»
                    r"(?<![A-Za-z])" + re.escape(alias) + r"(?![A-Za-z])", " ",
                    sent, flags=re.IGNORECASE)
                if n:
                    off.add(cid)
    for clause in _CLAUSE.split(text or ""):
        if not clause.strip():
            continue
        # самое длинное имя в начале полупредложения; «Adrian Hayes’s» — это
        # притяжательное, и короткое «Adrian» внутри него уже не пробуем
        for alias, cid in order:
            m = re.match(_HEAD + re.escape(alias) + r"(?![A-Za-z])", clause,
                         flags=re.IGNORECASE)
            if m:
                if not re.match(r"['’]s\b", clause[m.end():]):
                    seen.add(cid)
                break
    return seen - off


def check_cast(frames, names: Dict[str, List[str]]) -> List[str]:
    """Ставит form_error/form_missing на кадры, где текст и in_frame про разных людей."""
    bad = []
    for f in frames:
        # описание и расстановка читаются как один текст: пометка «за кадром»
        # в staging гасит упоминание в prompt_en
        # осанка тоже называет людей, и раньше сторож её не читал: герой, названный
        # только там, проходил мимо проверки (замер 26.09)
        поза = getattr(f, "posture", None)
        поза_текст = " ".join(str(k) + " " + str(v) for k, v in поза.items()) if isinstance(поза, dict) else str(поза or "")
        want = cast_mentioned((f.prompt_en or "") + "\n" + (f.staging or "") + "\n" + поза_текст, names)
        missing = sorted(want - set(f.in_frame or []))
        if missing:
            f.form_error = "cast_mismatch"
            f.form_missing = missing
            bad.append((f.idx, missing))
        else:
            # не оставлять метку от прошлого прогона на уже починенном кадре
            if getattr(f, "form_error", "") == "cast_mismatch":
                f.form_error = ""
                f.form_missing = []
    if bad:
        return [f"описание называет героя, которого нет в in_frame (cast_mismatch): "
                f"{len(bad)} кадров — " +
                ", ".join(f"{i}:{'+'.join(m)}" for i, m in bad[:8])]
    return []


def cast_ruler(frames, тексты: Dict[int, str], имена: Dict[str, str], log=print,
               варианты: Optional[Dict[str, List[str]]] = None) -> dict:
    """Линейка: текст кадра называет героя, а в составе кадра его нет.

    Сверяет ТЕКСТ РОЛИКА (на языке ролика) с составом кадра — в отличие от check_cast,
    который сверяет английское описание, написанное по этому же составу, и потому всегда
    сходится. Ничего не чинит и не блокирует: возвращает числа.

    Настоящая беда выглядит так (кадр 62 ролика 0d537744): текст «Джулиан закрыл
    коммуникатор», а на картинке коммуникатор закрывает майор Рейес.

    ОГОВОРКА. Ложные срабатывания есть и остаются: «Синклер узнал, что Маркус не воспитывал
    сына» — Маркус назван, но в кадре ему делать нечего. Поэтому линейкой надо СРАВНИВАТЬ
    один и тот же ролик до и после правки, а не судить ролик по одному числу: ложные
    срабатывания при сравнении одинаковы с обеих сторон и взаимно гасятся.
    """
    import re as _re

    def _слова_по_героям(словарь):
        # по слову, которое принадлежит ровно одному герою (английская сторона)
        чей = {}
        for cid, als in (словарь or {}).items():
            for a in ([als] if isinstance(als, str) else als or []):
                for w in str(a).split():
                    if len(w) >= 3:
                        чей.setdefault(w.lower().strip(".,'"), set()).add(cid)
        # засчитываем слово всем владельцам: эта сторона только гасит сработку,
        # и лишняя осторожность здесь дешевле ложной тревоги
        свои = {}
        for w, cids in чей.items():
            for cid in cids:
                свои.setdefault(cid, set()).add(w)
        return свои

    def _упомянут(текст, свои):
        низ = (текст or "").lower()
        return {cid for cid, ws in (свои or {}).items() if any(w in низ for w in ws)}

    # Ищем слово, которое принадлежит ровно одному герою. Фамилия у семьи общая:
    # «В особняке Эшфордов Джулиан Эшфорд…» иначе считается упоминанием ещё четверых.
    чьё = {}
    for cid, имя in (имена or {}).items():
        for w in str(имя or "").split():
            if w[:1].isupper() and len(w) >= 3:
                чьё.setdefault(w.lower(), set()).add(cid)
    ищем = {}
    for cid, имя in (имена or {}).items():
        свои = [w for w in str(имя or "").split()
                if w[:1].isupper() and len(w) >= 3 and len(чьё.get(w.lower(), ())) == 1]
        if свои:
            # без падежного хвоста: «Итан» — действует, «Итана» — принадлежность
            ищем[cid] = _re.compile(
                "|".join(r"\b" + _re.escape(w) + r"\b" for w in свои), _re.UNICODE)
    англ_слова = _слова_по_героям(варианты)
    мимо, толпа, примеры = 0, 0, []
    for f in frames:
        t = тексты.get(getattr(f, "idx", 0)) or ""
        состав = set(getattr(f, "in_frame", None) or [])
        # Описание картинки знает про героя («as Julian speaks from outside the frame») —
        # это приём, а не беда. Беда — когда описание картинки о нём молчит вовсе.
        англ = set()
        if англ_слова:
            англ = _упомянут((getattr(f, "prompt_en", "") or "") + "\n"
                             + (getattr(f, "staging", "") or ""), англ_слова)
        если_нет = [cid for cid, rx in ищем.items()
                    if cid not in состав and cid not in англ and rx.search(t)]
        if если_нет:
            мимо += 1
            if len(примеры) < 5:
                примеры.append((f.idx, ",".join(если_нет[:2])))
        if len(состав) > 3:
            толпа += 1
    итог = {"текст_называет_кого_нет_в_кадре": мимо, "в_кадре_больше_трёх": толпа,
            "кадров": len(frames), "примеры": примеры}
    log(f"  линейка состава: текст называет героя вне кадра — {мимо} из {len(frames)}; "
        f"людей больше трёх — {толпа}")
    if примеры:
        log("    например: " + "; ".join(f"кадр {i}: {c}" for i, c in примеры))
    return итог


def check_quality(frames, scenes=None) -> List[str]:
    """Негодные значения граф. Считается мгновенно, ни одной картинки."""
    out = []
    short = [f.idx for f in frames if len((f.prompt_en or "").split()) < 20]
    if short:
        out.append(f"описание короче 20 слов: {len(short)} кадров (например {short[:6]})")
    cyr = [f.idx for f in frames if _CYR.search(f.prompt_en or "")]
    if cyr:
        out.append(f"кириллица в английском описании: {len(cyr)} кадров {cyr[:6]}")
    vague = [f.idx for f in frames if _VAGUE.search(f.prompt_en or "")]
    if vague:
        out.append(f"туманные глаголы вместо позы: {len(vague)} кадров {vague[:6]}")
    bad_emo = [f.idx for f in frames
               if f.emotion and f.emotion.lower() not in _EMOTIONS]
    if bad_emo:
        out.append(f"эмоция вне списка: {len(bad_emo)} кадров {bad_emo[:6]}")
    if scenes:
        off = []
        for f in frames:
            sc = scenes.get(getattr(f, "scene_num", 0))
            anchors = getattr(sc, "anchors", None) or []
            if anchors and f.camera_pos and f.camera_pos not in anchors:
                off.append(f.idx)
        if off:
            out.append(f"точка съёмки не из списка сцены: {len(off)} кадров {off[:6]}")
    return out


def fix_camera_runs(frames, log=print) -> int:
    """Три одинаковые крупности подряд чиним КОДОМ, а не просьбой к модели.

    Просьба «не повторяй ракурс» в промпте уже есть, и модель её всё равно
    нарушает: в первом прогоне 31 тройка подряд. Раз мы всё равно эти тройки
    считаем, дешевле их и починить — сдвинуть средний кадр на ступень по шкале.
    """
    fixed = 0
    for i in range(len(frames) - 2):
        a, b, c = frames[i], frames[i + 1], frames[i + 2]
        if a.scene_num != b.scene_num or b.scene_num != c.scene_num:
            continue
        if not a.shot_size or a.shot_size != b.shot_size or b.shot_size != c.shot_size:
            continue
        if a.shot_size not in _SHOT_SCALE:
            continue
        k = _SHOT_SCALE.index(a.shot_size)
        b.shot_size = _SHOT_SCALE[k + 1] if k + 1 < len(_SHOT_SCALE) else _SHOT_SCALE[k - 1]
        b.camera = b.shot_size
        fixed += 1
    if fixed:
        log(f"  крупность разведена кодом у {fixed} кадров")
    return fixed


_STATE_STOP = frozenset("""with into onto from over under both beside beneath behind against
across between above below that this them they their than then still again while after before
down back away longer side left right hand hands front near next around along inside outside
upon without within toward towards small large thin thick open opened closed fresh same other
another some several taken placed puts put place places leaves leave drops drop releases release
wears worn wear tightly loosely partly""".split())


def _state_keys(s: str) -> set:
    """Значимые слова записи о состоянии героя: без предлогов, служебных
    глаголов и имён (слово с большой буквы не в начале). «holds»/«hold»,
    «gloves»/«glove» — одно слово."""
    out = set()
    for k, w in enumerate(re.findall(r"[A-Za-zА-Яа-яЁё']+", s)):
        if k and w[0].isupper():
            continue
        w = w.lower().strip("'")
        if len(w) < 4 or w in _STATE_STOP:
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    return out


def carry_costume(frames, log=print) -> int:
    """Переносит смену СОСТОЯНИЯ героя вперёд по сцене: одежда, причёска,
    травмы, что в руках. Имя оставлено историческое — начиналось с одежды.

    Одежда в паспорте сцены статична, а по сюжету она меняется: герой снимает
    куртку, надевает фартук, закатывает рукава. Раньше паспорт продолжал
    требовать прежнее, спорил с действием кадра, и верхний слой мигал через
    кадр — 14 сбоев одежды из 16 в одной сцене оказались одним и тем же
    мигающим слоем. Теперь состояние живёт как состояние: сменилось — и дальше
    таким и едет до конца сцены.
    """
    cur = {}
    scene = None
    n = 0
    for f in frames:
        if f.scene_num != scene:
            scene, cur = f.scene_num, {}
        for cid, what in (getattr(f, "costume_change", None) or {}).items():
            what = str(what).strip()
            if not what:
                continue
            # Накапливаем, а не перезаписываем: «куртка снята», потом «рукава
            # закатаны» — раньше второе стирало первое, и куртка возвращалась
            # на героя со следующего кадра. Если модель уже написала полное
            # состояние (новое повторяет суть прежних записей — их первую
            # фразу до запятой) — берём его как есть; иначе склеиваем
            # «прежнее; новое», держим последние 3 изменения.
            # Запись про ту же вещь (общие значимые слова: «holds», «jacket»,
            # «bottle») — это обновление, а не добавление: «now holds a pen» →
            # «now holds the phone» руки держат одно, «jacket off» → «jacket now
            # worn» куртка одна. Без этого на run_v (147 смен, почти все — что
            # в руках) 312 кадров получали «holds a pen; holds the phone; puts
            # the phone under the textbook» разом (замер 10.09.2026). Записи про
            # РАЗНЫЕ вещи копятся, держим последние 3.
            from frameprompt import items_off   # колец импорта нет: frameprompt visuals не зовёт
            new_keys = _state_keys(what)

            РУКИ = ("hold", "carr", "grip", "clutch", "clasp")

            def _про_руки(t):
                t = (t or "").lower()
                return any(w in t for w in РУКИ)

            def _заменяет(прежняя):
                снято = items_off(прежняя)
                if снято and not any(it in (what or "").lower() for it in снято):
                    return False   # прежняя говорит, что вещь снята, а новая про неё молчит
                # руки держат одно: запись про руки всегда сменяет прежнюю запись про руки,
                # иначе в задании оказывается «держит ручку; держит телефон» разом
                if _про_руки(прежняя) and _про_руки(what):
                    return True
                k = _state_keys(прежняя)
                общих = len(k & new_keys)
                # одно общее слово при длинной прежней записи — это НЕ та же вещь:
                # «очки сняты» не должно стирать весь новый облик героя из шести предметов
                # (кадр 101: облик исчез до конца ролика, замер 25.09)
                return bool(общих) and not (общих == 1 and len(k) >= 6)

            keep = [p for p in cur.get(cid, "").split("; ") if p and not _заменяет(p)]
            lst = keep + [what]
            # запись о СНЯТОЙ вещи не вытесняем: иначе снятый пиджак молча возвращается
            хвост, старые = lst[-3:], lst[:-3]
            держим = [p for p in старые
                      if items_off(p) and not any(items_off(p)[0] in t for t in хвост)]
            cur[cid] = "; ".join((держим + хвост)[-4:])
            n += 1
        f.costume_state = dict(cur)
    if n:
        log(f"  смен состояния героев по ходу сцен: {n}, дальше состояние едет вперёд")
    return n
