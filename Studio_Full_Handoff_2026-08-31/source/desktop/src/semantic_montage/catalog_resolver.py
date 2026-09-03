"""Fail-closed resolution of a production-v2 sound plan from a local catalog.

The catalog is durable and content-addressed; absolute paths are runtime input
only.  Natural sounds are selected by :class:`AudioIndex` and
:class:`SoundRetriever`.  This module adds the two boundaries those lower
level APIs intentionally do not own:

* adapt the portable catalog to the runtime index and verify file/hash/license;
* strip runtime paths again before returning a durable sound plan.

An ordinary catalog miss is data, not an exception: the returned plan has
``status=needs_sound`` and machine-readable diagnostics.  Invalid programmer
input (for example a non-v2 plan) still raises ``CatalogResolutionError``.
"""

from __future__ import annotations

import copy
import math
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .audio_index import AudioIndex, AudioIndexError
from .common import sha256_file
from .sound import (
    FOREGROUND_SOUND_DISPOSITION_SCHEMA_VERSION,
    SOUND_PLAN_SCHEMA_VERSION,
    SoundPlanError,
    foreground_sound_disposition_cache_key,
    music_generation_cache_key,
    natural_sound_search_cache_key,
    refresh_sound_plan_cache_ids,
    stable_cache_key,
    validate_asset_provenance,
)
from .sound_retrieval import ClapAdapter, RetrievalPolicy, SoundRetriever


CATALOG_RESOLVER_SCHEMA_VERSION = 1
CATALOG_RESOLVER_VERSION = "catalog-resolver-v1.4"
ENVIRONMENT_FAMILY_MATCH_VERSION = "nature-environment-family-v1.4"
AMBIENCE_SOURCE_OFFSET_VERSION = "ambience-absolute-continuity-v1"
CLASSIFIER_SOURCE_KIND_AGGREGATE_VERSION = "classifier-source-kind-aggregate-v1"
CLASSIFIER_SOURCE_KIND_MIN_MARGIN = 0.05
FOREGROUND_ENVIRONMENT_ACTION_CONTRACT = (
    "foreground-environment-physical-action-v1"
)
FOREGROUND_ENVIRONMENT_ACTIONS = frozenset(
    {"water_light_pour_dribble", "water_push_splash"}
)
FOREGROUND_PHYSICAL_ACTIONS = frozenset(
    {*FOREGROUND_ENVIRONMENT_ACTIONS, "water_wading_splash"}
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PROJECT_ASSET_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_TOKEN_RE = re.compile(r"[a-zа-яё0-9]+")
_CANDIDATE_AUDIT_LIMIT = 10
_VOCAL_ACTION_TOKENS = frozenset(
    {
        "bark",
        "barking",
        "call",
        "calling",
        "chirp",
        "chirping",
        "cry",
        "growl",
        "growling",
        "grunt",
        "grunting",
        "hiss",
        "hissing",
        "howl",
        "howling",
        "roar",
        "roaring",
        "scream",
        "screaming",
        "singing",
        "snort",
        "snorting",
        "song",
        "trumpet",
        "trumpeting",
        "vocalization",
        "vocalisation",
        "vocalizing",
        "vocalising",
        "yelp",
        "yelping",
    }
)


_ENVIRONMENT_FAMILY_PRECEDENCE = (
    "underwater",
    "waterfall",
    "wetland",
    "rain",
    "desert",
    "savanna",
    "jungle",
)


def _environment_family(
    value: Any, *, excluded: Sequence[str] = ()
) -> str | None:
    """Map a free-form natural location to one licensed ambience family.

    The order is intentional: compound planner descriptions such as
    ``waterfall/river gorge`` or ``floodplain/grassland`` retain their most
    acoustically specific visible feature.  Unknown text remains unresolved;
    this helper is never applied to animal/foley taxonomy.
    """

    folded = _canonical(value)
    if not folded:
        return None
    words = _tokens(folded)
    flattened = re.sub(r"[^a-zа-яё0-9]+", " ", folded).strip()

    matches = {
        "underwater": bool(
            {"underwater", "subaquatic", "submerged"} & words
            or re.search(r"\bunder water\b", flattened) is not None
        ),
        "waterfall": bool(
            {"waterfall", "waterfalls", "cascade", "cascades"} & words
            or "falling water" in flattened
            or "falling stream" in flattened
        ),
        "wetland": bool(
            any(
                token.startswith(
                    (
                        "wetland",
                        "floodplain",
                        "river",
                        "lake",
                        "waterhole",
                        "marsh",
                        "pond",
                        "stream",
                        "delta",
                        "mudflat",
                        "shore",
                    )
                )
                for token in words
            )
            or re.search(r"\bshallow water\b", flattened) is not None
            or re.search(r"\bwater shallow\b", flattened) is not None
            or "flooded plain" in flattened
            or "reed lined channel" in flattened
            or "vegetated channel" in flattened
            or "standing water" in flattened
            or "near water" in flattened
            or "with water" in flattened
        ),
        "rain": bool({"rain", "rainfall", "raining"} & words),
        "desert": bool(
            {"desert", "deserts", "dune", "dunes"} & words
        ),
        "savanna": bool(
            {"savanna", "savannah", "savanah", "grassland", "scrub"} & words
            or any(token.startswith(("shrub", "grass")) for token in words)
            or "open plain" in flattened
            or "dry plain" in flattened
            or "sandy plain" in flattened
            or "woodland edge" in flattened
            or "wooded grassland" in flattened
            or "light woodland" in flattened
            or "acacia" in words
        ),
        "jungle": bool(
            {"jungle", "forest", "forested", "canopy"} & words
        ),
    }
    blocked = {_canonical(value) for value in excluded}
    return next(
        (
            family
            for family in _ENVIRONMENT_FAMILY_PRECEDENCE
            if matches[family] and family not in blocked
        ),
        None,
    )


class CatalogResolutionError(SoundPlanError):
    """The resolver was called with an invalid contract rather than a miss."""


def _tokens(value: Any) -> set[str]:
    return set(_TOKEN_RE.findall(str(value or "").casefold()))


def _strings(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _canonical(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _asset_keys(raw: Mapping[str, Any]) -> list[str]:
    provenance = raw.get("provenance")
    if not isinstance(provenance, Mapping):
        provenance = {}
    return [
        str(raw.get("asset_id") or "").strip(),
        str(raw.get("canonical_id") or "").strip(),
        str(raw.get("sha256") or "").strip(),
        str(provenance.get("provider_asset_id") or "").strip(),
        str(provenance.get("asset_id") or "").strip(),
    ]


def _override_for(
    raw: Mapping[str, Any], asset_mapping: Mapping[str, Any]
) -> dict[str, Any]:
    for key in _asset_keys(raw):
        if key and key in asset_mapping:
            value = asset_mapping[key]
            if isinstance(value, Mapping):
                return copy.deepcopy(dict(value))
            if isinstance(value, (str, Path)):
                return {"path": str(value)}
            return {}
    return {}


def _catalog_rows(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = catalog.get("assets")
    if isinstance(raw, Mapping):
        output = []
        for key, value in raw.items():
            if not isinstance(value, Mapping):
                continue
            row = copy.deepcopy(dict(value))
            row.setdefault("asset_id", str(key))
            output.append(row)
        return output
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return [copy.deepcopy(dict(item)) for item in raw if isinstance(item, Mapping)]
    return []


def _project_asset_id(raw: Mapping[str, Any], override: Mapping[str, Any]) -> str:
    provenance = raw.get("provenance")
    if not isinstance(provenance, Mapping):
        provenance = {}
    value = str(
        override.get("asset_id")
        or raw.get("asset_id")
        or provenance.get("provider_asset_id")
        or provenance.get("asset_id")
        or ""
    ).strip()
    if not value:
        digest = str(raw.get("sha256") or "").strip().casefold()
        value = f"audio-{digest[:24]}" if digest else ""
    if not _PROJECT_ASSET_ID_RE.fullmatch(value):
        raise ValueError("invalid_project_asset_id")
    return value


def _digest(raw: Mapping[str, Any], override: Mapping[str, Any]) -> str:
    value = str(override.get("sha256") or raw.get("sha256") or "").strip().casefold()
    if not value:
        canonical_id = str(raw.get("canonical_id") or "").strip().casefold()
        if canonical_id.startswith("sha256:"):
            value = canonical_id.split(":", 1)[1]
    if not _SHA256_RE.fullmatch(value):
        raise ValueError("invalid_sha256")
    return value


def _runtime_path(
    raw: Mapping[str, Any],
    override: Mapping[str, Any],
    *,
    catalog_root: Path | None,
) -> Path:
    supplied = override.get("path") or raw.get("path")
    if supplied:
        path = Path(str(supplied)).expanduser()
        if not path.is_absolute() and catalog_root is not None:
            path = catalog_root / path
        return path.resolve()

    relative = override.get("relative_path") or raw.get("canonical_path")
    if not relative or catalog_root is None:
        raise ValueError("missing_runtime_path")
    root = catalog_root.expanduser().resolve()
    path = (root / str(relative)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("catalog_path_escape") from exc
    return path


def _source_provenance(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    value = raw.get("provenance")
    return value if isinstance(value, Mapping) else {}


def _license(raw: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    source = _source_provenance(raw)
    value = override.get("license") or raw.get("license") or source.get("license")
    if not isinstance(value, Mapping):
        raise ValueError("missing_license")
    result = copy.deepcopy(dict(value))
    if result.get("commercial_use_allowed") is not True:
        raise ValueError("license_not_commercial")
    if not str(result.get("license_type") or "").strip() or not str(
        result.get("license_ref") or ""
    ).strip():
        raise ValueError("invalid_license_receipt")
    return result


def _duration(raw: Mapping[str, Any], override: Mapping[str, Any]) -> float:
    media = raw.get("media")
    if not isinstance(media, Mapping):
        media = {}
    value = override.get("duration", raw.get("duration", media.get("duration_seconds")))
    if isinstance(value, bool):
        raise ValueError("invalid_duration")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_duration") from exc
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError("invalid_duration")
    return number


def _roles(raw: Mapping[str, Any], override: Mapping[str, Any]) -> set[str]:
    roles = {item.casefold() for item in _strings(override.get("roles") or raw.get("roles"))}
    kind = _canonical(override.get("kind") or raw.get("kind"))
    if kind == "music":
        roles.add("music")
    elif kind == "ambience":
        roles.add("ambience")
    elif kind == "sfx":
        roles.update(("presence", "event"))
    taxonomy = override.get("taxonomy") or raw.get("taxonomy")
    if isinstance(taxonomy, Mapping):
        source_kind = _canonical(taxonomy.get("source_kind"))
        if source_kind == "environment":
            # A classifier-consistent close water recording can be used as an
            # exact foreground physical action without pretending it is a
            # biome ambience.  The allowlist and contract are checked again
            # during catalog admission; ordinary environment rows retain the
            # existing ambience-only family rules.
            if not _is_foreground_environment_action(taxonomy):
                roles.add("ambience")
        elif source_kind and source_kind != "music":
            roles.update(("presence", "event"))
    return roles


def _is_foreground_environment_action(taxonomy: Mapping[str, Any]) -> bool:
    return bool(
        _canonical(taxonomy.get("source_kind")) == "environment"
        and str(taxonomy.get("foreground_action_contract") or "")
        == FOREGROUND_ENVIRONMENT_ACTION_CONTRACT
        and _canonical(taxonomy.get("action"))
        in FOREGROUND_ENVIRONMENT_ACTIONS
        and not _canonical(taxonomy.get("environment"))
    )


def _taxonomy_from_tags(raw: Mapping[str, Any]) -> dict[str, Any]:
    semantic = raw.get("semantic")
    if not isinstance(semantic, Mapping):
        return {}
    tags = _strings(semantic.get("taxonomy"))
    domain = _canonical(semantic.get("domain"))
    result: dict[str, Any] = {}
    if domain == "environment":
        result["source_kind"] = "environment"
    elif domain == "animal":
        result["source_kind"] = "animal"
    elif domain == "foley":
        result["source_kind"] = "foley"
    labels = set(tags)
    for tag in tags:
        folded = tag.casefold()
        if folded.startswith("environment."):
            environment = folded.split(".", 1)[1].replace("_", " ")
            # The provider title is geographically specific, while the visual
            # planner intentionally uses the portable biome label.
            if environment == "african savanna":
                environment = "savanna"
            # An either/or tag cannot prove an exact environment.
            if " or " not in environment:
                result["environment"] = environment
        elif folded.startswith("action."):
            result["action"] = folded.split(".", 1)[1].replace("_", " ")
        elif folded == "foley.footsteps":
            result["action"] = "footstep"
        elif folded == "vocalization.trumpet":
            result["action"] = "trumpeting"
        elif folded == "vocalization.call":
            result["action"] = "calling"
        elif folded.startswith("distance."):
            result["distance"] = folded.split(".", 1)[1].replace("_", " ")
    result["labels"] = sorted(labels)
    return result if result.get("source_kind") else {}


def _taxonomy(raw: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    semantic = raw.get("semantic")
    if not isinstance(semantic, Mapping):
        semantic = {}
    value = (
        override.get("taxonomy")
        or raw.get("taxonomy")
        or semantic.get("retrieval_taxonomy")
        or semantic.get("audio_index_taxonomy")
    )
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    return _taxonomy_from_tags(raw)


def _classifier_catalog_contradiction(
    taxonomy: Mapping[str, Any],
) -> str | None:
    """Reject explicit classifier evidence that contradicts catalog domain.

    The zero-shot bank does not contain every environment subtype (notably
    ``underwater``), so subtype identity remains anchored to licensed catalog
    taxonomy.  The classifier is authoritative only for a demonstrated domain
    conflict such as an animal recording catalogued as environment ambience.
    """

    agrees_key = "classifier_agrees_with_catalog_domain"
    agrees = taxonomy.get(agrees_key)
    top_label = _canonical(taxonomy.get("classifier_top_label"))
    if agrees is not None and not isinstance(agrees, bool):
        return "invalid_classifier_catalog_domain_binding"
    catalog_kind = _canonical(taxonomy.get("source_kind"))

    # A single subtype label is not a fair domain decision: the pinned prompt
    # bank contains several environment subtypes but only two foley subtypes.
    # Prefer the already-persisted aggregate source-kind scores when they are
    # complete and have a material winner.  This keeps a close water-action
    # recording from being rejected merely because ``environment.wetland`` is
    # its strongest individual label, while the aggregate foley probability is
    # still higher than the aggregate environment probability.
    raw_scores = taxonomy.get("classifier_source_kind_scores")
    if raw_scores is not None:
        expected_kinds = {"music", "environment", "animal", "foley", "noise", "silence"}
        if not isinstance(raw_scores, Mapping) or set(raw_scores) != expected_kinds:
            return "invalid_classifier_catalog_domain_binding"
        scores: dict[str, float] = {}
        for raw_kind, raw_value in raw_scores.items():
            if isinstance(raw_value, bool):
                return "invalid_classifier_catalog_domain_binding"
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                return "invalid_classifier_catalog_domain_binding"
            if not math.isfinite(value) or value < 0.0 or value > 1.0:
                return "invalid_classifier_catalog_domain_binding"
            scores[str(raw_kind)] = value
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        winner, winner_score = ranked[0]
        margin = winner_score - ranked[1][1]
        if margin >= CLASSIFIER_SOURCE_KIND_MIN_MARGIN:
            if catalog_kind and winner != catalog_kind:
                return "classifier_catalog_source_kind_mismatch"
            return None

    if agrees is False:
        return "classifier_catalog_source_kind_mismatch"
    if not top_label:
        return None
    classifier_kind = top_label.split(".", 1)[0]
    if catalog_kind and classifier_kind != catalog_kind:
        return "classifier_catalog_source_kind_mismatch"
    return None


def _quality(raw: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    usability = raw.get("usability")
    if not isinstance(usability, Mapping):
        usability = {}
    value = override.get("quality") or raw.get("quality") or usability.get("quality")
    if not isinstance(value, Mapping):
        # Numeric contamination/loop claims are never invented from a filename
        # or a prior-use note.  The caller must supply classifier/QA results.
        raise ValueError("missing_quality_classifier")
    return copy.deepcopy(dict(value))


def _semantic(raw: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    value = raw.get("semantic")
    result = copy.deepcopy(dict(value)) if isinstance(value, Mapping) else {}
    semantic_override = override.get("semantic")
    if isinstance(semantic_override, Mapping):
        for key in ("queries", "hard_negatives", "taxonomy"):
            if semantic_override.get(key) is not None:
                result[key] = copy.deepcopy(semantic_override[key])
    for key in ("queries", "hard_negatives"):
        value = override.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            result[key] = copy.deepcopy(value)
    # Classifier mappings expose retrieval taxonomy as a Mapping.  It must not
    # overwrite catalog semantic tags such as ``instrumental`` and mood tags.
    taxonomy_tags = override.get("taxonomy")
    if isinstance(taxonomy_tags, Sequence) and not isinstance(
        taxonomy_tags, (str, bytes)
    ):
        result["taxonomy"] = copy.deepcopy(taxonomy_tags)
    return result


def _common_asset(
    raw: Mapping[str, Any],
    override: Mapping[str, Any],
    *,
    catalog_root: Path | None,
    catalog_id: str,
) -> dict[str, Any]:
    usability = raw.get("usability")
    if isinstance(usability, Mapping) and usability.get("render_eligible") is not True:
        raise ValueError("not_render_eligible")
    asset_id = _project_asset_id(raw, override)
    mapped_taxonomy = _taxonomy(raw, override)
    classifier_conflict = _classifier_catalog_contradiction(mapped_taxonomy)
    if classifier_conflict is not None:
        raise ValueError(classifier_conflict)
    digest = _digest(raw, override)
    path = _runtime_path(raw, override, catalog_root=catalog_root)
    if not path.is_file():
        raise ValueError("missing_file")
    if sha256_file(path) != digest:
        raise ValueError("sha256_mismatch")
    license_info = _license(raw, override)
    source = _source_provenance(raw)
    source_ref = str(
        override.get("source_ref") or raw.get("source_ref") or source.get("source_ref") or ""
    ).strip()
    acquired_at = str(
        override.get("acquired_at")
        or raw.get("acquired_at")
        or source.get("acquired_at")
        or ""
    ).strip()
    if not source_ref:
        raise ValueError("missing_source_ref")
    if not acquired_at:
        raise ValueError("missing_acquired_at")
    canonical_id = str(raw.get("canonical_id") or f"sha256:{digest}").strip()
    provenance = {
        "provider": "local_licensed",
        "asset_id": asset_id,
        "source_ref": source_ref,
        "sha256": digest,
        "acquired_at": acquired_at,
        "license": license_info,
        "catalog_id": catalog_id,
        "canonical_id": canonical_id,
        "source_provider": str(source.get("provider") or raw.get("provider") or "unknown"),
    }
    if source.get("provider_asset_id"):
        provenance["source_provider_asset_id"] = str(source["provider_asset_id"])
    if source.get("rights_basis"):
        provenance["rights_basis"] = str(source["rights_basis"])
    # Reuse the canonical sound provenance validator with a runtime-only path.
    validate_asset_provenance(
        {"path": str(path), "provenance": provenance},
        expected_provider="local_licensed",
    )
    return {
        "asset_id": asset_id,
        "sha256": digest,
        "path": str(path),
        "duration": _duration(raw, override),
        "title": str(override.get("title") or raw.get("title") or asset_id).strip(),
        "description": str(override.get("description") or raw.get("description") or "").strip(),
        "roles": _roles(raw, override),
        "semantic": _semantic(raw, override),
        "provenance": provenance,
        "license": license_info,
        "acquired_at": acquired_at,
        "source_ref": source_ref,
        "raw": raw,
        "override": override,
    }


def _portable_asset(common: Mapping[str, Any], *, natural: bool) -> dict[str, Any]:
    provenance = copy.deepcopy(dict(common["provenance"]))
    result: dict[str, Any] = {
        "asset_id": common["asset_id"],
        "sha256": common["sha256"],
        "duration": common["duration"],
        "provenance": provenance,
    }
    if natural:
        taxonomy = copy.deepcopy(dict(common["taxonomy"]))
        quality = copy.deepcopy(dict(common["quality"]))
        result["taxonomy"] = taxonomy
        result["quality"] = quality
        provenance["taxonomy"] = copy.deepcopy(taxonomy)
        provenance["index_classifier_version"] = str(
            quality.get("classifier_version") or ""
        )
    return result


def _catalog_fingerprint(
    catalog_id: str,
    rows: Sequence[Mapping[str, Any]],
    asset_mapping: Mapping[str, Any],
) -> str:
    identities = []
    for raw in rows:
        provenance = _source_provenance(raw)
        override = _override_for(raw, asset_mapping)
        # Runtime locations must not affect a portable retrieval receipt, but
        # changing a classifier result or project identity must invalidate it.
        portable_override = {
            key: copy.deepcopy(value)
            for key, value in override.items()
            if key not in {"path", "relative_path"}
        }
        identities.append(
            {
                "canonical_id": raw.get("canonical_id"),
                "sha256": raw.get("sha256"),
                "asset_id": raw.get("asset_id") or provenance.get("provider_asset_id"),
                "kind": raw.get("kind"),
                "roles": raw.get("roles"),
                "semantic": raw.get("semantic"),
                "license": raw.get("license") or provenance.get("license"),
                "resolution_metadata": portable_override,
            }
        )
    identities.sort(key=lambda row: str(row.get("asset_id") or row.get("canonical_id") or ""))
    return stable_cache_key(
        "portable_audio_catalog",
        {"catalog_id": catalog_id, "assets": identities},
        version=CATALOG_RESOLVER_SCHEMA_VERSION,
    )


def _rejection(asset_id: str, reason: str, *, role: str = "catalog") -> dict[str, str]:
    return {"asset_id": asset_id or "unknown", "role": role, "reason_code": reason}


def _music_text(common: Mapping[str, Any]) -> str:
    semantic = common.get("semantic")
    if not isinstance(semantic, Mapping):
        semantic = {}
    return " ".join(
        [
            str(common.get("title") or ""),
            *_strings(semantic.get("taxonomy")),
            *_strings(semantic.get("queries")),
        ]
    )


_MOOD_EQUIVALENTS = {
    "calm": {"calm", "contemplative", "peaceful", "serene", "gentle"},
    "contemplative": {"calm", "contemplative", "reflective", "restrained"},
    "tension": {"tension", "tense", "suspense", "danger", "predator", "prey"},
    "danger": {"danger", "tense", "tension", "threat", "suspense"},
    "power": {"power", "powerful", "force", "majestic"},
    "movement": {"movement", "motion", "rhythm", "process"},
    "opening": {"opening", "introduction", "prologue", "wonder"},
    "resolution": {"resolution", "finale", "ending", "conclusion"},
}


def _expand_mood(tokens: set[str]) -> set[str]:
    expanded = set(tokens)
    for token in list(tokens):
        expanded.update(_MOOD_EQUIVALENTS.get(token, ()))
    return expanded


def _quantitative_mood_tokens(mood: Mapping[str, Any]) -> set[str]:
    """Derive broad compatible mood tags when a prose label has no tag overlap."""

    try:
        arousal = float(mood.get("arousal"))
        valence = float(mood.get("valence"))
        tension = float(mood.get("tension"))
    except (TypeError, ValueError) as exc:
        raise CatalogResolutionError("music mood dimensions must be numeric") from exc
    if not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in (
        arousal,
        valence,
        tension,
    )):
        raise CatalogResolutionError("music mood dimensions must be in [0, 1]")

    tokens: set[str] = set()
    if tension >= 0.6:
        tokens.update(("danger", "tension"))
    elif tension >= 0.45:
        tokens.update(("power", "tension"))
    if arousal >= 0.65:
        tokens.update(("movement", "power"))
    elif arousal >= 0.5:
        tokens.update(("movement", "process"))
    else:
        tokens.update(("calm", "contemplative"))
    if valence >= 0.55 and tension <= 0.35:
        tokens.update(("calm", "contemplative"))
    return _expand_mood(tokens)


_GENERIC_MUSIC_TOKENS = {
    "background",
    "cinematic",
    "documentary",
    "instrumental",
    "music",
    "natural",
    "score",
    "soundtrack",
}


def _music_score(segment: Mapping[str, Any], common: Mapping[str, Any]) -> float:
    mood = segment.get("mood")
    if not isinstance(mood, Mapping):
        mood = {}
    requested = _tokens(mood.get("label"))
    for keyword in _strings(mood.get("keywords")):
        requested.update(_tokens(keyword))
    requested = _expand_mood(requested)
    asset_tokens = _expand_mood(_tokens(_music_text(common)))
    label_score = len(requested & asset_tokens) / max(1, len(requested))
    quantitative = _quantitative_mood_tokens(mood)
    quantitative_score = len(quantitative & asset_tokens) / max(1, len(quantitative))
    # Prose remains authoritative when it names a catalog mood.  Quantitative
    # dimensions only supply a conservative compatibility floor for descriptive
    # labels (for example, an animal/location chapter title).
    mood_score = max(label_score, 0.5 * quantitative_score)
    prompt = _tokens(segment.get("prompt")) - _GENERIC_MUSIC_TOKENS
    prompt_score = len(prompt & asset_tokens) / max(1, len(prompt))
    return round(0.8 * mood_score + 0.2 * prompt_score, 6)


def _music_hard_negative(segment: Mapping[str, Any], common: Mapping[str, Any]) -> str | None:
    semantic = common.get("semantic")
    if not isinstance(semantic, Mapping):
        return None
    target_text = " ".join(
        [
            str(segment.get("prompt") or ""),
            str((segment.get("mood") or {}).get("label") or ""),
            *_strings((segment.get("mood") or {}).get("keywords")),
        ]
    )
    target = _tokens(target_text)
    folded_target = _canonical(target_text)
    for negative in _strings(semantic.get("hard_negatives")):
        negative_tokens = _tokens(negative)
        folded_negative = _canonical(negative)
        explicitly_negated = any(
            marker in folded_target
            for marker in (
                f"no {folded_negative}",
                f"without {folded_negative}",
                f"без {folded_negative}",
            )
        )
        if negative_tokens and negative_tokens.issubset(target) and not explicitly_negated:
            return negative
    return None


def _resolve_music(
    segment: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    *,
    min_score: float,
    catalog_fingerprint: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    updated = copy.deepcopy(dict(segment))
    updated.pop("asset", None)
    audit: list[dict[str, Any]] = []
    ranked: list[tuple[float, str, Mapping[str, Any]]] = []
    for common in candidates:
        semantic = common.get("semantic")
        taxonomy = _strings(semantic.get("taxonomy")) if isinstance(semantic, Mapping) else []
        instrumental = bool(common.get("override", {}).get("instrumental")) or any(
            _canonical(item) == "instrumental" for item in taxonomy
        )
        if not instrumental:
            audit.append(
                {"asset_id": common["asset_id"], "rejected": "instrumental_not_proven"}
            )
            continue
        negative = _music_hard_negative(segment, common)
        if negative:
            audit.append(
                {
                    "asset_id": common["asset_id"],
                    "rejected": "hard_negative",
                    "value": negative,
                }
            )
            continue
        score = _music_score(segment, common)
        audit.append({"asset_id": common["asset_id"], "score": score})
        ranked.append((-score, str(common["asset_id"]), common))
    ranked.sort(key=lambda item: (item[0], item[1]))
    if not ranked or -ranked[0][0] < min_score:
        return updated, {
            "lane": "music_segments",
            "layer_id": str(segment.get("segment_id") or ""),
            "reason_code": "no_mood_compatible_licensed_music",
            "candidate_audit": audit[:10],
        }
    score, _asset_id, winner = ranked[0]
    updated["provider"] = "local_licensed"
    updated["asset"] = _portable_asset(winner, natural=False)
    updated["generation_cache_key"] = music_generation_cache_key(updated)
    updated["retrieval"] = {
        "status": "resolved",
        "asset_id": winner["asset_id"],
        "score": round(-score, 6),
        "catalog_fingerprint": catalog_fingerprint,
        "resolver": {"version": CATALOG_RESOLVER_VERSION},
        "candidate_audit": audit[:10],
        "retrieval_key": stable_cache_key(
            "catalog_music_retrieval",
            {
                "segment_id": segment.get("segment_id"),
                "winner": winner["asset_id"],
                "score": round(-score, 6),
                "catalog": catalog_fingerprint,
            },
            version=CATALOG_RESOLVER_SCHEMA_VERSION,
        ),
    }
    return updated, None


def _is_vocal_action(taxonomy: Mapping[str, Any]) -> bool:
    action_tokens = _tokens(taxonomy.get("action"))
    labels = taxonomy.get("labels")
    if isinstance(labels, Sequence) and not isinstance(labels, (str, bytes)):
        for label in labels:
            action_tokens.update(_tokens(label))
    return bool(action_tokens & _VOCAL_ACTION_TOKENS)


def _post_retrieval_rejection(
    layer: Mapping[str, Any], result: Mapping[str, Any]
) -> str | None:
    target = layer.get("taxonomy")
    asset = result.get("asset")
    actual = asset.get("taxonomy") if isinstance(asset, Mapping) else None
    if not isinstance(target, Mapping) or not isinstance(actual, Mapping):
        return "missing_taxonomy_proof"
    specificity = _canonical(target.get("specificity"))
    level = _canonical(result.get("degradation_level"))
    expected_kind = _canonical(target.get("source_kind"))
    actual_kind = _canonical(actual.get("source_kind"))
    if not expected_kind or actual_kind != expected_kind:
        return "source_kind_proof_missing_or_wrong"

    expected_environment = _canonical(target.get("environment"))
    actual_environment = _canonical(actual.get("environment"))
    if specificity == "environment" or layer.get("lane") == "ambience":
        if (
            level != "environment"
            or not expected_environment
            or expected_environment != actual_environment
        ):
            return "ambience_environment_not_exact"
        return None

    expected_action = _canonical(target.get("action"))
    actual_action = _canonical(actual.get("action"))
    expected_species = _canonical(target.get("species"))
    actual_species = _canonical(actual.get("species"))
    expected_family = _canonical(target.get("family"))
    actual_family = _canonical(actual.get("family"))

    if actual_species:
        if not expected_species or actual_species != expected_species:
            return "wrong_species_substitution_blocked"
        if level != "exact_species":
            return "exact_species_must_pass_exact_gate"
        if expected_action and actual_action != expected_action:
            return "action_proof_missing_or_wrong"
        return None

    if level == "family":
        if _is_vocal_action(target):
            return "generic_vocalization_blocked"
        if not expected_family or actual_family != expected_family:
            return "family_proof_missing_or_wrong"
        if not expected_action or actual_action != expected_action:
            return "action_proof_missing_or_wrong"
        return None

    if level == "neutral_action":
        if _is_vocal_action(target):
            return "generic_vocalization_blocked"
        if actual_family:
            return "neutral_action_requires_species_and_family_empty"
        if not expected_action or actual_action != expected_action:
            return "action_proof_missing_or_wrong"
        return None

    if specificity == "exact_species" and expected_species:
        return "exact_species_degradation_blocked"
    if specificity == "family":
        return "family_degradation_blocked"
    return "unsafe_degradation_level"


def _apply_effective_degradation(
    layer: dict[str, Any], result: Mapping[str, Any]
) -> None:
    level = _canonical(result.get("degradation_level"))
    taxonomy = layer.get("taxonomy")
    if level not in {"family", "neutral_action"} or not isinstance(
        taxonomy, Mapping
    ):
        return
    requested = copy.deepcopy(dict(taxonomy))
    effective = copy.deepcopy(requested)
    effective.pop("species", None)
    if level == "neutral_action":
        effective.pop("family", None)
    effective["specificity"] = level
    if effective != requested:
        layer["requested_taxonomy"] = requested
        layer["taxonomy"] = effective
        layer["search_cache_key"] = natural_sound_search_cache_key(layer)


def _portable_retrieval_receipt(
    result: Mapping[str, Any],
    *,
    layer: Mapping[str, Any],
    catalog_fingerprint: str,
    post_filter_audit: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    receipt = copy.deepcopy({key: value for key, value in result.items() if key != "asset"})
    receipt["index_fingerprint"] = catalog_fingerprint
    post_rows = [
        copy.deepcopy(dict(item)) for item in post_filter_audit
    ][-_CANDIDATE_AUDIT_LIMIT:]
    remaining = _CANDIDATE_AUDIT_LIMIT - len(post_rows)
    retriever_rows = [
        copy.deepcopy(dict(item)) for item in receipt.get("candidate_audit", [])
    ]
    receipt["candidate_audit"] = [
        *post_rows,
        *(retriever_rows[-remaining:] if remaining else []),
    ]
    retriever = receipt.get("retriever")
    if not isinstance(retriever, Mapping):
        retriever = {"version": "sound-retrieval-v2.1"}
    receipt["retriever"] = {
        **copy.deepcopy(dict(retriever)),
        "catalog_resolver_version": CATALOG_RESOLVER_VERSION,
    }
    receipt["retrieval_key"] = stable_cache_key(
        "portable_sound_retrieval",
        {
            "layer_search_key": layer.get("search_cache_key"),
            "status": receipt.get("status"),
            "level": receipt.get("degradation_level"),
            "asset_id": receipt.get("asset_id"),
            "catalog": catalog_fingerprint,
            "score": receipt.get("score"),
            "post_filter_audit": list(post_filter_audit),
        },
        version=CATALOG_RESOLVER_SCHEMA_VERSION,
    )
    return receipt


def _environment_water_veto(layer: Mapping[str, Any]) -> bool:
    environment = layer.get("environment")
    water = (
        _canonical(environment.get("water"))
        if isinstance(environment, Mapping)
        else ""
    )
    if water in {"none", "absent", "dry"}:
        return True
    dry_or_absent = any(
        marker in water
        for marker in (
            "dry sand",
            "no visible",
            "none visible",
            "no open water",
            "without water",
        )
    )
    negatives = " ".join(_strings(layer.get("hard_negatives"))).casefold()
    explicitly_forbidden = "water" in negatives and any(
        marker in negatives for marker in ("flowing", "visible", "open")
    )
    return dry_or_absent and explicitly_forbidden


def _dry_land_environment_family(layer: Mapping[str, Any]) -> str | None:
    """Map only narrowly evidenced dry-land planner descriptions."""

    environment = layer.get("environment")
    if not isinstance(environment, Mapping):
        return None
    location = re.sub(
        r"[^a-zа-яё0-9]+",
        " ",
        _canonical(environment.get("location_type")),
    ).strip()
    visual_evidence = layer.get("visual_evidence")
    description = (
        str(visual_evidence.get("description") or "")
        if isinstance(visual_evidence, Mapping)
        else ""
    )
    evidence_words = _tokens(
        " ".join(
            (
                str(environment.get("weather") or ""),
                str(environment.get("water") or ""),
                description,
            )
        )
    )
    if (
        location == "open landscape"
        and evidence_words.intersection({"dry", "dusty"})
        and evidence_words.intersection({"desert", "savanna", "savannah"})
    ):
        return "savanna"
    if location == "wooded clearing" and evidence_words.intersection(
        {"dry", "dust", "dusty"}
    ):
        return "savanna"
    water = _canonical(environment.get("water"))
    location_words = _tokens(location)
    if (
        location == "arid plain"
        and water in {"none", "absent", "dry"}
        and evidence_words.intersection({"sand", "sandy"})
        and evidence_words.intersection(
            {"bush", "bushes", "shrub", "shrubs", "tree", "trees"}
        )
    ):
        return "savanna"
    if (
        "bushveld" in location_words
        and "dry" in location_words.union(evidence_words)
    ):
        return "savanna"
    if (
        location == "open field"
        and water in {"none", "absent", "dry"}
        and "dry" in evidence_words
    ):
        return "savanna"
    if (
        water in {"none", "absent", "dry"}
        and {"riverbed", "floodplain"}.intersection(location_words)
        and "dry" in evidence_words
    ):
        return "savanna"
    return None


def _time_of_day_classes(value: Any) -> set[str]:
    words = _tokens(value)
    result: set[str] = set()
    if words & {"night", "nighttime", "nocturnal", "midnight"}:
        result.add("night")
    if words & {
        "day",
        "daylight",
        "daytime",
        "morning",
        "dawn",
        "sunrise",
        "afternoon",
        "sunny",
    }:
        result.add("day")
    if words & {"evening", "dusk", "sunset", "twilight"}:
        result.add("transition")
    return result


def _asset_time_of_day_classes(taxonomy: Mapping[str, Any]) -> set[str]:
    labels = taxonomy.get("labels")
    return _time_of_day_classes(
        " ".join(_strings(labels)) if isinstance(labels, Sequence) else ""
    )


def _prepare_ambience_family(
    layer: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    taxonomy = layer.get("taxonomy")
    if not isinstance(taxonomy, Mapping):
        return None, "missing_taxonomy_proof"
    requested = copy.deepcopy(dict(taxonomy))
    if _canonical(requested.get("source_kind")) != "environment":
        return None, "ambience_source_kind_not_environment"
    location = str(requested.get("environment") or "").strip()
    water_veto = _environment_water_veto(layer)
    excluded_families = ("waterfall", "wetland") if water_veto else ()
    family = _dry_land_environment_family(layer)
    basis = "environment.dry_land_evidence"
    if family is None:
        family = _environment_family(location, excluded=excluded_families)
        basis = "taxonomy.environment"
    if family is None:
        environment = layer.get("environment")
        if isinstance(environment, Mapping):
            water = str(environment.get("water") or "").strip()
            family = _environment_family(water, excluded=excluded_families)
            if family is not None:
                basis = "environment.water"
    if family is None:
        environment = layer.get("environment")
        if isinstance(environment, Mapping):
            weather = str(environment.get("weather") or "").strip()
            weather_family = _environment_family(
                weather,
                excluded=excluded_families,
            )
            if weather_family == "rain":
                family = weather_family
                basis = "environment.weather"
    if family is None:
        return None, "unknown_environment_family"

    effective = copy.deepcopy(requested)
    effective["environment"] = family
    if effective != requested:
        layer["requested_taxonomy"] = requested
        layer["taxonomy"] = effective
    receipt = {
        "version": ENVIRONMENT_FAMILY_MATCH_VERSION,
        "source_kind": "environment",
        "requested_environment": location,
        "canonical_family": family,
        "match_basis": basis,
        "original_query_en": str(layer.get("query_en") or ""),
        "time_of_day": str(
            (layer.get("environment") or {}).get("time_of_day") or ""
        ) if isinstance(layer.get("environment"), Mapping) else "",
    }
    receipt["time_of_day_classes"] = sorted(
        _time_of_day_classes(receipt["time_of_day"])
    )
    if excluded_families:
        receipt["excluded_families"] = list(excluded_families)
        environment = layer.get("environment")
        water = (
            _canonical(environment.get("water"))
            if isinstance(environment, Mapping)
            else ""
        )
        receipt["family_veto_reason"] = (
            "explicit_environment_water_absence"
            if water in {"none", "absent", "dry"}
            else "explicit_dry_no_water_hard_negative"
        )
    layer["environment_family_match"] = copy.deepcopy(receipt)
    return receipt, None


def _resolve_natural(
    layer: Mapping[str, Any],
    retriever: SoundRetriever | None,
    common_by_id: Mapping[str, Mapping[str, Any]],
    *,
    catalog_fingerprint: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    updated = copy.deepcopy(dict(layer))
    updated.pop("asset", None)
    updated["provider"] = "local_licensed"
    layer_id = str(
        updated.get("region_id") or updated.get("layer_id") or updated.get("cue_id") or ""
    )
    lane_name = {
        "ambience": "ambience_regions",
        "presence": "presence_layers",
        "events": "event_cues",
    }.get(str(updated.get("lane")), str(updated.get("lane") or "natural"))
    family_match: dict[str, Any] | None = None
    if str(updated.get("lane")) == "ambience":
        family_match, family_error = _prepare_ambience_family(updated)
        if family_error is not None:
            updated["search_cache_key"] = natural_sound_search_cache_key(updated)
            updated["retrieval"] = {
                "status": "needs_sound",
                "degradation_level": "needs_sound",
                "reason": family_error,
                "index_fingerprint": catalog_fingerprint,
                "candidate_audit": [],
            }
            return updated, {
                "lane": lane_name,
                "layer_id": layer_id,
                "reason_code": family_error,
                "query": str(updated.get("query_en") or ""),
                "taxonomy": copy.deepcopy(updated.get("taxonomy") or {}),
                "candidate_audit": [],
            }
    updated["search_cache_key"] = natural_sound_search_cache_key(updated)
    if retriever is None:
        updated["retrieval"] = {
            "status": "needs_sound",
            "degradation_level": "needs_sound",
            "reason": "no production-eligible natural assets in catalog",
            "index_fingerprint": catalog_fingerprint,
            "candidate_audit": [],
        }
        return updated, {
            "lane": lane_name,
            "layer_id": layer_id,
            "reason_code": "no_production_eligible_natural_assets",
            "candidate_audit": [],
        }

    excluded: list[str] = []
    post_filter_audit: list[dict[str, Any]] = []
    for _attempt in range(len(common_by_id) + 1):
        result = retriever.retrieve(updated, excluded_asset_ids=excluded)
        if result.get("status") != "resolved":
            updated["retrieval"] = _portable_retrieval_receipt(
                result,
                layer=updated,
                catalog_fingerprint=catalog_fingerprint,
                post_filter_audit=post_filter_audit,
            )
            return updated, {
                "lane": lane_name,
                "layer_id": layer_id,
                "reason_code": "no_licensed_taxonomy_match",
                "query": str(updated.get("query_en") or ""),
                "taxonomy": copy.deepcopy(updated.get("taxonomy") or {}),
                "candidate_audit": updated["retrieval"].get("candidate_audit", []),
            }
        reason = _post_retrieval_rejection(updated, result)
        asset_id = str(result.get("asset_id") or "")
        common = common_by_id.get(asset_id)
        required_role = {
            "ambience": "ambience",
            "presence": "presence",
            "events": "event",
        }.get(str(updated.get("lane")))
        if (
            reason is None
            and common is not None
            and required_role is not None
            and required_role not in common.get("roles", set())
        ):
            reason = "catalog_role_not_allowed"
        if reason is None and family_match is not None and common is not None:
            actual_taxonomy = common.get("taxonomy")
            if not isinstance(actual_taxonomy, Mapping) or _canonical(
                actual_taxonomy.get("source_kind")
            ) != "environment":
                reason = "ambience_asset_not_environment"
            else:
                actual_family = _environment_family(
                    actual_taxonomy.get("environment")
                )
                if actual_family is None:
                    reason = "unknown_asset_environment_family"
                elif actual_family != family_match["canonical_family"]:
                    reason = "contradictory_environment_family"
                else:
                    requested_times = set(family_match["time_of_day_classes"])
                    asset_times = _asset_time_of_day_classes(actual_taxonomy)
                    if requested_times and asset_times and not (
                        requested_times & asset_times
                    ):
                        reason = "ambience_time_of_day_mismatch"
        if reason is None and family_match is not None and common is not None:
            required_tail = float(updated.get("duration") or 0.0) + float(
                updated.get("crossfade_seconds") or 0.0
            )
            if float(common.get("duration") or 0.0) + 1e-6 < required_tail:
                reason = "ambience_source_tail_too_short"
        if reason is not None:
            post_filter_audit.append(
                {
                    "asset_id": asset_id,
                    "level": result.get("degradation_level"),
                    "rejected": reason,
                }
            )
            excluded.append(asset_id)
            continue
        if common is None:
            post_filter_audit.append(
                {"asset_id": asset_id, "rejected": "catalog_identity_missing"}
            )
            excluded.append(asset_id)
            continue
        _apply_effective_degradation(updated, result)
        updated["asset"] = _portable_asset(common, natural=True)
        retrieval_receipt = _portable_retrieval_receipt(
            result,
            layer=updated,
            catalog_fingerprint=catalog_fingerprint,
            post_filter_audit=post_filter_audit,
        )
        if family_match is not None:
            actual_taxonomy = common["taxonomy"]
            retrieval_receipt["environment_family_match"] = {
                **copy.deepcopy(family_match),
                "asset_environment": str(actual_taxonomy.get("environment") or ""),
                "asset_canonical_family": _environment_family(
                    actual_taxonomy.get("environment")
                ),
                "asset_time_of_day_classes": sorted(
                    _asset_time_of_day_classes(actual_taxonomy)
                ),
            }
        updated["retrieval"] = retrieval_receipt
        return updated, None

    updated["retrieval"] = {
        "status": "needs_sound",
        "degradation_level": "needs_sound",
        "index_fingerprint": catalog_fingerprint,
        "candidate_audit": post_filter_audit[-10:],
        "reason": "all candidates were rejected by portable safety gates",
    }
    return updated, {
        "lane": lane_name,
        "layer_id": layer_id,
        "reason_code": "all_candidates_rejected",
        "candidate_audit": post_filter_audit[-10:],
    }


def _interval_union(
    intervals: Sequence[tuple[float, float]], duration: float
) -> tuple[float, list[dict[str, float]], list[dict[str, float]]]:
    clipped = sorted(
        (max(0.0, float(start)), min(duration, float(end)))
        for start, end in intervals
        if float(end) > float(start)
    )
    gaps: list[dict[str, float]] = []
    overlaps: list[dict[str, float]] = []
    cursor = 0.0
    covered = 0.0
    for start, end in clipped:
        if start > cursor + 1e-6:
            gaps.append({"start": round(cursor, 6), "end": round(start, 6)})
        elif start < cursor - 1e-6:
            overlaps.append({"start": round(start, 6), "end": round(min(cursor, end), 6)})
        if end > cursor:
            covered += end - max(start, cursor)
            cursor = end
    if cursor < duration - 1e-6:
        gaps.append({"start": round(cursor, 6), "end": round(duration, 6)})
    return covered, gaps, overlaps


def _ambience_coverage(plan: Mapping[str, Any]) -> dict[str, Any]:
    duration = float(plan.get("timeline_duration") or 0.0)
    all_regions = [
        item for item in plan.get("ambience_regions", []) if isinstance(item, Mapping)
    ]
    structural_intervals = [
        (float(item.get("start") or 0.0), float(item.get("end") or 0.0))
        for item in all_regions
    ]
    resolved_intervals = [
        (float(item.get("start") or 0.0), float(item.get("end") or 0.0))
        for item in all_regions
        if isinstance(item.get("asset"), Mapping)
    ]
    structural, structural_gaps, overlaps = _interval_union(structural_intervals, duration)
    resolved, resolved_gaps, _resolved_overlaps = _interval_union(resolved_intervals, duration)
    structural_ratio = structural / duration if duration > 0.0 else 0.0
    resolved_ratio = resolved / duration if duration > 0.0 else 0.0
    exact = (
        duration > 0.0
        and bool(all_regions)
        and not structural_gaps
        and not overlaps
        and not resolved_gaps
        and abs(resolved - duration) <= 1e-6
    )
    return {
        "required_ratio": 1.0,
        "structural_ratio": round(structural_ratio, 6),
        "resolved_ratio": round(resolved_ratio, 6),
        "exact": exact,
        "gaps": resolved_gaps,
        "structural_gaps": structural_gaps,
        "overlaps": overlaps,
    }


def _assign_ambience_source_offsets(
    regions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Choose portable offsets with a full region+crossfade source tail."""

    output = [copy.deepcopy(dict(region)) for region in regions]
    previous: dict[str, Any] | None = None
    for region in output:
        asset = region.get("asset")
        if not isinstance(asset, Mapping):
            previous = None
            continue
        asset_id = str(asset.get("asset_id") or "")
        source_duration = float(asset.get("duration") or 0.0)
        region_duration = float(region.get("duration") or 0.0)
        crossfade = float(region.get("crossfade_seconds") or 0.0)
        required_tail = region_duration + crossfade
        maximum_offset = source_duration - required_tail
        if not all(
            math.isfinite(value)
            for value in (
                source_duration,
                region_duration,
                crossfade,
                maximum_offset,
            )
        ) or maximum_offset < -1e-6:
            raise CatalogResolutionError(
                f"ambience asset {asset_id!r} has no safe non-looping source tail"
            )
        maximum_offset = max(0.0, maximum_offset)
        absolute_start = float(region.get("start") or 0.0)
        mode = "absolute_time_modulo"
        offset = absolute_start % maximum_offset if maximum_offset > 1e-6 else 0.0
        previous_region_id: str | None = None
        if (
            previous is not None
            and previous["asset_id"] == asset_id
            and abs(absolute_start - previous["end"]) <= 1e-5
        ):
            continuation = previous["source_offset"] + previous["duration"]
            if continuation <= maximum_offset + 1e-6:
                offset = continuation
                mode = "contiguous_same_asset"
                previous_region_id = previous["region_id"]
        # Flooring, rather than rounding up, preserves the promised tail at
        # the final microsecond boundary.
        offset = math.floor(max(0.0, min(offset, maximum_offset)) * 1_000_000) / 1_000_000
        region["source_offset"] = offset
        receipt = {
            "version": AMBIENCE_SOURCE_OFFSET_VERSION,
            "mode": mode,
            "asset_id": asset_id,
            "absolute_region_start": absolute_start,
            "source_duration": source_duration,
            "region_duration": region_duration,
            "crossfade_reserve": crossfade,
            "maximum_safe_offset": maximum_offset,
            "source_offset": offset,
        }
        if previous_region_id is not None:
            receipt["previous_region_id"] = previous_region_id
        retrieval = region.get("retrieval")
        if isinstance(retrieval, dict):
            retrieval["source_offset_assignment"] = receipt
        previous = {
            "asset_id": asset_id,
            "region_id": str(region.get("region_id") or ""),
            "end": float(region.get("end") or 0.0),
            "duration": region_duration,
            "source_offset": offset,
        }
    return output


def _portable_asset_id(asset: Mapping[str, Any]) -> str:
    provenance = asset.get("provenance")
    return str(
        asset.get("asset_id")
        or (provenance.get("asset_id") if isinstance(provenance, Mapping) else "")
        or ""
    )


def _exact_ambience_receipt(
    plan: Mapping[str, Any], slot_ids: Sequence[Any]
) -> dict[str, list[str]] | None:
    required = {str(slot_id) for slot_id in slot_ids if str(slot_id)}
    covered: set[str] = set()
    region_ids: set[str] = set()
    asset_ids: set[str] = set()
    for region in plan.get("ambience_regions", []):
        if not isinstance(region, Mapping):
            continue
        retrieval = region.get("retrieval")
        asset = region.get("asset")
        if (
            not isinstance(retrieval, Mapping)
            or retrieval.get("status") != "resolved"
            or retrieval.get("degradation_level") != "environment"
            or not isinstance(asset, Mapping)
        ):
            continue
        region_slots = {str(slot_id) for slot_id in region.get("slot_ids", [])}
        matching = required & region_slots
        if not matching:
            continue
        asset_id = _portable_asset_id(asset)
        region_id = str(region.get("region_id") or "")
        if not asset_id or not region_id:
            continue
        covered.update(matching)
        region_ids.add(region_id)
        asset_ids.add(asset_id)
    if not required or covered != required:
        return None
    return {
        "ambience_region_ids": sorted(region_ids),
        "ambience_asset_ids": sorted(asset_ids),
    }


def _scene_foreground_refs(
    plan: Mapping[str, Any], layer: Mapping[str, Any]
) -> list[dict[str, Any]] | None:
    lane = str(layer.get("lane") or "")
    collection = "visible_sources" if lane == "presence" else "events"
    identity_key = "source_id" if lane == "presence" else "event_id"
    audible_key = "audible_presence" if lane == "presence" else "audible_event"
    source_id = str(layer.get("visual_source_id") or "")
    slot_ids = {str(slot_id) for slot_id in layer.get("slot_ids", [])}
    refs: list[dict[str, Any]] = []
    found_slots: set[str] = set()
    for scene in plan.get("acoustic_scenes", []):
        if not isinstance(scene, Mapping):
            continue
        slot_id = str(scene.get("slot_id") or "")
        if slot_id not in slot_ids:
            continue
        for item in scene.get(collection, []):
            if not isinstance(item, dict) or str(item.get(identity_key) or "") != source_id:
                continue
            refs.append(item)
            found_slots.add(slot_id)
            break
    if not source_id or not slot_ids or found_slots != slot_ids:
        return None
    if any(ref.get(audible_key) is not True for ref in refs):
        return None
    return refs


def _apply_scene_degradation(
    plan: Mapping[str, Any],
    layer: Mapping[str, Any],
    *,
    catalog_fingerprint: str,
) -> bool:
    requested = layer.get("requested_taxonomy")
    effective = layer.get("taxonomy")
    asset = layer.get("asset")
    retrieval = layer.get("retrieval")
    if (
        not isinstance(requested, Mapping)
        or not isinstance(effective, Mapping)
        or not isinstance(asset, Mapping)
        or not isinstance(retrieval, Mapping)
    ):
        return True
    refs = _scene_foreground_refs(plan, layer)
    if refs is None:
        return False
    level = str(effective.get("specificity") or "")
    resolution = {
        "outcome": f"resolved_{level}",
        "requested_taxonomy": copy.deepcopy(dict(requested)),
        "effective_taxonomy": copy.deepcopy(dict(effective)),
        "asset_id": _portable_asset_id(asset),
        "catalog_fingerprint": catalog_fingerprint,
        "wrong_species_used": False,
    }
    physical_refs = [
        ref
        for ref in refs
        if isinstance(ref.get("physical_foley_obligation"), Mapping)
    ]
    if physical_refs:
        # These rows are deterministic compiler output.  Adding even an
        # otherwise valid resolver receipt inside the row would break the
        # compiler's byte-exact replay proof.  Resolution lives on the sound
        # layer; here we only prove that the selected asset did not degrade the
        # compiler-owned taxonomy.
        if len(physical_refs) != len(refs):
            return False
        identity_fields = ("source_kind", "family", "species", "action", "specificity")
        return all(
            all(ref.get(key) == effective.get(key) for key in identity_fields)
            for ref in physical_refs
        )
    for ref in refs:
        for key in ("source_kind", "family", "species", "action", "specificity"):
            ref[key] = effective.get(key)
        ref["sound_resolution"] = copy.deepcopy(resolution)
    return True


def _quality_rejection_matches(
    layer: Mapping[str, Any], rejected: Sequence[Mapping[str, Any]]
) -> list[str]:
    target = layer.get("taxonomy")
    if not isinstance(target, Mapping):
        return []
    matches: list[str] = []
    for row in rejected:
        actual = row.get("taxonomy")
        if not isinstance(actual, Mapping):
            continue
        if _canonical(actual.get("source_kind")) != _canonical(target.get("source_kind")):
            continue
        specificity = _canonical(target.get("specificity"))
        if specificity == "exact_species":
            keys = ("species", "action")
        elif specificity == "family":
            if actual.get("species"):
                continue
            keys = ("family", "action")
        elif specificity == "neutral_action":
            if actual.get("species") or actual.get("family"):
                continue
            keys = ("action",)
        else:
            keys = ("environment",)
        if all(
            _canonical(target.get(key))
            and _canonical(target.get(key)) == _canonical(actual.get(key))
            for key in keys
        ):
            matches.append(str(row.get("asset_id") or ""))
    return sorted({asset_id for asset_id in matches if asset_id})


def _disposition_reason(
    issue: Mapping[str, Any], quality_rejected_ids: Sequence[str]
) -> str:
    if quality_rejected_ids:
        return "quality_classifier_rejected"
    priority = (
        "wrong_species_substitution_blocked",
        "generic_vocalization_blocked",
        "neutral_action_requires_species_and_family_empty",
        "source_kind_proof_missing_or_wrong",
    )
    rejected = {
        str(row.get("rejected") or "")
        for row in issue.get("candidate_audit", [])
        if isinstance(row, Mapping)
    }
    return next(
        (reason for reason in priority if reason in rejected),
        str(issue.get("reason_code") or "no_licensed_taxonomy_match"),
    )


def _suppress_optional_foreground(
    plan: Mapping[str, Any],
    layer: Mapping[str, Any],
    issue: Mapping[str, Any],
    *,
    catalog_fingerprint: str,
    quality_rejected: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    if layer.get("required") is True or layer.get("mandatory") is True:
        return None
    ambience_receipt = _exact_ambience_receipt(plan, layer.get("slot_ids", []))
    refs = _scene_foreground_refs(plan, layer)
    taxonomy = layer.get("taxonomy")
    if ambience_receipt is None or refs is None or not isinstance(taxonomy, Mapping):
        return None
    lane = str(layer.get("lane") or "")
    audible_key = "audible_presence" if lane == "presence" else "audible_event"
    quality_ids = _quality_rejection_matches(layer, quality_rejected)
    layer_id = str(
        layer.get("layer_id") or layer.get("cue_id") or layer.get("region_id") or ""
    )
    disposition: dict[str, Any] = {
        "schema_version": FOREGROUND_SOUND_DISPOSITION_SCHEMA_VERSION,
        "outcome": "covered_by_exact_ambience",
        "lane": lane,
        "original_layer_id": layer_id,
        "slot_ids": [str(slot_id) for slot_id in layer.get("slot_ids", [])],
        "visual_source_id": str(layer.get("visual_source_id") or ""),
        "requested_taxonomy": copy.deepcopy(dict(taxonomy)),
        "catalog_fingerprint": catalog_fingerprint,
        "reason_code": _disposition_reason(issue, quality_ids),
        "quality_rejected_asset_ids": quality_ids,
        "ambience_region_ids": ambience_receipt["ambience_region_ids"],
        "ambience_asset_ids": ambience_receipt["ambience_asset_ids"],
        "wrong_species_used": False,
        "resolver": {"version": CATALOG_RESOLVER_VERSION},
        "candidate_audit": [
            copy.deepcopy(dict(row))
            for row in issue.get("candidate_audit", [])
            if isinstance(row, Mapping)
        ][-10:],
    }
    disposition["disposition_key"] = foreground_sound_disposition_cache_key(
        disposition
    )
    for ref in refs:
        ref["audibility_requested"] = True
        ref[audible_key] = False
        ref["sound_disposition"] = copy.deepcopy(disposition)
    return disposition


def _scene_contract_issue(plan: Mapping[str, Any]) -> dict[str, Any] | None:
    expected = {
        str(slot_id)
        for region in plan.get("ambience_regions", [])
        if isinstance(region, Mapping)
        for slot_id in region.get("slot_ids", [])
    }
    actual = {
        str(scene.get("slot_id") or "")
        for scene in plan.get("acoustic_scenes", [])
        if isinstance(scene, Mapping)
    }
    if expected and actual == expected:
        return None
    return {
        "lane": "acoustic_scenes",
        "layer_id": "acoustic-scene-contract",
        "reason_code": "missing_or_extra_acoustic_scenes",
        "missing_slot_ids": sorted(expected - actual),
        "extra_slot_ids": sorted(actual - expected),
    }


def resolve_sound_plan_from_catalog(
    planned_plan: Mapping[str, Any],
    catalog: Mapping[str, Any],
    *,
    catalog_root: str | Path | None = None,
    asset_mapping: Mapping[str, Any] | None = None,
    clap: ClapAdapter | None = None,
    policy: RetrievalPolicy = RetrievalPolicy(),
    min_music_score: float = 0.12,
) -> dict[str, Any]:
    """Resolve all four sound lanes and return a portable ready/miss plan.

    ``asset_mapping`` is runtime-only.  It is keyed by any catalog identity
    (canonical id, digest or provider asset id) and may supply ``path``, the
    project ``asset_id``, and explicit ``taxonomy``/``quality`` classifier
    metadata.  None of its paths are copied to the returned plan.
    """

    if not isinstance(planned_plan, Mapping):
        raise CatalogResolutionError("planned sound plan must be a mapping")
    if planned_plan.get("schema_version") != SOUND_PLAN_SCHEMA_VERSION:
        raise CatalogResolutionError("catalog resolver accepts only production-v2 plans")
    if not isinstance(catalog, Mapping):
        raise CatalogResolutionError("audio catalog must be a mapping")
    try:
        music_threshold = float(min_music_score)
    except (TypeError, ValueError) as exc:
        raise CatalogResolutionError("min_music_score must be in [0, 1]") from exc
    if not math.isfinite(music_threshold) or not 0.0 <= music_threshold <= 1.0:
        raise CatalogResolutionError("min_music_score must be in [0, 1]")

    updated = copy.deepcopy(dict(planned_plan))
    planned_counts = {
        "music": len(updated.get("music_segments", [])),
        "ambience": len(updated.get("ambience_regions", [])),
        "presence": len(updated.get("presence_layers", [])),
        "events": len(updated.get("event_cues", [])),
    }
    updated["status"] = "needs_sound"
    updated.pop("resolution_diagnostics", None)
    for lane_name in (
        "music_segments",
        "ambience_regions",
        "presence_layers",
        "event_cues",
    ):
        for item in updated.get(lane_name, []):
            if isinstance(item, dict):
                item.pop("asset", None)

    rows = _catalog_rows(catalog)
    catalog_id = str(catalog.get("catalog_id") or "portable-audio-catalog").strip()
    mapping = asset_mapping if isinstance(asset_mapping, Mapping) else {}
    fingerprint = _catalog_fingerprint(catalog_id, rows, mapping)
    runtime_root = Path(catalog_root).expanduser() if catalog_root is not None else None
    rejections: list[dict[str, str]] = []
    common_assets: list[dict[str, Any]] = []

    if not rows:
        rejections.append(_rejection("catalog", "catalog_has_no_assets"))
    for raw in rows:
        override = _override_for(raw, mapping)
        fallback_id = next((key for key in _asset_keys(raw) if key), "unknown")
        try:
            common = _common_asset(
                raw,
                override,
                catalog_root=runtime_root,
                catalog_id=catalog_id,
            )
        except (ValueError, SoundPlanError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "invalid_provenance"
            rejections.append(_rejection(fallback_id, reason))
            continue
        common_assets.append(common)

    # A project asset id must map to exactly one digest.  Ambiguous identities
    # are removed from every lane rather than depending on catalog order.
    by_project_id: dict[str, set[str]] = {}
    for common in common_assets:
        by_project_id.setdefault(str(common["asset_id"]), set()).add(str(common["sha256"]))
    collisions = {asset_id for asset_id, digests in by_project_id.items() if len(digests) > 1}
    if collisions:
        common_assets = [item for item in common_assets if item["asset_id"] not in collisions]
        rejections.extend(
            _rejection(asset_id, "asset_id_hash_collision") for asset_id in sorted(collisions)
        )
    deduplicated: dict[tuple[str, str], dict[str, Any]] = {}
    for common in common_assets:
        key = (str(common["asset_id"]), str(common["sha256"]))
        deduplicated.setdefault(key, common)
    common_assets = list(deduplicated.values())

    music_candidates = [item for item in common_assets if "music" in item["roles"]]
    natural_records: list[dict[str, Any]] = []
    natural_common: dict[str, dict[str, Any]] = {}
    quality_rejected_natural: list[dict[str, Any]] = []
    for common in common_assets:
        if not common["roles"].intersection({"ambience", "presence", "event", "events"}):
            continue
        try:
            taxonomy = _taxonomy(common["raw"], common["override"])
            quality = _quality(common["raw"], common["override"])
            record = {
                "asset_id": common["asset_id"],
                "path": common["path"],
                "sha256": common["sha256"],
                "duration": common["duration"],
                "title": common["title"],
                "description": common["description"],
                "keywords": sorted(
                    {
                        *_strings(common["semantic"].get("queries")),
                        *_strings(common["semantic"].get("taxonomy")),
                    }
                ),
                "taxonomy": taxonomy,
                "quality": quality,
                "source_ref": common["source_ref"],
                "acquired_at": common["acquired_at"],
                "license": common["license"],
            }
            # Validate each row separately so one broken catalog entry does not
            # hide otherwise usable licensed assets or its own diagnostic.
            validated = AudioIndex([record]).get(str(common["asset_id"]))
        except (ValueError, AudioIndexError, SoundPlanError) as exc:
            message = str(exc).casefold()
            if "quality" in message or "classifier" in message:
                reason = "missing_or_invalid_quality_classifier"
            elif "taxonomy" in message:
                reason = "missing_or_invalid_taxonomy"
            elif "sha-256" in message:
                reason = "sha256_mismatch"
            elif "license" in message or "commercial" in message:
                reason = "invalid_license"
            else:
                reason = str(exc) if isinstance(exc, ValueError) else "invalid_audio_index_entry"
            rejections.append(_rejection(str(common["asset_id"]), reason, role="natural"))
            continue
        validated_taxonomy = copy.deepcopy(validated["taxonomy"])
        common["taxonomy"] = copy.deepcopy(validated_taxonomy)
        common["quality"] = copy.deepcopy(validated["quality"])
        if (
            quality.get("production_eligible") is False
            or validated["quality"].get("production_eligible") is not True
        ):
            quality_rejected_natural.append(
                {
                    "asset_id": str(common["asset_id"]),
                    "taxonomy": copy.deepcopy(validated["taxonomy"]),
                    "quality": copy.deepcopy(validated["quality"]),
                }
            )
            rejections.append(
                _rejection(
                    str(common["asset_id"]),
                    "quality_classifier_rejected",
                    role="natural",
                )
            )
            continue
        retrieval_taxonomy = copy.deepcopy(validated_taxonomy)
        declared_foreground_contract = str(
            retrieval_taxonomy.get("foreground_action_contract") or ""
        ).strip()
        if declared_foreground_contract and not _is_foreground_environment_action(
            retrieval_taxonomy
        ):
            rejections.append(
                _rejection(
                    str(common["asset_id"]),
                    "invalid_foreground_environment_action_contract",
                    role="natural",
                )
            )
            continue
        if _canonical(retrieval_taxonomy.get("source_kind")) == "environment":
            if _is_foreground_environment_action(retrieval_taxonomy):
                if common.get("roles", set()) != {"presence", "event"}:
                    rejections.append(
                        _rejection(
                            str(common["asset_id"]),
                            "foreground_environment_roles_not_exact",
                            role="natural",
                        )
                    )
                    continue
            else:
                if "ambience" not in common.get("roles", set()):
                    rejections.append(
                        _rejection(
                            str(common["asset_id"]),
                            "environment_asset_lacks_ambience_role",
                            role="natural",
                        )
                    )
                    continue
                family = _environment_family(retrieval_taxonomy.get("environment"))
                if family is None:
                    rejections.append(
                        _rejection(
                            str(common["asset_id"]),
                            "unknown_asset_environment_family",
                            role="natural",
                        )
                    )
                    continue
                retrieval_taxonomy["environment"] = family
        record["taxonomy"] = retrieval_taxonomy
        record["quality"] = copy.deepcopy(validated["quality"])
        natural_records.append(record)
        natural_common[str(common["asset_id"])] = common

    retriever: SoundRetriever | None = None
    if natural_records:
        retriever = SoundRetriever(AudioIndex(natural_records), clap=clap, policy=policy)

    needs_sound: list[dict[str, Any]] = []
    music_output = []
    for segment in updated.get("music_segments", []):
        if not isinstance(segment, Mapping):
            continue
        resolved, issue = _resolve_music(
            segment,
            music_candidates,
            min_score=music_threshold,
            catalog_fingerprint=fingerprint,
        )
        music_output.append(resolved)
        if issue is not None:
            needs_sound.append(issue)
    updated["music_segments"] = music_output

    ambience_output = []
    for layer in updated.get("ambience_regions", []):
        if not isinstance(layer, Mapping):
            continue
        resolved, issue = _resolve_natural(
            layer,
            retriever,
            natural_common,
            catalog_fingerprint=fingerprint,
        )
        ambience_output.append(resolved)
        if issue is not None:
            needs_sound.append(issue)
    updated["ambience_regions"] = _assign_ambience_source_offsets(
        ambience_output
    )

    coverage = _ambience_coverage(updated)
    if not coverage["exact"]:
        needs_sound.append(
            {
                "lane": "ambience_regions",
                "layer_id": "ambience-coverage",
                "reason_code": "ambience_not_exactly_100_percent_resolved",
                "coverage": copy.deepcopy(coverage),
            }
        )

    foreground_dispositions: list[dict[str, Any]] = []
    for lane_name in ("presence_layers", "event_cues"):
        output: list[dict[str, Any]] = []
        for layer in updated.get(lane_name, []):
            if not isinstance(layer, Mapping):
                continue
            resolved, issue = _resolve_natural(
                layer,
                retriever,
                natural_common,
                catalog_fingerprint=fingerprint,
            )
            if issue is None and not _apply_scene_degradation(
                updated, resolved, catalog_fingerprint=fingerprint
            ):
                resolved = copy.deepcopy(dict(layer))
                resolved.pop("asset", None)
                issue = {
                    "lane": lane_name,
                    "layer_id": str(
                        layer.get("layer_id") or layer.get("cue_id") or ""
                    ),
                    "reason_code": "acoustic_scene_degradation_update_failed",
                    "candidate_audit": [],
                }
            if issue is None:
                output.append(resolved)
                continue
            disposition = None
            if coverage["exact"]:
                disposition = _suppress_optional_foreground(
                    updated,
                    layer,
                    issue,
                    catalog_fingerprint=fingerprint,
                    quality_rejected=quality_rejected_natural,
                )
            if disposition is not None:
                foreground_dispositions.append(disposition)
                continue
            output.append(resolved)
            needs_sound.append(issue)
        updated[lane_name] = output
    scene_issue = _scene_contract_issue(updated)
    if scene_issue is not None:
        needs_sound.append(scene_issue)

    counts = {
        "planned": planned_counts,
        "resolved": {
            "music": sum(
                isinstance(item.get("asset"), Mapping)
                for item in updated.get("music_segments", [])
            ),
            "ambience": sum(
                isinstance(item.get("asset"), Mapping)
                for item in updated.get("ambience_regions", [])
            ),
            "presence": sum(
                isinstance(item.get("asset"), Mapping)
                for item in updated.get("presence_layers", [])
            ),
            "events": sum(
                isinstance(item.get("asset"), Mapping)
                for item in updated.get("event_cues", [])
            ),
        },
        "disposed": {
            "presence": sum(
                item.get("lane") == "presence" for item in foreground_dispositions
            ),
            "events": sum(
                item.get("lane") == "events" for item in foreground_dispositions
            ),
        },
    }
    updated["status"] = "ready" if not needs_sound else "needs_sound"
    updated["resolution_diagnostics"] = {
        "schema_version": CATALOG_RESOLVER_SCHEMA_VERSION,
        "resolver_version": CATALOG_RESOLVER_VERSION,
        "classifier_source_kind_binding": {
            "version": CLASSIFIER_SOURCE_KIND_AGGREGATE_VERSION,
            "minimum_winner_margin": CLASSIFIER_SOURCE_KIND_MIN_MARGIN,
        },
        "status": updated["status"],
        "catalog_id": catalog_id,
        "catalog_fingerprint": fingerprint,
        "counts": counts,
        "ambience_coverage": coverage,
        "foreground_dispositions": foreground_dispositions,
        "needs_sound": needs_sound,
        "catalog_rejections": sorted(
            rejections,
            key=lambda item: (item.get("asset_id", ""), item.get("role", ""), item.get("reason_code", "")),
        ),
    }
    return refresh_sound_plan_cache_ids(updated)


def resolve_sound_plan_from_catalog_file(
    planned_plan: Mapping[str, Any],
    catalog_path: str | Path,
    **kwargs: Any,
) -> dict[str, Any]:
    """Load ``catalog.json`` and resolve it with its parent as asset root."""

    import json

    path = Path(catalog_path).expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise CatalogResolutionError("audio catalog document must be an object")
    kwargs.setdefault("catalog_root", path.parent)
    return resolve_sound_plan_from_catalog(planned_plan, payload, **kwargs)


# Short alias for production-stage wiring.
resolve_sound_plan_v2 = resolve_sound_plan_from_catalog


__all__ = [
    "AMBIENCE_SOURCE_OFFSET_VERSION",
    "CATALOG_RESOLVER_SCHEMA_VERSION",
    "CATALOG_RESOLVER_VERSION",
    "CLASSIFIER_SOURCE_KIND_AGGREGATE_VERSION",
    "CLASSIFIER_SOURCE_KIND_MIN_MARGIN",
    "CatalogResolutionError",
    "ENVIRONMENT_FAMILY_MATCH_VERSION",
    "FOREGROUND_ENVIRONMENT_ACTION_CONTRACT",
    "FOREGROUND_ENVIRONMENT_ACTIONS",
    "FOREGROUND_PHYSICAL_ACTIONS",
    "resolve_sound_plan_from_catalog",
    "resolve_sound_plan_from_catalog_file",
    "resolve_sound_plan_v2",
]
