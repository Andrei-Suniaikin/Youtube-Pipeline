"""LLM visual briefs and temporal top-K judging.

Only selected candidates reach the paid temporal judge. Any malformed response
or provider failure leaves the slot unresolved; no retrieval fallback is selected.
"""
from __future__ import annotations

import base64
import concurrent.futures as cf
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .llm_client import chat as _chat
from .llm_client import extract_json


VISUAL_BRIEF_CONTRACT_VERSION = "observable-proxy-v1.1"
VISUAL_BRIEF_MAX_MUST_HAVE = 2
MUST_HAVE_LOCATIVE_FLORA_COMPILER_VERSION = "must-have-locative-flora-state-v1"
STRICT_JUDGE_PROMPT_CONTRACT_VERSION = "strict-temporal-contact-sheet-v2"
STRICT_JUDGE_SCHEMA_REPAIR_CONTRACT_VERSION = "strict-temporal-schema-repair-v1"
STRICT_JUDGE_SCHEMA_REPAIR_MAX_ATTEMPTS = 2


BRIEF_PROMPT = """You prepare camera-observable visual search briefs for a universal documentary editor.
Contract version: {contract_version}.
For every narration slot return one JSON object with exactly:
contract_version, slot_id, query_en, must_have, must_not, generic_fallback_en,
proxy_mode, observable_basis_en.

Hard contract:
- query_en and must_have describe only evidence directly visible in one ordinary 4--8 second
  live-action documentary shot. must_have is ordered by importance and contains at most two items.
- proxy_mode is literal_visible, observable_proxy, or observable_consequence.
- Slow, hidden, or causal claims are never illustrated as if the process itself were visible.
  Convert growth, underground roots, germination/seed dispersal, and causality into an honest
  present-time observable proxy or consequence. Examples: a mature canopy or nearby saplings for
  growth; exposed buttress roots for underground roots; ripe fruit, fallen seeds, or seedlings for
  reproduction; the visible affected habitat or behaviour for an asserted cause.
- observable_basis_en names the concrete camera-visible evidence that justifies the query.
- Never request CGI, animation, time-lapse, diagrams, infographics, x-ray, cutaway, cross-section,
  causal arrows, or other explanatory graphics. Never invent a named animal, place, person, or
  event absent from the narration.
- If no honest specific proxy exists, use an honest neutral live-action consequence or environment
  in generic_fallback_en instead of fake evidence.

Return only a JSON array.

SLOTS:
{slots}
"""


_SLOW_OR_HIDDEN_CLAIM = re.compile(
    r"(?:\b(?:grow|grows|growing|growth|germinat\w*|seed\w*|root\w*|"
    r"cause\w*|causal\w*|because|therefore|leads?\s+to|results?\s+in)\b|"
    # Keep Russian growth/causality verbs explicit: broad stems such as
    # ``раст\w*`` and ``привод\w*`` also match visible nouns (растения, привод).
    r"\b(?:раст(?:и|[её]т|ут|ущ\w*)|рост(?:а|у|ом|е)?|"
    r"(?:вы|раз)раст\w*|вырос\w*|корн\w*|семен\w*|прораст\w*|причин\w*|"
    r"потому\s+что|привод(?:ит|ят|ил(?:а|о|и)?|ящ\w*|ивш\w*)|"
    r"привед(?:[её]т|ут)|привести|прив(?:ёл|ел|ела|ели)|"
    r"вызыва\w*|следств\w*)\b)",
    re.IGNORECASE,
)
_FORBIDDEN_EXPLANATORY_VISUAL = re.compile(
    r"\b(?:cgi|computer[- ]generated|animat\w*|time[- ]?lapse|diagram\w*|"
    r"schematic\w*|infographic\w*|x[- ]?ray|cutaway|cross[- ]?section|"
    r"causal arrows?)\b",
    re.IGNORECASE,
)
_NONOBSERVABLE_SEARCH_PROCESS = re.compile(
    r"\b(?:grow(?:s|ing|th)?|germinat\w*|seed\s+dispers\w*|"
    r"underground\s+roots?|caus(?:e|es|ing|ality|al)|because|"
    r"leads?\s+to|results?\s+in)\b",
    re.IGNORECASE,
)
_OBSERVABLE_QUERY_COMPILER = "observable-basis-query-v1"
_OBSERVABLE_QUERY_NORMALIZATION_REASON = (
    "nonobservable_query_replaced_with_clean_observable_basis"
)
_VISIBLE_EVIDENCE_PROXY_COMPILER = "hidden-narration-visible-evidence-v1"
_VISIBLE_EVIDENCE_PROXY_REASON = "hidden_narration_compiled_to_visible_evidence"
_PROXY_MODE_SCHEMA_ALIASES = {"observable_basis_en", "observable_basis"}
_PROXY_MODE_ALIAS_COMPILER = "proxy-mode-schema-alias-v1"
_PROXY_MODE_ALIAS_REASON = "proxy_mode_schema_alias"
_MUST_HAVE_LOCATIVE_FLORA_REASON = "locative_flora_growth_replaced_with_visible_state"
_MUST_HAVE_LOCATIVE_FLORA_PROCESS = re.compile(
    r"(?P<prefix>\b(?:plants?|plant\s+life|vegetation|flora|grasses?|reeds?|"
    r"rushes|trees?|saplings?|shrubs?|bushes?|moss(?:es)?|algae|seaweed|"
    r"lil(?:y|ies)|ferns?|vines?|crops?|canop(?:y|ies))\b"
    r"(?:\s*\([^()\r\n]{1,120}\))?\s+)"
    r"(?P<process>grow(?:s|ing)?)"
    r"(?=\s+(?:on|in|at|across|along|among|within)\b)",
    re.IGNORECASE,
)


JUDGE_PROMPT = """You are the final temporal visual judge for a documentary montage.
Choose the candidate that best illustrates the narration without contradiction. Each image is
a 2x2 contact sheet ordered through time, so judge the whole action, not one frame.

Narration: {text}
Visual brief: {query}
Must have: {must_have}
Must not show: {must_not}

Return only JSON:
{{"chosen_index": 0, "verdict": "exact|relevant|generic|wrong|contradiction",
  "confidence": 0.0, "reason": "short explanation"}}
Use zero-based candidate index. Prefer an honest generic shot over a wrong specific event.
"""


JUDGE_ALL_PROMPT = """You are a fail-closed temporal visual judge for a documentary montage.
Prompt contract version: {contract_version}.
You receive exactly {candidate_count} candidate image(s). Each attached image is exactly one
candidate. Cells inside one image are chronological time samples from the same video interval,
not separate candidates. A contact sheet may use any grid layout, including 1x5, 2x2, 2x3, or
another shape; layout never changes the candidate count.

Evaluate every candidate contact sheet, not only the best midpoint. A candidate is
exact/relevant only when the whole used interval supports the narration and must-have
constraints without any must-not subject, title, logo or foreign tail.

Narration: {text}
Visual brief: {query}
Must have: {must_have}
Must not show: {must_not}

Return only strict JSON with zero-based integer indices:
{{"candidates":[{{"index":0,"verdict":"exact|relevant|generic|wrong|contradiction",
"confidence":0.0,"reason":"short evidence"}}],"chosen_index":0}}
Return exactly {candidate_count} candidates entries: one entry for each supplied image, in
index order. Never create entries for cells and never use one-based numbering. If none is
acceptable, chosen_index must be null.
"""


def _chunks(values: list[dict], size: int) -> Iterable[list[dict]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _string_list(value) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _query_from_observable_basis(observable_basis: str) -> str:
    return f"Live-action documentary shot of: {observable_basis}"


def _compile_locative_flora_must_have(
    must_have: Sequence[str],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Compile only flora + growth + location fragments into visible states."""

    normalized: list[str] = []
    changes: list[dict[str, Any]] = []
    for index, item in enumerate(must_have):
        matched_processes = [
            match.group("process")
            for match in _MUST_HAVE_LOCATIVE_FLORA_PROCESS.finditer(item)
        ]
        compiled = _MUST_HAVE_LOCATIVE_FLORA_PROCESS.sub(
            lambda match: f'{match.group("prefix")}present',
            item,
        )
        normalized.append(compiled)
        if compiled != item:
            changes.append(
                {
                    "index": index,
                    "original": item,
                    "normalized": compiled,
                    "matched_processes": matched_processes,
                    "replacement": "present",
                }
            )
    return normalized, changes


def _must_have_normalization_receipt(changes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": MUST_HAVE_LOCATIVE_FLORA_COMPILER_VERSION,
        "reason": _MUST_HAVE_LOCATIVE_FLORA_REASON,
        "changes": changes,
    }


def validate_visual_brief_contract(
    beat: dict,
    brief: dict,
    *,
    contract_version: str = VISUAL_BRIEF_CONTRACT_VERSION,
) -> dict:
    """Normalize one model brief and enforce the observable-proxy contract."""

    if contract_version != VISUAL_BRIEF_CONTRACT_VERSION:
        raise ValueError(f"unsupported visual brief contract: {contract_version}")
    if str(brief.get("contract_version") or "") != contract_version:
        raise ValueError("visual brief contract_version is missing or stale")
    slot_id = str(beat.get("slot_id") or "")
    if str(brief.get("slot_id") or "") != slot_id:
        raise ValueError(f"visual brief slot mismatch for {slot_id}")
    query = str(brief.get("query_en") or "").strip()
    if not query:
        raise ValueError(f"visual brief missing for {slot_id}")
    must_have = _string_list(brief.get("must_have"))
    if len(must_have) > VISUAL_BRIEF_MAX_MUST_HAVE:
        raise ValueError(
            f"visual brief {slot_id} has more than {VISUAL_BRIEF_MAX_MUST_HAVE} must_have items"
        )
    original_must_have: list[str] = []
    must_have_receipt: dict[str, Any] | None = None
    raw_original_must_have = brief.get("original_must_have")
    raw_must_have_receipt = brief.get("must_have_normalization_receipt")
    if raw_original_must_have is not None or raw_must_have_receipt is not None:
        if not isinstance(raw_original_must_have, list) or any(
            not isinstance(item, str) or not item.strip()
            for item in raw_original_must_have
        ):
            raise ValueError(
                f"visual brief {slot_id} has invalid original_must_have audit field"
            )
        if not isinstance(raw_must_have_receipt, Mapping):
            raise ValueError(
                f"visual brief {slot_id} has invalid must_have normalization receipt"
            )
        original_must_have = [item.strip() for item in raw_original_must_have]
        expected_must_have, expected_changes = _compile_locative_flora_must_have(
            original_must_have
        )
        expected_receipt = _must_have_normalization_receipt(expected_changes)
        if not expected_changes:
            raise ValueError(
                f"visual brief {slot_id} original_must_have has no normalizable process"
            )
        if must_have != expected_must_have:
            raise ValueError(
                f"visual brief {slot_id} normalized must_have does not match its audit source"
            )
        if dict(raw_must_have_receipt) != expected_receipt:
            raise ValueError(
                f"visual brief {slot_id} has invalid must_have normalization receipt"
            )
        must_have_receipt = expected_receipt
    else:
        compiled_must_have, changes = _compile_locative_flora_must_have(must_have)
        if changes:
            original_must_have = list(must_have)
            must_have = compiled_must_have
            must_have_receipt = _must_have_normalization_receipt(changes)
    must_not = _string_list(brief.get("must_not"))
    fallback = str(
        brief.get("generic_fallback_en")
        or "neutral live-action documentary environment shot"
    ).strip()
    raw_proxy_mode = str(brief.get("proxy_mode") or "").strip().casefold()
    schema_alias_original = ""
    if raw_proxy_mode in _PROXY_MODE_SCHEMA_ALIASES:
        schema_alias_original = raw_proxy_mode
        proxy_mode = "observable_proxy"
    elif raw_proxy_mode in {
        "literal_visible",
        "observable_proxy",
        "observable_consequence",
    }:
        proxy_mode = raw_proxy_mode
    else:
        raise ValueError(f"visual brief {slot_id} has invalid proxy_mode")
    observable_basis = str(brief.get("observable_basis_en") or "").strip()
    if not observable_basis:
        raise ValueError(f"visual brief {slot_id} has no observable_basis_en")
    requested_visuals = " ".join([query, *must_have, fallback, observable_basis])
    forbidden = _FORBIDDEN_EXPLANATORY_VISUAL.search(requested_visuals)
    if forbidden:
        raise ValueError(
            f"visual brief {slot_id} requests forbidden explanatory visual: "
            f"{forbidden.group(0)}"
        )
    narration = str(beat.get("text") or "")
    hidden_narration = _SLOW_OR_HIDDEN_CLAIM.search(narration)
    basis_nonobservable = _NONOBSERVABLE_SEARCH_PROCESS.search(observable_basis)
    if basis_nonobservable:
        raise ValueError(
            f"visual brief {slot_id} observable_basis_en is not camera-visible: "
            f"{basis_nonobservable.group(0)}"
        )
    must_have_nonobservable = _NONOBSERVABLE_SEARCH_PROCESS.search(
        " ".join(must_have)
    )
    if must_have_nonobservable:
        raise ValueError(
            f"visual brief {slot_id} leaves a non-observable process in must_have: "
            f"{must_have_nonobservable.group(0)}"
        )

    original_query = ""
    matched_process = ""
    raw_receipt = brief.get("normalization_receipt")
    query_nonobservable = _NONOBSERVABLE_SEARCH_PROCESS.search(query)
    if query_nonobservable:
        if schema_alias_original:
            raise ValueError(
                f"visual brief {slot_id} schema alias cannot hide a non-observable "
                f"query process: {query_nonobservable.group(0)}"
            )
        if proxy_mode not in {"observable_proxy", "observable_consequence"}:
            raise ValueError(
                f"visual brief {slot_id} leaves a non-observable process in its "
                f"search contract: {query_nonobservable.group(0)}"
            )
        original_query = query
        matched_process = query_nonobservable.group(0)
        query = _query_from_observable_basis(observable_basis)
    else:
        audit_original = str(brief.get("original_query_en") or "").strip()
        if audit_original:
            audit_match = _NONOBSERVABLE_SEARCH_PROCESS.search(audit_original)
            if not audit_match:
                raise ValueError(
                    f"visual brief {slot_id} has an invalid original_query_en audit field"
                )
            if _FORBIDDEN_EXPLANATORY_VISUAL.search(audit_original):
                raise ValueError(
                    f"visual brief {slot_id} original_query_en requests a forbidden visual"
                )
            if proxy_mode not in {"observable_proxy", "observable_consequence"}:
                raise ValueError(
                    f"visual brief {slot_id} has query normalization in literal_visible mode"
                )
            if query != _query_from_observable_basis(observable_basis):
                raise ValueError(
                    f"visual brief {slot_id} normalized query is not bound to observable_basis_en"
                )
            original_query = audit_original
            matched_process = audit_match.group(0)

    original_proxy_mode = ""
    matched_hidden_narration = ""
    audit_proxy_mode = str(brief.get("original_proxy_mode") or "").strip().casefold()
    if schema_alias_original:
        if audit_proxy_mode and audit_proxy_mode != schema_alias_original:
            raise ValueError(
                f"visual brief {slot_id} has conflicting proxy_mode alias audit"
            )
        if original_query:
            raise ValueError(
                f"visual brief {slot_id} schema alias cannot normalize a hidden query"
            )
        original_proxy_mode = schema_alias_original
        proxy_mode = "observable_proxy"
    elif hidden_narration and proxy_mode == "literal_visible":
        if original_query:
            raise ValueError(
                f"visual brief {slot_id} cannot normalize a hidden query as literal evidence"
            )
        original_proxy_mode = "literal_visible"
        matched_hidden_narration = hidden_narration.group(0)
        proxy_mode = "observable_consequence"
    elif audit_proxy_mode:
        if audit_proxy_mode in _PROXY_MODE_SCHEMA_ALIASES:
            if proxy_mode != "observable_proxy":
                raise ValueError(
                    f"visual brief {slot_id} normalized schema alias mode is invalid"
                )
            if original_query:
                raise ValueError(
                    f"visual brief {slot_id} has conflicting query and proxy normalizations"
                )
            original_proxy_mode = audit_proxy_mode
            schema_alias_original = audit_proxy_mode
        elif audit_proxy_mode != "literal_visible":
            raise ValueError(
                f"visual brief {slot_id} has invalid original_proxy_mode audit field"
            )
        elif not hidden_narration:
            raise ValueError(
                f"visual brief {slot_id} original_proxy_mode is not justified by narration"
            )
        elif proxy_mode not in {"observable_proxy", "observable_consequence"}:
            raise ValueError(
                f"visual brief {slot_id} normalized proxy_mode is invalid"
            )
        elif original_query:
            raise ValueError(
                f"visual brief {slot_id} has conflicting query and proxy normalizations"
            )
        else:
            original_proxy_mode = "literal_visible"
            matched_hidden_narration = hidden_narration.group(0)
            proxy_mode = "observable_consequence"
    elif raw_receipt is not None and not original_query:
        raise ValueError(
            f"visual brief {slot_id} normalization_receipt has no audit source field"
        )

    if original_query and original_proxy_mode:
        raise ValueError(
            f"visual brief {slot_id} has conflicting normalizations"
        )

    normalized = {
        "contract_version": contract_version,
        "slot_id": slot_id,
        "query_en": query,
        "must_have": must_have,
        "must_not": must_not,
        "generic_fallback_en": fallback,
        "proxy_mode": proxy_mode,
        "observable_basis_en": observable_basis,
    }
    if original_must_have:
        normalized["original_must_have"] = original_must_have
        normalized["must_have_normalization_receipt"] = must_have_receipt
    if original_query:
        normalized["original_query_en"] = original_query
        normalized["normalization_receipt"] = {
            "compiler": _OBSERVABLE_QUERY_COMPILER,
            "reason": _OBSERVABLE_QUERY_NORMALIZATION_REASON,
            "matched_nonobservable_process": matched_process,
            "source_field": "observable_basis_en",
        }
    elif original_proxy_mode:
        normalized["original_proxy_mode"] = original_proxy_mode
        if schema_alias_original:
            normalized["normalization_receipt"] = {
                "compiler": _PROXY_MODE_ALIAS_COMPILER,
                "reason": _PROXY_MODE_ALIAS_REASON,
                "canonical_proxy_mode": "observable_proxy",
                "source_field": "proxy_mode",
            }
        else:
            normalized["normalization_receipt"] = {
                "compiler": _VISIBLE_EVIDENCE_PROXY_COMPILER,
                "reason": _VISIBLE_EVIDENCE_PROXY_REASON,
                "matched_hidden_narration": matched_hidden_narration,
                "source_fields": ["query_en", "must_have", "observable_basis_en"],
            }
    return normalized


def request_visual_briefs_raw(
    beats: list[dict],
    *,
    model: str = "gpt-5-nano",
    batch_size: int = 20,
    chat_fn: Callable = _chat,
    contract_version: str = VISUAL_BRIEF_CONTRACT_VERSION,
) -> dict:
    """Request briefs without parsing or validating paid provider responses.

    Production persists this raw envelope before any local contract check.  A
    malformed but successfully returned provider response can therefore be
    inspected and reused after restart without risking a duplicate paid call.
    """

    if contract_version != VISUAL_BRIEF_CONTRACT_VERSION:
        raise ValueError(f"unsupported visual brief contract: {contract_version}")
    raw_batches: list[dict] = []
    for batch in _chunks(beats, max(1, batch_size)):
        compact = [
            {"slot_id": row["slot_id"], "text": row.get("text", "")}
            for row in batch
        ]
        answer = chat_fn(
            model,
            [{"role": "user", "content": BRIEF_PROMPT.format(
                contract_version=contract_version,
                slots=json.dumps(compact, ensure_ascii=False))}],
            reasoning_effort="minimal",
            max_tokens=max(1800, 220 * len(batch)),
        )
        raw_batches.append(
            {
                "slot_ids": [str(row["slot_id"]) for row in batch],
                "content": str(answer),
            }
        )
    return {
        "contract_version": contract_version,
        "batches": raw_batches,
    }


def validate_visual_briefs_response(
    beats: list[dict],
    raw_response: dict,
    *,
    contract_version: str = VISUAL_BRIEF_CONTRACT_VERSION,
) -> list[dict]:
    """Parse a persisted raw response and enforce the visual-brief contract."""

    if contract_version != VISUAL_BRIEF_CONTRACT_VERSION:
        raise ValueError(f"unsupported visual brief contract: {contract_version}")
    if not isinstance(raw_response, dict):
        raise ValueError("raw visual brief response must be an object")
    if str(raw_response.get("contract_version") or "") != contract_version:
        raise ValueError("raw visual brief response contract_version is missing or stale")
    raw_batches = raw_response.get("batches")
    if not isinstance(raw_batches, list):
        raise ValueError("raw visual brief response batches must be an array")

    expected_ids = [str(beat.get("slot_id") or "") for beat in beats]
    cursor = 0
    briefs: list[dict] = []
    for raw_batch in raw_batches:
        if not isinstance(raw_batch, dict):
            raise ValueError("raw visual brief batch must be an object")
        slot_ids = raw_batch.get("slot_ids")
        if not isinstance(slot_ids, list) or not slot_ids:
            raise ValueError("raw visual brief batch slot_ids must be a non-empty array")
        slot_ids = [str(slot_id) for slot_id in slot_ids]
        expected_batch_ids = expected_ids[cursor:cursor + len(slot_ids)]
        if slot_ids != expected_batch_ids:
            raise ValueError("raw visual brief batch is bound to different slots")
        content = raw_batch.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("raw visual brief batch has no provider content")
        parsed = extract_json(content)
        if not isinstance(parsed, list):
            raise ValueError("visual brief response must be a JSON array")
        if any(not isinstance(row, dict) for row in parsed):
            raise ValueError("visual brief response contains a non-object row")
        parsed_ids = [str(row.get("slot_id") or "") for row in parsed]
        if len(set(parsed_ids)) != len(parsed_ids) or set(parsed_ids) != set(slot_ids):
            raise ValueError("visual brief response slot_ids are missing, extra, or duplicate")
        by_id = {str(row.get("slot_id")): row for row in parsed}
        for beat in beats[cursor:cursor + len(slot_ids)]:
            slot_id = str(beat["slot_id"])
            briefs.append(
                validate_visual_brief_contract(
                    dict(beat), dict(by_id[slot_id]), contract_version=contract_version
                )
            )
        cursor += len(slot_ids)
    if cursor != len(beats):
        raise ValueError("raw visual brief response does not cover every slot")
    return briefs


def build_visual_briefs(
    beats: list[dict],
    *,
    model: str = "gpt-5-nano",
    batch_size: int = 20,
    chat_fn: Callable = _chat,
    contract_version: str = VISUAL_BRIEF_CONTRACT_VERSION,
) -> list[dict]:
    """Compatibility wrapper for callers that do not manage paid state."""

    raw_response = request_visual_briefs_raw(
        beats,
        model=model,
        batch_size=batch_size,
        chat_fn=chat_fn,
        contract_version=contract_version,
    )
    return validate_visual_briefs_response(
        beats,
        raw_response,
        contract_version=contract_version,
    )


def _image_part(path: str) -> dict:
    with open(path, "rb") as source:
        encoded = base64.standard_b64encode(source.read()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}


def judge_one(
    beat: dict,
    brief: dict,
    candidates: list[dict],
    *,
    model: str = "gpt-5-nano",
    chat_fn: Callable = _chat,
) -> dict:
    if not candidates:
        return {"chosen_index": None, "verdict": "wrong", "confidence": 0.0,
                "reason": "no candidates", "status": "unresolved"}
    prompt = JUDGE_PROMPT.format(
        text=beat.get("text", ""),
        query=brief.get("query_en", ""),
        must_have=", ".join(brief.get("must_have") or []) or "none",
        must_not=", ".join(brief.get("must_not") or []) or "none",
    )
    content: list[dict] = [{"type": "text", "text": prompt}]
    for index, candidate in enumerate(candidates):
        content.append({"type": "text", "text": f"CANDIDATE {index}"})
        content.append(_image_part(str(candidate["contact_sheet"])))
    answer = chat_fn(
        model,
        [{"role": "user", "content": content}],
        reasoning_effort="minimal",
        max_tokens=500,
    )
    row = extract_json(answer)
    if not isinstance(row, dict):
        raise ValueError("temporal judge response must be a JSON object")
    try:
        chosen = int(row.get("chosen_index"))
    except (TypeError, ValueError):
        chosen = -1
    if not 0 <= chosen < len(candidates):
        raise ValueError(f"temporal judge index out of range: {chosen}")
    verdict = str(row.get("verdict") or "wrong").lower()
    if verdict not in {"exact", "relevant", "generic", "wrong", "contradiction"}:
        verdict = "wrong"
    return {
        "chosen_index": chosen,
        "verdict": verdict,
        "confidence": max(0.0, min(1.0, float(row.get("confidence") or 0.0))),
        "reason": str(row.get("reason") or "")[:500],
        "status": "resolved" if verdict in {"exact", "relevant"} else "unresolved",
    }


def judge_many(
    jobs: list[tuple[dict, dict, list[dict]]],
    *,
    model: str = "gpt-5-nano",
    workers: int = 6,
    chat_fn: Callable = _chat,
) -> list[dict]:
    results: list[dict | None] = [None] * len(jobs)

    def run(index: int, job):
        beat, brief, candidates = job
        try:
            value = judge_one(beat, brief, candidates, model=model, chat_fn=chat_fn)
            value["judge_error"] = None
        except Exception as error:
            value = {
                "chosen_index": None,
                "verdict": "wrong",
                "confidence": 0.0,
                "reason": "temporal judge failed; no candidate selected",
                "status": "unresolved",
                "judge_error": f"{type(error).__name__}: {error}"[:500],
            }
        return index, value

    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(run, index, job) for index, job in enumerate(jobs)]
        for future in cf.as_completed(futures):
            index, value = future.result()
            results[index] = value
    return [row for row in results if row is not None]


_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _candidate_image(candidate: Mapping[str, Any]) -> tuple[bytes, str, int]:
    raw_path = candidate.get("contact_sheet")
    if not isinstance(raw_path, (str, Path)) or not str(raw_path):
        raise ValueError("strict judge candidate has no contact sheet")
    data = Path(raw_path).read_bytes()
    if not data:
        raise ValueError("strict judge contact sheet is empty")
    return data, hashlib.sha256(data).hexdigest(), len(data)


def _image_bytes_part(data: bytes) -> dict:
    encoded = base64.standard_b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}


def _strict_judge_provider_options(candidate_count: int) -> dict[str, Any]:
    return {
        "reasoning_effort": "minimal",
        # The completion budget also includes hidden reasoning for GPT-5
        # models. Six temporal candidates routinely need 1.3--1.7k visible
        # JSON characters; a smaller budget truncated otherwise valid arrays.
        "max_tokens": max(1200, 320 * candidate_count),
        "response_format": {"type": "json_object"},
    }


def _base_strict_judge_prompt(request: Mapping[str, Any]) -> str:
    brief = request["visual_brief"]
    return JUDGE_ALL_PROMPT.format(
        contract_version=request["contract_version"],
        candidate_count=request["candidate_count"],
        text=request["narration"],
        query=brief["query_en"],
        must_have=", ".join(brief["must_have"]) or "none",
        must_not=", ".join(brief["must_not"]) or "none",
    )


def _strict_judge_prompt(request: Mapping[str, Any]) -> str:
    prompt = _base_strict_judge_prompt(request)
    repair = request.get("schema_repair")
    if not isinstance(repair, Mapping):
        return prompt
    previous = json.dumps(
        repair["previous_raw_answer"], ensure_ascii=False, separators=(",", ":")
    )
    return (
        f"{prompt}\n\n"
        f"SCHEMA REPAIR {repair['attempt']} OF {repair['max_attempts']}.\n"
        "The previous answer below failed the strict output schema. Re-evaluate every attached "
        "candidate image and return a complete replacement object. Do not copy a missing row, "
        "do not treat contact-sheet cells as candidates, and do not comment outside JSON.\n"
        f"PREVIOUS_RAW_ANSWER_AS_JSON_STRING:\n{previous}\n"
    )


def _validate_strict_judge_request(
    request: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    if not isinstance(request, Mapping):
        raise ValueError("strict judge request must be an object")
    if str(request.get("contract_version") or "") != (
        STRICT_JUDGE_PROMPT_CONTRACT_VERSION
    ):
        raise ValueError("strict judge request contract_version is missing or stale")
    if not isinstance(request.get("model"), str) or not str(request["model"]).strip():
        raise ValueError("strict judge request model is missing")
    if not isinstance(request.get("narration"), str):
        raise ValueError("strict judge request narration must be a string")
    brief = request.get("visual_brief")
    if not isinstance(brief, Mapping) or not isinstance(brief.get("query_en"), str):
        raise ValueError("strict judge request visual brief is invalid")
    for field in ("must_have", "must_not"):
        values = brief.get(field)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError(f"strict judge request {field} must be a string array")
    candidate_count = request.get("candidate_count")
    if type(candidate_count) is not int or candidate_count < 1:
        raise ValueError("strict judge request candidate_count must be positive")
    bindings = request.get("candidate_bindings")
    if not isinstance(bindings, list) or len(bindings) != candidate_count:
        raise ValueError("strict judge request candidate binding count mismatch")
    for expected_index, binding in enumerate(bindings):
        if not isinstance(binding, Mapping) or binding.get("index") != expected_index:
            raise ValueError("strict judge request indices must be exact zero-based order")
        image_sha256 = binding.get("image_sha256")
        if not isinstance(image_sha256, str) or not _SHA256_HEX.fullmatch(image_sha256):
            raise ValueError("strict judge request image hash is invalid")
        if type(binding.get("image_bytes")) is not int or binding["image_bytes"] < 1:
            raise ValueError("strict judge request image byte count is invalid")
        retrieval_score = binding.get("retrieval_score")
        if isinstance(retrieval_score, bool) or not isinstance(
            retrieval_score, (int, float)
        ):
            raise ValueError("strict judge request retrieval score is invalid")
        if not math.isfinite(float(retrieval_score)):
            raise ValueError("strict judge request retrieval score is invalid")
    if request.get("provider_options") != _strict_judge_provider_options(candidate_count):
        raise ValueError("strict judge request provider options are stale")
    repair = request.get("schema_repair")
    if repair is not None:
        if not isinstance(repair, Mapping):
            raise ValueError("strict judge schema repair binding must be an object")
        if str(repair.get("contract_version") or "") != (
            STRICT_JUDGE_SCHEMA_REPAIR_CONTRACT_VERSION
        ):
            raise ValueError("strict judge schema repair contract is stale")
        attempt = repair.get("attempt")
        max_attempts = repair.get("max_attempts")
        if (
            type(attempt) is not int
            or type(max_attempts) is not int
            or not 1 <= attempt <= max_attempts <= STRICT_JUDGE_SCHEMA_REPAIR_MAX_ATTEMPTS
        ):
            raise ValueError("strict judge schema repair attempt is out of bounds")
        for field in ("parent_request_sha256", "parent_raw_envelope_sha256"):
            value = repair.get(field)
            if not isinstance(value, str) or not _SHA256_HEX.fullmatch(value):
                raise ValueError(f"strict judge schema repair {field} is invalid")
        previous_raw_answer = repair.get("previous_raw_answer")
        if not isinstance(previous_raw_answer, str):
            raise ValueError("strict judge schema repair raw answer must be a string")
        if repair.get("previous_raw_answer_sha256") != hashlib.sha256(
            previous_raw_answer.encode("utf-8")
        ).hexdigest():
            raise ValueError("strict judge schema repair raw answer hash is invalid")
    prompt = request.get("prompt")
    if not isinstance(prompt, str) or prompt != _strict_judge_prompt(request):
        raise ValueError("strict judge request prompt is missing or stale")
    return bindings


def build_strict_judge_request(
    beat: Mapping[str, Any],
    brief: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    *,
    model: str = "gpt-5-nano",
    contract_version: str = STRICT_JUDGE_PROMPT_CONTRACT_VERSION,
) -> dict[str, Any]:
    """Build a content-bound durable request without filesystem paths.

    Candidate images are represented by their bytes and SHA-256 bindings. Runtime
    paths are accepted only to read those images and never enter the envelope.
    """

    if contract_version != STRICT_JUDGE_PROMPT_CONTRACT_VERSION:
        raise ValueError(f"unsupported strict judge contract: {contract_version}")
    if not candidates:
        raise ValueError("strict judge requires at least one candidate")
    model = str(model).strip()
    if not model:
        raise ValueError("strict judge model is missing")
    bindings: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        _data, image_sha256, image_bytes = _candidate_image(candidate)
        try:
            retrieval_score = float(candidate.get("retrieval_score") or 0.0)
        except (TypeError, ValueError) as exc:
            raise ValueError("strict judge candidate retrieval score is invalid") from exc
        if not math.isfinite(retrieval_score):
            raise ValueError("strict judge candidate retrieval score is invalid")
        bindings.append(
            {
                "index": index,
                "image_sha256": image_sha256,
                "image_bytes": image_bytes,
                "retrieval_score": retrieval_score,
            }
        )
    request: dict[str, Any] = {
        "contract_version": contract_version,
        "model": model,
        "narration": str(beat.get("text") or ""),
        "visual_brief": {
            "query_en": str(brief.get("query_en") or ""),
            "must_have": _string_list(brief.get("must_have")),
            "must_not": _string_list(brief.get("must_not")),
        },
        "candidate_count": len(candidates),
        "candidate_bindings": bindings,
        "provider_options": _strict_judge_provider_options(len(candidates)),
    }
    request["prompt"] = _strict_judge_prompt(request)
    _validate_strict_judge_request(request)
    return request


def request_strict_judge_raw(
    request: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    *,
    chat_fn: Callable = _chat,
) -> dict[str, Any]:
    """Make exactly one provider call and return its answer without parsing it."""

    bindings = _validate_strict_judge_request(request)
    if len(candidates) != len(bindings):
        raise ValueError("strict judge runtime candidate count mismatch")
    content: list[dict[str, Any]] = [{"type": "text", "text": request["prompt"]}]
    for candidate, binding in zip(candidates, bindings):
        data, image_sha256, image_bytes = _candidate_image(candidate)
        if (
            image_sha256 != binding["image_sha256"]
            or image_bytes != binding["image_bytes"]
        ):
            raise ValueError("strict judge runtime image changed after request binding")
        content.append(
            {
                "type": "text",
                "text": (
                    f"CANDIDATE INDEX {binding['index']} OF {len(bindings)}: the next one "
                    "image is exactly one candidate; all cells are time samples of that "
                    "same interval."
                ),
            }
        )
        content.append(_image_bytes_part(data))
    answer = chat_fn(
        request["model"],
        [{"role": "user", "content": content}],
        **dict(request["provider_options"]),
    )
    return {
        "contract_version": request["contract_version"],
        "request_sha256": _canonical_sha256(request),
        "candidate_count": len(bindings),
        # Deliberately no extract_json/validation here. The caller can persist
        # this envelope atomically before any local deterministic parsing.
        "raw_answer": str(answer),
    }


def _strict_judge_raw_answer(
    request: Mapping[str, Any], raw_response: Mapping[str, Any]
) -> str:
    bindings = _validate_strict_judge_request(request)
    if not isinstance(raw_response, Mapping):
        raise ValueError("strict judge raw response must be an object")
    if raw_response.get("contract_version") != request["contract_version"]:
        raise ValueError("strict judge raw response contract is stale")
    if raw_response.get("request_sha256") != _canonical_sha256(request):
        raise ValueError("strict judge raw response is bound to another request")
    if raw_response.get("candidate_count") != len(bindings):
        raise ValueError("strict judge raw response candidate binding is invalid")
    answer = raw_response.get("raw_answer")
    if not isinstance(answer, str):
        raise ValueError("strict judge raw response has no string answer")
    return answer


def normalize_strict_judge_response(
    request: Mapping[str, Any],
    raw_response: Mapping[str, Any],
) -> dict[str, Any]:
    """Purely normalize one persisted raw answer with an exact fail-closed schema."""

    bindings = _validate_strict_judge_request(request)
    payload = extract_json(_strict_judge_raw_answer(request, raw_response))
    if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list):
        raise ValueError("strict judge response must contain candidates array")
    rows = payload["candidates"]
    if len(rows) != len(bindings):
        raise ValueError("strict judge candidate count mismatch")
    evaluations: list[dict[str, Any]] = []
    allowed_verdicts = {"exact", "relevant", "generic", "wrong", "contradiction"}
    for expected_index, row in enumerate(rows):
        if not isinstance(row, dict) or type(row.get("index")) is not int:
            raise ValueError("strict judge index must be an integer")
        if row["index"] != expected_index:
            raise ValueError("strict judge indices must be exact zero-based order")
        raw_verdict = row.get("verdict")
        if not isinstance(raw_verdict, str) or raw_verdict.lower() not in allowed_verdicts:
            raise ValueError(f"strict judge invalid verdict at {expected_index}")
        raw_confidence = row.get("confidence")
        if isinstance(raw_confidence, bool) or not isinstance(
            raw_confidence, (int, float)
        ):
            raise ValueError(f"strict judge invalid confidence at {expected_index}")
        confidence = float(raw_confidence)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError(f"strict judge invalid confidence at {expected_index}")
        reason = row.get("reason")
        if not isinstance(reason, str):
            raise ValueError(f"strict judge invalid reason at {expected_index}")
        evaluations.append(
            {
                "index": expected_index,
                "verdict": raw_verdict.lower(),
                "confidence": confidence,
                "reason": reason[:500],
            }
        )
    chosen_raw = payload.get("chosen_index")
    if chosen_raw is None:
        model_chosen = None
    elif type(chosen_raw) is int and 0 <= chosen_raw < len(bindings):
        model_chosen = chosen_raw
    else:
        raise ValueError("strict judge chosen_index is invalid")
    # Per-candidate verdicts are the semantic evidence. Resolve them locally
    # with a stable policy, independent of an inconsistent model choice.
    eligible = [
        index
        for index, row in enumerate(evaluations)
        if row["verdict"] in {"exact", "relevant"}
    ]
    verdict_priority = {"exact": 2, "relevant": 1}
    chosen = max(
        eligible,
        key=lambda index: (
            verdict_priority[evaluations[index]["verdict"]],
            evaluations[index]["confidence"],
            float(bindings[index]["retrieval_score"]),
            -index,
        ),
        default=None,
    )
    return {
        "chosen_index": chosen,
        "status": "resolved" if chosen is not None else "unresolved",
        "evaluations": evaluations,
        "reason": (
            evaluations[chosen]["reason"]
            if chosen is not None
            else "no acceptable candidate"
        ),
        "model_chosen_index": model_chosen,
        "choice_correction": (
            "deterministic_from_evaluations" if model_chosen != chosen else None
        ),
    }


def build_strict_judge_schema_repair_request(
    request: Mapping[str, Any],
    raw_response: Mapping[str, Any],
    *,
    attempt: int,
    max_attempts: int = STRICT_JUDGE_SCHEMA_REPAIR_MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Purely build one bounded, path-free repair request for malformed schema."""

    _validate_strict_judge_request(request)
    previous_raw_answer = _strict_judge_raw_answer(request, raw_response)
    if (
        type(attempt) is not int
        or type(max_attempts) is not int
        or not 1 <= attempt <= max_attempts <= STRICT_JUDGE_SCHEMA_REPAIR_MAX_ATTEMPTS
    ):
        raise ValueError("strict judge schema repair attempt is out of bounds")
    parent_repair = request.get("schema_repair")
    previous_attempt = (
        int(parent_repair["attempt"]) if isinstance(parent_repair, Mapping) else 0
    )
    if attempt != previous_attempt + 1:
        raise ValueError("strict judge schema repair attempts must be consecutive")
    if isinstance(parent_repair, Mapping) and max_attempts != parent_repair["max_attempts"]:
        raise ValueError("strict judge schema repair max_attempts changed mid-chain")
    repair_request: dict[str, Any] = {
        "contract_version": request["contract_version"],
        "model": request["model"],
        "narration": request["narration"],
        "visual_brief": {
            "query_en": request["visual_brief"]["query_en"],
            "must_have": list(request["visual_brief"]["must_have"]),
            "must_not": list(request["visual_brief"]["must_not"]),
        },
        "candidate_count": request["candidate_count"],
        "candidate_bindings": [dict(binding) for binding in request["candidate_bindings"]],
        "provider_options": {
            **request["provider_options"],
            "response_format": dict(request["provider_options"]["response_format"]),
        },
        "schema_repair": {
            "contract_version": STRICT_JUDGE_SCHEMA_REPAIR_CONTRACT_VERSION,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "parent_request_sha256": _canonical_sha256(request),
            "parent_raw_envelope_sha256": _canonical_sha256(raw_response),
            "previous_raw_answer": previous_raw_answer,
            "previous_raw_answer_sha256": hashlib.sha256(
                previous_raw_answer.encode("utf-8")
            ).hexdigest(),
        },
    }
    repair_request["prompt"] = _strict_judge_prompt(repair_request)
    _validate_strict_judge_request(repair_request)
    return repair_request


def normalize_strict_judge_schema_repair_response(
    repair_request: Mapping[str, Any],
    raw_response: Mapping[str, Any],
) -> dict[str, Any]:
    """Purely normalize a raw response bound to a bounded repair request."""

    _validate_strict_judge_request(repair_request)
    if not isinstance(repair_request.get("schema_repair"), Mapping):
        raise ValueError("strict judge response is not bound to a schema repair")
    return normalize_strict_judge_response(repair_request, raw_response)


def judge_candidates_strict(
    beat: dict,
    brief: dict,
    candidates: list[dict],
    *,
    model: str = "gpt-5-nano",
    chat_fn: Callable = _chat,
) -> dict:
    """Compatibility wrapper for callers that do not persist paid raw answers."""

    if not candidates:
        return {
            "chosen_index": None,
            "status": "unresolved",
            "evaluations": [],
            "reason": "no candidates",
        }
    request = build_strict_judge_request(
        beat, brief, candidates, model=model
    )
    raw_response = request_strict_judge_raw(
        request, candidates, chat_fn=chat_fn
    )
    return normalize_strict_judge_response(request, raw_response)


def judge_many_strict(
    jobs: list[tuple[dict, dict, list[dict]]],
    *,
    model: str = "gpt-5-nano",
    workers: int = 6,
    chat_fn: Callable = _chat,
) -> list[dict]:
    """Parallel strict judging; provider failure never resolves a candidate."""

    results: list[dict | None] = [None] * len(jobs)

    def run(index: int, job):
        beat, brief, candidates = job
        try:
            value = judge_candidates_strict(
                beat, brief, candidates, model=model, chat_fn=chat_fn
            )
            value["judge_error"] = None
        except Exception as error:
            value = {
                "chosen_index": None,
                "status": "unresolved",
                "evaluations": [],
                "reason": "strict judge failed",
                "judge_error": f"{type(error).__name__}: {error}"[:500],
            }
        return index, value

    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(run, index, job) for index, job in enumerate(jobs)]
        for future in cf.as_completed(futures):
            index, value = future.result()
            results[index] = value
    return [row for row in results if row is not None]
