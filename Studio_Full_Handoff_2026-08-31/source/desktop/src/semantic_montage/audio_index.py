"""Content-addressed index for a user-owned, commercially licensed audio library."""

from __future__ import annotations

import copy
import json
import math
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .common import atomic_write_json, sha256_file
from .sound import stable_cache_key, validate_asset_provenance


AUDIO_INDEX_SCHEMA_VERSION = 1


class AudioIndexError(ValueError):
    """A local asset is missing, tampered, unlicensed, or not classified."""


def _nonempty(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise AudioIndexError(f"{name} must be non-empty")
    return text


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise AudioIndexError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AudioIndexError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise AudioIndexError(f"{name} must be finite")
    return number


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(sorted(set(re.findall(r"[a-zа-яё0-9]+", value.casefold()))))


def _normalise_taxonomy(raw: Any, asset_id: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise AudioIndexError(f"audio asset {asset_id} requires taxonomy")
    result = copy.deepcopy(dict(raw))
    result["source_kind"] = _nonempty(
        result.get("source_kind"), f"audio asset {asset_id} taxonomy.source_kind"
    ).casefold()
    for key in ("family", "species", "action", "environment", "distance"):
        value = str(result.get(key) or "").strip().casefold()
        if value:
            result[key] = value
        else:
            result.pop(key, None)
    labels = result.get("labels", [])
    if not isinstance(labels, Sequence) or isinstance(labels, (str, bytes)):
        raise AudioIndexError(f"audio asset {asset_id} taxonomy.labels must be a list")
    result["labels"] = sorted(
        {str(item).strip().casefold() for item in labels if str(item).strip()}
    )
    return result


def _normalise_quality(raw: Any, asset_id: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise AudioIndexError(f"audio asset {asset_id} requires quality metadata")
    result = copy.deepcopy(dict(raw))
    for key in ("speech_probability", "music_probability", "noise_probability"):
        value = _finite(result.get(key), f"audio asset {asset_id} quality.{key}")
        if not 0.0 <= value <= 1.0:
            raise AudioIndexError(f"audio asset {asset_id} quality.{key} must be in [0, 1]")
        result[key] = value
    loop_score = _finite(result.get("loop_score", 0.0), f"audio asset {asset_id} loop_score")
    if not 0.0 <= loop_score <= 1.0:
        raise AudioIndexError(f"audio asset {asset_id} loop_score must be in [0, 1]")
    result["loop_score"] = loop_score
    result["clipping"] = bool(result.get("clipping", False))
    result["dc_offset"] = bool(result.get("dc_offset", False))
    result["classifier_version"] = _nonempty(
        result.get("classifier_version"),
        f"audio asset {asset_id} quality.classifier_version",
    )
    result["production_eligible"] = (
        result["speech_probability"] <= 0.05
        and result["music_probability"] <= 0.05
        and not result["clipping"]
        and not result["dc_offset"]
    )
    return result


class AudioIndex:
    """Immutable-in-practice deterministic metadata/search corpus.

    Audio embeddings are optional and are deliberately not computed here. A
    pinned CLAP adapter can consume records from :mod:`sound_retrieval`; the
    metadata fallback remains deterministic and independently testable.
    """

    def __init__(
        self,
        assets: Sequence[Mapping[str, Any]],
        *,
        probe_duration: Callable[[Path], float] | None = None,
        verify_files: bool = True,
    ) -> None:
        if isinstance(assets, (str, bytes)):
            raise AudioIndexError("audio asset catalog must be a list")
        self._records: dict[str, dict[str, Any]] = {}
        for raw in assets:
            record = self._normalise_record(
                raw, probe_duration=probe_duration, verify_files=verify_files
            )
            asset_id = record["asset_id"]
            if asset_id in self._records:
                raise AudioIndexError(f"duplicate audio asset_id: {asset_id}")
            self._records[asset_id] = record
        if not self._records:
            raise AudioIndexError("audio index requires at least one asset")
        material = [self._records[key] for key in sorted(self._records)]
        self._fingerprint = stable_cache_key(
            "audio_index", material, version=AUDIO_INDEX_SCHEMA_VERSION
        )

    @staticmethod
    def _normalise_record(
        raw: Mapping[str, Any],
        *,
        probe_duration: Callable[[Path], float] | None,
        verify_files: bool,
    ) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise AudioIndexError("audio asset entry must be a mapping")
        asset_id = _nonempty(raw.get("asset_id"), "audio asset_id")
        path = Path(_nonempty(raw.get("path"), f"audio asset {asset_id} path")).expanduser()
        if verify_files:
            path = path.resolve()
            if not path.is_file():
                raise AudioIndexError(f"audio asset does not exist: {path}")
            actual_sha = sha256_file(path)
        else:
            actual_sha = _nonempty(raw.get("sha256"), f"audio asset {asset_id} sha256")
        declared_sha = str(raw.get("sha256") or "").strip().casefold()
        if declared_sha and declared_sha != actual_sha:
            raise AudioIndexError(f"audio asset {asset_id} SHA-256 mismatch")
        if not re.fullmatch(r"[0-9a-f]{64}", actual_sha):
            raise AudioIndexError(f"audio asset {asset_id} SHA-256 is invalid")

        duration_raw = raw.get("duration")
        if duration_raw is None and probe_duration is not None and verify_files:
            duration_raw = probe_duration(path)
        duration = _finite(duration_raw, f"audio asset {asset_id} duration")
        if duration <= 0:
            raise AudioIndexError(f"audio asset {asset_id} duration must be positive")

        license_raw = raw.get("license")
        if not isinstance(license_raw, Mapping):
            raise AudioIndexError(f"audio asset {asset_id} requires license metadata")
        license_info = copy.deepcopy(dict(license_raw))
        if license_info.get("commercial_use_allowed") is not True:
            raise AudioIndexError(f"audio asset {asset_id} is not cleared for commercial use")
        _nonempty(license_info.get("license_type"), f"audio asset {asset_id} license_type")
        _nonempty(license_info.get("license_ref"), f"audio asset {asset_id} license_ref")

        taxonomy = _normalise_taxonomy(raw.get("taxonomy"), asset_id)
        quality = _normalise_quality(raw.get("quality"), asset_id)
        keywords_raw = raw.get("keywords", [])
        if not isinstance(keywords_raw, Sequence) or isinstance(keywords_raw, (str, bytes)):
            raise AudioIndexError(f"audio asset {asset_id} keywords must be a list")
        keywords = sorted(
            {str(item).strip().casefold() for item in keywords_raw if str(item).strip()}
        )
        title = str(raw.get("title") or path.stem).strip()
        description = str(raw.get("description") or "").strip()
        searchable = " ".join(
            [title, description, *keywords, *[str(value) for value in taxonomy.values()]]
        )
        provenance = {
            "provider": "local_licensed",
            "asset_id": asset_id,
            "source_ref": str(raw.get("source_ref") or path),
            "sha256": actual_sha,
            "acquired_at": _nonempty(
                raw.get("acquired_at"), f"audio asset {asset_id} acquired_at"
            ),
            "license": license_info,
            "taxonomy": taxonomy,
            "index_classifier_version": quality["classifier_version"],
        }
        render_asset = {
            "path": str(path),
            "duration": duration,
            "taxonomy": taxonomy,
            "quality": quality,
            "provenance": provenance,
        }
        validate_asset_provenance(render_asset, expected_provider="local_licensed")
        return {
            "asset_id": asset_id,
            "path": str(path),
            "duration": duration,
            "title": title,
            "description": description,
            "keywords": keywords,
            "taxonomy": taxonomy,
            "quality": quality,
            "provenance": provenance,
            "tokens": list(_tokens(searchable)),
            "embedding": copy.deepcopy(raw.get("embedding")),
        }

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def __len__(self) -> int:
        return len(self._records)

    def records(self, *, production_only: bool = False) -> list[dict[str, Any]]:
        output = []
        for asset_id in sorted(self._records):
            record = self._records[asset_id]
            if production_only and not record["quality"]["production_eligible"]:
                continue
            output.append(copy.deepcopy(record))
        return output

    def get(self, asset_id: str) -> dict[str, Any]:
        asset_id = _nonempty(asset_id, "asset_id")
        try:
            return copy.deepcopy(self._records[asset_id])
        except KeyError as exc:
            raise AudioIndexError(f"unknown audio asset_id: {asset_id}") from exc

    def resolve_asset(self, asset_id: str) -> dict[str, Any]:
        record = self.get(asset_id)
        if not record["quality"]["production_eligible"]:
            raise AudioIndexError(f"audio asset {asset_id} is not production eligible")
        return {
            "path": record["path"],
            "duration": record["duration"],
            "taxonomy": record["taxonomy"],
            "quality": record["quality"],
            "provenance": record["provenance"],
        }

    def to_manifest(self) -> dict[str, Any]:
        return {
            "schema_version": AUDIO_INDEX_SCHEMA_VERSION,
            "fingerprint": self.fingerprint,
            "assets": self.records(),
        }

    def write(self, path: str | Path) -> None:
        atomic_write_json(path, self.to_manifest())

    @classmethod
    def read(cls, path: str | Path, *, verify_files: bool = True) -> "AudioIndex":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping) or payload.get("schema_version") != AUDIO_INDEX_SCHEMA_VERSION:
            raise AudioIndexError("unsupported audio index manifest")
        raw_assets = payload.get("assets")
        if not isinstance(raw_assets, Sequence) or isinstance(raw_assets, (str, bytes)):
            raise AudioIndexError("audio index manifest assets must be a list")
        catalog = []
        for record in raw_assets:
            if not isinstance(record, Mapping):
                raise AudioIndexError("audio index manifest contains a non-mapping asset")
            provenance = record.get("provenance") or {}
            catalog.append(
                {
                    "asset_id": record.get("asset_id"),
                    "path": record.get("path"),
                    "duration": record.get("duration"),
                    "title": record.get("title"),
                    "description": record.get("description"),
                    "keywords": record.get("keywords", []),
                    "taxonomy": record.get("taxonomy"),
                    "quality": record.get("quality"),
                    "embedding": record.get("embedding"),
                    "source_ref": provenance.get("source_ref"),
                    "sha256": provenance.get("sha256"),
                    "acquired_at": provenance.get("acquired_at"),
                    "license": provenance.get("license"),
                }
            )
        index = cls(catalog, verify_files=verify_files)
        if payload.get("fingerprint") != index.fingerprint:
            raise AudioIndexError("audio index fingerprint is stale")
        return index


def index_local_audio(
    assets: Sequence[Mapping[str, Any]],
    *,
    output_path: str | Path | None = None,
    probe_duration: Callable[[Path], float] | None = None,
) -> AudioIndex:
    index = AudioIndex(assets, probe_duration=probe_duration)
    if output_path is not None:
        index.write(output_path)
    return index


__all__ = [
    "AUDIO_INDEX_SCHEMA_VERSION",
    "AudioIndex",
    "AudioIndexError",
    "index_local_audio",
]
