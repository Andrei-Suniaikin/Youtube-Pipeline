"""Durable paid re-briefing for semantically unresolved montage slots.

The request function deliberately returns an unparsed provider envelope.  The
caller must persist that envelope before invoking the validation function, so a
local parse or contract failure can never cause an accidental duplicate paid
request.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .llm_client import chat as _chat
from .llm_client import extract_json
from .semantic import (
    VISUAL_BRIEF_CONTRACT_VERSION,
    validate_visual_brief_contract,
)


SEMANTIC_PROXY_REPAIR_PROMPT_VERSION = "semantic-proxy-repair-v5"
POST_RESEG_SEMANTIC_PROXY_REPAIR_PROMPT_VERSION = (
    "post-resegment-semantic-proxy-v2"
)
NATURAL_CARRIER_POLICY_VERSION = "continuous-natural-carrier-v1"
NATURAL_STATUS_ENVIRONMENT_POLICY_VERSION = "observable-natural-environment-v1"
GROUNDED_MAGNITUDE_DROP_POLICY_VERSION = "grounded-magnitude-drop-v1"
HONEST_VISIBLE_FALLBACK_POLICY_VERSION = "honest-visible-fallback-v1"
HONEST_VISIBLE_FALLBACK_RECEIPT_FIELD = "honest_visible_fallback_receipt"


SEMANTIC_PROXY_REPAIR_PROMPT = """You repair failed camera-observable search briefs for a universal documentary editor.
Prompt version: {prompt_version}.
Visual brief contract version: {contract_version}.

Return one replacement brief for every target slot. Every replacement must use exactly the
same schema as the visual-brief contract:
contract_version, slot_id, query_en, must_have, must_not, generic_fallback_en, proxy_mode,
observable_basis_en.

Repair policy:
- Simplify each failed brief to ONE honest, ordinary, live-action 4--8 second shot concept.
  must_have may contain at most two camera-visible facets of that same shot concept.
- Do not require a montage, a shot sequence, split screen, or two distant places/events in one
  shot. Do not combine an establishing view with a later detail merely to cover every noun.
- A legal, political, administrative, historic, protected, ownership, identity, or causal claim
  is not physical proof. Never invent a border post, checkpoint, fence, flag, sign, marker, plaque,
  logo, road, building, settlement, distinct/different infrastructure, different land use,
  document, uniformed official, ceremony, damage, behaviour, or other visible evidence that the
  narration does not explicitly say the camera can see. Never ask viewers to infer status from
  different development or land use on opposite sides of a natural feature.
- For such a non-visible status claim, use the concrete natural carrier or environment named by
  the narration or its supplied neighbour context. Keep any separately camera-visible scale fact,
  but express it qualitatively (wide/broad/expansive) rather than fabricating a measurement overlay.
  When the scarce-candidate policy says an exact landmark is scarce or reserved for an adjacent
  detail, do not require that landmark again. In a water setting, use one continuous broad natural
  watercourse with both opposite natural banks visible in the same shot. The legal/status fact
  remains narration-only. Set proxy_mode to observable_proxy or observable_consequence.
- Recognition or protected/heritage status of a natural place is not a reason to request a sign,
  logo, plaque, visitor centre, gate, certificate, or emblem unless narration explicitly says that
  object is camera-visible. Use one ordinary live-action view of the named natural environment as
  the honest observable proxy; keep the recognition/status fact in narration only.
- Neighbour narration is context for continuity and anchor allocation, not another must-have
  list. Allocate a rare or unusually specific visual anchor to the adjacent slot whose narration
  names it most specifically. For example, a river-establishing slot should use the river and its
  banks while a following waterfall-detail slot keeps the scarce waterfall shot.
- Use the strict evaluation failure reasons and scarce-candidate context to remove the failed
  constraint; never repeat it with synonyms. Prefer a broad truthful proxy over a false exact shot.
- generic_fallback_en is not permission to drop the primary visible subject, its action/state, or
  its spatial relation. It may relax only a secondary subtype, habitat, or camera/framing facet.
  For example, a baboon visibly high in a tree may be an honest fallback when the exact tree
  subtype is unavailable; an empty tree or habitat-only shot is not. Never rename a generic
  fallback object as the unavailable exact subtype.
- query_en, must_have, and observable_basis_en must contain only directly camera-visible content.
  Never request CGI, animation, time-lapse, diagrams, maps, infographics, x-ray, cutaways, causal
  arrows, or explanatory graphics.
- proxy_mode must be literal_visible, observable_proxy, or observable_consequence. If the
  narration's fact itself is not directly visible, literal_visible is forbidden.
- Return exactly the requested slot_ids once each and return only a JSON array.

TARGETS:
{targets}
"""


_POST_RESEG_MAGNITUDE_RULES = """Post-resegmentation physical magnitude policy:
- The target has already been split at exact narration word boundaries and an exact visual search
  has failed. For a numerical height or drop claim, keep the number in narration only. Do not ask
  the picture to prove an exact measurement.
- Use one grounded natural drop shot: directly visible falling water over a natural rock
  edge/steep face, plus natural mist, spray, rock, or vegetation around the drop. A visible natural
  consequence is honest; an invented scale prop is not.
- Never invent people, a person, boats, scale markers, measurement labels, captions, added text,
  maps, CGI, animation, diagrams, split screens, montages, or shot sequences. People or a boat may
  remain visible only when the target narration explicitly names them.
- Set proxy_mode to observable_consequence or observable_proxy. The directly visible natural cues,
  not the narrated number, must be stated in query_en, must_have, and observable_basis_en.
"""


POST_RESEG_SEMANTIC_PROXY_REPAIR_PROMPT = SEMANTIC_PROXY_REPAIR_PROMPT.replace(
    "\nTARGETS:\n",
    f"\n{_POST_RESEG_MAGNITUDE_RULES}\nTARGETS:\n",
)


_CURRENT_BRIEF_FIELDS = (
    "contract_version",
    "slot_id",
    "query_en",
    "must_have",
    "must_not",
    "generic_fallback_en",
    "proxy_mode",
    "observable_basis_en",
)
_MULTI_SHOT_REQUIREMENT = re.compile(
    r"\b(?:montage|split[- ]screen|shot sequence|sequence of shots|two shots|"
    r"cut(?:s|ting)? (?:to|between)|followed by another shot|then (?:show|cut))\b",
    re.IGNORECASE,
)
_STATUS_OR_JURISDICTION_CLAIM = re.compile(
    r"\b(?:political border|national border|boundary|jurisdiction|territor(?:y|ial)|"
    r"unesco|world heritage|heritage status|protected status|legally protected|"
    r"designated|recognized|recognised|ownership|"
    r"границ\w*|территор\w*|юрисдикц\w*|юнеско|наследи\w*|статус\w*|"
    r"охраня\w*|признан\w*|принадлеж\w*)\b",
    re.IGNORECASE,
)
_SPECIFIC_NATURAL_LOCATION = re.compile(
    r"\b(?:waterfalls?|falls|rivers?|streams?|banks?|shores?|wetlands?|deltas?|"
    r"marsh(?:es)?|floodplains?|lakes?|oceans?|coasts?|islands?|forests?|"
    r"grasslands?|savannas?|deserts?|mountains?|valleys?|plains?|habitats?)\b",
    re.IGNORECASE,
)
_WIDTH_CLAIM = re.compile(
    r"\b(?:width|wide(?:st)?|breadth|span|ширин\w*)\b",
    re.IGNORECASE,
)
_VISIBLE_WIDTH_PROXY = re.compile(
    r"\b(?:wide|broad|expansive|panoramic|full breadth|spanning|vast)\b",
    re.IGNORECASE,
)
_WATER_CARRIER_CONTEXT = re.compile(
    r"\b(?:waterfalls?|falls|cataracts?|cascades?|rivers?|streams?|watercourses?|flows?|"
    r"banks?|shores?|channels?|водопад\w*|каскад\w*|рек(?:а|и|у|ой|ою|е|ам|ами|ах)?|"
    r"берег\w*|водоток\w*|русл\w*|поток\w*)\b",
    re.IGNORECASE,
)
_SCARCE_WATER_LANDMARK = re.compile(
    r"\b(?:waterfalls?|falls|cataracts?|cascades?|водопад\w*|каскад\w*)\b",
    re.IGNORECASE,
)
_PHYSICAL_MAGNITUDE_OR_DROP_CLAIM = re.compile(
    r"\b(?:height|drop|vertical drop|falling from|metres?|meters?|hundred[- ]metre|"
    r"hundred[- ]meter|обрушивается|обрушиваются|падает|падают|"
    r"высота|высоты|высоте|высоту|высотой|высотою|"
    r"стометровая|стометровой|стометровую|стометровое|стометрового|"
    r"метр|метра|метров|метре|метрах)\b",
    re.IGNORECASE,
)
_VISIBLE_NATURAL_DROP_CUE = re.compile(
    r"\b(?:falling water|water falling|water crossing|natural (?:rock )?edge|"
    r"steep (?:rock )?(?:face|edge|cliff)|rock face|drop edge)\b",
    re.IGNORECASE,
)
_VISIBLE_FALLING_WATER = re.compile(
    r"\b(?:falling water|water falling|waterfalls?|cascades?|cataracts?)\b",
    re.IGNORECASE,
)
_WATER_DROP_ACTION = re.compile(
    r"\b(?:waterfalls?|falls|cataracts?|cascades?|falling water|water falling|"
    r"plung(?:e|es|ing)|drop(?:s|ping)?|обрушива\w*|пада\w*|водопад\w*|каскад\w*)\b",
    re.IGNORECASE,
)
_VISIBLE_NATURAL_DROP_CONTEXT = re.compile(
    r"\b(?:mist|spray|rock|rocks|rocky|vegetation|cliff|gorge)\b",
    re.IGNORECASE,
)
_NARRATED_PEOPLE = re.compile(
    r"\b(?:people|person|persons|human|humans|visitors?|tourists?|crowd|"
    r"люди|человек|человека|человеку|человеком|турист|туристы|туристов)\b",
    re.IGNORECASE,
)
_NARRATED_BOAT = re.compile(
    r"\b(?:boats?|vessels?|canoes?|rafts?|лодка|лодки|лодку|лодке|лодкой|"
    r"судно|судна|каноэ|плот|плота)\b",
    re.IGNORECASE,
)
_NUMERIC_MAGNITUDE_PROOF = re.compile(
    r"(?:\b\d+(?:[.,]\d+)?\s*(?:m|metres?|meters?)\b|"
    r"\b(?:one )?hundred\s+(?:metres?|meters?)\b|"
    r"\bexact (?:height|drop|measurement)\b)",
    re.IGNORECASE,
)
_FORBIDDEN_SYNTHETIC_DROP_VISUAL = re.compile(
    r"\b(?:scale markers?|measurement labels?|height labels?|numeric overlays?|"
    r"captions?|added text|maps?|cgi|animations?|diagrams?|split[- ]screens?|"
    r"montages?|shot sequences?)\b",
    re.IGNORECASE,
)
_NATURAL_ENVIRONMENT_TYPES = (
    (
        "wetland delta",
        re.compile(
            r"\b(?:wetlands?|deltas?|marsh(?:es)?|floodplains?|"
            r"дельт\w*|болот\w*|пойм(?:а|ы|у|е|ой|ою|ам|ами|ах)?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "river or watercourse",
        re.compile(
            r"\b(?:waterfalls?|falls|rivers?|streams?|watercourses?|channels?|banks?|"
            r"водопад\w*|рек(?:а|и|у|е|ой|ою|ам|ами|ах)?|"
            r"водоток\w*|русл\w*|берег\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "lake",
        re.compile(r"\b(?:lakes?|озер\w*)\b", re.IGNORECASE),
    ),
    (
        "coast or ocean",
        re.compile(
            r"\b(?:oceans?|coasts?|shores?|seas?|океан\w*|побереж\w*|"
            r"мор(?:е|я|ю|ем|ей|ям|ями|ях|ск\w*))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "forest",
        re.compile(
            r"\b(?:forests?|woodlands?|лес(?:а|у|ом|е|ов|ам|ами|ах)?|лесн\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "grassland or savanna",
        re.compile(
            r"\b(?:grasslands?|savannas?|степ(?:ь|и|ью|ей|ям|ями|ях)|"
            r"степн\w*|саванн\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "desert",
        re.compile(r"\b(?:deserts?|пустын\w*)\b", re.IGNORECASE),
    ),
    (
        "mountain or valley",
        re.compile(
            r"\b(?:mountains?|valleys?|cliffs?|"
            r"гор(?:а|ы|у|е|ой|ою|ам|ами|ах)|горн\w*|долин\w*|скал\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "island",
        re.compile(r"\b(?:islands?|остров\w*)\b", re.IGNORECASE),
    ),
)


# A status claim may coexist with genuinely narrated visible objects.  Each
# rule therefore blocks a proposed proof only when that feature is absent from
# the narration itself.  ``must_not`` is intentionally excluded downstream:
# naming a forbidden flag or sign there is safe and useful.
_STATUS_PROOF_RULES = (
    (
        re.compile(
            r"\b(?:(?:distinct|different|differing|contrasting)\s+"
            r"(?:infrastructure|land use|development|built features?)|"
            r"(?:infrastructure|land use)\s+(?:differs?|contrasts?))\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:infrastructure|land use|development|built features?|"
            r"инфраструктур\w*|землепольз\w*|застро\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        re.compile(r"\b(?:roads?|highways?|tracks?)\b", re.IGNORECASE),
        re.compile(r"\b(?:roads?|highways?|tracks?|дорог\w*|трасс\w*)\b", re.IGNORECASE),
    ),
    (
        re.compile(
            r"\b(?:buildings?|built (?:features?|structures?)|infrastructure|"
            r"visitor cent(?:er|re)|gates?)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:buildings?|infrastructure|visitor cent(?:er|re)|gates?|"
            r"здани\w*|строени\w*|инфраструктур\w*|ворот\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        re.compile(
            r"\b(?:settlements?|villages?|towns?|cities|urban development)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:settlements?|villages?|towns?|cities|urban development|"
            r"посел\w*|деревн\w*|город\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        re.compile(r"\b(?:flags?|banners?)\b", re.IGNORECASE),
        re.compile(r"\b(?:flags?|banners?|флаг\w*|знам\w*)\b", re.IGNORECASE),
    ),
    (
        re.compile(
            r"\b(?:signs?|signposts?|signage|placards?|boards?|panels?|plaques?|"
            r"logos?|emblems?)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:signs?|signposts?|signage|placards?|boards?|panels?|plaques?|"
            r"logos?|emblems?|знак\w*|таблич\w*|панел\w*|вывес\w*|"
            r"логотип\w*|эмблем\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        re.compile(
            r"\b(?:checkpoints?|customs posts?|border guards?|"
            r"uniformed (?:guards?|officers?|officials?))\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:checkpoints?|customs posts?|border guards?|"
            r"контрольно-пропускн\w*|кпп|тамож\w*|погранич\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        re.compile(
            r"\b(?:(?:border|boundary) (?:posts?|markers?|fences?)|"
            r"marked boundary|fences?)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:(?:border|boundary) (?:posts?|markers?|fences?)|"
            r"markers?|fences?|столб\w*|маркер\w*|ограждени\w*|забор\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        re.compile(r"\b(?:certificates?|legal documents?)\b", re.IGNORECASE),
        re.compile(
            r"\b(?:certificates?|legal documents?|сертификат\w*|документ\w*)\b",
            re.IGNORECASE,
        ),
    ),
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


_HONEST_FALLBACK_ROLE_ORDER = ("subject", "action", "spatial_relation")
_HONEST_FALLBACK_SECONDARY_KINDS = frozenset({"subtype", "habitat", "camera"})
_HONEST_FALLBACK_WORD = re.compile(r"[a-z]+(?:['-][a-z]+)*", re.IGNORECASE)
_HONEST_FALLBACK_CONDITIONAL_SHOW = re.compile(
    r"\b(?:show|film|use|choose)\s+(.+)$", re.IGNORECASE
)
_HONEST_FALLBACK_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "beneath",
        "below",
        "by",
        "camera",
        "clear",
        "clearly",
        "documentary",
        "emphasizing",
        "film",
        "for",
        "frame",
        "from",
        "if",
        "in",
        "include",
        "into",
        "live-action",
        "near",
        "of",
        "on",
        "one",
        "or",
        "perspective",
        "shot",
        "show",
        "showing",
        "the",
        "their",
        "them",
        "through",
        "to",
        "under",
        "up",
        "use",
        "view",
        "visible",
        "visibly",
        "where",
        "with",
    }
)
_HONEST_FALLBACK_LOCATION_WORDS = frozenset(
    {
        "area",
        "bank",
        "branch",
        "canopy",
        "crown",
        "environment",
        "forest",
        "ground",
        "habitat",
        "landscape",
        "location",
        "plain",
        "river",
        "scene",
        "shore",
        "sky",
        "tree",
        "trunk",
        "vegetation",
        "water",
        "wetland",
    }
)
_HONEST_FALLBACK_STATE_WORDS = frozenset(
    {
        "climb",
        "climbing",
        "fall",
        "falling",
        "feed",
        "feeding",
        "fly",
        "flying",
        "graze",
        "grazing",
        "high",
        "huddle",
        "huddled",
        "hunt",
        "hunting",
        "low",
        "move",
        "moving",
        "perch",
        "perched",
        "rest",
        "resting",
        "run",
        "running",
        "sit",
        "sitting",
        "stand",
        "standing",
        "swim",
        "swimming",
        "walk",
        "walking",
    }
)
_HONEST_FALLBACK_SPATIAL_PREPOSITIONS = frozenset(
    {
        "above",
        "along",
        "around",
        "at",
        "beneath",
        "below",
        "beside",
        "between",
        "in",
        "inside",
        "into",
        "near",
        "on",
        "onto",
        "over",
        "through",
        "under",
        "within",
    }
)
_HONEST_FALLBACK_CAMERA_WORDS = frozenset(
    {
        "aerial",
        "angle",
        "closeup",
        "drone",
        "framing",
        "overhead",
        "perspective",
        "topdown",
    }
)
_HONEST_FALLBACK_GENERIC_MODIFIERS = frozenset(
    {
        "broad",
        "dense",
        "distant",
        "high",
        "large",
        "low",
        "lower",
        "natural",
        "open",
        "small",
        "smooth",
        "steep",
        "straight",
        "tall",
        "upper",
        "wide",
    }
)


def _honest_fallback_lemma(word: str) -> str:
    value = str(word).casefold().replace("’", "'").strip("'-")
    if value.endswith("ies") and len(value) > 4:
        return f"{value[:-3]}y"
    if value.endswith("s") and len(value) > 3 and not value.endswith(
        ("ss", "us", "is")
    ):
        return value[:-1]
    return value


def _honest_fallback_tokens(value: Any) -> list[tuple[str, str]]:
    words: list[tuple[str, str]] = []
    for match in _HONEST_FALLBACK_WORD.finditer(str(value or "").replace("-", " ")):
        surface = match.group(0).casefold()
        words.append((surface, _honest_fallback_lemma(surface)))
    return words


def _honest_fallback_lemmas(value: Any) -> list[str]:
    return [lemma for _surface, lemma in _honest_fallback_tokens(value)]


def _honest_fallback_contains(value: Any, term: Any) -> bool:
    haystack = _honest_fallback_lemmas(value)
    needle = _honest_fallback_lemmas(term)
    if not needle or len(needle) > len(haystack):
        return False
    width = len(needle)
    return any(
        haystack[index:index + width] == needle
        for index in range(len(haystack) - width + 1)
    )


def _honest_fallback_match_any(value: Any, terms: Sequence[Any]) -> str:
    for term in terms:
        normalized = " ".join(str(term or "").split())
        if normalized and _honest_fallback_contains(value, normalized):
            return normalized
    return ""


def _honest_fallback_query(value: Any) -> str:
    fallback = " ".join(str(value or "").split()).strip()
    if not fallback:
        return ""
    match = _HONEST_FALLBACK_CONDITIONAL_SHOW.search(fallback)
    if match:
        fallback = match.group(1).strip()
    return fallback.rstrip(" .")


def _honest_policy_brief_source(brief: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "slot_id": str(brief.get("slot_id") or "").strip(),
        "query_en": str(brief.get("query_en") or "").strip(),
        "must_have": sorted(
            {
                str(value).strip()
                for value in brief.get("must_have") or ()
                if str(value).strip()
            },
            key=str.casefold,
        ),
        "generic_fallback_en": str(
            brief.get("generic_fallback_en") or ""
        ).strip(),
        "observable_basis_en": str(
            brief.get("observable_basis_en") or ""
        ).strip(),
    }


def _honest_matcher(label: str) -> dict[str, Any]:
    surface = " ".join(str(label).split()).strip()
    lemmas = _honest_fallback_lemmas(surface)
    alternatives = {surface}
    if len(lemmas) == 1:
        lemma = lemmas[0]
        alternatives.update({lemma, f"{lemma}s"})
    return {
        "label": surface,
        "match_any": sorted(
            (value for value in alternatives if value), key=str.casefold
        ),
    }


def _honest_primary_facets(
    current_brief: Mapping[str, Any], fallback_clause: str
) -> dict[str, dict[str, Any]] | None:
    query = str(current_brief.get("query_en") or "")
    basis = str(current_brief.get("observable_basis_en") or "")
    must_have = [str(value) for value in current_brief.get("must_have") or ()]
    if not query or not basis or not must_have or not fallback_clause:
        return None
    query_lemmas = set(_honest_fallback_lemmas(query))
    basis_tokens = _honest_fallback_tokens(basis)
    fallback_lemmas = set(_honest_fallback_lemmas(fallback_clause))
    must_lemmas = set(
        lemma
        for value in must_have
        for lemma in _honest_fallback_lemmas(value)
    )

    subject: tuple[str, str] | None = None
    for surface, lemma in basis_tokens:
        if (
            lemma in _HONEST_FALLBACK_STOPWORDS
            or lemma in _HONEST_FALLBACK_LOCATION_WORDS
            or lemma in _HONEST_FALLBACK_STATE_WORDS
            or lemma in _HONEST_FALLBACK_CAMERA_WORDS
            or len(lemma) < 3
        ):
            continue
        if lemma in query_lemmas and lemma in must_lemmas:
            subject = (surface, lemma)
            break
    if subject is None or subject[1] not in fallback_lemmas:
        return None

    action: tuple[str, str] | None = None
    for surface, lemma in basis_tokens:
        state_like = (
            lemma in _HONEST_FALLBACK_STATE_WORDS
            or surface.endswith("ing")
            or surface.endswith("ed")
        )
        if (
            state_like
            and lemma != subject[1]
            and lemma in query_lemmas
            and lemma in fallback_lemmas
        ):
            action = (surface, lemma)
            break
    if action is None:
        return None

    spatial: tuple[str, str] | None = None
    recent_preposition = -10
    for index, (surface, lemma) in enumerate(basis_tokens):
        if lemma in _HONEST_FALLBACK_SPATIAL_PREPOSITIONS:
            recent_preposition = index
            continue
        spatial_like = (
            lemma in _HONEST_FALLBACK_LOCATION_WORDS
            or index - recent_preposition <= 3
        )
        if (
            spatial_like
            and lemma not in {subject[1], action[1]}
            and lemma not in _HONEST_FALLBACK_STOPWORDS
            and lemma in query_lemmas
            and lemma in must_lemmas
            and lemma in fallback_lemmas
        ):
            spatial = (surface, lemma)
            break
    if spatial is None:
        return None

    return {
        "subject": _honest_matcher(subject[0]),
        "action": _honest_matcher(action[0]),
        "spatial_relation": _honest_matcher(spatial[0]),
    }


def _honest_secondary_facets(
    current_brief: Mapping[str, Any],
    fallback_clause: str,
    primary_facets: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    positive = [
        str(current_brief.get("query_en") or ""),
        *[str(value) for value in current_brief.get("must_have") or ()],
        str(current_brief.get("observable_basis_en") or ""),
    ]
    fallback_lemmas = set(_honest_fallback_lemmas(fallback_clause))
    primary_lemmas = {
        lemma
        for facet in primary_facets.values()
        for lemma in _honest_fallback_lemmas(facet.get("label"))
    }
    subtype_scores: dict[str, int] = {}
    camera_scores: dict[str, int] = {}
    for value in positive:
        tokens = _honest_fallback_tokens(value)
        for index, (surface, lemma) in enumerate(tokens):
            if lemma in fallback_lemmas or lemma in primary_lemmas:
                continue
            if lemma in _HONEST_FALLBACK_CAMERA_WORDS:
                camera_scores[lemma] = camera_scores.get(lemma, 0) + 1
                continue
            if (
                lemma in _HONEST_FALLBACK_STOPWORDS
                or lemma in _HONEST_FALLBACK_GENERIC_MODIFIERS
                or lemma in _HONEST_FALLBACK_LOCATION_WORDS
                or lemma in _HONEST_FALLBACK_STATE_WORDS
                or len(lemma) < 3
            ):
                continue
            following = [
                next_lemma for _next_surface, next_lemma in tokens[index + 1:index + 3]
            ]
            if any(
                next_lemma in fallback_lemmas
                and next_lemma in _HONEST_FALLBACK_LOCATION_WORDS
                for next_lemma in following
            ):
                subtype_scores[lemma] = subtype_scores.get(lemma, 0) + 1

    rows: list[dict[str, Any]] = []
    if subtype_scores:
        best_score = max(subtype_scores.values())
        if best_score >= 2:
            for lemma in sorted(
                key for key, score in subtype_scores.items() if score == best_score
            ):
                rows.append(
                    {
                        "kind": "subtype",
                        **_honest_matcher(lemma),
                    }
                )
    if not rows and camera_scores:
        best_camera_score = max(camera_scores.values())
        for lemma in sorted(
            key for key, score in camera_scores.items() if score == best_camera_score
        ):
            rows.append(
                {
                    "kind": "camera",
                    **_honest_matcher(lemma),
                }
            )
    return rows


def build_honest_visible_fallback_policy(
    current_brief: Mapping[str, Any], failure_context: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Build a conservative, slot-agnostic visible-fallback matcher policy.

    The builder uses only the current positive visual contract and its provider
    fallback.  It returns ``None`` whenever a primary subject, action/state,
    spatial relation, or one safely relaxable secondary facet cannot be found
    deterministically.  Production therefore needs no species or slot rules.
    """

    if not isinstance(current_brief, Mapping) or not isinstance(
        failure_context, Mapping
    ):
        return None
    if failure_context.get("no_acceptable_candidate") is not True:
        return None
    fallback = str(current_brief.get("generic_fallback_en") or "")
    fallback_clause = _honest_fallback_query(fallback)
    primary = _honest_primary_facets(current_brief, fallback_clause)
    if primary is None:
        return None
    secondary = _honest_secondary_facets(
        current_brief, fallback_clause, primary
    )
    if not secondary:
        return None
    source = _honest_policy_brief_source(current_brief)
    return {
        "contract_version": HONEST_VISIBLE_FALLBACK_POLICY_VERSION,
        "builder": HONEST_VISIBLE_FALLBACK_POLICY_VERSION,
        "source_brief_sha256": _sha256_text(_canonical_json(source)),
        "source_fallback_sha256": _sha256_text(fallback),
        "primary_visible_facets": primary,
        "secondary_visible_facets": secondary,
    }


def _validated_honest_fallback_policy(
    request: Mapping[str, Any]
) -> dict[str, Any] | None:
    scarce_context = request.get("scarce_candidate_context")
    if not isinstance(scarce_context, Mapping):
        return None
    policy = scarce_context.get("honest_visible_fallback_policy")
    if not isinstance(policy, Mapping) or str(
        policy.get("contract_version") or ""
    ) != HONEST_VISIBLE_FALLBACK_POLICY_VERSION:
        return None
    if scarce_context.get("no_acceptable_candidate") is not True:
        return None
    current_brief = request.get("current_brief")
    if not isinstance(current_brief, Mapping):
        raise ValueError("honest visible fallback has no current_brief")
    expected_source_hash = _sha256_text(
        _canonical_json(_honest_policy_brief_source(current_brief))
    )
    if str(policy.get("source_brief_sha256") or "") != expected_source_hash:
        raise ValueError("honest visible fallback policy is bound to another brief")
    expected_source_fallback_hash = _sha256_text(
        str(current_brief.get("generic_fallback_en") or "")
    )
    if str(policy.get("source_fallback_sha256") or "") != (
        expected_source_fallback_hash
    ):
        raise ValueError(
            "honest visible fallback policy is bound to another source fallback"
        )

    raw_primary = policy.get("primary_visible_facets")
    if not isinstance(raw_primary, Mapping) or set(raw_primary) != set(
        _HONEST_FALLBACK_ROLE_ORDER
    ):
        raise ValueError("honest visible fallback primary facets are invalid")
    primary: dict[str, dict[str, Any]] = {}
    for role in _HONEST_FALLBACK_ROLE_ORDER:
        raw_facet = raw_primary.get(role)
        if not isinstance(raw_facet, Mapping):
            raise ValueError(f"honest visible fallback {role} facet is invalid")
        label = " ".join(str(raw_facet.get("label") or "").split()).strip()
        match_any = _string_list(
            raw_facet.get("match_any"),
            field=f"honest_visible_fallback_policy.{role}.match_any",
            required=True,
        )
        if not label:
            raise ValueError(f"honest visible fallback {role} label is empty")
        primary[role] = {"label": label, "match_any": match_any}

    raw_secondary = policy.get("secondary_visible_facets")
    if not isinstance(raw_secondary, Sequence) or isinstance(
        raw_secondary, (str, bytes, bytearray)
    ) or not raw_secondary:
        raise ValueError("honest visible fallback secondary facets are invalid")
    secondary: list[dict[str, Any]] = []
    for index, raw_facet in enumerate(raw_secondary):
        if not isinstance(raw_facet, Mapping):
            raise ValueError("honest visible fallback secondary facet is invalid")
        kind = str(raw_facet.get("kind") or "").strip().casefold()
        if kind not in _HONEST_FALLBACK_SECONDARY_KINDS:
            raise ValueError("honest visible fallback secondary kind is invalid")
        label = " ".join(str(raw_facet.get("label") or "").split()).strip()
        match_any = _string_list(
            raw_facet.get("match_any"),
            field=(
                "honest_visible_fallback_policy.secondary_visible_facets"
                f"[{index}].match_any"
            ),
            required=True,
        )
        if not label:
            raise ValueError("honest visible fallback secondary label is empty")
        secondary.append(
            {"kind": kind, "label": label, "match_any": match_any}
        )
    return {
        "contract_version": HONEST_VISIBLE_FALLBACK_POLICY_VERSION,
        "source_brief_sha256": expected_source_hash,
        "source_fallback_sha256": expected_source_fallback_hash,
        "primary_visible_facets": primary,
        "secondary_visible_facets": sorted(
            secondary, key=lambda row: (row["kind"], row["label"].casefold())
        ),
    }


def compile_honest_visible_fallback(
    request: Mapping[str, Any], brief: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Compile a provider fallback into one clean active v1.1 brief.

    Returns ``(brief, None)`` when the explicit policy does not apply or the
    provider kept every secondary facet.  Once an applicable policy exists,
    losing any primary subject/action/spatial facet is a hard local error.
    """

    clean_brief = dict(brief)
    policy = _validated_honest_fallback_policy(request)
    if policy is None:
        return clean_brief, None
    source_fallback = str(clean_brief.get("generic_fallback_en") or "").strip()
    active_clause = _honest_fallback_query(source_fallback)
    if not active_clause:
        raise ValueError("honest visible fallback is empty")

    retained: dict[str, str] = {}
    retained_matches: dict[str, str] = {}
    for role in _HONEST_FALLBACK_ROLE_ORDER:
        facet = policy["primary_visible_facets"][role]
        matched = _honest_fallback_match_any(active_clause, facet["match_any"])
        if not matched:
            if role == "subject":
                raise ValueError(
                    "honest visible fallback drops the primary visible subject"
                )
            raise ValueError(
                f"honest visible fallback drops the primary visible {role}"
            )
        retained[role] = str(facet["label"])
        retained_matches[role] = matched

    demoted: list[dict[str, str]] = []
    for facet in policy["secondary_visible_facets"]:
        if not _honest_fallback_match_any(active_clause, facet["match_any"]):
            demoted.append(
                {"kind": str(facet["kind"]), "label": str(facet["label"])}
            )
    if not demoted:
        return clean_brief, None

    current_positive = " ".join(
        [
            str(request.get("narration_text") or ""),
            str(request.get("current_brief", {}).get("query_en") or "")
            if isinstance(request.get("current_brief"), Mapping)
            else "",
            str(request.get("current_brief", {}).get("observable_basis_en") or "")
            if isinstance(request.get("current_brief"), Mapping)
            else "",
        ]
    )
    current_lemmas = set(_honest_fallback_lemmas(current_positive))
    spatial_terms = set(
        _honest_fallback_lemmas(
            policy["primary_visible_facets"]["spatial_relation"]["label"]
        )
    )
    active_tokens = _honest_fallback_tokens(active_clause)
    for index, (_surface, lemma) in enumerate(active_tokens[:-1]):
        next_lemma = active_tokens[index + 1][1]
        if (
            next_lemma in spatial_terms
            and lemma not in current_lemmas
            and lemma not in _HONEST_FALLBACK_STOPWORDS
            and lemma not in _HONEST_FALLBACK_GENERIC_MODIFIERS
        ):
            raise ValueError(
                "honest visible fallback invents a replacement subtype or habitat"
            )

    active_query = f"Live-action documentary shot of {active_clause}"
    candidate = {
        "contract_version": str(clean_brief["contract_version"]),
        "slot_id": str(clean_brief["slot_id"]),
        "query_en": active_query,
        "must_have": [active_clause],
        "must_not": list(clean_brief.get("must_not") or ()),
        "generic_fallback_en": active_clause,
        "proxy_mode": "observable_proxy",
        "observable_basis_en": active_clause,
    }
    raw_beat = request.get("beat")
    beat = raw_beat if isinstance(raw_beat, Mapping) else {}
    active_brief = validate_visual_brief_contract(
        {
            "slot_id": str(
                request.get("slot_id")
                or beat.get("slot_id")
                or clean_brief.get("slot_id")
                or ""
            ),
            "text": str(
                request.get("narration_text") or beat.get("text") or ""
            ),
        },
        candidate,
    )
    receipt = {
        "contract_version": HONEST_VISIBLE_FALLBACK_POLICY_VERSION,
        "compiler": HONEST_VISIBLE_FALLBACK_POLICY_VERSION,
        "source_brief_sha256": str(policy["source_brief_sha256"]),
        "policy_source_fallback_sha256": str(
            policy["source_fallback_sha256"]
        ),
        "source_fallback_sha256": _sha256_text(source_fallback),
        "policy_sha256": _sha256_text(_canonical_json(policy)),
        "retained_facets": retained,
        "retained_facet_matches": retained_matches,
        "unresolved_narrated_details": [row["label"] for row in demoted],
        "demoted_secondary_facets": demoted,
        "active_brief_sha256": _sha256_text(_canonical_json(active_brief)),
    }
    return active_brief, receipt


def pop_honest_visible_fallback_receipt(
    brief: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Detach and verify the repair-only receipt from a clean visual brief."""

    clean = dict(brief)
    raw_receipt = clean.pop(HONEST_VISIBLE_FALLBACK_RECEIPT_FIELD, None)
    if raw_receipt is None:
        return clean, None
    if not isinstance(raw_receipt, Mapping) or str(
        raw_receipt.get("contract_version") or ""
    ) != HONEST_VISIBLE_FALLBACK_POLICY_VERSION:
        raise ValueError("honest visible fallback receipt is invalid")
    receipt = dict(raw_receipt)
    expected_hash = _sha256_text(_canonical_json(clean))
    if str(receipt.get("active_brief_sha256") or "") != expected_hash:
        raise ValueError("honest visible fallback receipt brief hash mismatch")
    return clean, receipt


def _canonical_context(value: Any, *, field: str) -> Any:
    """Return a JSON-only, key-sorted context value for stable paid identity."""

    if value is None:
        return None
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError(f"{field} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key in sorted(value, key=lambda item: str(item)):
            key = str(raw_key).strip()
            if not key:
                raise ValueError(f"{field} contains an empty object key")
            if key in normalized:
                raise ValueError(f"{field} contains duplicate normalized key {key!r}")
            normalized[key] = _canonical_context(value[raw_key], field=f"{field}.{key}")
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [
            _canonical_context(item, field=f"{field}[{index}]")
            for index, item in enumerate(value)
        ]
    raise ValueError(f"{field} must contain only JSON-compatible values")


def _string_list(value: Any, *, field: str, required: bool = False) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = list(value)
    else:
        raise ValueError(f"{field} must be a string or array of strings")
    normalized = sorted(
        {str(item).strip() for item in values if str(item).strip()},
        key=str.casefold,
    )
    if required and not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized


def _normalize_current_brief(brief: Mapping[str, Any], slot_id: str) -> dict[str, Any]:
    current_slot_id = str(brief.get("slot_id") or "").strip()
    if current_slot_id != slot_id:
        raise ValueError(f"current_brief slot mismatch for {slot_id}")
    normalized: dict[str, Any] = {}
    for field in _CURRENT_BRIEF_FIELDS:
        value = brief.get(field)
        if field in {"must_have", "must_not"}:
            normalized[field] = _string_list(value or [], field=f"current_brief.{field}")
        elif value is not None:
            normalized[field] = str(value).strip()
    return normalized


def _normalize_repair_requests(repair_requests: list[dict]) -> list[dict[str, Any]]:
    if not isinstance(repair_requests, list) or not repair_requests:
        raise ValueError("repair_requests must be a non-empty array")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, request in enumerate(repair_requests):
        if not isinstance(request, Mapping):
            raise ValueError(f"repair_requests[{index}] must be an object")
        beat = request.get("beat")
        brief = request.get("current_brief")
        if not isinstance(beat, Mapping):
            raise ValueError(f"repair_requests[{index}].beat must be an object")
        if not isinstance(brief, Mapping):
            raise ValueError(
                f"repair_requests[{index}].current_brief must be an object"
            )
        slot_id = str(beat.get("slot_id") or "").strip()
        narration_text = str(beat.get("text") or "").strip()
        if not slot_id or not narration_text:
            raise ValueError(f"repair_requests[{index}] needs beat slot_id and text")
        if slot_id in seen:
            raise ValueError(f"duplicate semantic proxy repair slot_id: {slot_id}")
        seen.add(slot_id)

        neighbors = _canonical_context(
            request.get("neighbor_beat_texts", {}),
            field=f"repair_requests[{index}].neighbor_beat_texts",
        )
        if not isinstance(neighbors, (dict, list)):
            raise ValueError("neighbor_beat_texts must be an object or array")
        row: dict[str, Any] = {
            "slot_id": slot_id,
            "narration_text": narration_text,
            "current_brief": _normalize_current_brief(brief, slot_id),
            "neighbor_beat_texts": neighbors,
            "strict_evaluation_failure_reasons": _string_list(
                request.get("failure_reasons"),
                field=f"repair_requests[{index}].failure_reasons",
                required=True,
            ),
        }
        for field in ("scarce_candidate_context", "adjacent_anchor_context"):
            if request.get(field) is not None:
                row[field] = _canonical_context(
                    request[field], field=f"repair_requests[{index}].{field}"
                )
        normalized.append(row)
    return normalized


def _request_spec(
    repair_requests: list[dict],
    *,
    model: str,
    contract_version: str,
    repair_prompt_version: str = SEMANTIC_PROXY_REPAIR_PROMPT_VERSION,
) -> dict[str, Any]:
    if contract_version != VISUAL_BRIEF_CONTRACT_VERSION:
        raise ValueError(f"unsupported visual brief contract: {contract_version}")
    model = str(model).strip()
    if not model:
        raise ValueError("semantic proxy repair model must not be empty")
    repair_prompt_version = str(repair_prompt_version).strip()
    if not repair_prompt_version:
        raise ValueError("semantic proxy repair prompt version must not be empty")
    return {
        "repair_prompt_version": repair_prompt_version,
        "contract_version": contract_version,
        "model": model,
        "targets": _normalize_repair_requests(repair_requests),
    }


def _request_semantic_proxy_repair_raw(
    repair_requests: list[dict],
    *,
    prompt: str,
    repair_prompt_version: str,
    model: str = "gpt-5-nano",
    chat_fn: Callable = _chat,
    contract_version: str = VISUAL_BRIEF_CONTRACT_VERSION,
) -> dict:
    spec = _request_spec(
        repair_requests,
        model=model,
        contract_version=contract_version,
        repair_prompt_version=repair_prompt_version,
    )
    rendered_prompt = prompt.format(
        prompt_version=repair_prompt_version,
        contract_version=contract_version,
        targets=json.dumps(
            spec["targets"], ensure_ascii=False, sort_keys=True, indent=2
        ),
    )
    answer = chat_fn(
        str(spec["model"]),
        [{"role": "user", "content": rendered_prompt}],
        reasoning_effort="minimal",
        max_tokens=max(1400, 420 * len(spec["targets"])),
    )
    content = "" if answer is None else str(answer)
    return {
        "repair_prompt_version": repair_prompt_version,
        "contract_version": contract_version,
        "model": str(spec["model"]),
        "slot_ids": [str(row["slot_id"]) for row in spec["targets"]],
        "request_sha256": _sha256_text(_canonical_json(spec)),
        "content_sha256": _sha256_text(content),
        "content": content,
    }


def request_semantic_proxy_repair_raw(
    repair_requests: list[dict],
    *,
    model: str = "gpt-5-nano",
    chat_fn: Callable = _chat,
    contract_version: str = VISUAL_BRIEF_CONTRACT_VERSION,
) -> dict:
    """Make one paid repair call and return its deterministic raw envelope.

    This function intentionally performs no parsing or output validation.  The
    caller must durably persist the returned mapping before calling
    :func:`validate_semantic_proxy_repair_response`.
    """

    return _request_semantic_proxy_repair_raw(
        repair_requests,
        prompt=SEMANTIC_PROXY_REPAIR_PROMPT,
        repair_prompt_version=SEMANTIC_PROXY_REPAIR_PROMPT_VERSION,
        model=model,
        chat_fn=chat_fn,
        contract_version=contract_version,
    )


def request_post_resegment_semantic_proxy_repair_raw(
    repair_requests: list[dict],
    *,
    model: str = "gpt-5-nano",
    chat_fn: Callable = _chat,
    contract_version: str = VISUAL_BRIEF_CONTRACT_VERSION,
) -> dict:
    """Return an unparsed paid envelope for the bounded post-split repair."""

    return _request_semantic_proxy_repair_raw(
        repair_requests,
        prompt=POST_RESEG_SEMANTIC_PROXY_REPAIR_PROMPT,
        repair_prompt_version=POST_RESEG_SEMANTIC_PROXY_REPAIR_PROMPT_VERSION,
        model=model,
        chat_fn=chat_fn,
        contract_version=contract_version,
    )


def _reserved_anchor_terms(context: Any, slot_id: str) -> list[str]:
    contexts = context if isinstance(context, list) else [context]
    reserved: list[str] = []
    for row in contexts:
        if not isinstance(row, Mapping):
            continue
        allocated_to = str(row.get("allocated_to_slot_id") or "").strip()
        if not allocated_to or allocated_to == slot_id:
            continue
        anchor = row.get("anchor_en")
        if isinstance(anchor, str):
            values = [anchor]
        elif isinstance(anchor, Sequence) and not isinstance(anchor, (bytes, bytearray)):
            values = list(anchor)
        else:
            values = []
        reserved.extend(str(value).strip() for value in values if str(value).strip())
    return sorted(set(reserved), key=str.casefold)


def _fabricated_status_proof(
    narration: str, proposed_visuals: str
) -> re.Match[str] | None:
    for proposed_pattern, narration_pattern in _STATUS_PROOF_RULES:
        fabricated = proposed_pattern.search(proposed_visuals)
        if fabricated and not narration_pattern.search(narration):
            return fabricated
    return None


def _natural_environment_type(source: str) -> str:
    for canonical_name, pattern in _NATURAL_ENVIRONMENT_TYPES:
        if pattern.search(source):
            return canonical_name
    return ""


def _compile_natural_carrier_proxy(
    request: dict[str, Any], brief: dict[str, Any]
) -> dict[str, Any]:
    """Replace a scarce exact water landmark with honest continuous evidence.

    This compiler is deliberately narrow.  It activates only for a compound
    jurisdiction/status plus horizontal-scale claim, only under the explicit
    versioned production policy, and only when strict evaluation found no
    acceptable candidate or an adjacent slot owns the rare landmark.  The raw
    paid response remains the immutable audit record; the returned brief is the
    canonical camera-visible contract used for retrieval and judging.
    """

    narration = str(request.get("narration_text") or "")
    if not (
        _STATUS_OR_JURISDICTION_CLAIM.search(narration)
        and _WIDTH_CLAIM.search(narration)
    ):
        return brief

    scarce_context = request.get("scarce_candidate_context")
    if not isinstance(scarce_context, Mapping):
        return brief
    policy = scarce_context.get("natural_carrier_policy")
    if not isinstance(policy, Mapping) or str(
        policy.get("contract_version") or ""
    ) != NATURAL_CARRIER_POLICY_VERSION:
        return brief
    if policy.get("enforce_compound_status_visible_scale") is not True:
        return brief

    slot_id = str(request.get("slot_id") or "")
    reserved_terms = _reserved_anchor_terms(
        request.get("adjacent_anchor_context"), slot_id
    )
    has_reserved_landmark = any(
        _SCARCE_WATER_LANDMARK.search(term) for term in reserved_terms
    )
    if scarce_context.get("no_acceptable_candidate") is not True and not (
        has_reserved_landmark
    ):
        return brief

    current_brief = request.get("current_brief")
    current_context = ""
    if isinstance(current_brief, Mapping):
        current_context = " ".join(
            [
                str(current_brief.get("query_en") or ""),
                *[str(item) for item in current_brief.get("must_have") or ()],
                str(current_brief.get("observable_basis_en") or ""),
            ]
        )
    proposed_visuals = " ".join(
        [
            str(brief.get("query_en") or ""),
            *[str(item) for item in brief.get("must_have") or ()],
            str(brief.get("generic_fallback_en") or ""),
            str(brief.get("observable_basis_en") or ""),
        ]
    )
    if not _WATER_CARRIER_CONTEXT.search(
        " ".join([narration, current_context, proposed_visuals])
    ):
        return brief
    if not _SCARCE_WATER_LANDMARK.search(proposed_visuals):
        return brief

    return {
        "contract_version": str(brief["contract_version"]),
        "slot_id": str(brief["slot_id"]),
        "query_en": (
            "wide live-action view of one continuous natural watercourse "
            "between two opposite natural banks"
        ),
        "must_have": [
            "one continuous broad natural watercourse",
            "both opposite natural banks visible in the same shot",
        ],
        "must_not": [
            (
                "border signs, flags, checkpoints, markers, fences, labels, "
                "maps, or infrastructure presented as status proof"
            ),
            "CGI, diagrams, split screens, montages, or shot sequences",
        ],
        "generic_fallback_en": (
            "stable wide live-action view of a natural watercourse with both "
            "opposite natural banks"
        ),
        "proxy_mode": "observable_proxy",
        "observable_basis_en": (
            "the broad continuous natural watercourse and both opposite natural "
            "banks are directly camera-visible in one shot"
        ),
    }


def _compile_natural_status_environment_proxy(
    request: dict[str, Any], brief: dict[str, Any]
) -> dict[str, Any]:
    """Compile invented status proof into an honest natural-place proxy.

    A model-suggested sign or plaque is discarded only when the narration does
    not name that physical object, strict evaluation found no acceptable shot,
    and a versioned policy plus a concrete natural environment are present.
    Unknown/non-natural locations remain fail-closed.
    """

    narration = str(request.get("narration_text") or "")
    if not _STATUS_OR_JURISDICTION_CLAIM.search(narration):
        return brief
    scarce_context = request.get("scarce_candidate_context")
    if not isinstance(scarce_context, Mapping):
        return brief
    policy = scarce_context.get("natural_status_environment_policy")
    if not isinstance(policy, Mapping) or str(
        policy.get("contract_version") or ""
    ) != NATURAL_STATUS_ENVIRONMENT_POLICY_VERSION:
        return brief
    if (
        policy.get("compile_unnarrated_status_proof") is not True
        or scarce_context.get("no_acceptable_candidate") is not True
    ):
        return brief

    proposed_visuals = " ".join(
        [
            str(brief.get("query_en") or ""),
            *[str(item) for item in brief.get("must_have") or ()],
            str(brief.get("generic_fallback_en") or ""),
            str(brief.get("observable_basis_en") or ""),
        ]
    )
    if _fabricated_status_proof(narration, proposed_visuals) is None:
        return brief

    current_brief = request.get("current_brief")
    current_context = ""
    if isinstance(current_brief, Mapping):
        current_context = " ".join(
            [
                str(current_brief.get("query_en") or ""),
                *[str(item) for item in current_brief.get("must_have") or ()],
                str(current_brief.get("generic_fallback_en") or ""),
                str(current_brief.get("observable_basis_en") or ""),
            ]
        )
    environment = _natural_environment_type(
        " ".join([narration, current_context, proposed_visuals])
    )
    if not environment:
        return brief

    return {
        "contract_version": str(brief["contract_version"]),
        "slot_id": str(brief["slot_id"]),
        "query_en": f"wide live-action view of a natural {environment} landscape",
        "must_have": [f"natural {environment} landscape in one continuous shot"],
        "must_not": [
            (
                "signs, plaques, logos, emblems, flags, gates, visitor centres, "
                "labels, maps, certificates, or infrastructure presented as status proof"
            ),
            "CGI, diagrams, split screens, montages, or shot sequences",
        ],
        "generic_fallback_en": (
            f"stable live-action view of the natural {environment} environment"
        ),
        "proxy_mode": "observable_proxy",
        "observable_basis_en": (
            f"the natural {environment} landscape is directly camera-visible "
            "in one continuous shot"
        ),
    }


def _grounded_magnitude_drop_policy_applies(request: Mapping[str, Any]) -> bool:
    narration = str(request.get("narration_text") or "")
    if not _PHYSICAL_MAGNITUDE_OR_DROP_CLAIM.search(narration):
        return False
    scarce_context = request.get("scarce_candidate_context")
    if not isinstance(scarce_context, Mapping):
        return False
    policy = scarce_context.get("grounded_magnitude_drop_policy")
    if not isinstance(policy, Mapping) or str(
        policy.get("contract_version") or ""
    ) != GROUNDED_MAGNITUDE_DROP_POLICY_VERSION:
        return False
    if (
        scarce_context.get("no_acceptable_candidate") is not True
        or policy.get("compile_numeric_drop_to_natural_cues") is not True
    ):
        return False

    current_brief = request.get("current_brief")
    current_context = ""
    if isinstance(current_brief, Mapping):
        current_context = " ".join(
            [
                str(current_brief.get("query_en") or ""),
                *[str(item) for item in current_brief.get("must_have") or ()],
                str(current_brief.get("observable_basis_en") or ""),
            ]
        )
    neighbor_context = json.dumps(
        request.get("neighbor_beat_texts") or {}, ensure_ascii=False, sort_keys=True
    )
    combined_context = " ".join([narration, current_context, neighbor_context])
    return bool(
        _WATER_CARRIER_CONTEXT.search(combined_context)
        and _WATER_DROP_ACTION.search(combined_context)
    )


def _compile_grounded_magnitude_drop_proxy(
    request: dict[str, Any], brief: dict[str, Any]
) -> dict[str, Any]:
    """Canonicalize a failed numerical water-drop brief to visible natural cues."""

    if not _grounded_magnitude_drop_policy_applies(request):
        return brief

    narration = str(request.get("narration_text") or "")
    narrated_people = bool(_NARRATED_PEOPLE.search(narration))
    narrated_boat = bool(_NARRATED_BOAT.search(narration))
    narrated_scale: list[str] = []
    if narrated_people:
        narrated_scale.append("people")
    if narrated_boat:
        narrated_scale.append("a boat")
    scale_phrase = " and ".join(narrated_scale)

    query = (
        "live-action natural waterfall with falling water over a visible drop edge or "
        "steep rock face, plus natural mist, spray, rock, or vegetation"
    )
    second_facet = "natural mist, spray, rock, or vegetation around the drop"
    fallback = (
        "steady live-action view of falling water over a natural rock edge, with mist, "
        "spray, rock, or vegetation"
    )
    basis = (
        "falling water over a steep natural rock edge and the surrounding mist, spray, "
        "rock, or vegetation are directly camera-visible; the numerical height remains "
        "narration-only"
    )
    if scale_phrase:
        query = f"{query}, with {scale_phrase} visible in the same shot"
        second_facet = f"{second_facet}, with {scale_phrase} visible in the same shot"
        fallback = f"{fallback}, with {scale_phrase} visible"
        basis = f"{basis}; {scale_phrase} are directly camera-visible"

    forbidden_scale: list[str] = []
    if not narrated_people:
        forbidden_scale.append("unnarrated people")
    if not narrated_boat:
        forbidden_scale.append("unnarrated boats")
    forbidden_scale.extend(
        ["scale markers", "measurement labels", "maps", "captions", "added text"]
    )
    return {
        "contract_version": str(brief["contract_version"]),
        "slot_id": str(brief["slot_id"]),
        "query_en": query,
        "must_have": [
            "falling water over a visible natural drop edge or steep rock face",
            second_facet,
        ],
        "must_not": [
            ", ".join(forbidden_scale),
            "CGI, animation, diagrams, split screens, montages, or shot sequences",
        ],
        "generic_fallback_en": fallback,
        "proxy_mode": "observable_consequence",
        "observable_basis_en": basis,
    }


def _validate_grounded_magnitude_drop_policy(
    request: dict[str, Any], brief: dict[str, Any]
) -> None:
    if not _grounded_magnitude_drop_policy_applies(request):
        return
    narration = str(request.get("narration_text") or "")
    visible_contract = " ".join(
        [
            str(brief.get("query_en") or ""),
            *[str(item) for item in brief.get("must_have") or ()],
            str(brief.get("observable_basis_en") or ""),
        ]
    )
    proposed_visuals = " ".join(
        [visible_contract, str(brief.get("generic_fallback_en") or "")]
    )
    if brief.get("proxy_mode") == "literal_visible":
        raise ValueError(
            f"post-resegment magnitude repair {request['slot_id']} remains literal_visible"
        )
    if not _VISIBLE_NATURAL_DROP_CUE.search(visible_contract):
        raise ValueError(
            f"post-resegment magnitude repair {request['slot_id']} has no visible drop cue"
        )
    if not _VISIBLE_FALLING_WATER.search(visible_contract):
        raise ValueError(
            f"post-resegment magnitude repair {request['slot_id']} has no visible falling water"
        )
    if not _VISIBLE_NATURAL_DROP_CONTEXT.search(visible_contract):
        raise ValueError(
            f"post-resegment magnitude repair {request['slot_id']} has no natural context"
        )
    fabricated_number = _NUMERIC_MAGNITUDE_PROOF.search(proposed_visuals)
    if fabricated_number:
        raise ValueError(
            f"post-resegment magnitude repair {request['slot_id']} tries to prove the "
            f"narrated number visually: {fabricated_number.group(0)}"
        )
    synthetic = _FORBIDDEN_SYNTHETIC_DROP_VISUAL.search(proposed_visuals)
    if synthetic:
        raise ValueError(
            f"post-resegment magnitude repair {request['slot_id']} requests synthetic "
            f"evidence: {synthetic.group(0)}"
        )
    if not _NARRATED_PEOPLE.search(narration) and _NARRATED_PEOPLE.search(
        proposed_visuals
    ):
        raise ValueError(
            f"post-resegment magnitude repair {request['slot_id']} invents people for scale"
        )
    if not _NARRATED_BOAT.search(narration) and _NARRATED_BOAT.search(proposed_visuals):
        raise ValueError(
            f"post-resegment magnitude repair {request['slot_id']} invents a boat for scale"
        )


def _validate_repair_policy(request: dict[str, Any], brief: dict[str, Any]) -> None:
    slot_id = str(request["slot_id"])
    visible_contract = " ".join(
        [
            str(brief.get("query_en") or ""),
            *[str(item) for item in brief.get("must_have") or []],
            str(brief.get("observable_basis_en") or ""),
        ]
    )
    proposed_visuals = " ".join(
        [
            visible_contract,
            str(brief.get("generic_fallback_en") or ""),
        ]
    )
    multi_shot = _MULTI_SHOT_REQUIREMENT.search(visible_contract)
    if multi_shot:
        raise ValueError(
            f"semantic proxy repair {slot_id} still requires multiple shots: "
            f"{multi_shot.group(0)}"
        )

    narration = str(request.get("narration_text") or "")
    if _STATUS_OR_JURISDICTION_CLAIM.search(narration):
        if brief.get("proxy_mode") == "literal_visible":
            raise ValueError(
                f"semantic proxy repair {slot_id} marks a status claim literal_visible"
            )
        if not _SPECIFIC_NATURAL_LOCATION.search(visible_contract):
            raise ValueError(
                f"semantic proxy repair {slot_id} status proxy has no specific "
                "natural location or environment"
            )
        fabricated = _fabricated_status_proof(narration, proposed_visuals)
        if fabricated:
            raise ValueError(
                f"semantic proxy repair {slot_id} fabricates physical status proof: "
                f"{fabricated.group(0)}"
            )
        if _WIDTH_CLAIM.search(narration) and not _VISIBLE_WIDTH_PROXY.search(
            visible_contract
        ):
            raise ValueError(
                f"semantic proxy repair {slot_id} drops the visible width claim"
            )

    reserved_terms = _reserved_anchor_terms(
        request.get("adjacent_anchor_context"), slot_id
    )
    visible_casefold = visible_contract.casefold()
    for anchor in reserved_terms:
        if anchor.casefold() in visible_casefold:
            raise ValueError(
                f"semantic proxy repair {slot_id} steals adjacent reserved anchor: {anchor}"
            )


def _validate_semantic_proxy_repair_response(
    repair_requests: list[dict],
    raw_response: dict,
    *,
    repair_prompt_version: str,
    compile_grounded_magnitude_drop: bool,
    model: str = "gpt-5-nano",
    contract_version: str = VISUAL_BRIEF_CONTRACT_VERSION,
) -> list[dict]:
    spec = _request_spec(
        repair_requests,
        model=model,
        contract_version=contract_version,
        repair_prompt_version=repair_prompt_version,
    )
    if not isinstance(raw_response, Mapping):
        raise ValueError("raw semantic proxy repair response must be an object")
    if (
        str(raw_response.get("repair_prompt_version") or "")
        != repair_prompt_version
    ):
        raise ValueError("raw semantic proxy repair prompt version is missing or stale")
    if str(raw_response.get("contract_version") or "") != contract_version:
        raise ValueError("raw semantic proxy repair visual contract is missing or stale")
    if str(raw_response.get("model") or "") != str(spec["model"]):
        raise ValueError("raw semantic proxy repair is bound to a different model")

    expected_slot_ids = [str(row["slot_id"]) for row in spec["targets"]]
    raw_slot_ids = raw_response.get("slot_ids")
    if not isinstance(raw_slot_ids, list) or [str(value) for value in raw_slot_ids] != expected_slot_ids:
        raise ValueError("raw semantic proxy repair is bound to different slots")

    expected_request_hash = _sha256_text(_canonical_json(spec))
    if str(raw_response.get("request_sha256") or "") != expected_request_hash:
        raise ValueError("raw semantic proxy repair request hash mismatch")
    content = raw_response.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("raw semantic proxy repair has no provider content")
    if str(raw_response.get("content_sha256") or "") != _sha256_text(content):
        raise ValueError("raw semantic proxy repair content hash mismatch")

    parsed = extract_json(content)
    if not isinstance(parsed, list):
        raise ValueError("semantic proxy repair response must be a JSON array")
    if any(not isinstance(row, Mapping) for row in parsed):
        raise ValueError("semantic proxy repair response contains a non-object row")
    parsed_ids = [str(row.get("slot_id") or "") for row in parsed]
    if len(parsed_ids) != len(set(parsed_ids)) or set(parsed_ids) != set(expected_slot_ids):
        raise ValueError(
            "semantic proxy repair response slot_ids are missing, extra, or duplicate"
        )

    parsed_by_id = {str(row["slot_id"]): row for row in parsed}
    requests_by_id = {str(row["slot_id"]): row for row in spec["targets"]}
    beats_by_id = {
        str(request["beat"]["slot_id"]): dict(request["beat"])
        for request in repair_requests
    }
    validated: list[dict] = []
    for slot_id in expected_slot_ids:
        brief = validate_visual_brief_contract(
            beats_by_id[slot_id],
            dict(parsed_by_id[slot_id]),
            contract_version=contract_version,
        )
        brief, honest_fallback_receipt = compile_honest_visible_fallback(
            requests_by_id[slot_id], brief
        )
        if honest_fallback_receipt is None:
            brief = _compile_natural_carrier_proxy(requests_by_id[slot_id], brief)
            brief = _compile_natural_status_environment_proxy(
                requests_by_id[slot_id], brief
            )
            if compile_grounded_magnitude_drop:
                brief = _compile_grounded_magnitude_drop_proxy(
                    requests_by_id[slot_id], brief
                )
        _validate_repair_policy(requests_by_id[slot_id], brief)
        if compile_grounded_magnitude_drop and honest_fallback_receipt is None:
            _validate_grounded_magnitude_drop_policy(
                requests_by_id[slot_id], brief
            )
        if honest_fallback_receipt is not None:
            brief = {
                **brief,
                HONEST_VISIBLE_FALLBACK_RECEIPT_FIELD: honest_fallback_receipt,
            }
        validated.append(brief)
    return validated


def validate_semantic_proxy_repair_response(
    repair_requests: list[dict],
    raw_response: dict,
    *,
    model: str = "gpt-5-nano",
    contract_version: str = VISUAL_BRIEF_CONTRACT_VERSION,
) -> list[dict]:
    """Validate a previously persisted general repair envelope, fail-closed."""

    return _validate_semantic_proxy_repair_response(
        repair_requests,
        raw_response,
        repair_prompt_version=SEMANTIC_PROXY_REPAIR_PROMPT_VERSION,
        compile_grounded_magnitude_drop=False,
        model=model,
        contract_version=contract_version,
    )


def validate_post_resegment_semantic_proxy_repair_response(
    repair_requests: list[dict],
    raw_response: dict,
    *,
    model: str = "gpt-5-nano",
    contract_version: str = VISUAL_BRIEF_CONTRACT_VERSION,
) -> list[dict]:
    """Validate and compile a durable post-resegmentation repair envelope."""

    return _validate_semantic_proxy_repair_response(
        repair_requests,
        raw_response,
        repair_prompt_version=POST_RESEG_SEMANTIC_PROXY_REPAIR_PROMPT_VERSION,
        compile_grounded_magnitude_drop=True,
        model=model,
        contract_version=contract_version,
    )


__all__ = [
    "GROUNDED_MAGNITUDE_DROP_POLICY_VERSION",
    "HONEST_VISIBLE_FALLBACK_POLICY_VERSION",
    "HONEST_VISIBLE_FALLBACK_RECEIPT_FIELD",
    "NATURAL_CARRIER_POLICY_VERSION",
    "NATURAL_STATUS_ENVIRONMENT_POLICY_VERSION",
    "POST_RESEG_SEMANTIC_PROXY_REPAIR_PROMPT_VERSION",
    "SEMANTIC_PROXY_REPAIR_PROMPT_VERSION",
    "build_honest_visible_fallback_policy",
    "compile_honest_visible_fallback",
    "pop_honest_visible_fallback_receipt",
    "request_post_resegment_semantic_proxy_repair_raw",
    "request_semantic_proxy_repair_raw",
    "validate_post_resegment_semantic_proxy_repair_response",
    "validate_semantic_proxy_repair_response",
]
