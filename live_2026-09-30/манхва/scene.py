# -*- coding: utf-8 -*-
"""Паспорт сцены: то, что записывается ОДИН раз и дословно копируется во все её кадры.

Почему это отдельная сущность, а не поля кадра. Половина связности не принадлежит
ни одному кадру: время суток, погода, откуда светит, во что одеты, кто ранен, кто
слева и кто справа. Если такие вещи решает каждый кадр сам, они переголосовываются
по сорок раз за сцену — отсюда «день переходит в закат внутри одной сцены» и
«герой выходит из драки чистым».

В студии это лист постановки: место, время суток, погода, источник света, длина —
обязательные пометки, без которых лист не принимают. У нас из них было только место.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

from llm import call_json, LLMError
import claude_pool as CP
# общий предел одновременных вызовов модели на службу (MANGA_CLAUDE_CONCURRENCY); без него — прямой вызов
call_json = CP.limited(call_json)

SCENE_PASSPORT_PROMPT = """You are the art director of a manhwa recap video.
Below is one scene of the story. Fill in its passport: the facts that must stay
IDENTICAL in every single shot of this scene.

Answer in English, in JSON, nothing else:
{{"time_of_day": "<morning|midday|afternoon|dusk|night|late night>",
  "weather": "<clear|overcast|rain|snow|indoor - no weather>",
  "light": "<one sentence: what the light source is, warm or cold, which way shadows fall>",
  "palette": "<three or four colours that dominate this scene, comma separated>",
  "exposure": "<overall brightness of the picture from ANY camera position in this scene:
                'dim', 'medium' or 'bright' — one word>",
  "costume": {{"<character id>": "<head to toe, as worn in the FIRST SHOT of this scene: outer
                layer and whether it is on or off, top, bottom, shoes, and any accessory that is
                visible. EVERY garment with its COLOUR and type ('burgundy knee-length dress',
                'navy cardigan', 'charcoal trousers'); for a shirt-and-jacket outfit always say
                'black tie' or 'no tie, collar open'. A snapshot of the start, never the story
                of the scene, and NEVER a reference to another scene>"}},
  "costume_evidence": {{"<character id>": "<the exact words of the STORY TEXT that decide this
                outfit (a short quotation in the language of the text), or the single word
                'inherited' when the text says nothing about what this character wears>"}},
  "condition": {{"<character id>": "<visible physical state in the FIRST SHOT of this scene:
                injuries, dirt, wet, tears, or 'normal'>"}},
  "sides": {{"<character id>": "<left|right|centre>"}},
  "crowd": "<NAMELESS extras only, present through the whole scene: 'empty', 'two or three
              students at the far desks', 'a full classroom', 'a few passers-by'. Never a
              character from CHARACTERS PRESENT>",
  "props": [{{"name": "<the object, 1-3 words>", "who": "<character id who holds or owns it>",
              "look": "<what it looks like, 3-8 words — colour, material, shape only.
                        NEVER a subject, a title or anything that implies printed words:
                        write 'thick blue hardcover book', not 'calculus textbook'; for
                        paper write 'folded cream sheet, almost blank', never 'handwritten
                        letter'>"}}],
  "surfaces": {{"walls": "<colour and material: 'cream painted plaster, scuffed near the floor'>",
                "floor": "<colour and material: 'honey-coloured wooden boards'>",
                "ceiling": "<colour and what hangs from it>",
                "windows": "<shape, frame colour, and WHAT IS SEEN THROUGH THEM from the
                            height of THIS floor: for an upper floor only sky, skyline, the
                            city far below — never people or vehicles>",
                "wear": "<how worn or tidy the place is: 'clean and new', 'damp stains,
                          peeling corners, cluttered'>"}},
  "room": {{"width": <metres>, "depth": <metres>,
            "items": [{{"name": "<english noun, 1-3 words>", "x": <m>, "y": <m>,
                        "w": <m>, "d": <m>, "kind": "furniture|door|window|fixture",
                        "look": "<colour and material, 2-5 words: 'dark green chalkboard',
                                  'pale wooden desk', 'steel-framed window'>"}}]}}
}}

RULES
- time_of_day and weather: take them from the story text if it says; otherwise choose
  what fits and STICK to it. Never answer "unknown".
- weather is the weather WHERE THE CAMERA IS. If the scene happens indoors, weather is
  always "indoor - no weather", even when it snows outside — otherwise snow gets drawn
  lying on the sofa and the table inside the flat (measured: it did, in most shots of one
  scene). Weather seen through a window belongs in "light", not here.
- exposure is how bright the finished picture is overall, and it must be the SAME from
  every camera position: turning the camera from the window wall to the dark wall must
  not turn a bright picture into a dim one. Measured: 152 of 152 brightness jumps between
  neighbouring shots happened exactly when the camera moved.
- light: name one main source. "cold white ceiling tubes, shadows short and downward",
  "low sun through a west window, long shadows to the east". Not "dramatic lighting".
- costume must be COMPLETE and leave nothing to decide. Say explicitly whether the
  jacket/blazer/coat is worn or not, whether sleeves are rolled, whether a bag is carried.
  A missing garment is written as "NO coat" — never "coat absent" or "coat not worn": the
  drawer paints every garment it reads, so "camel coat absent" produces a camel coat.
  A half-description ("school uniform") makes every shot re-decide the missing half, and the
  blazer then appears and disappears between neighbouring shots. Write "white shirt, NO blazer,
  dark trousers, black shoes", not "school uniform".
- costume comes from the STORY TEXT ONLY: a quotation ("she pulled her coat tighter") or its
  direct consequence ("in uniform" -> the uniform; "came straight from school" -> the school
  clothes). NEVER dress a character by the place or by the job the place suggests: a
  schoolgirl visiting a hospital is a visitor in her own clothes, not a nurse in a white
  coat; a guest in a shop wears no apron; a visitor in a kitchen wears no chef's whites.
  Measured: the passport put the schoolgirl into a doctor's white coat in the hospital
  scene (frame 145) only because the text never described her clothes there.
- If the text of THIS scene says nothing about what a character wears, do NOT invent an
  outfit for the place: write "inherited" in costume_evidence and describe, still head to
  toe, the plainest continuation of what such a person keeps on between two scenes (the
  same everyday clothes, no uniform of the place). The pipeline then replaces it with the
  last outfit actually known from an earlier scene.
- costume is CONCRETE WORDS ONLY, even when the outfit continues from an earlier scene.
  FORBIDDEN in costume: "as previously worn", "same as before", "same as in scene N",
  "same everyday outer layer", "her usual clothes", "unchanged" — any reference instead of
  clothes. This line is the only clothing the shot ever sees; a reference gives the shot
  nothing to draw, and it re-invents the outfit every time. Measured: "same everyday outer
  layer, top and bottom as previously worn" went into 19 shots of one scene, and the
  blouse, blazer and skirt changed between neighbouring shots (frames 26-52). Write the
  clothes out in words again: "navy cardigan over a cream blouse, charcoal trousers".
- Every garment needs a COLOUR. "elegant dress" is re-coloured in every shot (measured:
  burgundy, blue, black and beige in five shots of one scene); "burgundy knee-length
  dress" stays burgundy. Same for the jacket, the blouse, the skirt, the shoes.
- The tie is never implied. With a shirt, always write either "black tie" (colour + tie)
  or "no tie, collar open". Measured: with the tie unmentioned it appeared in 7 shots of
  scenes where the text had him open-collared.
- costume_evidence must have an entry for EVERY character in costume: either the quotation
  or "inherited". Nothing else is accepted there.
- costume and condition describe the FIRST SHOT of the scene ONLY: the starting state, a
  snapshot, not what happens later. If the jacket comes off half-way through, write "jacket
  ON"; if coffee is spilled in the middle, condition is "normal". Never write "initially",
  "then", "later", "by the end", "after" — this line is copied into EVERY shot of the scene,
  so a coffee stain described here was drawn eight shots BEFORE the cup was spilled
  (measured: scene 2, shots 29-41, eight wrong shots). What changes during the scene reaches
  the shots through their own state notes, not through the passport.
- crowd is nameless background only: extras, passers-by, staff, a full classroom. NEVER put a
  character from CHARACTERS PRESENT into crowd — the line is copied into every shot of the
  scene, including the shots where that character is absent, and a second figure gets drawn.
  Measured: "the company director by the door" went into all 44 shots of a one-person scene
  and produced an extra silhouette in three of them.
- props: the objects the story carries through this scene — a note, a phone, a bottle, a bag.
  These are the things a reader tracks; if they vanish between shots the scene falls apart.
- condition: EITHER the single word "normal", OR a short list of marks ADDED on top of the
  person in this scene — dirt, blood, bandages, wet clothes, tears, snow on the shoulders.
  Never write "normal, with ..." — a compound like "normal, with a blanched lip" is read as
  a facial injury and comes back as a bruise in every shot. Never mention hair colour, eye
  colour, age or build: those come from the character sheet, and repeating them repaints the
  character. Never describe the mouth, lips or eyes unless there is a real visible injury.
- sides: assign each character a side of the frame ONCE for the whole scene. Two people
  talking must not swap sides. If only one character, put them left or right, not centre,
  unless the scene is a single portrait.
- room: a top-down floor plan of the place, seen from above, in metres. x is measured
  from the WEST wall, y from the NORTH wall, to the near corner of the object.
  Include 4 to 7 objects, and always the door and at least one window if the place has one.
  This plan is what keeps the room the same across shots — be concrete, not decorative.
- surfaces are what makes the room recognisable when the furniture is out of shot: the wall
  colour, the floor material, the light fixture, the view through the window. A room whose
  walls turn from cream to grey reads as a different room even with the same table.
- windows show ONLY what is visible from the height of this floor. Unless the room is at
  street level, no people and no vehicles are described behind the glass: write "city
  skyline, street far below and not visible", not "journalists and vans below". Measured:
  "windows showing office vans, security guards and clustered journalists below" put the
  crowd and the vans right behind the glass at floor level in 10 shots of an upper-floor
  office. What happens down in the street belongs to the story, not to the window.
- The item list must include EVERY large thing the eye reads as part of the room:
  the ceiling light, the kitchen counter or sink if there is one, the sofa, the bed, the
  wardrobe, the stove. What is missing from the list gets re-invented in every shot and
  the room stops being the same room — measured: the ceiling lamp changed shape in 13
  frames out of 22 because it was not in the list.
- The item list is the COMPLETE contents of this place. Anything not listed does not exist
  here: no extra bookcase, no map on the wall, no poster. Give every item a "look" so the
  same object is the same colour in every shot — a board that turns from green to white
  makes the room read as a different room.
- If two people share one piece of furniture, put ONE item in the list, not two, and say so
  in its name ("one long shared desk").
- If the scene happens outdoors, still give a "room": the open area with its landmarks
  (wall, tree, car, bench) placed on the same grid.

SCENE {n}: {where}
CHARACTERS PRESENT: {who}
STORY TEXT:
{text}
"""


# --- защита от «дуги» в паспорте ------------------------------------------------
# Паспорт иногда описывает не старт сцены, а её ход: «suit jacket initially worn
# then removed … coffee stain across jacket chest». Такая строка уходит в SAME
# THROUGHOUT во ВСЕ кадры сцены — замер run_ceo, сцена 2: пятно кофе нарисовано
# за 8 кадров ДО пролива (8 ACTION в кадрах 29–41). Промпт теперь просит «первый
# кадр», а здесь — страховка: от описания оставляем только то, что до маркера.
_ARC_END = re.compile(r"\b(by the end|at the end|by the close|by now|eventually)\b", re.I)
_ARC_SEQ = re.compile(r"\b(initially|at first|then|later|afterwards?|after)\b", re.I)
_ARC_TAIL = re.compile(r"[\s,]*\b(and|but|then|which|while|who)\s*$", re.I)


# Паспорт круга 3 (run_ceo, сцены 6–7): «camel knee-length coat absent, cream blouse…».
# Рисовальщик читает предмет, а не оговорку, и надевает пальто. Сегмент
# «<что угодно> <предмет> absent / not worn / missing / left behind» → «no <предмет>».
# «coat OFF and carried» не трогаем — пальто в руках и есть смысл текста.
_ABSENT_ITEM = re.compile(
    r"^(?P<head>.*?\b(?P<item>jacket|blazer|overcoat|raincoat|trench ?coat|coat|cardigan|hoodie|sweater|"
    r"jumper|vest|waistcoat|apron|necktie|tie|scarf|cap|hat|beanie|helmet|gloves?|glasses|sunglasses|"
    r"watch|bag|backpack|purse|umbrella|mask|badge|lanyard|earrings|ring|bracelet|necklace)\b[^,;]*?)"
    r"\s+(?:absent|not worn|not present|missing|left (?:at home|behind|off)|isn't worn|is not worn)\s*$",
    re.I)


def negate_absent(text: str) -> str:
    """«camel knee-length coat absent» → «no coat» (по сегментам между запятыми)."""
    text = str(text or "")
    if not text or not re.search(r"\b(absent|not worn|not present|missing|left (?:at home|behind|off)|isn't worn|is not worn)\b", text, re.I):
        return text
    out = []
    for seg in re.split(r"(\s*[,;]\s*)", text):
        m = _ABSENT_ITEM.match(seg.strip()) if seg.strip() and not re.match(r"\s*[,;]\s*$", seg) else None
        out.append(("no " + m.group("item").lower()) if m else seg)
    return "".join(out)


def start_state(text: str) -> str:
    """Состояние на ПЕРВЫЙ кадр сцены из описания, в котором могла оказаться дуга.

    По сегментам между запятыми: сегмент про конец («jacket worn by the end of
    the scene», «ring on her finger by the end») выбрасывается целиком — в первом
    кадре этого ещё нет; в сегменте с «initially/then/later/after» остаётся то, что
    до первого маркера («jacket initially worn then removed» -> «jacket»; «camera
    carried and later concealed» -> «camera carried»). Остальное не трогается.
    """
    text = negate_absent(str(text or ""))
    if not text or not (_ARC_END.search(text) or _ARC_SEQ.search(text)):
        return text
    out = []
    for seg in re.split(r"\s*[,;]\s*", text.strip()):
        if not seg:
            continue
        if _ARC_END.search(seg) and not _ARC_SEQ.search(seg):
            continue                      # описан конец сцены — в начале этого нет
        m = _ARC_SEQ.search(seg)
        if m:
            head = seg[:m.start()]
            if not seg[m.end():].strip():
                # маркер стоит в конце («… and removed afterward»): позднее действие —
                # вся последняя связка, режем по последнему «and»
                k = head.rfind(" and ")
                if k > 0:
                    head = head[:k]
            head = _ARC_TAIL.sub("", head).strip(" ,;(")   # «coat (later removed)» -> «coat»
            if not head:
                continue                  # сегмент начинался с маркера: «then removed»
            seg = head
        out.append(seg.strip(" ,;"))
    return ", ".join(x for x in out if x) or text


def crowd_without_cast(crowd: str, cast: List[str],
                       names: Optional[Dict[str, str]] = None):
    """Вырезает из массовки героев паспорта. Возвращает (текст, [что вырезано]).

    Замер run_ceo, сцена 4: crowd «the company director by the door» ушёл строкой
    WHO ELSE IS HERE во все 44 кадра при «CAST: exactly 1» — лишний силуэт в 101,
    111, 116 (EXTRA). Герой либо в составе кадра, либо его нет; в массовке ему не
    место. Совпадение ищется по id как фразе («company director») и по полному
    имени; одно имя «Emma» нарочно не берём — «Emma's colleagues» это массовка.
    """
    crowd = str(crowd or "")
    if not crowd or not cast:
        return crowd, []

    def norm(x: str) -> str:
        return re.sub(r"[’']", "", str(x).lower())

    keys = set()
    for cid in cast:
        w = re.sub(r"[_\-]+", " ", str(cid)).strip()
        if w:
            keys.add(norm(w))
        nm = str((names or {}).get(cid, "") or "").strip()
        if nm and " " in nm:
            keys.add(norm(nm))
    pats = [re.compile(r"\b" + re.escape(k) + r"\b") for k in keys if k]
    if not any(p.search(norm(crowd)) for p in pats):
        return crowd, []
    keep, cut = [], []
    for seg in re.split(r"\s*;\s*|\s*,\s*|\s+and\s+", crowd.strip()):
        if not seg:
            continue
        (cut if any(p.search(norm(seg)) for p in pats) else keep).append(seg)
    if not cut:
        return crowd, []
    return ("; ".join(keep) or "empty"), cut


# --- костюм-«ссылка» -----------------------------------------------------------
# Паспорт иногда пишет не одежду, а отсылку к прошлой сцене: «same everyday outer
# layer, top and bottom as previously worn». Такая строка уходит в SAME THROUGHOUT
# всех кадров сцены, а рисовать по ней нечего — наряд выдумывается заново в каждом
# кадре. Замер run_ceo круг 2, сцена 2: 19 промптов без конкретной одежды, кадры
# 26 27 28 33 41 51 52 скачут между блузкой, пиджаком и юбками. Промпт теперь
# запрещает отсылки; здесь — страховка кодом: отсылку заменяем текстом одежды из
# последней более ранней сцены, где герой описан конкретно.
# Скептик 10.09: обороты сужены до отсылок к ДРУГОЙ СЦЕНЕ. Голое «previously» /
# «same as» / «the usual» ловило и конкретную одежду («cardigan previously
# unbuttoned», «navy blazer, same as her sister's», «the usual school uniform:
# navy blazer…») — и весь наряд подменялся нарядом прошлой сцены.
_COSTUME_REF = re.compile(
    r"\b(as previously|previously worn|as before|same as (?:before|earlier|in scene|"
    r"in the (?:previous|earlier|last) scene|last time)|same everyday|"
    r"as in (?:the )?(?:previous |earlier |last )?scene|unchanged from|"
    r"(?:the|her|his|their) usual (?:clothes|clothing|outfit|attire|wear|look)|"
    r"earlier scene|the previous scene)\b", re.I)


def is_costume_ref(text: str) -> bool:
    """True, если костюм описан отсылкой, а не одеждой."""
    return bool(_COSTUME_REF.search(str(text or "")))


# --- окно на уровне земли ---------------------------------------------------------
# Паспорт места сцены 4 написал в windows «showing office vans, security guards and
# clustered journalists below» — и модель поставила толпу и фургоны вплотную за
# стеклом на уровне пола (run_ceo круг 2: 93 95 97 100 103 109 114 119 128 134 —
# 10 кадров, в круге 1 таких 0). Люди и машины «внизу» за окном верхнего этажа —
# это то, что видно с высоты, значит писать надо «город далеко внизу, улицы не
# видно». Промпт теперь об этом просит; здесь — защитная замена для уже собранных
# scenes.json.
_WIN_PEOPLE = re.compile(
    r"\b(journalists?|reporters?|photographers?|press|crowds?|people|passers-?by|"
    r"guards?|security|police|officers?|vans?|cars?|trucks?|vehicles?|buses|taxis?)\b", re.I)
# Скептик 10.09: «at street level / in the street / on the pavement» убраны — это
# приметы уровня земли, а не высоты; с ними окно кафе на первом этаже получало
# «city far below». Комната, названная первым этажом, не трогается вовсе.
_WIN_BELOW = re.compile(r"\b(below|beneath|down below|far down|downstairs)\b", re.I)
_WIN_GROUND = re.compile(r"\b(ground[- ]floor|street[- ]level|at street|"
                         r"shop|cafe|café|storefront)\b", re.I)
_WIN_VERB = re.compile(r"\b(showing|overlooking|revealing|with a view (?:of|over)|"
                       r"looking (?:out )?(?:on|over|onto|down on)|opening onto|"
                       r"giving onto|framing)\b", re.I)
_WIN_HIGH = "showing the city far below, street not visible"


def windows_from_height(text: str) -> str:
    """Убирает людей и машины из вида за окном верхнего этажа.

    Срабатывает только когда в описании есть и люди/машины, и «below/street»:
    окно кафе на уровне улицы с прохожими («passers-by outside») не трогается.
    Часть до глагола вида («tall black-framed windows») сохраняется — по ней
    комната узнаётся; хвост заменяется на вид с высоты.
    """
    text = str(text or "")
    if not text or not (_WIN_PEOPLE.search(text) and _WIN_BELOW.search(text)):
        return text
    if _WIN_GROUND.search(text):
        return text
    m = _WIN_VERB.search(text)
    p = _WIN_PEOPLE.search(text)
    cut = min(x.start() for x in (m, p) if x)
    head = text[:cut].rstrip(" ,;")
    return (head + " " + _WIN_HIGH).strip() if head else "windows " + _WIN_HIGH


@dataclass
class Scene:
    num: int
    where: str = ""
    time_of_day: str = ""
    weather: str = ""
    light: str = ""
    palette: str = ""
    exposure: str = ""
    costume: Dict[str, str] = field(default_factory=dict)
    # Откуда взялась одежда: цитата из текста или «inherited». Кадр 145: школьница
    # в больнице получила белый халат врача — паспорт одел её по месту, а не по
    # тексту. Поле нужно, чтобы это было видно в логе и чтобы «inherited»
    # можно было заменить последним известным нарядом из более ранней сцены.
    costume_evidence: Dict[str, str] = field(default_factory=dict)
    props: List[dict] = field(default_factory=list)
    crowd: str = ""
    condition: Dict[str, str] = field(default_factory=dict)
    sides: Dict[str, str] = field(default_factory=dict)
    room: dict = field(default_factory=dict)
    surfaces: dict = field(default_factory=dict)
    plan_path: str = ""
    plan_line: str = ""
    anchors: List[str] = field(default_factory=list)

    def cast_ids(self) -> List[str]:
        """Кто вообще есть в этой сцене по паспорту — ключи одежды/состояния/сторон."""
        ids: List[str] = []
        for d in (self.costume, self.condition, self.sides, self.costume_evidence):
            for cid in (d or {}):
                if cid not in ids:
                    ids.append(str(cid))
        return ids

    def constants_block(self, names: Optional[Dict[str, str]] = None) -> str:
        """Дословно один и тот же текст во все кадры сцены."""
        bits = []
        when = " ".join(x for x in (self.time_of_day, self.weather) if x
                        and "no weather" not in x)
        if when:
            bits.append(f"TIME: {when}.")
        if self.light:
            bits.append(f"LIGHT: {self.light}.")
        if self.palette:
            bits.append(f"PALETTE: {self.palette}.")
        if self.exposure:
            bits.append(f"EXPOSURE: the picture is {self.exposure} overall, and it stays "
                        f"{self.exposure} from every camera position in this scene.")
        # Массовка без героев паспорта — страховка для уже собранных scenes.json
        # (run_ceo, сцена 4: «the company director by the door» в 44 кадрах).
        crowd, _ = crowd_without_cast(self.crowd, self.cast_ids(), names)
        if crowd:
            # Без этой строки класс то пустой, то битком — между соседними
            # кадрами непрерывного действия. Правило про массовку в промпте
            # было только про лица и молчало про количество.
            bits.append(f"WHO ELSE IS HERE: {crowd}.")
        return " ".join(bits)

    def wear_block(self, who: List[str], names: Dict[str, str]) -> str:
        """Одежда и состояние — по каждому, кто в кадре. Только на них."""
        out = []
        for cid in who:
            nm = names.get(cid, cid)
            # Только состояние на первый кадр: хвосты «initially … then …»,
            # «by the end» режутся (см. start_state и замер сцены 2).
            c = start_state((self.costume or {}).get(cid, ""))
            st = start_state((self.condition or {}).get(cid, ""))
            st = "" if st.lower() in ("normal", "none", "") else st
            if c and st:
                out.append(f"{nm} wears {c}; {st}")
            elif c:
                out.append(f"{nm} wears {c}")
            elif st:
                out.append(f"{nm}: {st}")
        return ("SAME THROUGHOUT THIS SCENE: " + "; ".join(out) + ".") if out else ""

    def surfaces_block(self) -> str:
        """Поверхности — то, по чему комната узнаётся, когда мебели в кадре нет.

        Замер: комната «менялась» в 73 замечаниях, и чаще всего это была не мебель,
        а стены, пол и потолочный светильник — их в паспорте не было вовсе.
        """
        sf = dict(self.surfaces or {})
        # Страховка для уже собранных scenes.json: толпа и фургоны «below» за окном
        # верхнего этажа (run_ceo круг 2, сцена 4, 10 кадров) — см. windows_from_height.
        if sf.get("windows"):
            sf["windows"] = windows_from_height(sf["windows"])
        order = [("walls", "walls"), ("floor", "floor"), ("ceiling", "ceiling"),
                 ("windows", "windows"), ("wear", "condition")]
        bits = [f"{label}: {sf[k]}" for k, label in order if sf.get(k)]
        return ("SURFACES OF THIS PLACE, identical in every shot — "
                + "; ".join(bits) + ".") if bits else ""

    def props_block(self, names: Dict[str, str], seen=None) -> str:
        """Реквизит сцены — дословно во все её кадры.

        Разбор 493 кадров показал: ключевой предмет теряется между соседними
        кадрами (смятая записка превращается в тетрадь), потому что графы
        «реквизит» не было ни на одном уровне бланка. Зритель следит именно
        за предметом — на нём держится сюжет сцены.
        """
        if not self.props:
            return ""
        # Перечисляем ВСЕ предметы сцены как её опознавательные приметы, но
        # требуем нарисовать только те, что действительно в кадре: иначе
        # телефон и бутылка появятся в каждом кадре сцены.
        seen = [str(x).lower() for x in (seen or [])]
        out = []
        from floorplan import blank_writing
        for p in self.props:
            nm = names.get(p.get("who", ""), p.get("who", ""))
            look = (p.get("look") or "").strip()
            # Газета/бейдж/планер: «печать неразборчивыми штрихами» прямо у предмета —
            # общий запрет текста в NEG газету не спасал (круг 3: 6 кадров сцены 3).
            out.append(blank_writing(f"{p.get('name')}" + (f" ({look})" if look else "")) +
                       (f", carried by {nm}" if nm else ""))
        head = ("OBJECTS OF THIS SCENE, always the same ones, never replaced by "
                "something similar: " + "; ".join(out) + ".")
        if seen:
            vis = [p.get("name") for p in self.props
                   if str(p.get("name", "")).lower() in seen]
            if vis:
                head += (" Visible in THIS shot: " + ", ".join(vis) +
                         ". The other objects are not in this shot at all.")
            else:
                head += " None of them is visible in this shot."
        return head

    def side_of(self, cid: str) -> str:
        return (self.sides or {}).get(cid, "")


def build(scenes_in: List[dict], src: str, model: str = "claude:sonnet",
          workers: int = 4, log=print,
          names: Optional[Dict[str, str]] = None) -> Dict[int, Scene]:
    """scenes_in — сцены скелета: {num, where, who, start, end}.
    names — cid -> имя героя (для чистки массовки от героев; можно не давать)."""
    out: Dict[int, Scene] = {}

    def one(sc: dict):
        n = int(sc.get("num") or sc.get("n") or 0)
        text = src[sc.get("start", 0):sc.get("end", 0)][:6000]
        prompt = SCENE_PASSPORT_PROMPT.format(
            n=n, where=sc.get("where", ""),
            who=", ".join(sc.get("who") or []) or "(none named)",
            text=text)
        s = Scene(num=n, where=sc.get("where", ""))
        try:
            data, _ = call_json(prompt, model=model, timeout=300, retries=1)
        except LLMError as e:
            log(f"  сцена {n}: паспорт не собрался — {e}")
            out[n] = s
            return
        # Полнота комнаты: модель иногда отдаёт room без предметов (run_ceo, сцена 7,
        # «Northbridge Technologies» — 0 предметов, чертёж не рисуется, прогон
        # останавливается на проверке). Дозапрашиваем один раз, как ряд роста.
        room0 = data.get("room") if isinstance(data.get("room"), dict) else {}
        if not (room0.get("items") or []):
            try:
                data2, _ = call_json(prompt + "\n\nYour previous answer left \"room\" empty. "
                                     "The room MUST list 3-8 concrete items (door, window, desk, "
                                     "chairs, cabinet…) with their positions and looks — invent a "
                                     "plausible layout for this place if the text does not describe one.",
                                     model=model, timeout=300, retries=1)
                if isinstance(data2.get("room"), dict) and (data2["room"].get("items") or []):
                    data["room"] = data2["room"]
                    log(f"  сцена {n}: комната дозапрошена — {len(data2['room']['items'])} предметов")
            except LLMError as e:
                log(f"  сцена {n}: дозапрос комнаты не удался — {e}")
        for k in ("time_of_day", "weather", "light", "palette", "crowd", "exposure"):
            setattr(s, k, str(data.get(k, "") or "").strip())
        s.props = data.get("props") if isinstance(data.get("props"), list) else []
        for k in ("costume", "condition", "sides", "costume_evidence"):
            v = data.get(k)
            setattr(s, k, v if isinstance(v, dict) else {})
        s.room = data.get("room") if isinstance(data.get("room"), dict) else {}
        s.surfaces = data.get("surfaces") if isinstance(data.get("surfaces"), dict) else {}
        # Люди и машины за окном верхнего этажа — в scenes.json уходит вид с высоты
        # (run_ceo круг 2, сцена 4: толпа за стеклом на уровне пола в 10 кадрах).
        w0 = str((s.surfaces or {}).get("windows", "") or "")
        w1 = windows_from_height(w0)
        if w1 != w0:
            s.surfaces["windows"] = w1
            log(f"  сцена {n}: за окном были люди/машины «{w0[:60]}…» -> «{w1[:60]}»")
        # Дуга вместо старта — режем сразу, чтобы в scenes.json (а значит и в лист
        # костюма, и в наследование одежды) ушло состояние первого кадра.
        for k in ("costume", "condition"):
            d = getattr(s, k) or {}
            for cid, v in list(d.items()):
                nv = start_state(v)
                if nv != str(v or ""):
                    log(f"  сцена {n}: {k} {cid} описан как ход сцены, оставлен старт: "
                        f"«{str(v)[:60]}…» -> «{nv[:60]}»")
                    d[cid] = nv
        cast = list(dict.fromkeys(list(sc.get("who") or []) + s.cast_ids()))
        s.crowd, cut = crowd_without_cast(s.crowd, cast, names)
        if cut:
            log(f"  сцена {n}: из массовки вырезаны герои паспорта: {cut}")
        out[n] = s
        log(f"  сцена {n}: {s.time_of_day or '?'} / {s.weather or '?'} / "
            f"предметов в плане {len((s.room.get('items') or []))}")
        # Основание одежды — в лог по каждому герою: без этого «белый халат на
        # школьнице» (кадр 145) виден только на готовой картинке.
        for cid in (s.costume or {}):
            ev = str((s.costume_evidence or {}).get(cid, "") or "").strip()
            log(f"    одежда {cid}: {ev[:70] if ev else '(основание не указано)'}")

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(CP.carry(one), scenes_in))
    inherit_costumes(out, log=log)
    return out


def inherit_costumes(scenes: Dict[int, Scene], log=print) -> int:
    """Где паспорт честно написал «inherited», одежда берётся из последней более
    ранней сцены, в которой этот герой был одет, — а не из догадки по месту.

    Паспорт каждой сцены собирается отдельно и параллельно, поэтому сам он
    предыдущего наряда не знает; наследование делается здесь, кодом, по порядку
    сцен. Одинаковый текст костюма даёт и одинаковый лист костюма в sheets.
    """
    last: Dict[str, str] = {}
    last_n: Dict[str, int] = {}      # из какой сцены взят конкретный наряд — для лога
    changed = 0
    for n in sorted(scenes):
        s = scenes[n]
        if not isinstance(s.costume, dict):
            # Паспорт без одежды (LLM вернул null) — иначе запись наряда падает
            s.costume = {}
        for cid, ev in list((s.costume_evidence or {}).items()):
            if str(ev or "").strip().lower() != "inherited":
                continue
            if cid in last:
                if (s.costume or {}).get(cid) != last[cid]:
                    changed += 1
                    log(f"  сцена {n}: одежда {cid} унаследована из более ранней сцены")
                s.costume[cid] = last[cid]
            else:
                log(f"  сцена {n}: одежда {cid} — текст молчит, раннего наряда нет, "
                    f"остаётся догадка паспорта")
        # Костюм-«ссылка» («same … as previously worn») при цитате в evidence: паспорт
        # нашёл в тексте туфли и сумку, а одежду описал отсылкой. Замер run_ceo круг 2,
        # сцена 2 (Эмма): 19 промптов без одежды, 7 кадров с другим нарядом. Отсылку
        # заменяем текстом из последней сцены, где герой описан конкретно.
        for cid, c in list((s.costume or {}).items()):
            if not is_costume_ref(c):
                continue
            if cid in last:
                changed += 1
                log(f"  сцена {n}: одежда {cid} описана отсылкой «{str(c)[:50]}…» — "
                    f"взят текст из сцены {last_n[cid]}: «{last[cid][:60]}»")
                s.costume[cid] = last[cid]
            else:
                log(f"  сцена {n}: одежда {cid} описана отсылкой «{str(c)[:50]}…», "
                    f"а конкретного наряда в ранних сценах нет — остаётся как есть")
        for cid, c in (s.costume or {}).items():
            # Наследуется только конкретная одежда: отсылка, не нашедшая источника,
            # не должна стать «последним нарядом» для следующих сцен.
            if str(c or "").strip() and not is_costume_ref(c):
                last[cid] = c
                last_n[cid] = n
    return changed


def draw_plans(scenes: Dict[int, Scene], out_dir: str, log=print) -> None:
    """Чертежи рисуются кодом — ни одной генерации, ни одного кредита."""
    import os
    from floorplan import from_json
    os.makedirs(out_dir, exist_ok=True)
    for n, s in sorted(scenes.items()):
        if not (s.room or {}).get("items"):
            log(f"  сцена {n}: плана нет, чертёж пропущен")
            continue
        plan = from_json(f"scene{n}", s.room)
        s.plan_path = plan.draw(os.path.join(out_dir, f"scene{n}.png"))
        s.plan_line = plan.layout_line()
        s.anchors = plan.anchors()
    log(f"  чертежей нарисовано: {sum(1 for s in scenes.values() if s.plan_path)}")


# --- относительный рост -------------------------------------------------------
# Сантиметры модель игнорирует, сравнение — исполняет. Без общей шкалы каждый
# кадр решает рост заново: в одном герой выше собеседника, в соседнем вровень.
# В студии это отдельный лист сравнения ростов, который раздают всем без исключения.

HEIGHT_PROMPT = """Below are the characters of one story with their appearance.
Rank them by height and describe the differences the way an artist can draw them.

Answer in JSON only:
{{"order": ["<tallest character id>", "...", "<shortest character id>"],
  "notes": {{"<character id>": "<how this character compares to the others, e.g.
             'a head taller than shen_buyu, same height as the father'>"}}}}

Return only the order, tallest first. Nothing else.
The list MUST contain EVERY character id listed below, all of them, none skipped and none
invented. A partial list leaves the missing characters without a height at all.

CHARACTERS:
{cast}
"""


def heights(characters: List[dict], model: str = "codex:gpt-5.6-sol",
            log=print) -> Dict[str, str]:
    cast = "\n".join(f"  - {c.get('id')}: {c.get('name')} — {c.get('appearance_en','')}"
                     for c in characters)
    try:
        data, _ = call_json(HEIGHT_PROMPT.format(cast=cast), model=model,
                            timeout=240, retries=1)
    except LLMError as e:
        log(f"  рост: не собрался — {e}")
        return {}
    # Раньше модель писала на каждого свободную заметку вида «шире, но ниже
    # store_manager» — и она попадала в кадр, где store_manager нет вовсе.
    # Порядок по росту однозначен и всегда пересекается с составом кадра.
    ids = [str(c.get("id")) for c in characters if c.get("id")]
    order = [str(x) for x in (data.get("order") or []) if str(x) in ids]

    # Молчаливую неполноту здесь принимать нельзя: в прошлом прогоне модель
    # вернула 7 героев из 13, код это проглотил, и в кадрах с остальными
    # шестью строки роста не было вовсе — а по отчёту всё выглядело исправно.
    missing = [i for i in ids if i not in order]
    if missing:
        log(f"  рост: не хватило {len(missing)} героев ({missing}), доспрашиваю")
        try:
            more, _ = call_json(
                HEIGHT_PROMPT.format(cast=cast) +
                "\n\nYou previously left out these ids: " + ", ".join(missing) +
                ".\nReturn the FULL order again, with every id present.",
                model=model, timeout=240, retries=1)
            second = [str(x) for x in (more.get("order") or []) if str(x) in ids]
            if len(second) > len(order):
                order = second
        except LLMError as e:
            log(f"  рост: повтор не вышел — {e}")
    missing = [i for i in ids if i not in order]
    if missing:
        log(f"  ! рост: остались без места в ряду: {missing}. "
            f"В кадрах с ними строка роста выводиться НЕ БУДЕТ — "
            f"лучше промолчать, чем соврать про рост")
    log(f"  рост: порядок из {len(order)} героев из {len(ids)}")
    return {cid: str(i) for i, cid in enumerate(order)}


# --- паспорт персонажа --------------------------------------------------------
# Лист лица задаёт внешность картинкой, но между кадрами всё равно плывёт тон:
# у отца в сцене 6 волосы то с сединой, то каштановые — замечено глазами на
# четырёх подряд идущих кадрах. Причина структурная: цвет в промпте не назван
# ни разу, он есть только на картинке, а картинка каждый раз интерпретируется
# заново. Лечение — короткая неизменная строка на героя, которая уходит дословно
# в КАЖДЫЙ кадр с этим героем, рядом с картинкой, а не вместо неё.

LOCK_PROMPT = """For each character below write the permanent facts that must never change
between shots. Замер показал, где эти факты рвутся: в кадрах, где ЛИЦА НЕ ВИДНО — со спины,
сверху, во врезках. Значит одного описания лица мало: нужно то, по чему героя узнают
СЗАДИ и ИЗДАЛЕКА.

Answer in JSON only:
{{"locks": {{"<character id>": {{
   "hair": "<colour, exact length measured against the body (chin / shoulders / mid-back),
             and ONE fixed style — never 'or', never 'sometimes'>",
   "hair_back": "<what the back of the head looks like: where the hair ends, whether the
                  nape is bare, how a ponytail or bun sits>",
   "face": "<eye colour, skin tone, brows, any permanent mark ON THE BODY: scar, mole,
             freckles — with its EXACT COUNT and place ('ONE beauty mark below the left eye,
             nothing else on the face'); glasses only if the description says they are worn
             always>",
   "build": "<height class among adults (tall / average / short), body build, shoulder width,
              posture — the silhouette a viewer recognises from across a room>",
   "limits": "<any permanent bodily fact that limits how the character may be drawn, e.g.
               a mute character never drawn with an open shouting mouth; empty if none>"
 }}}}}}

RULES
- Every field is a plain visual fact. Never write "as in the reference".
- RESOLVE EVERY AMBIGUITY. If the source says "often loose or in a ponytail", CHOOSE ONE and
  write only that one. A description that allows two hairstyles is re-decided in every shot
  and the hair then flips from frame to frame — measured: 27 flips in 156 frames.
- hair length must be measurable: "black, straight, reaching mid-back, always in a high
  ponytail" — not "long black hair". Measured: a single back view grew the hair from
  shoulder length to mid-back because the length was never stated.
- build is what holds the character together when the face is not visible at all.
- ONLY THE BODY IS A LOCK: hair, face, skin, build, and marks that are part of the body
  (a scar, a mole, freckles), plus glasses if worn always. Objects and accessories are NOT
  locks — a necklace, a ring, a bracelet, earrings, a bandage, a bag, a hat, a gift — even
  when the description says the character "wears" it. Such things appear at some moment
  of the story and do not exist before it; written here they get drawn in EVERY shot,
  including the shots before the object is given. Measured: "always wears a gold padlock
  necklace" was locked for a newborn, and the padlock was drawn on her from her first frame,
  although it is only given to her in frame 425. Leave every object to the scene passport,
  which decides costume and accessories scene by scene.
- "limits" is about the body as well (a mute mouth, an infant who cannot walk yet) — never
  about an object, never "always wears ...".
- A mark on the face is written with its exact NUMBER and PLACE, and with the words
  "nothing else on the face": "ONE beauty mark below the left eye, nothing else on the
  face". Measured: "small beauty mark beneath left eye" grew a second mark by the mouth
  in three shots (181 183 186) — a count-less mark gets multiplied.
- Keep each field under 15 words.

CHARACTERS:
{cast}
"""


LOCK_NOTE_RU = ("Замер показал, где эти факты рвутся: в кадрах, где ЛИЦА НЕ ВИДНО — со спины,\n"
                "сверху, во врезках. Значит одного описания лица мало: нужно то, по чему героя узнают\n"
                "СЗАДИ и ИЗДАЛЕКА.")
LOCK_NOTE_EN = ("Measured: these facts break in shots where the FACE IS NOT VISIBLE — from behind,\n"
                "from above, in insets. So a face description is not enough: write what the character\n"
                "is recognised by FROM BEHIND and FROM AFAR.")


def lock_prompt(lang: str = "Russian") -> str:
    """Для русского — LOCK_PROMPT побайтно. Для остальных языков пометка разработчика
    переведена на английский, как в visuals.batch_prompt: в польском промпте нет кириллицы."""
    if lang == "Russian":
        return LOCK_PROMPT
    assert LOCK_NOTE_RU in LOCK_PROMPT
    return LOCK_PROMPT.replace(LOCK_NOTE_RU, LOCK_NOTE_EN)


def appearance_locks(characters: List[dict], model: str = "codex:gpt-5.6-sol",
                     log=print, lang: str = "Russian") -> Dict[str, str]:
    cast = "\n".join(f"  - {c.get('id')}: {c.get('appearance_en','')}" for c in characters)
    try:
        data, _ = call_json(lock_prompt(lang).format(cast=cast), model=model,
                            timeout=240, retries=1)
    except LLMError as e:
        log(f"  паспорт внешности: не собрался — {e}")
        return {}
    raw = data.get("locks") if isinstance(data.get("locks"), dict) else {}
    out = {}
    for cid, v in raw.items():
        if isinstance(v, dict):
            parts = [v.get("hair", ""), v.get("hair_back", ""), v.get("face", ""),
                     v.get("build", ""), v.get("limits", "")]
            out[str(cid)] = "; ".join(x.strip() for x in parts if str(x).strip())
        else:
            out[str(cid)] = str(v)
    # Полнота: на 21 герое модель молча потеряла одного (lin_lu, прогон 09.09),
    # и проверка перед картинками остановила прогон. Недостающих спрашиваем
    # отдельно, один раз, — как уже делается с рядом роста.
    missing = [c for c in characters if str(c.get("id")) not in out]
    if missing and len(missing) < len(characters):
        cast2 = "\n".join(f"  - {c.get('id')}: {c.get('appearance_en','')}" for c in missing)
        try:
            data2, _ = call_json(lock_prompt(lang).format(cast=cast2), model=model,
                                 timeout=240, retries=1)
            raw2 = data2.get("locks") if isinstance(data2.get("locks"), dict) else {}
            for cid, v in raw2.items():
                if isinstance(v, dict):
                    parts = [v.get("hair", ""), v.get("hair_back", ""), v.get("face", ""),
                             v.get("build", ""), v.get("limits", "")]
                    out[str(cid)] = "; ".join(x.strip() for x in parts if str(x).strip())
                else:
                    out[str(cid)] = str(v)
            log(f"  паспорт внешности: дозапрошено {len(missing)}, получено {len(raw2)}")
        except LLMError as e:
            log(f"  паспорт внешности: дозапрос не удался — {e}")
    log(f"  паспорт внешности: {len(out)} героев, "
        f"в среднем {sum(len(x.split()) for x in out.values()) // max(1, len(out))} слов")
    return out


# --- паспорт внешности ПО КАРТИНКЕ листа --------------------------------------
# Текстовый паспорт (LOCK_PROMPT) пишется по appearance_en и спорит с листом:
# директор — locks «thinning crown», на листе густая чёлка -> 5 из 7 кадров лысый;
# фотограф — locks «close-cropped», кепка с листа в промпте не названа -> 4 кадра
# без кепки (run_ceo, разбор B). Кадры рисуются С ЛИСТА, значит и строка NEVER
# CHANGES должна описывать лист, а не текст. Поэтому — та же модель с картинками,
# что у судьи (judge._codex), и те же поля, что в LOCK_PROMPT.

LOCK_IMAGE_PROMPT = """You are looking at the reference sheet of ONE character of a manhwa
recap video: image 1 is the face, image 2 is the full-figure front view. Every shot of the
video is drawn FROM THESE PICTURES, so describe what is REALLY DRAWN here — the pictures are
the truth, not the script.

Answer in JSON only — EVERY field is required, none may be missing:
{{"hair_colour": "<the actual dominant base hair colour visible in the attached pictures; use any colour, not a fixed palette; ignore highlights>",
  "hair_length": "<exactly ONE of: 'short, above the ears' / 'chin-length' / 'shoulder-length' /
                  'longer than the shoulders' — measured against the body, never 'or'>",
  "hair_shape": "<ONE fixed style: 'swept back', 'combed back', 'bob', 'ponytail', 'bun',
                 'loose', 'flattened close to the head' — a bob ONLY when the hair hangs down
                 in a curtain around the whole face; hair combed back off the face is 'swept
                 back', even if strands reach the jaw at the sides>",
  "forehead": "<'exposed, no fringe' when the forehead is open (any parting, side or centre,
               is NOT a fringe); 'fringe covers the forehead' ONLY when hair hangs over the
               brow; add 'hairline receding' if it is>",
  "hair_back": "<what the back of the head looks like: where the hair ends, whether the nape
                is bare, how a ponytail or bun sits — inferred from the front and face views>",
  "face": "<eye colour, skin tone, brows, any permanent mark with its EXACT COUNT and place:
           'ONE beauty mark below the left eye, nothing else on the face', or 'no marks on
           the face'>",
  "glasses": "<'yes: <shape and colour>' if glasses are drawn, else 'no'>",
  "facial_hair": "<'clean-shaven' / 'stubble' / 'moustache' / 'beard' with colour>",
  "headwear": "<REQUIRED: 'none' if the head is bare; otherwise the item with its colour and
               how it sits — 'black baseball cap always on, pulled low, hair hidden under it'>",
  "build": "<height class among adults (tall / average / short), body build, shoulder width,
            posture — the silhouette a viewer recognises from across a room>",
  "limits": "<any permanent bodily fact from the written note that limits how the character
             may be drawn (mute, cannot walk yet); empty if none>"}}

RULES
- Describe the drawing, not the script. If the note says "thinning hair" and the picture shows
  a full fringe, write the full fringe. Measured: a lock line copied from the script drew the
  character bald in five shots of seven although the reference sheet had a full fringe.
- If the sheet shows a cap or glasses, they are ALWAYS worn: write them in headwear / glasses
  with the word "always". The sheet is copied into every shot, so a cap drawn on it must be
  named, or the shots lose it (measured: five shots without the cap once the cap line was
  dropped from the passport).
- The word "fringe" is used ONLY when hair covers the forehead. For an open forehead write
  "no fringe". Measured: "side-parted fringe leaves forehead exposed" drew a fringe across the
  brow in 8 shots — the word wins over the clause after it.
- Hair length is ONE of the four classes above, nothing in between. "chin-length at sides and
  back" turned a swept-back old man into a silver bob in 9 shots; if the hair is combed back
  and the back is short, the length is "short, above the ears" and the shape is "swept back".
- One fixed style, no "or", no "sometimes". Each field under 18 words. Plain visual facts,
  never "as in the reference".

Character id: {cid}
Written note from the script — use it ONLY for "limits"; for everything else the PICTURES win
even where the note disagrees:
{note}
"""


def _json_obj(raw: str):
    """Первый JSON-объект из ответа модели (с ```-оградой или прозой вокруг).

    Не judge._parse: тот после правки судьи 10.09 принимает ТОЛЬКО {"frames": [...]}
    и на плоский паспорт {"hair": …} отдаёт None — все герои молча уходили бы на
    текстовый паспорт (проверка скептика: 0 из 5 по картинке при верном ответе).
    """
    s = str(raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s).strip()
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass
    k = s.find("{")
    if k < 0:
        return None
    try:
        d, _ = json.JSONDecoder().raw_decode(s, k)
        return d if isinstance(d, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _clean(x) -> str:
    """Поле паспорта без точки на конце: иначе в NEVER CHANGES выходит «hairline.;».
    «;» внутри поля заменяется запятой — иначе строка hair; hair_back; face рвётся
    (живой прогон 10.09: hair_back фотографа пришёл с двумя «;» внутри, и
    pipeline._memory_sheets через split(';')[:3] потерял бы лицо)."""
    return str(x or "").replace(";", ",").strip().rstrip(".").strip()


# Скептик 10.09: «нет» модель пишет по-разному — «no», «none», «no glasses drawn»,
# «not drawn», «None (bare head)». Точный список из трёх слов переворачивал смысл:
# «no glasses drawn» становилось «glasses always on».
_NEG = re.compile(r"^(no|none|not|n/a|bare|empty|nothing|absent|without)\b", re.I)


def _lock_line(v: dict) -> str:
    """Строка паспорта из ответа LOCK_IMAGE_PROMPT в формате appearance_locks:
    hair; hair_back; face; build; headwear; limits — первые три части те же, что у
    текстового паспорта (на них опирается pipeline._memory_sheets через split(';')[:3]).

    Волосы собираются из четырёх обязательных полей (цвет, длина, форма, лоб): по
    замеру круга 2 именно свободный текст «chin-length at sides and back» дал деду
    каре в 9 кадрах, а «fringe leaves forehead exposed» — чёлку Эдриану в 8.
    Старый ответ с одним полем "hair" тоже принимается.
    """
    hair_bits = [_clean(v.get(k)) for k in ("hair_colour", "hair_length", "hair_shape", "forehead")]
    if hair_bits[3] and "forehead" not in hair_bits[3].lower():
        # Модель отвечает «exposed, no fringe» — без слова «forehead» в строке
        # непонятно, что именно открыто (живой прогон 10.09: все три героя).
        hair_bits[3] = "forehead " + hair_bits[3]
    hair = ", ".join(x for x in hair_bits if x) or _clean(v.get("hair"))
    face = _clean(v.get("face"))
    g = _clean(v.get("glasses"))
    if g and not _NEG.match(g):
        face = "; ".join(x for x in (face, "glasses always on" +
                                     (": " + g.split(":", 1)[1].strip() if ":" in g else "")) if x)
    elif g:
        face = "; ".join(x for x in (face, "no glasses") if x)
    fh = _clean(v.get("facial_hair"))
    if fh and not _NEG.match(fh):
        face = "; ".join(x for x in (face, fh) if x)
    hw = _clean(v.get("headwear"))
    if _NEG.match(hw):
        hw = ""
    elif hw and "always" not in hw.lower():
        # Кепка с листа рисуется в каждом кадре — без «always» она пропадает
        # (run_ceo круг 2: 176 177 178 236 237 без кепки).
        hw = hw + ", always on"
    parts = [hair, _clean(v.get("hair_back")), face, _clean(v.get("build")), hw,
             _clean(v.get("limits"))]
    return "; ".join(x for x in parts if x)


_HAIR_LEN = [("long", re.compile(r"\b(mid-back|waist|longer than the shoulders|past the shoulders|"
                                 r"below the shoulders|hip-length)\b", re.I)),
             ("shoulder", re.compile(r"\bshoulder(?:-length| length|s)\b", re.I)),
             ("chin", re.compile(r"\b(chin|jaw)(?:-length| length)\b", re.I)),
             ("short", re.compile(r"\b(short|above (?:the )?ears?|ear-length|close-cropped|"
                                  r"cropped|buzz)\b", re.I))]
_HEADWEAR = re.compile(r"\b(cap|hat|hood|beanie|headband|helmet|beret|headscarf|turban)\b", re.I)


def _hair_len_class(text: str) -> str:
    """Класс длины волос из строки паспорта: long / shoulder / chin / short / '' (не сказано).
    Смотрится только первая часть строки (волосы), чтобы «short pointed side sections»
    из hair_back не считались длиной."""
    head = str(text or "").split(";", 1)[0]
    for name, rx in _HAIR_LEN:
        if rx.search(head):
            return name
    return ""


def _headwear_class(text: str) -> str:
    """'cap'/'hat'/… если головной убор назван в строке паспорта, иначе 'none'."""
    m = _HEADWEAR.search(str(text or ""))
    return m.group(1).lower() if m else "none"


def locks_from_sheets(characters: List[dict], sheets: dict, model: str = "codex:gpt-5.6-sol",
                      fallback: Optional[Dict[str, str]] = None, log=print,
                      workers: int = 4) -> Dict[str, str]:
    """Паспорт внешности по кропам лица и фигуры с листа (sheets[cid].files['face'/'front']).

    Возвращает {cid: строка в формате appearance_locks}: hair; hair_back; face; build;
    headwear; limits — первые три части те же, что и у текстового паспорта (на них
    опирается _memory_sheets через split(';')[:3]). Где картинки нет или вызов упал —
    остаётся текстовый паспорт из fallback (запасной путь, не молчаливая пустота).
    """
    import judge as JU   # тот же вызов Codex с картинками, что у судьи; CODEX_HOME из окружения
    from concurrent.futures import ThreadPoolExecutor
    mdl = str(model or "")
    mdl = mdl.split(":", 1)[1] if mdl.startswith("codex:") else (mdl or JU.MODEL)
    out: Dict[str, str] = dict(fallback or {})

    def files_of(sh) -> dict:
        return getattr(sh, "files", None) or (sh if isinstance(sh, dict) else {})

    def one(c: dict):
        cid = str(c.get("id") or "")
        f = files_of((sheets or {}).get(cid)) if cid else {}
        imgs = [f.get(k) for k in ("face", "front")]
        imgs = [p for p in imgs if p and os.path.exists(p) and os.path.getsize(p) > 4096]
        if not imgs:
            return cid, "", "нет картинки листа"
        note = str(c.get("appearance_en") or c.get("appearance") or "").strip() or "(none)"
        prompt = LOCK_IMAGE_PROMPT.format(cid=cid, note=note)
        if len(imgs) == 1:
            # Старые прогоны (run_v/sheets) хранят только *_face.png: второго вида нет,
            # и «build» модели придумывать не с чего — пусть скажет только то, что видно.
            prompt += ("\nOnly ONE picture is attached (the face view); there is no full-figure "
                       "view. For \"build\" write only what this picture shows, or leave it empty.\n")
        try:
            raw = JU._codex(prompt, imgs, mdl, timeout=240)
        except CP.JobGone:
            raise
        except Exception as e:
            if type(e).__name__ == "TokenError":
                raise    # пул Codex: входа нет ни у одной учётки — не прятать за текстовым паспортом
            return cid, "", f"{type(e).__name__}: {str(e)[:160]}"
        data = _json_obj(raw)
        if not isinstance(data, dict):
            return cid, "", f"ответ не JSON: {raw[:120]!r}"
        v = data.get("locks") if isinstance(data.get("locks"), dict) else data
        if isinstance(v.get(cid), dict):       # модель могла обернуть в {cid: {...}}
            v = v[cid]
        text = _lock_line(v)
        return cid, text, ("" if text else "пустой ответ")

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        results = list(ex.map(CP.carry(one), characters))
    ok = 0
    for cid, text, err in results:
        if not cid:
            continue
        if text:
            ok += 1
            # Сверка с текстовым паспортом по длине волос и головному убору: правка B
            # круга 2 молча сделала деду каре и сняла с фотографа кепку (v1 → v2), и
            # это было видно только на кадрах. Расхождение — в лог обеими строками.
            old = str((fallback or {}).get(cid, "") or "")
            if old:
                for what, fn in (("длина волос", _hair_len_class),
                                 ("головной убор", _headwear_class)):
                    a, b = fn(old), fn(text)
                    if a and b and a != b:
                        log(f"  паспорт по листу {cid}: {what} расходится с текстовым "
                            f"({a} -> {b})\n      текст: {old[:110]}\n      лист:  {text[:110]}")
            out[cid] = text
            log(f"  паспорт по листу {cid}: {text[:100]}")
        else:
            log(f"  паспорт по листу {cid}: не вышло ({err}) — остаётся текстовый"
                + ("" if cid in out else " (а его НЕТ: герой без паспорта)"))
    log(f"  паспорт по листу: {ok} из {len(characters)} героев по картинке, "
        f"{len(characters) - ok} по тексту")
    return out
