"""Классификатор сцен: тип + поисковые запросы + пины эпохи.

Вход: сцены из scenes.json (subject/visual_prompt/text_segment).
Выход: SceneContract (тип, запросы, точные сущности, уровень claim и stock policy).
Кэшируется в <project>/parser/classify.json; ключ включает сцены, модель и версию контракта.
"""
import hashlib
import json
import os
import re

from .llm import chat, extract_json

TYPES = ("named", "generic", "reconstruction", "modern_anchor", "abstract", "stats")
CLASSIFIER_VERSION = "v24-localized"  # 17.07 E2: localized_query для национальных архивов (Kramerius/DDB)

_GENERIC_PERSON_WORDS = frozenset({
    "unknown", "civilian", "subject", "detained", "arrested", "man", "woman",
    "person", "prisoner", "detainee", "officer", "soldier", "driver", "witness",
    "victim", "guard", "author", "writer", "individual", "male", "female",
    "unnamed", "victims", "people", "men", "women", "crowd", "civilians",
})
_INTRO_PATTERNS = (
    "his name was", "her name was", "named him", "named her", "jmenoval se",
    "jmenovala se", "jmenuje se", "jeho jméno", "его звали", "её звали",
    "sein name war", "hieß", "s'appelait", "se llamaba",
)

_TIME_SHIFT_PATTERNS = (
    r"\bvraťme\s+se\b", r"\bpřesuňme\s+se\b", r"\bdo\s+minulosti\b",
    r"\b(?:měsíc|měsíce|měsíců|rok|roky|roků|let)\s+zpět\b",
    r"\b(?:go|jump|move|return)\s+back\b", r"\bflashback\b",
    r"\b(?:months?|years?)\s+(?:earlier|back)\b", r"\byears?\s+passed\b",
    r"\b(?:čas|roky?|léta)\s+uplyn", r"\btime\s+(?:shift|jump)\b",
)
_TEASER_PATTERNS = (
    r"\bse\s+dostaneme\b", r"\bpozději\b", r"\bvrátíme\s+se\b",
    r"\bwe(?:'ll|\s+will)\s+(?:return|get)\b", r"\bmore\s+on\s+that\s+later\b",
)
_SMALL_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "twelve": 12, "thirteen": 13,
    "jeden": 1, "jedna": 1, "dva": 2, "dvě": 2, "tři": 3, "čtyři": 4,
    "pět": 5, "šest": 6, "dvanáct": 12, "třináct": 13,
}
_YEAR_UNITS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "dva": 2, "dvě": 2, "tři": 3, "čtyři": 4, "pět": 5, "šest": 6,
}

PROMPT_HEAD = """You are the ARCHIVE EDITOR of a {niche_desc} YouTube documentary.
For each scene you get the NARRATION text (source of truth) and an old AI-image hint
(often invents paper documents / moods — do not trust it).
The scenes are CHRONOLOGICAL parts of one narration. Resolve pronouns and delayed
introductions across the whole batch: "this man", "he", "the prisoner" and equivalent
phrases may refer to a person named several lines later. Never guess a more famous person
when a neighboring scene identifies the actual subject (Karl Hermann Frank is not Heydrich).
Decide what real archival footage a documentary editor would PUT ON SCREEN for this
narration: people, places, events. NEVER a paper document/letter/form close-up unless
the narration itself is specifically about that document.
For EACH scene return a JSON object. Answer with a JSON array only, same order:
{{"i": <scene index>,
 "evidence": "<exact 3-8 word quote copied from THIS narration>",
 "show": "<5-10 words EN: what to show on screen>",
 "type": "named|generic|reconstruction|modern_anchor|abstract|stats",
 "queries": ["<archive search query EN>", "<second EN query>"],
 "year": <estimated year or null>, "region": "<theater/region or null>",
 "motion": true|false,
 "person": "<FULL canonical identity of the visible/referenced person or null>",
 "place": "<specific visible place/city or null>",
 "country": "<country relevant to the visual, English name or null>",
 "event": "<specific named event/action or null>",
 "claim_level": "exact|context|metaphor",
 "stock_ok": true|false,
 "stock_queries": ["<modern neutral stock query EN>", "<fallback stock query EN>"],
 "fallback_query": "<broad era/place query archives surely have>",
 "localized_query": "<best archive query in the language of the scene country, or null>"}}
Types: named = a real named person/place/event is the visual subject; generic = typical era scene
(soldiers, vehicles, camps, streets); reconstruction = private/unwitnessed moment (letters, dialogue,
thoughts); modern_anchor = present-day framing (archives today, museum, historian); abstract =
mood/emotion without concrete subject; stats = numbers/maps/data.
Queries are for archive full-text search that ANDs every word — keep them SHORT:
2-4 words, only nouns/places/units/years. NO verbs, NO adjectives, NO filler words.
English only. queries[0] = specific, queries[1] = broader fallback.
THINK LIKE A PHOTO ARCHIVE. Archives contain: soldiers, officers, named leaders,
military equipment, battle aftermath, cities/streets/buildings, crowds, prisons, camps,
trials, executions, refugees, ceremonies, transport. Archives DO NOT contain: private
family moments, unnamed individuals, emotions, conversations, thoughts, interiors of
private homes. NEVER query unphotographable things literally (no "family mother",
no "man thinking") — instead query the closest PUBLIC theme of the same era/place:
scene "German-speaking family mother in Bohemia" -> "Sudeten Germans 1938" or
"Czechoslovakia civilians 1930s".
For "named" include the proper name in queries[0].
Also return "fallback_query": one broad query that archives GUARANTEED have for this
scene's era/place — soldiers, street, crowd or ruins + year (e.g. "Prague 1946",
"German soldiers 1938"). It is used when both queries find nothing.
"person": the FULL canonical identity whenever the current visual shows or directly refers to a
known real person, including pronouns resolved from earlier/later scenes. It may be present for a
generic/reconstruction shot if the visible man is still that person. Leave it null only when the
visual is truly anonymous context. Never collapse identities to a surname (Karl Hermann Frank !=
Hans Frank), and never substitute a more famous official. Places, units and events are not persons.
claim_level=exact when the narration names a person/place/event/document; context for generic
era/location activity; metaphor only for mood/abstract transitions. stock_ok is true for
modern_anchor, abstract, and any generic/reconstruction scene WITHOUT a named person or
exact object — neutral textures (fire, smoke, sea waves, steppe grass, mud, rain, fog, night
sky, trench dirt, old paper) enrich the edit and cannot be mistaken for evidence of a specific
historical claim. stock_ok is false only for named persons and exact claims.
When stock_ok=true, stock_queries describe only the visible neutral modern motif for Pexels/Pixabay:
2-4 simple words, NO WWII/year/named person/event terms ("archivist archive shelves", not
"World War II film archive"). For modern_anchor, year should normally be null.
When stock_ok=false, stock_queries must be [].
"localized_query": national archives (Czech Kramerius, German DDB) index metadata in their
own language, so English queries miss them. When the scene country/place clearly implies a
non-English archive language, render the BEST archive query in that language (Czech scene ->
Czech, Germany/Austria -> German, Romania -> Romanian, Poland -> Polish), same 2-4 noun rules,
keeping proper names in their local spelling ("okupace Prahy 1942", not "occupation of
Prague"). Otherwise null."""

REPAIR_PROMPT = """You repair archive-search scene contracts that accidentally borrowed
the meaning of a neighboring narration line. Classify EACH input scene independently.
NARRATION is the only factual source; the ai-hint is only an alignment hint and may be
wrong. Never show an event that appears only before/after the current line.
Return a JSON array, preserving each supplied i exactly, with this object shape:
{"i":0,"evidence":"exact 3-8 word quote from THIS narration","show":"5-10 words EN","type":"named|generic|reconstruction|modern_anchor|abstract|stats",
"queries":["short archive query EN","broader query EN"],"year":1945,"region":null,
"motion":false,"person":"FULL canonical name or null","place":null,"country":null,
"event":"specific event/action or null","claim_level":"exact|context|metaphor",
"stock_ok":false,"stock_queries":[],"fallback_query":"broad archive query",
"localized_query":"archive query in the scene country language or null"}.
A name introduction must show that named person. A time-shift line must be abstract and
stock-safe. A killing/execution line must not become a court scene; a court/testimony line
must not become an execution scene. Use stock only for neutral metaphor/anonymous context,
never for a named or exact historical claim. JSON only."""

_SEMANTIC_CUES = {
    "transition": (
        r"\btime\s+(?:transition|shift|jump|leap)\b", r"\byears?\s+(?:back|earlier)\b",
        r"\bmove\s+.*\btime\b", r"\bclock\s+(?:face|hands)\b",
    ),
    "court": (
        r"\b(?:court|courtroom|trial|judge|judicial|verdict|testimony)\w*\b",
        r"\b(?:soud|soudu|soudem|soudní|proces|rozsudek|verdikt|svědectví)\w*\b",
    ),
    "execution": (
        r"\b(?:execution|executed|shooting|shot|killed|massacre|firing\s+squad)\w*\b",
        r"\b(?:poprav|zastřel|postřílel|masakr|padlo|u\s+zdi)\w*\b",
    ),
}


def _scene_cache_hash(scenes: list, model: str) -> str:
    payload = [{k: s.get(k) for k in ("text_segment", "subject", "visual_prompt")}
               for s in scenes]
    raw = json.dumps({"version": CLASSIFIER_VERSION, "model": model, "scenes": payload},
                     ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _clean_stock_query(value: str) -> str:
    text = re.sub(r"\b(?:world\s+war\s+(?:ii|2)|ww2|wwii)\b", " ", value or "",
                  flags=re.IGNORECASE)
    text = re.sub(r"\b(?:18|19|20)\d{2}s?\b", " ", text)
    return " ".join(text.split())[:100]


def _safe_stock_motif(text: str) -> bool:
    low = (text or "").casefold()
    motifs = ("archivist", "archive shelves", "film archive", "storage boxes",
              "researcher handling", "hands sorting files", "projector beam",
              "microfilm reader", "empty corridor", "empty prison cell", "prison cell",
              "prison corridor", "hands holding envelope", "writing at desk",
              "writing memoirs", "old clock", "clock face", "clock hands",
              "calendar pages", "hourglass", "film leader", "time transition",
              "rain on window")
    return any(motif in low for motif in motifs)


def _exact_paper_subject(show: str, claim_level: str, person: str) -> bool:
    if claim_level == "metaphor":
        return False
    low = (show or "").casefold()
    terms = ("ticket", "document", "form", "stamp", "seal", "letter", "paper note",
             "printed paper", "identity card", "passport")
    if not any(term in low for term in terms):
        return False
    # A document remains only a prop when the requested visual clearly centres the
    # resolved person ("man presenting documents").  A bare ticket/relic must not be
    # converted into that person's portrait merely because narration later names him.
    if person:
        surname = person.split()[-1].casefold()
        visible_person = surname in low or bool(re.search(
            r"\b(?:man|woman|person|prisoner|detainee|officer|soldier|portrait|face)\b",
            low))
        if visible_person:
            return False
    return True


def _matches_any(text: str, patterns: tuple) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _is_narrative_transition(scene: dict) -> bool:
    return _matches_any(str((scene or {}).get("text_segment") or ""),
                        _TIME_SHIFT_PATTERNS)


def _is_narrative_teaser(scene: dict) -> bool:
    return _matches_any(str((scene or {}).get("text_segment") or ""),
                        _TEASER_PATTERNS)


def _has_semantic_cue(text: str, cue: str) -> bool:
    return _matches_any(str(text or ""), _SEMANTIC_CUES[cue])


def _intro_person(scene: dict):
    """Extract a full name only from an explicit narration introduction phrase."""
    text = str((scene or {}).get("text_segment") or "")
    for phrase in _INTRO_PATTERNS:
        match = re.search(re.escape(phrase), text, flags=re.IGNORECASE)
        if not match:
            continue
        tail = text[match.end():].lstrip(" :-—–,\t")
        tokens = re.findall(r"[^\W\d_]+(?:[-'][^\W\d_]+)?", tail,
                            flags=re.UNICODE)
        name = []
        for token in tokens[:5]:
            if not token or not token[0].isupper():
                break
            name.append(token)
        return _canonical_person(" ".join(name))
    return None


def _contract_semantic_mismatch(row: dict, scene: dict,
                                expected_year: int = None) -> bool:
    """Catch blatant adjacent-scene contamination before it reaches retrieval.

    This intentionally checks only high-precision contradictions. Related documentary
    context remains allowed; a court contract on an execution line, or a clock contract
    on a name introduction, does not.
    """
    narration = str((scene or {}).get("text_segment") or "")
    evidence = " ".join(str(row.get("_evidence") or "").split()).strip(" \"'“”„")
    if evidence and evidence.casefold() not in " ".join(narration.split()).casefold():
        return True
    if _is_narrative_transition(scene) or _is_narrative_teaser(scene):
        return False  # deterministic policy below owns these contracts
    output = " ".join([
        str(row.get("show") or ""), str(row.get("event") or ""),
        str(row.get("place") or ""),
        " ".join(str(q) for q in (row.get("queries") or [])),
    ])
    intro = _intro_person(scene)
    if intro and row.get("person") != intro:
        return True
    if (expected_year and isinstance(row.get("year"), int)
            and row.get("type") != "modern_anchor" and row["year"] != expected_year):
        return True
    if _has_semantic_cue(output, "transition"):
        return True
    for cue in ("court", "execution"):
        if _has_semantic_cue(output, cue) and not _has_semantic_cue(narration, cue):
            return True
    return False


def _safe_scene_contract(scene: dict) -> dict:
    """Fail-closed aligned fallback when a semantic repair is missing or still bad."""
    narration = str((scene or {}).get("text_segment") or "")
    subject = " ".join(str((scene or {}).get("subject") or "").split())
    intro = _intro_person(scene)
    event = None
    if _has_semantic_cue(narration, "execution"):
        event = "mass execution"
    elif _has_semantic_cue(narration, "court"):
        event = "court proceedings"
    show = intro or subject or event or "historical context"
    obj = {
        "type": "named" if intro else "generic",
        "queries": [intro or subject or event or "historical context"],
        "year": _narrated_year(scene), "region": None, "motion": False,
        "person": intro, "place": None, "country": None, "event": event,
        "claim_level": "exact" if intro else "context", "stock_ok": False,
        "stock_queries": [], "fallback_query": subject or event, "show": show,
    }
    return _normalize_scene_object(obj, scene)


def _anchor_narrated_people(rows: list, scenes: list) -> int:
    """Pin explicit introductions and later unambiguous surname mentions."""
    anchors, changed = {}, 0
    for i, scene in enumerate(scenes):
        name = _intro_person(scene)
        if not name:
            continue
        anchors.setdefault(name.split()[-1].casefold(), set()).add(name)
        row = rows[i]
        before = (row.get("type"), row.get("person"), row.get("show"), row.get("event"))
        row.update({"type": "named", "show": name, "event": "identity introduction",
                    "place": None, "claim_level": "exact", "stock_ok": False,
                    "stock_queries": []})
        row["queries"] = [f"{name} {row.get('year') or ''}".strip(), name]
        _pin_person(row, name)
        after = (row.get("type"), row.get("person"), row.get("show"), row.get("event"))
        changed += before != after

    unique = {surname: next(iter(names)) for surname, names in anchors.items()
              if len(names) == 1}
    for i, (row, scene) in enumerate(zip(rows, scenes)):
        narration = str((scene or {}).get("text_segment") or "").casefold()
        for surname, name in unique.items():
            if not re.search(rf"(?<!\w){re.escape(surname)}(?!\w)", narration):
                continue
            if row.get("person") != name:
                _pin_person(row, name)
                row["type"] = "named"
                changed += 1
            break
    return changed


def _narrated_year(scene: dict):
    """Extract explicit four-digit/Czech-English 1940s years from narration."""
    text = str((scene or {}).get("text_segment") or "").casefold()
    digit = re.search(r"(?<!\d)((?:18|19|20)\d{2})(?!\d)", text)
    if digit:
        return int(digit.group(1))
    tail = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9,
            "jedna": 1, "jeden": 1, "dva": 2, "dvě": 2, "tři": 3,
            "čtyři": 4, "pět": 5, "šest": 6, "sedm": 7, "osm": 8, "devět": 9}
    match = re.search(
        r"(?:nineteen\s+)?forty[-\s]+(" + "|".join(map(re.escape, tail)) + r")\b",
        text)
    if not match:
        match = re.search(
            r"(?:devatenáct\s+set\s+)?čtyřicet\s+("
            + "|".join(map(re.escape, tail)) + r")\b", text)
    return 1940 + tail[match.group(1)] if match else None


def _relative_years_back(scene: dict):
    text = str((scene or {}).get("text_segment") or "").casefold()
    years = re.search(r"\b(" + "|".join(map(re.escape, _YEAR_UNITS))
                      + r")\s+(?:years?|roky?|let)\b", text)
    if years:
        return _YEAR_UNITS[years.group(1)]
    months = re.search(r"\b(" + "|".join(map(re.escape, _SMALL_NUMBERS))
                       + r")\s+(?:months?|měsíc(?:e|ů)?)\b", text)
    if months:
        count = _SMALL_NUMBERS[months.group(1)]
        return max(1, int(round(count / 12)))
    return None


def _narration_timeline_years(scenes: list) -> list:
    """Return narration-derived active year without consulting model contracts."""
    current, years = None, []
    for scene in scenes:
        explicit = _narrated_year(scene)
        if explicit:
            current = explicit
        elif _is_narrative_transition(scene) and current:
            delta = _relative_years_back(scene)
            if delta:
                current -= delta
        years.append(current)
    return years


def _canonical_person(value: str):
    """Return a plausible full identity; reject role descriptions as fake names."""
    value = " ".join(str(value or "").split()).strip()
    words = re.findall(r"[^\W\d_]+(?:[-'][^\W\d_]+)?", value, flags=re.UNICODE)
    if len(words) < 2 or any(w.casefold() in _GENERIC_PERSON_WORDS for w in words):
        return None
    return value


def _name_mentioned(name: str, scene: dict) -> bool:
    # Only narration is evidence. Old subject/visual_prompt fields are AI hints and
    # can contain the exact wrong identity we are trying to eliminate.
    text = str(scene.get("text_segment") or "").casefold()
    words = [w.casefold() for w in re.findall(r"[^\W\d_]+", name, flags=re.UNICODE)
             if len(w) > 1]
    return bool(words) and sum(w in text for w in words) >= min(2, len(words))


def _pin_person(row: dict, name: str):
    old_name = _canonical_person(row.get("_person_hint")) or _canonical_person(row.get("person"))
    if old_name and old_name != name:
        old_last = old_name.split()[-1].casefold()
        row["queries"] = [q for q in (row.get("queries") or [])
                          if old_last not in str(q).casefold()]
        show = str(row.get("show") or "")
        row["show"] = re.sub(re.escape(old_name), name, show, flags=re.IGNORECASE)
        row["show"] = re.sub(rf"\b{re.escape(old_name.split()[-1])}\b", name,
                             row["show"], flags=re.IGNORECASE)
    row["person"] = name
    row["exact_object"] = None  # document is a prop once the visible person is resolved
    row["claim_level"] = "exact"
    row["stock_ok"] = False
    row["stock_queries"] = []
    queries = [q for q in (row.get("queries") or []) if isinstance(q, str) and q.strip()]
    if name.casefold() not in " ".join(queries).casefold():
        queries.insert(0, f"{name} {row.get('year') or ''}".strip())
    row["queries"] = queries[:3]


def _continuity_boundary(row: dict, scene: dict = None) -> bool:
    return (_is_narrative_transition(scene) or _is_narrative_teaser(scene)
            or row.get("type") in ("modern_anchor", "stats"))


def _czech_short_pronoun(narration: str) -> bool:
    """Recognize short Czech pronouns without treating English ``on`` as a pronoun."""
    czech_cue = re.search(r"[áčďéěíňóřšťúůýž]", narration) or re.search(
        r"(?<!\w)(?:se|si|byl|byla|byli|měl|měla|mohl|mohla)(?!\w)", narration)
    return bool(czech_cue and any(
        re.search(rf"(?<!\w){pronoun}(?!\w)", narration)
        for pronoun in ("on", "ho", "mu", "ji")
    ))


def _delayed_intro_reference(row: dict, scene: dict = None) -> bool:
    """Broad evidence allowed only when a later explicit introduction backfills a row."""
    if row.get("_person_hint") or row.get("person"):
        return True
    narration = str((scene or {}).get("text_segment") or "").casefold()
    text = " ".join([
        narration, str(row.get("show") or ""),
        str(row.get("event") or ""),
        " ".join(str(q) for q in (row.get("queries") or [])),
    ]).casefold()
    roles = ("man", "woman", "prisoner", "detainee", "detained", "civilian",
             "inmate", "suspect", "captive", "official", "arrested", "captured",
             "he", "him", "his", "she",
             "her", "hers", "jeho", "jemu", "ona", "její", "muž", "muže", "muži",
             "vězeň", "vězně", "vězni",
             "zadržený", "zadržen", "zatčený", "zatčen", "podezřelý",
             "odsouzený", "civilista")
    if any(re.search(rf"(?<!\w){re.escape(role)}(?!\w)", text) for role in roles):
        return True
    return _czech_short_pronoun(narration)


def _strict_narration_anaphor(scene: dict) -> bool:
    """Narration-only evidence for forward identity continuity.

    Standalone generic roles and model-authored show/query hints are intentionally not
    evidence: only pronouns or explicitly definite/anaphoric role phrases qualify.
    """
    narration = str((scene or {}).get("text_segment") or "").casefold()
    if any(re.search(rf"(?<!\w){pronoun}(?!\w)", narration)
           for pronoun in ("he", "him", "his", "she", "her", "hers", "jeho",
                           "jemu", "ona", "její", "něj", "něho", "němu", "ním")):
        return True
    if _czech_short_pronoun(narration):
        return True
    english_role = re.search(
        r"(?<!\w)(?:this|that|the|same|previous)\s+"
        r"(?:(?:detained|arrested|captured|accused|former)\s+)?"
        r"(?:man|woman|prisoner|detainee|official|suspect|inmate|captive)(?!\w)",
        narration)
    czech_role = re.search(
        r"(?<!\w)(?:tento|ten|tohoto|tomuto|tomto|tímto|tato|této|tu|onen|"
        r"onoho|tentýž|stejný|stejného|předchozí)\s+"
        r"(?:(?:zadržený|zatčený|obviněný|bývalý)\s+)?"
        r"(?:muž|muže|muži|žena|ženu|ženy|vězeň|vězně|vězni|"
        r"úředník|úředníka|podezřelý|podezřelého)(?!\w)",
        narration)
    return bool(english_role or czech_role)


def _nearest_prior_identity(rows: list, supports: list, index: int, scenes: list = None):
    """Nearest established identity within the continuity window and no boundary."""
    for support_index, name in reversed(supports):
        if not 0 < index - support_index <= 8:
            continue
        if any(_continuity_boundary(rows[k], scenes[k] if scenes else None)
               for k in range(support_index + 1, index + 1)):
            continue
        return name
    return None


def _name_loosely_referenced(name: str, row: dict, scene: dict) -> bool:
    """Surname in narration/show is enough after a full-name anchor established identity."""
    last = name.split()[-1].casefold()
    text = f"{scene.get('text_segment') or ''} {row.get('show') or ''}".casefold()
    return len(last) >= 4 and bool(re.search(rf"(?<!\w){re.escape(last)}(?!\w)", text))


def _clear_unsupported_person(row: dict):
    name = str(row.get("person") or "")
    row["person"] = None
    row["queries"] = [q for q in (row.get("queries") or [])
                      if name.casefold() not in str(q).casefold()]
    if _exact_paper_subject(str(row.get("show") or ""), row.get("claim_level"), None):
        row["exact_object"] = "document"
        row["claim_level"] = "exact"
        row["stock_ok"] = False
        row["stock_queries"] = []


def _resolve_person_continuity(rows: list, scenes: list, log=None) -> int:
    """Resolve delayed name introductions without trusting model guesses.

    An explicit full name in narration is a strong anchor. Introduction phrases such
    as Czech ``jmenoval se`` backfill role/pronoun scenes until a narrative boundary,
    overriding an unsupported famous-person guess. Later role descriptions inherit the
    nearest already-established identity. Internal ``_person_hint`` never reaches cache.
    """
    strong = {}
    for i, (row, scene) in enumerate(zip(rows, scenes)):
        name = _canonical_person(row.get("person"))
        if name and _name_mentioned(name, scene):
            strong[i] = name

    changed, resolved = 0, set(strong)
    for i, name in strong.items():
        narration = str(scenes[i].get("text_segment") or "").casefold()
        if not any(pat in narration for pat in _INTRO_PATTERNS):
            continue
        for j in range(i - 1, max(-1, i - 9), -1):
            if _continuity_boundary(rows[j], scenes[j]):
                break
            if j in strong and strong[j] != name:
                break
            if _delayed_intro_reference(rows[j], scenes[j]):
                if rows[j].get("person") != name:
                    changed += 1
                _pin_person(rows[j], name)
                resolved.add(j)

    known_names = set(strong.values())
    for i, row in enumerate(rows):
        if (row.get("person") in known_names
                and _name_loosely_referenced(row["person"], row, scenes[i])):
            resolved.add(i)

    # Nano can correctly repeat the canonical identity in the scene after an explicit
    # anchor, while the narration switches to a pronoun/role. Preserve that name only
    # when it agrees with the nearest established identity. A different model guess is
    # still cleared below and cannot be inherited back because its canonical hint stays.
    established = [(i, row.get("person")) for i, row in enumerate(rows)
                   if i in resolved and row.get("person") in known_names]
    for i, row in enumerate(rows):
        name = _canonical_person(row.get("person"))
        if (not name or i in resolved
                or not _strict_narration_anaphor(scenes[i])):
            continue
        if name == _nearest_prior_identity(rows, established, i, scenes):
            resolved.add(i)

    for i, row in enumerate(rows):
        if row.get("person") and i not in resolved:
            _clear_unsupported_person(row)
            changed += 1

    supports = [(i, row.get("person")) for i, row in enumerate(rows)
                if i in resolved and row.get("person") in known_names]
    for i, row in enumerate(rows):
        # Forward inheritance is only for role/pronoun hints ("detained man").
        # A canonical but unsupported model guess was intentionally cleared above.
        if (row.get("person") or not _strict_narration_anaphor(scenes[i])
                or _canonical_person(row.get("_person_hint"))):
            continue
        prior_name = _nearest_prior_identity(rows, supports, i, scenes)
        if prior_name:
            _pin_person(row, prior_name)
            changed += 1
            resolved.add(i)

    for row in rows:
        row.pop("_person_hint", None)
        row.pop("_evidence", None)
    if changed and log:
        log(f"[classify] continuity: исправлено person-контрактов: {changed}")
    return changed


def _drop_person(row: dict):
    """Remove a visual identity and its injected full-name queries."""
    name = str(row.get("person") or "").strip()
    row["person"] = None
    if name:
        row["queries"] = [q for q in (row.get("queries") or [])
                          if name.casefold() not in str(q).casefold()]


def _enforce_narrative_contracts(rows: list, scenes: list) -> int:
    """Deterministic contracts for facts that must not depend on nano sampling.

    Timeline transitions are always continuity boundaries and stock-safe motifs;
    exact paper visuals remain documents even when a later introduction backfills a
    person.  These rules run after continuity so they also clean model/person drift.
    """
    changed = 0
    for row, scene in zip(rows, scenes):
        before = (row.get("type"), row.get("claim_level"), row.get("person"),
                  row.get("exact_object"), row.get("stock_ok"),
                  tuple(row.get("stock_queries") or []))
        if _is_narrative_transition(scene):
            _drop_person(row)
            row.update({"type": "abstract", "claim_level": "metaphor",
                        "exact_object": None, "stock_ok": True,
                        "queries": ["old clock", "time transition"],
                        "stock_queries": ["old clock close up", "clock hands turning"],
                        "fallback_query": None, "event": "time transition",
                        "place": None, "country": None, "region": None})
            row["show"] = "time transition with old clock"
        elif _is_narrative_teaser(scene):
            _drop_person(row)
            row.update({"type": "abstract", "claim_level": "metaphor",
                        "exact_object": None, "stock_ok": True,
                        "queries": ["archive shelves", "film projector"],
                        "stock_queries": ["film projector beam", "shadowed archive shelves"],
                        "fallback_query": None, "event": "narrative transition",
                        "place": None, "country": None, "region": None})
            row["show"] = "documentary transition"
        elif _exact_paper_subject(str(row.get("show") or ""),
                                  row.get("claim_level"), row.get("person")):
            _drop_person(row)
            row.update({"exact_object": "document", "claim_level": "exact",
                        "stock_ok": False, "stock_queries": []})
        elif (row.get("type") == "reconstruction"
              and row.get("claim_level") != "exact" and not row.get("person")
              and _safe_stock_motif(" ".join((str(row.get("show") or ""),
                                                str(scene.get("subject") or ""))))):
            row["stock_ok"] = True
            if not row.get("stock_queries"):
                row["stock_queries"] = [_clean_stock_query(
                    str(row.get("show") or scene.get("subject") or ""))]
        after = (row.get("type"), row.get("claim_level"), row.get("person"),
                 row.get("exact_object"), row.get("stock_ok"),
                 tuple(row.get("stock_queries") or []))
        changed += before != after
    return changed


def _enforce_year_continuity(rows: list, scenes: list) -> int:
    """Pin chronology from narration so short anaphoric scenes cannot jump years."""
    current, changed = None, 0
    for row, scene in zip(rows, scenes):
        explicit = _narrated_year(scene)
        if explicit:
            current = explicit
        elif _is_narrative_transition(scene) and current:
            delta = _relative_years_back(scene)
            if delta:
                current -= delta
            elif isinstance(row.get("year"), int):
                current = row["year"]
        if row.get("type") == "modern_anchor":
            continue
        if current and row.get("year") != current:
            row["year"] = current
            changed += 1
    return changed


def _extract_scene_objects(text: str) -> list:
    """Parse the full response, then salvage independently valid scene objects.

    Nano occasionally emits one malformed string inside an otherwise valid JSON array.
    Throwing away the other 19 objects silently degrades the whole batch to generic.
    ``raw_decode`` from every opening brace recovers valid siblings after the bad one.
    """
    try:
        parsed = extract_json(text)
        if isinstance(parsed, dict):
            parsed = parsed.get("scenes") or []
        if isinstance(parsed, list):
            return [obj for obj in parsed if isinstance(obj, dict) and "i" in obj]
    except Exception:  # noqa: BLE001 — partial recovery below is intentional
        pass

    decoder = json.JSONDecoder()
    recovered, seen = [], set()
    for match in re.finditer(r"\{", text):
        try:
            obj, _end = decoder.raw_decode(text[match.start():])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(obj, dict) or "i" not in obj:
            continue
        try:
            key = int(obj["i"])
        except (TypeError, ValueError):
            continue
        if key not in seen:
            seen.add(key)
            recovered.append(obj)
    return recovered


def _normalize_scene_object(obj: dict, scene: dict) -> dict:
    """Validate one model object and enforce deterministic stock safety policy."""
    t = obj.get("type") if obj.get("type") in TYPES else "generic"
    qs = [q for q in (obj.get("queries") or [])
          if isinstance(q, str) and q.strip()][:3]
    raw_person = obj.get("person")
    raw_person = raw_person.strip() if isinstance(raw_person, str) and raw_person.strip() else None
    person = _canonical_person(raw_person)

    def clean(name):
        value = obj.get(name)
        return value.strip() if isinstance(value, str) and value.strip() else None

    fq = obj.get("fallback_query")
    fq = fq.strip() if isinstance(fq, str) and fq.strip() else None
    show = obj.get("show")
    show = show.strip() if isinstance(show, str) and show.strip() else None
    claim_level = (obj.get("claim_level")
                   if obj.get("claim_level") in ("exact", "context", "metaphor")
                   else ("exact" if t == "named" else "context"))
    if person:
        claim_level = "exact"
        # A resolved identity must also reach archive search; otherwise a generic query
        # can still return a convincing but different recognizable man.
        if person.casefold() not in " ".join(qs).casefold():
            year = obj.get("year")
            qs.insert(0, f"{person} {year or ''}".strip())
            qs = qs[:3]
    effective_show = show or scene.get("subject", "")
    stock_text = " ".join((effective_show or "", scene.get("text_segment", ""),
                           scene.get("subject", "")))
    safe_motif = _safe_stock_motif(stock_text)
    exact_object = ("document"
                    if _exact_paper_subject(effective_show or "", claim_level, person)
                    else None)
    if exact_object:
        claim_level = "exact"
    stock_ok = (t == "modern_anchor"
                or (t == "abstract" and (claim_level == "metaphor" or safe_motif))
                or (t in ("reconstruction", "generic") and claim_level != "exact"
                    and not person and safe_motif))
    stock_queries = ([_clean_stock_query(q) for q in (obj.get("stock_queries") or [])
                      if isinstance(q, str) and _clean_stock_query(q)][:2]
                     if stock_ok else [])
    if stock_ok and not stock_queries:
        fallback_stock = _clean_stock_query(show or scene.get("subject", ""))
        if fallback_stock:
            stock_queries = [fallback_stock]
    return {"type": t, "queries": qs or [scene.get("subject", "")],
            "year": None if t == "modern_anchor" else obj.get("year"),
            "region": obj.get("region"),
            "motion": bool(obj.get("motion")), "person": person,
            "place": clean("place"), "country": clean("country"),
            "event": clean("event"), "claim_level": claim_level,
            "exact_object": exact_object,
            "stock_ok": stock_ok, "stock_queries": stock_queries,
            "fallback_query": fq, "show": effective_show,
            # E2: запрос на языке страны сцены — для национальных архивов
            # (Kramerius/DDB); маршрутизацию делает router.search_source
            "localized_query": (clean("localized_query") or "")[:100] or None,
            "_person_hint": raw_person,
            "_evidence": clean("evidence")}


def classify_scenes(scenes: list, niche: dict, project_dir: str,
                    log=print, force: bool = False) -> list:
    out_dir = os.path.join(project_dir, "parser")
    os.makedirs(out_dir, exist_ok=True)
    cache_path = os.path.join(out_dir, "classify.json")
    meta_path = os.path.join(out_dir, "classify.meta.json")
    cfg = niche.get("classifier", {})
    model = cfg.get("model", "gpt-5-mini")
    cache_hash = _scene_cache_hash(scenes, model)
    if not force and os.path.exists(cache_path):
        try:
            cached = json.load(open(cache_path))
            meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
            if len(cached) == len(scenes) and meta.get("hash") == cache_hash:
                log(f"[classify] кэш: {len(cached)} сцен")
                return cached
        except Exception:  # noqa: BLE001
            pass

    effort = cfg.get("reasoning_effort", "minimal")
    batch = int(cfg.get("batch_size", 20))
    head = PROMPT_HEAD.format(niche_desc=niche.get("description", ""))

    results: list = [None] * len(scenes)

    def request_indices(indices: list, label: str, repair: bool = False) -> int:
        lines = []
        for i in indices:
            s = scenes[i]
            seg = (s.get("text_segment") or "")[:260].replace("\n", " ")
            lines.append(f'[{i}] NARRATION: {seg!r} '
                         f'(ai-hint: {(s.get("subject") or "")[:80]!r})')
        msg = (REPAIR_PROMPT if repair else head) + "\n\nScenes:\n" + "\n".join(lines)
        try:
            raw = chat(model, [{"role": "user", "content": msg}],
                       reasoning_effort=effort,
                       max_tokens=2500 if repair else 6000)
            arr = _extract_scene_objects(raw)
            if not arr:
                raise ValueError("в ответе нет валидных scene objects")
        except Exception as e:  # noqa: BLE001 — один битый батч не роняет прогон
            log(f"[classify] {label} упал: {e}")
            return 0
        allowed = set(indices)
        got = 0
        for obj in arr:
            try:
                i = int(obj["i"])
            except (KeyError, TypeError, ValueError):
                continue
            if i not in allowed:
                continue
            results[i] = _normalize_scene_object(obj, scenes[i])
            got += 1
        log(f"[classify] {label}: {got}/{len(indices)}")
        return got

    for start in range(0, len(scenes), batch):
        indices = list(range(start, min(start + batch, len(scenes))))
        request_indices(indices, f"сцены {indices[0]}-{indices[-1]}")

    # A malformed object must not poison its whole batch. Retry only missing indices
    # in small groups, which is both cheaper and more reliable than replaying 20 scenes.
    missing = [i for i, value in enumerate(results) if value is None]
    retry_batch = max(1, int(cfg.get("retry_batch_size", 4)))
    if missing:
        log(f"[classify] восстанавливаю {len(missing)} пропущенных сцен")
    for start in range(0, len(missing), retry_batch):
        indices = missing[start:start + retry_batch]
        request_indices(indices, f"retry {indices[0]}-{indices[-1]}")

    # One compact repair call handles only high-confidence semantic drift. This is far
    # cheaper than classifying every scene independently and prevents adjacent lines
    # from silently receiving each other's event/person contract.
    expected_years = _narration_timeline_years(scenes)
    suspect = [i for i, value in enumerate(results)
               if value is not None and _contract_semantic_mismatch(
                   value, scenes[i], expected_years[i])]
    semantic_batch = max(1, int(cfg.get("semantic_retry_batch_size", 4)))
    if suspect:
        log(f"[classify] semantic drift: перепроверяю сцен: {suspect}")
    for start in range(0, len(suspect), semantic_batch):
        indices = suspect[start:start + semantic_batch]
        for i in indices:
            results[i] = None
        request_indices(indices, f"semantic retry {indices[0]}-{indices[-1]}", repair=True)
        for i in indices:
            if (results[i] is None or _contract_semantic_mismatch(
                    results[i], scenes[i], expected_years[i])):
                log(f"[classify] semantic retry {i} не прошёл → aligned safe fallback")
                results[i] = _safe_scene_contract(scenes[i])

    # дырки (LLM пропустил) -> generic с subject как запросом
    for i, r in enumerate(results):
        if r is None:
            results[i] = {"type": "generic", "queries": [scenes[i].get("subject", "")],
                          "year": None, "region": None, "motion": False, "person": None,
                          "place": None, "country": None, "event": None,
                          "claim_level": "context", "exact_object": None,
                          "stock_ok": False,
                          "stock_queries": [],
                          "fallback_query": None, "localized_query": None,
                          "show": scenes[i].get("subject", "")}

    anchored = _anchor_narrated_people(results, scenes)
    if anchored:
        log(f"[classify] narration anchors: исправлено person-контрактов: {anchored}")
    _resolve_person_continuity(results, scenes, log=log)
    enforced = _enforce_narrative_contracts(results, scenes)
    if enforced:
        log(f"[classify] deterministic contracts: исправлено сцен: {enforced}")
    year_fixed = _enforce_year_continuity(results, scenes)
    if year_fixed:
        log(f"[classify] chronology: исправлено годов: {year_fixed}")

    tmp = cache_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    os.replace(tmp, cache_path)
    meta_tmp = meta_path + ".tmp"
    with open(meta_tmp, "w") as f:
        json.dump({"version": CLASSIFIER_VERSION, "hash": cache_hash,
                   "model": model, "count": len(results)}, f, indent=1)
    os.replace(meta_tmp, meta_path)
    log(f"[classify] готово: {len(results)} сцен -> {cache_path}")
    return results
