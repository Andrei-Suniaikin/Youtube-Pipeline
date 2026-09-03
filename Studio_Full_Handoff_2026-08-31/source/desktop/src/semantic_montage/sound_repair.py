"""Bounded fail-closed repair controller for production-v2 natural sound."""

from __future__ import annotations

import copy
from typing import Any, Mapping, MutableMapping, Sequence

from .sound import SOUND_PLAN_SCHEMA_VERSION, SoundPlanError, stable_cache_key
from .sound_retrieval import SoundRetriever


class SoundRepairError(SoundPlanError):
    """A sound failure cannot be repaired within the configured bound."""


_LANE_TO_PLAN_KEY = {
    "ambience_regions": ("ambience_regions", "region_id"),
    "presence_layers": ("presence_layers", "layer_id"),
    "event_cues": ("event_cues", "cue_id"),
    "ambience": ("ambience_regions", "region_id"),
    "presence": ("presence_layers", "layer_id"),
    "events": ("event_cues", "cue_id"),
}


def _asset_id(layer: Mapping[str, Any]) -> str | None:
    asset = layer.get("asset")
    provenance = asset.get("provenance") if isinstance(asset, Mapping) else None
    value = provenance.get("asset_id") if isinstance(provenance, Mapping) else None
    return str(value) if value else None


def _dedupe_actions(actions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for action in actions:
        identity = (str(action.get("lane")), str(action.get("layer_id")))
        if identity in seen:
            for existing in result:
                if (existing["lane"], existing["layer_id"]) == identity:
                    existing["reasons"] = sorted(
                        set(existing.get("reasons", [])) | set(action.get("reasons", []))
                    )
                    existing["excluded_asset_ids"] = sorted(
                        set(existing.get("excluded_asset_ids", []))
                        | set(action.get("excluded_asset_ids", []))
                    )
                    break
            continue
        seen.add(identity)
        result.append(copy.deepcopy(dict(action)))
    return result


def plan_sound_repairs(
    plan: Mapping[str, Any],
    qa_report: Mapping[str, Any],
    *,
    attempts: Mapping[str, int] | None = None,
    max_attempts: int = 3,
) -> list[dict[str, Any]]:
    """Translate QA blockers into explicit bounded repair actions."""

    if plan.get("schema_version") != SOUND_PLAN_SCHEMA_VERSION:
        raise SoundRepairError("sound repair requires production-v2 plan")
    if max_attempts < 1:
        raise SoundRepairError("max_attempts must be positive")
    attempt_state = attempts or {}
    actions: list[dict[str, Any]] = []

    def add(lane: str, layer_id: str, reason: str, asset_id: str | None = None) -> None:
        current = int(attempt_state.get(layer_id, 0))
        actions.append(
            {
                "operation": "retrieve_replacement",
                "lane": lane,
                "layer_id": layer_id,
                "reasons": [reason],
                "excluded_asset_ids": [asset_id] if asset_id else [],
                "attempt": current + 1,
                "terminal": current >= max_attempts,
            }
        )

    for issue_name in (
        "wrong_species",
        "wrong_family",
        "wrong_action",
        "wrong_environment",
        "hard_negative_hits",
        "missing_classification",
        "contamination",
        "provenance_errors",
        "unresolved_assets",
    ):
        for issue in qa_report.get(issue_name, []):
            if not isinstance(issue, Mapping):
                continue
            lane = str(issue.get("lane") or "")
            layer_id = str(issue.get("layer_id") or "")
            if lane and layer_id:
                add(lane, layer_id, issue_name, str(issue.get("asset_id") or "") or None)

    ambience_rows = plan.get("ambience_regions", [])
    for gap in ((qa_report.get("lanes") or {}).get("ambience") or {}).get("energy_gaps", []):
        if not isinstance(gap, Mapping):
            continue
        start, end = float(gap.get("start", 0.0)), float(gap.get("end", 0.0))
        for region in ambience_rows:
            if float(region.get("start", 0.0)) < end and start < float(region.get("end", 0.0)):
                add(
                    "ambience_regions",
                    str(region.get("region_id") or ""),
                    "ambience_energy_gap",
                    _asset_id(region),
                )

    coverage = ((qa_report.get("lanes") or {}).get("ambience") or {}).get("coverage") or {}
    if coverage.get("gaps") or coverage.get("overlaps"):
        actions.append(
            {
                "operation": "replan_ambience_regions",
                "lane": "ambience_regions",
                "layer_id": "__coverage__",
                "reasons": ["ambience_structural_coverage"],
                "excluded_asset_ids": [],
                "attempt": int(attempt_state.get("__coverage__", 0)) + 1,
                "terminal": int(attempt_state.get("__coverage__", 0)) >= max_attempts,
            }
        )
    return _dedupe_actions(actions)


def _find_layer(plan: MutableMapping[str, Any], lane: str, layer_id: str) -> tuple[list, int]:
    try:
        plan_key, id_key = _LANE_TO_PLAN_KEY[lane]
    except KeyError as exc:
        raise SoundRepairError(f"unknown repair lane: {lane}") from exc
    rows = plan.get(plan_key)
    if not isinstance(rows, list):
        raise SoundRepairError(f"plan lane {plan_key} is not a list")
    for index, row in enumerate(rows):
        if str(row.get(id_key) or "") == layer_id:
            return rows, index
    raise SoundRepairError(f"repair layer does not exist: {lane}/{layer_id}")


def repair_sound_plan(
    plan: Mapping[str, Any],
    qa_report: Mapping[str, Any],
    retriever: SoundRetriever,
    *,
    attempts: Mapping[str, int] | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Apply one repair round; structural coverage remains explicit/terminal."""

    attempt_state = dict(attempts or {})
    actions = plan_sound_repairs(
        plan, qa_report, attempts=attempt_state, max_attempts=max_attempts
    )
    updated = copy.deepcopy(dict(plan))
    trace: list[dict[str, Any]] = []
    terminal: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for action in actions:
        layer_id = str(action["layer_id"])
        if action["terminal"]:
            terminal.append(action)
            attempt_state[layer_id] = int(action["attempt"])
            continue
        if action["operation"] == "replan_ambience_regions":
            pending.append(action)
            attempt_state[layer_id] = int(action["attempt"])
            continue
        rows, index = _find_layer(updated, str(action["lane"]), layer_id)
        current = rows[index]
        exclusions = set(str(item) for item in action.get("excluded_asset_ids", []))
        current_asset_id = _asset_id(current)
        if current_asset_id:
            exclusions.add(current_asset_id)
        replacement = retriever.resolve_layer(
            current, excluded_asset_ids=sorted(exclusions)
        )
        attempt_state[layer_id] = int(action["attempt"])
        history = list(current.get("repair_history", []))
        history.append(
            {
                "attempt": int(action["attempt"]),
                "reasons": list(action["reasons"]),
                "excluded_asset_ids": sorted(exclusions),
                "result": replacement.get("retrieval", {}).get("status"),
                "replacement_asset_id": _asset_id(replacement),
            }
        )
        replacement["repair_history"] = history
        rows[index] = replacement
        trace.append(history[-1] | {"layer_id": layer_id, "lane": action["lane"]})
        if replacement.get("retrieval", {}).get("status") != "resolved":
            unresolved_action = {
                **action,
                "terminal": int(action["attempt"]) >= max_attempts,
                "reason": replacement.get("retrieval", {}).get("reason"),
            }
            if unresolved_action["terminal"]:
                terminal.append(unresolved_action)
            else:
                pending.append(unresolved_action)

    natural_resolved = all(
        isinstance(layer.get("asset"), Mapping)
        and layer.get("retrieval", {}).get("status") == "resolved"
        for lane in ("ambience_regions", "presence_layers", "event_cues")
        for layer in updated.get(lane, [])
    )
    music_resolved = all(
        isinstance(segment.get("asset"), Mapping)
        for segment in updated.get("music_segments", [])
    )
    updated["status"] = (
        "ready"
        if natural_resolved and music_resolved and not terminal and not pending
        else "needs_sound"
    )
    updated.pop("plan_cache_key", None)
    updated["plan_cache_key"] = stable_cache_key(
        "sound_plan", updated, version=SOUND_PLAN_SCHEMA_VERSION
    )
    return {
        "plan": updated,
        "attempts": attempt_state,
        "actions": actions,
        "trace": trace,
        "pending_unresolved": pending,
        "terminal_unresolved": terminal,
        "status": (
            "terminal_unresolved"
            if terminal
            else "needs_retry"
            if pending
            else "repaired"
        ),
    }


class SoundRepairController:
    def __init__(self, retriever: SoundRetriever, *, max_attempts: int = 3) -> None:
        if max_attempts < 1:
            raise SoundRepairError("max_attempts must be positive")
        self.retriever = retriever
        self.max_attempts = max_attempts
        self.attempts: dict[str, int] = {}

    def repair(self, plan: Mapping[str, Any], qa_report: Mapping[str, Any]) -> dict[str, Any]:
        result = repair_sound_plan(
            plan,
            qa_report,
            self.retriever,
            attempts=self.attempts,
            max_attempts=self.max_attempts,
        )
        self.attempts = dict(result["attempts"])
        return result


__all__ = [
    "SoundRepairController",
    "SoundRepairError",
    "plan_sound_repairs",
    "repair_sound_plan",
]
