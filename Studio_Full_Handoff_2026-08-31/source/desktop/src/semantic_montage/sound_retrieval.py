"""Deterministic semantic retrieval for natural-sound lanes.

The cheap path is licensed metadata/taxonomy matching.  A pinned CLAP scorer
may rerank candidates, but it cannot override hard negatives, species/action
constraints, contamination gates, or license provenance.
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from .audio_index import AudioIndex
from .sound import SOUND_PLAN_SCHEMA_VERSION, SoundPlanError, stable_cache_key


class SoundRetrievalError(SoundPlanError):
    """No licensed sound can satisfy the visual/taxonomic contract."""


_SEARCHABLE_TAXONOMY_SCALAR_FIELDS = (
    "source_kind",
    "family",
    "species",
    "action",
    "environment",
    "distance",
)


class ClapAdapter(Protocol):
    """Optional pinned audio-text model; implementations must be deterministic."""

    model_id: str
    model_hash: str

    def score(self, query: str, audio_path: str) -> float: ...


@dataclass(frozen=True)
class RetrievalPolicy:
    min_metadata_score: float = 0.18
    metadata_weight: float = 0.65
    clap_weight: float = 0.35
    min_hard_negative_margin: float = 0.08
    min_ambience_loop_score: float = 0.45
    candidate_audit_limit: int = 10

    def __post_init__(self) -> None:
        for name in (
            "min_metadata_score",
            "metadata_weight",
            "clap_weight",
            "min_hard_negative_margin",
            "min_ambience_loop_score",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise SoundRetrievalError(f"{name} must be in [0, 1]")
        if self.metadata_weight + self.clap_weight <= 0:
            raise SoundRetrievalError("retrieval weights cannot both be zero")
        if self.candidate_audit_limit < 1:
            raise SoundRetrievalError("candidate_audit_limit must be positive")


def _tokens(value: Any) -> set[str]:
    return set(re.findall(r"[a-zа-яё0-9]+", str(value or "").casefold()))


def _canonical(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _record_text(record: Mapping[str, Any]) -> str:
    taxonomy = record.get("taxonomy") if isinstance(record.get("taxonomy"), Mapping) else {}
    taxonomy_scalars = [
        value.strip()
        for key in _SEARCHABLE_TAXONOMY_SCALAR_FIELDS
        if isinstance((value := taxonomy.get(key)), str) and value.strip()
    ]
    labels = taxonomy.get("labels")
    taxonomy_labels = (
        [str(value).strip() for value in labels if str(value).strip()]
        if isinstance(labels, Sequence) and not isinstance(labels, (str, bytes))
        else []
    )
    return " ".join(
        [
            str(record.get("title") or ""),
            str(record.get("description") or ""),
            *[str(item) for item in record.get("keywords", [])],
            *taxonomy_scalars,
            *taxonomy_labels,
        ]
    ).casefold()


def _taxonomy(record: Mapping[str, Any]) -> Mapping[str, Any]:
    value = record.get("taxonomy")
    return value if isinstance(value, Mapping) else {}


def _hard_negative_hit(record: Mapping[str, Any], negatives: Sequence[str]) -> str | None:
    text = _record_text(record)
    tokens = _tokens(text)
    for negative in negatives:
        folded = _canonical(negative)
        if folded and (folded in text or _tokens(folded).issubset(tokens)):
            return str(negative)
    return None


def _metadata_score(query: str, target: Mapping[str, Any], record: Mapping[str, Any]) -> float:
    query_tokens = _tokens(query)
    record_tokens = _tokens(_record_text(record))
    overlap = len(query_tokens & record_tokens) / max(1, len(query_tokens))
    score = overlap * 0.60
    text = _record_text(record)
    if _canonical(query) in text:
        score += 0.10
    actual = _taxonomy(record)
    for key, bonus in (
        ("species", 0.16),
        ("family", 0.10),
        ("action", 0.10),
        ("environment", 0.10),
        ("source_kind", 0.06),
        ("distance", 0.04),
    ):
        expected = _canonical(target.get(key))
        got = _canonical(actual.get(key))
        if expected and got == expected:
            score += bonus
    return min(1.0, score)


def _taxonomy_allows(
    record: Mapping[str, Any], target: Mapping[str, Any], level: str
) -> tuple[bool, str | None]:
    actual = _taxonomy(record)
    source_kind = _canonical(target.get("source_kind"))
    actual_kind = _canonical(actual.get("source_kind"))
    if source_kind and actual_kind and source_kind != actual_kind:
        if not (level == "environment" and actual_kind == "environment"):
            return False, "wrong_source_kind"

    if level == "exact_species":
        expected = _canonical(target.get("species"))
        if not expected or _canonical(actual.get("species")) != expected:
            return False, "wrong_species"
    elif level == "family":
        expected = _canonical(target.get("family"))
        if not expected or _canonical(actual.get("family")) != expected:
            return False, "wrong_family"
    elif level == "neutral_action":
        if actual_kind and source_kind and actual_kind != source_kind:
            return False, "wrong_source_kind"
    elif level == "environment":
        expected = _canonical(target.get("environment"))
        if not expected:
            return False, "missing_environment_context"
        if _canonical(actual.get("environment")) != expected:
            return False, "wrong_environment"
        return True, None
    else:
        return False, "unsupported_degradation_level"

    expected_action = _canonical(target.get("action"))
    if expected_action and _canonical(actual.get("action")) != expected_action:
        return False, "wrong_action"
    return True, None


def _levels(target: Mapping[str, Any]) -> list[str]:
    levels: list[str] = []
    if target.get("species"):
        levels.append("exact_species")
    if target.get("family"):
        levels.append("family")
    if target.get("action"):
        levels.append("neutral_action")
    if target.get("environment") or target.get("source_kind") == "environment":
        levels.append("environment")
    return list(dict.fromkeys(levels)) or ["environment"]


def _query_for_level(layer: Mapping[str, Any], level: str) -> str:
    target = layer.get("taxonomy") or {}
    suffix = "natural sound no music no speech"
    if level == "exact_species":
        return f"{target.get('species')} {target.get('action') or ''} {suffix}".strip()
    if level == "family":
        return f"{target.get('family')} {target.get('action') or ''} {suffix}".strip()
    if level == "neutral_action":
        return f"{target.get('source_kind')} {target.get('action')} {suffix}".strip()
    return f"{target.get('environment') or layer.get('query_en')} ambience {suffix}".strip()


class SoundRetriever:
    def __init__(
        self,
        index: AudioIndex,
        *,
        clap: ClapAdapter | None = None,
        policy: RetrievalPolicy = RetrievalPolicy(),
    ) -> None:
        self.index = index
        self.clap = clap
        self.policy = policy
        if clap is not None:
            if not _canonical(getattr(clap, "model_id", "")):
                raise SoundRetrievalError("CLAP adapter requires pinned model_id")
            model_hash = _canonical(getattr(clap, "model_hash", ""))
            if not re.fullmatch(r"[0-9a-f]{64}", model_hash):
                raise SoundRetrievalError("CLAP adapter requires a 64-hex model_hash")

    def _score(
        self,
        query: str,
        target: Mapping[str, Any],
        record: Mapping[str, Any],
    ) -> tuple[float, float, float | None]:
        metadata = _metadata_score(query, target, record)
        clap_score: float | None = None
        if self.clap is not None:
            clap_score = float(self.clap.score(query, str(record["path"])))
            if not math.isfinite(clap_score) or not -1.0 <= clap_score <= 1.0:
                raise SoundRetrievalError("CLAP score must be finite and in [-1, 1]")
            normalised_clap = (clap_score + 1.0) / 2.0
            weight = self.policy.metadata_weight + self.policy.clap_weight
            combined = (
                metadata * self.policy.metadata_weight
                + normalised_clap * self.policy.clap_weight
            ) / weight
        else:
            combined = metadata
        return combined, metadata, clap_score

    def retrieve(
        self,
        layer: Mapping[str, Any],
        *,
        excluded_asset_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        if not isinstance(layer, Mapping):
            raise SoundRetrievalError("sound layer must be a mapping")
        lane = _canonical(layer.get("lane"))
        if lane not in {"ambience", "presence", "events"}:
            raise SoundRetrievalError(f"unsupported natural sound lane: {lane!r}")
        taxonomy = layer.get("taxonomy")
        if not isinstance(taxonomy, Mapping):
            raise SoundRetrievalError("sound layer requires taxonomy")
        target = copy.deepcopy(dict(taxonomy))
        perspective = layer.get("perspective")
        if isinstance(perspective, Mapping) and perspective.get("distance"):
            target["distance"] = perspective["distance"]
        hard_negatives = [str(item) for item in layer.get("hard_negatives", [])]
        excluded = {str(item) for item in excluded_asset_ids}
        audit: list[dict[str, Any]] = []

        for level in _levels(target):
            query = _query_for_level(layer, level)
            ranked: list[tuple[float, str, dict[str, Any]]] = []
            for record in self.index.records(production_only=True):
                asset_id = str(record["asset_id"])
                if asset_id in excluded:
                    audit.append({"asset_id": asset_id, "level": level, "rejected": "excluded"})
                    continue
                hit = _hard_negative_hit(record, hard_negatives)
                if hit is not None:
                    audit.append(
                        {"asset_id": asset_id, "level": level, "rejected": "hard_negative", "value": hit}
                    )
                    continue
                allowed, reason = _taxonomy_allows(record, target, level)
                if not allowed:
                    audit.append({"asset_id": asset_id, "level": level, "rejected": reason})
                    continue
                if lane == "ambience" and float(record["duration"]) < float(layer["duration"]):
                    if float(record["quality"].get("loop_score", 0.0)) < self.policy.min_ambience_loop_score:
                        audit.append(
                            {"asset_id": asset_id, "level": level, "rejected": "unsafe_loop"}
                        )
                        continue
                score, metadata, clap_score = self._score(query, target, record)
                hard_negative_margin: float | None = None
                if self.clap is not None and hard_negatives and clap_score is not None:
                    negative_scores = [
                        float(self.clap.score(str(negative), str(record["path"])))
                        for negative in hard_negatives
                    ]
                    if any(not math.isfinite(value) or not -1.0 <= value <= 1.0 for value in negative_scores):
                        raise SoundRetrievalError(
                            "CLAP hard-negative score must be finite and in [-1, 1]"
                        )
                    hard_negative_margin = (clap_score - max(negative_scores)) / 2.0
                    if hard_negative_margin < self.policy.min_hard_negative_margin:
                        audit.append(
                            {
                                "asset_id": asset_id,
                                "level": level,
                                "rejected": "weak_hard_negative_margin",
                                "margin": round(hard_negative_margin, 6),
                            }
                        )
                        continue
                if metadata < self.policy.min_metadata_score:
                    audit.append(
                        {"asset_id": asset_id, "level": level, "rejected": "weak_metadata", "metadata_score": metadata}
                    )
                    continue
                row = {
                    "asset_id": asset_id,
                    "level": level,
                    "query": query,
                    "score": round(score, 6),
                    "metadata_score": round(metadata, 6),
                    "clap_score": None if clap_score is None else round(clap_score, 6),
                    "hard_negative_margin": (
                        None
                        if hard_negative_margin is None
                        else round(hard_negative_margin, 6)
                    ),
                }
                ranked.append((-score, asset_id, row))
            ranked.sort(key=lambda value: (value[0], value[1]))
            if ranked:
                winner = ranked[0][2]
                return {
                    "status": "resolved",
                    "degradation_level": level,
                    "query": query,
                    "asset_id": winner["asset_id"],
                    "asset": self.index.resolve_asset(winner["asset_id"]),
                    "score": winner["score"],
                    "metadata_score": winner["metadata_score"],
                    "clap_score": winner["clap_score"],
                    "hard_negative_margin": winner["hard_negative_margin"],
                    "index_fingerprint": self.index.fingerprint,
                    "retriever": {
                        "version": "sound-retrieval-v2.1",
                        "clap_model": getattr(self.clap, "model_id", None),
                        "clap_model_hash": getattr(self.clap, "model_hash", None),
                    },
                    "candidate_audit": (
                        audit + [item[2] for item in ranked[: self.policy.candidate_audit_limit]]
                    )[-self.policy.candidate_audit_limit :],
                    "retrieval_key": stable_cache_key(
                        "sound_retrieval",
                        {
                            "layer_search_key": layer.get("search_cache_key"),
                            "level": level,
                            "winner": winner,
                            "index": self.index.fingerprint,
                            "excluded": sorted(excluded),
                            "clap_model": getattr(self.clap, "model_id", None),
                            "clap_model_hash": getattr(self.clap, "model_hash", None),
                            "policy": {
                                "min_metadata_score": self.policy.min_metadata_score,
                                "metadata_weight": self.policy.metadata_weight,
                                "clap_weight": self.policy.clap_weight,
                                "min_hard_negative_margin": self.policy.min_hard_negative_margin,
                                "min_ambience_loop_score": self.policy.min_ambience_loop_score,
                            },
                        },
                        version=2,
                    ),
                }
        return {
            "status": "needs_sound",
            "degradation_level": "needs_sound",
            "query": str(layer.get("query_en") or ""),
            "index_fingerprint": self.index.fingerprint,
            "candidate_audit": audit[-self.policy.candidate_audit_limit :],
            "reason": "no candidate survived taxonomy, action, hard-negative, quality, and license gates",
        }

    def resolve_layer(
        self,
        layer: Mapping[str, Any],
        *,
        excluded_asset_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        result = self.retrieve(layer, excluded_asset_ids=excluded_asset_ids)
        updated = copy.deepcopy(dict(layer))
        updated["retrieval"] = {key: value for key, value in result.items() if key != "asset"}
        if result["status"] == "resolved":
            updated["asset"] = result["asset"]
        else:
            updated.pop("asset", None)
        return updated


def resolve_plan_natural_layers(
    plan: Mapping[str, Any],
    retriever: SoundRetriever,
    *,
    excluded_assets_by_layer: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Resolve only natural lanes and refresh the plan key without hiding misses."""

    if plan.get("schema_version") != SOUND_PLAN_SCHEMA_VERSION:
        raise SoundRetrievalError("only production-v2 plans have natural lanes")
    exclusions = excluded_assets_by_layer or {}
    updated = copy.deepcopy(dict(plan))
    unresolved = False
    for lane_name, id_key in (
        ("ambience_regions", "region_id"),
        ("presence_layers", "layer_id"),
        ("event_cues", "cue_id"),
    ):
        output = []
        for layer in updated.get(lane_name, []):
            layer_id = str(layer.get(id_key) or "")
            resolved = retriever.resolve_layer(
                layer, excluded_asset_ids=exclusions.get(layer_id, ())
            )
            unresolved = unresolved or resolved.get("retrieval", {}).get("status") != "resolved"
            output.append(resolved)
        updated[lane_name] = output
    music_resolved = all(
        isinstance(item.get("asset"), Mapping) for item in updated.get("music_segments", [])
    )
    updated["status"] = "needs_sound" if unresolved or not music_resolved else "ready"
    updated.pop("plan_cache_key", None)
    updated["plan_cache_key"] = stable_cache_key(
        "sound_plan", updated, version=SOUND_PLAN_SCHEMA_VERSION
    )
    return updated


__all__ = [
    "ClapAdapter",
    "RetrievalPolicy",
    "SoundRetrievalError",
    "SoundRetriever",
    "resolve_plan_natural_layers",
]
