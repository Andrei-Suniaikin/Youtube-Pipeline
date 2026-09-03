"""Bounded fail-closed repair orchestration for rejected timeline slots."""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .common import canonical_hash
from .state import JobState


class RepairExhausted(RuntimeError):
    pass


@dataclass(frozen=True)
class RepairRequest:
    slot_id: str
    reason_code: str
    failed_range: Mapping[str, Any]
    details: Mapping[str, Any]


def canonical_candidate_range(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return the portable source-range identity used by durable repair bans."""

    source_asset_id = str(candidate.get("source_asset_id") or "").strip()
    source_id = str(candidate.get("source_id") or "").strip()
    if not source_asset_id and not source_id:
        raise ValueError("candidate range has no portable source identity")
    try:
        source_in = float(candidate["source_in"])
        source_out = float(candidate["source_out"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("candidate range has invalid source_in/source_out") from exc
    if not math.isfinite(source_in) or not math.isfinite(source_out) or source_out <= source_in:
        raise ValueError("candidate range is empty or non-finite")
    output: dict[str, Any] = {
        "source_asset_id": source_asset_id or None,
        "source_id": source_id or None,
        "clip_id": str(candidate.get("clip_id") or "").strip() or None,
        "source_in": round(source_in, 9),
        "source_out": round(source_out, 9),
    }
    for key in ("source_frame_start", "source_frame_end", "clean_frame_start", "clean_frame_end"):
        value = candidate.get(key)
        if type(value) is int:
            output[key] = value
    return {key: value for key, value in output.items() if value is not None}


def candidate_ranges_overlap(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    *,
    tolerance: float = 0.08,
) -> bool:
    """Compare portable source ranges without consulting runtime mount paths."""

    first_asset = str(first.get("source_asset_id") or "").strip()
    second_asset = str(second.get("source_asset_id") or "").strip()
    first_source = str(first.get("source_id") or "").strip()
    second_source = str(second.get("source_id") or "").strip()
    if first_asset and second_asset:
        if first_asset != second_asset:
            return False
    elif first_source and second_source:
        if first_source != second_source:
            return False
    else:
        return False
    try:
        overlap = min(float(first["source_out"]), float(second["source_out"])) - max(
            float(first["source_in"]), float(second["source_in"])
        )
    except (KeyError, TypeError, ValueError):
        return False
    return overlap > max(0.0, float(tolerance))


def next_non_overlapping_candidate_batch(
    candidates: Sequence[Mapping[str, Any]],
    banned: Sequence[Mapping[str, Any]],
    *,
    batch_size: int,
    tolerance: float = 0.08,
) -> list[dict[str, Any]]:
    """Select the next stable candidate batch, excluding tried source ranges."""

    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    portable_bans = [canonical_candidate_range(row) for row in banned]
    selected: list[dict[str, Any]] = []
    selected_ranges: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in candidates:
        candidate = dict(raw)
        candidate_range = canonical_candidate_range(candidate)
        range_key = canonical_hash(candidate_range)
        if range_key in seen:
            continue
        if any(
            candidate_ranges_overlap(candidate_range, blocked, tolerance=tolerance)
            for blocked in portable_bans
        ):
            continue
        if any(
            candidate_ranges_overlap(candidate_range, previous, tolerance=tolerance)
            for previous in selected_ranges
        ):
            continue
        seen.add(range_key)
        selected.append(candidate)
        selected_ranges.append(candidate_range)
        if len(selected) >= batch_size:
            break
    return selected


def _deduplicate_ranges(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in values:
        try:
            row = canonical_candidate_range(raw)
        except ValueError:
            continue
        key = canonical_hash(row)
        if key not in seen:
            seen.add(key)
            output.append(row)
    return output


def _result_rejected_ranges(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    output: list[Mapping[str, Any]] = []
    rejected = result.get("rejected_range")
    if isinstance(rejected, Mapping):
        output.append(rejected)
    rejected_many = result.get("rejected_ranges")
    if isinstance(rejected_many, Sequence) and not isinstance(rejected_many, (str, bytes)):
        output.extend(row for row in rejected_many if isinstance(row, Mapping))
    return output


class RepairController:
    def __init__(self, state: JobState, *, max_attempts: int = 2) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.state = state
        self.max_attempts = max_attempts

    def attempts(self, run_id: str, slot_id: str) -> list[dict[str, Any]]:
        """Return durable, decoded repair receipts in attempt order."""

        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT attempt,reason_code,banned_json,result_json,created_at "
                "FROM repair_attempts WHERE run_id=? AND slot_id=? ORDER BY attempt",
                (run_id, slot_id),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            output.append(
                {
                    "attempt": int(row["attempt"]),
                    "reason_code": str(row["reason_code"]),
                    "banned": json.loads(row["banned_json"]),
                    "result": (
                        json.loads(row["result_json"])
                        if row["result_json"] is not None
                        else None
                    ),
                    "created_at": float(row["created_at"]),
                }
            )
        return output

    def repair(
        self,
        run_id: str,
        request: RepairRequest,
        handler: Callable[[int, tuple[Mapping[str, Any], ...]], Mapping[str, Any]],
    ) -> dict[str, Any]:
        initial_bans: list[Mapping[str, Any]] = [dict(request.failed_range)]
        details_bans = request.details.get("initial_banned_ranges")
        if isinstance(details_bans, Sequence) and not isinstance(details_bans, (str, bytes)):
            initial_bans.extend(
                row for row in details_bans if isinstance(row, Mapping)
            )
        banned = _deduplicate_ranges(initial_bans)
        request_hash = canonical_hash(
            {
                "slot_id": request.slot_id,
                "reason_code": request.reason_code,
                "failed_range": canonical_candidate_range(request.failed_range),
                "details": dict(request.details),
            }
        )
        for attempt in range(1, self.max_attempts + 1):
            with self.state.transaction() as connection:
                previous = connection.execute(
                    "SELECT result_json,banned_json FROM repair_attempts "
                    "WHERE run_id=? AND slot_id=? AND attempt=?",
                    (run_id, request.slot_id, attempt),
                ).fetchone()
                if previous and previous["result_json"]:
                    result = json.loads(previous["result_json"])
                    provenance = result.get("repair_provenance")
                    if not isinstance(provenance, Mapping) or str(
                        provenance.get("request_hash") or ""
                    ) != request_hash:
                        # A stage re-run may have a new effective-beat hash.  A
                        # slot/attempt primary key alone is not sufficient to
                        # resume evidence produced for the old request.
                        connection.execute(
                            "DELETE FROM repair_attempts "
                            "WHERE run_id=? AND slot_id=? AND attempt=?",
                            (run_id, request.slot_id, attempt),
                        )
                        previous = None
                    else:
                        if result.get("status") == "resolved":
                            return result
                        banned = _deduplicate_ranges(
                            [
                                *banned,
                                *json.loads(previous["banned_json"]),
                                *_result_rejected_ranges(result),
                            ]
                        )
                        continue
                banned_snapshot = _deduplicate_ranges(banned)
                connection.execute(
                    "INSERT OR REPLACE INTO repair_attempts"
                    "(run_id,slot_id,attempt,reason_code,banned_json,result_json,created_at) "
                    "VALUES(?,?,?,?,?,NULL,?)",
                    (run_id, request.slot_id, attempt, request.reason_code,
                     json.dumps(banned_snapshot, ensure_ascii=False, sort_keys=True), time.time()),
                )
            result = dict(handler(attempt, tuple(banned_snapshot)))
            status = str(result.get("status") or "unresolved")
            if status not in {"resolved", "unresolved", "retry"}:
                raise ValueError(f"invalid repair status: {status}")
            strategy = str(
                result.get("strategy")
                or ("next_candidate_batch" if attempt == 1 else "visual_resegmentation")
            )
            result["attempt"] = attempt
            result["strategy"] = strategy
            result["repair_provenance"] = {
                "schema_version": 1,
                "request_hash": request_hash,
                "slot_id": request.slot_id,
                "reason_code": request.reason_code,
                "attempt": attempt,
                "strategy": strategy,
                "banned_before": banned_snapshot,
                "banned_before_hash": canonical_hash(banned_snapshot),
            }
            with self.state.transaction() as connection:
                connection.execute(
                    "UPDATE repair_attempts SET result_json=? "
                    "WHERE run_id=? AND slot_id=? AND attempt=?",
                    (json.dumps(result, ensure_ascii=False, sort_keys=True),
                     run_id, request.slot_id, attempt),
                )
            if status == "resolved":
                return result
            banned = _deduplicate_ranges(
                [*banned_snapshot, *_result_rejected_ranges(result)]
            )
        raise RepairExhausted(
            f"slot {request.slot_id} unresolved after {self.max_attempts} attempts: "
            f"{request.reason_code}"
        )


__all__ = [
    "RepairController",
    "RepairExhausted",
    "RepairRequest",
    "candidate_ranges_overlap",
    "canonical_candidate_range",
    "next_non_overlapping_candidate_batch",
]
