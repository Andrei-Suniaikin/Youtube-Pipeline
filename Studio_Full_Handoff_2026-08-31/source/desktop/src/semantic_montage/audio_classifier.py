"""Portable, fail-closed zero-shot classification for the production audio catalog.

The durable outputs of this module deliberately contain no runtime paths.  A
sanitised catalog keeps content hashes and relative canonical paths, while the
mapping is keyed by the provider identity already present in that catalog.

Microsoft CLAP probabilities below are *relative zero-shot softmax scores* over
versioned prompt banks.  They are not represented as calibrated confidence.
Signal measurements (clipping, DC offset, and loop seam quality) are computed
independently from decoded PCM over the complete asset.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import math
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from .common import atomic_write_json, canonical_hash, sha256_file


AUDIO_CLASSIFIER_SCHEMA_VERSION = 1
PORTABLE_CATALOG_SCHEMA_VERSION = 1
PROMPT_SET_VERSION = "semantic-audio-zero-shot-prompts-v1"
CLASSIFIER_VERSION = "microsoft-msclap-1.3.4-clap-2023-prompts-v1"
MODEL_PACKAGE = "msclap"
MODEL_PACKAGE_VERSION = "1.3.4"
MODEL_REPOSITORY = "microsoft/msclap"
MODEL_VERSION = "2023"
MODEL_WEIGHT_FILE = "CLAP_weights_2023.pth"
MODEL_SAMPLE_RATE = 44_100
MODEL_WINDOW_SECONDS = 7.0
SIGNAL_SAMPLE_RATE = 48_000


QUALITY_PROMPTS: dict[str, str] = {
    "speech": "human speech, narration, or conversation",
    "music": "music, an instrumental score, or a song",
    "natural_environment": "continuous natural environmental ambience outdoors",
    "animal": "an animal vocalization or wildlife sound",
    "foley": "footsteps, movement, or a close natural foley sound",
    "technical_noise": "unwanted recording noise, hiss, hum, static, or distortion",
    "silence": "silence or an almost silent recording",
}

TAXONOMY_PROMPTS: dict[str, str] = {
    "music.documentary": "instrumental cinematic documentary music",
    "environment.jungle": "natural tropical jungle ambience with birds and insects",
    "environment.savanna": "natural open African savanna ambience",
    "environment.wetland": "natural wetland ambience with shallow water and wildlife",
    "environment.waterfall": "a large continuous natural waterfall",
    "environment.other": "another natural outdoor environmental ambience",
    "animal.primate": "a monkey or other primate vocalization",
    "animal.bird": "a bird call or bird wing sound",
    "animal.elephant": "an elephant trumpet or elephant vocalization",
    "animal.hippopotamus": "a hippopotamus grunt, snort, or vocalization",
    "animal.other": "another animal vocalization",
    "foley.footsteps": "natural footsteps through grass or vegetation",
    "foley.other": "another close natural foley movement sound",
    "noise.technical": "unwanted technical recording noise or distortion",
    "silence": "silence or an almost silent recording",
}

PROMPT_GROUPS: dict[str, dict[str, str]] = {
    "quality": QUALITY_PROMPTS,
    "taxonomy": TAXONOMY_PROMPTS,
}

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_HOST_PATH_FRAGMENT_RE = re.compile(r"(?:/Users/|/private/|/tmp/|file://)")
_DROP_PORTABLE_KEYS = {
    "asset_root",
    "source_inventory",
    "original_locations",
    "absolute_path",
    "local_path",
    "download_path",
}


class AudioClassifierError(RuntimeError):
    """The classifier or its portable output contract failed."""


class ZeroShotAudioClassifier(Protocol):
    """Small injectable boundary used by the real CLAP adapter and tests."""

    @property
    def metadata(self) -> Mapping[str, Any]: ...

    def score_windows(
        self,
        window_paths: Sequence[Path],
        prompt_groups: Mapping[str, Mapping[str, str]],
    ) -> Mapping[str, Mapping[str, float]]: ...


def _is_host_path(value: str) -> bool:
    text = value.strip()
    return bool(
        text.startswith("/")
        or _WINDOWS_PATH_RE.match(text)
        or _HOST_PATH_FRAGMENT_RE.search(text)
    )


def _portable_clone(value: Any, *, key: str = "") -> Any:
    """Deep-copy JSON data while dropping machine-local location evidence."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            name = str(raw_key)
            if name.casefold() in _DROP_PORTABLE_KEYS:
                continue
            cloned = _portable_clone(raw_value, key=name)
            if cloned is not _OMIT:
                result[name] = cloned
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            cloned = _portable_clone(item, key=key)
            if cloned is not _OMIT:
                result.append(cloned)
        return result
    if isinstance(value, str) and _is_host_path(value):
        return _OMIT
    return copy.deepcopy(value)


_OMIT = object()


def make_portable_catalog(catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Return a mount-independent catalog suitable for content hashing."""

    if not isinstance(catalog, Mapping):
        raise AudioClassifierError("catalog must be a mapping")
    portable = _portable_clone(catalog)
    if not isinstance(portable, dict):
        raise AudioClassifierError("catalog sanitisation failed")
    rows = portable.get("assets")
    if not isinstance(rows, list) or not rows:
        raise AudioClassifierError("catalog requires a non-empty assets list")
    portable["portable_catalog"] = {
        "schema_version": PORTABLE_CATALOG_SCHEMA_VERSION,
        "sanitizer": "drop-machine-local-location-evidence-v1",
        "binding_basis": "exact_serialized_portable_catalog_sha256",
    }
    assert_no_host_paths(portable, label="portable catalog")
    return portable


def assert_no_host_paths(value: Any, *, label: str = "artifact") -> None:
    """Reject absolute filesystem locations anywhere in a portable artifact."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in {"path", "absolute_path", "local_path"}:
                raise AudioClassifierError(f"{label} contains forbidden path key: {key}")
            assert_no_host_paths(item, label=label)
        return
    if isinstance(value, list):
        for item in value:
            assert_no_host_paths(item, label=label)
        return
    if isinstance(value, str) and _is_host_path(value):
        raise AudioClassifierError(f"{label} contains a host path")


def _portable_message(error: BaseException | str) -> str:
    """Keep diagnostics useful without leaking a workstation path."""

    text = str(error).replace("\n", " ").strip()
    text = re.sub(r"(?:/Users|/private|/tmp)/[^ ]+", "<host-path>", text)
    text = re.sub(r"[A-Za-z]:[\\/][^ ]+", "<host-path>", text)
    return text[:1200] or type(error).__name__


def _finite_probability(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise AudioClassifierError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AudioClassifierError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise AudioClassifierError(f"{name} must be in [0, 1]")
    return number


def _softmax(values: Sequence[float]) -> list[float]:
    """Stable reference softmax, also used by focused unit tests."""

    if not values:
        raise AudioClassifierError("softmax requires at least one value")
    maximum = max(float(item) for item in values)
    exponentials = [math.exp(float(item) - maximum) for item in values]
    total = sum(exponentials)
    if not math.isfinite(total) or total <= 0.0:
        raise AudioClassifierError("softmax produced an invalid normalizer")
    return [item / total for item in exponentials]


class MSCLAPZeroShotClassifier:
    """Official Microsoft CLAP 2023 zero-shot adapter, pinned to msclap 1.3.4."""

    def __init__(self, *, model_path: str | Path | None = None) -> None:
        installed = importlib.metadata.version(MODEL_PACKAGE)
        if installed != MODEL_PACKAGE_VERSION:
            raise AudioClassifierError(
                f"requires {MODEL_PACKAGE}=={MODEL_PACKAGE_VERSION}, found {installed}"
            )
        from msclap import CLAP  # type: ignore[import-not-found]

        kwargs: dict[str, Any] = {"version": MODEL_VERSION, "use_cuda": False}
        if model_path is not None:
            kwargs["model_fp"] = str(model_path)
        self._model = CLAP(**kwargs)
        weight_path = Path(self._model.model_fp)
        if not weight_path.is_file():
            raise AudioClassifierError("Microsoft CLAP model weight is missing")
        revision = weight_path.parent.name
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            revision = "unrecorded-local-cache-revision"
        config_sha = hashlib.sha256(self._model.config_as_str.encode("utf-8")).hexdigest()
        packages = {}
        for name in ("torch", "torchaudio", "transformers", "torchlibrosa"):
            try:
                packages[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                packages[name] = "unknown"
        self._metadata = {
            "provider": "Microsoft",
            "package": MODEL_PACKAGE,
            "package_version": installed,
            "model_repository": MODEL_REPOSITORY,
            "model_revision": revision,
            "model_version": MODEL_VERSION,
            "weight_file": MODEL_WEIGHT_FILE,
            "weight_sha256": sha256_file(weight_path),
            "config_sha256": config_sha,
            "tokenizer": str(self._model.args.text_model),
            "sample_rate_hz": int(self._model.args.sampling_rate),
            "window_seconds": float(self._model.args.duration),
            "runtime_packages": packages,
            "device": "cpu",
        }

    @property
    def metadata(self) -> Mapping[str, Any]:
        return copy.deepcopy(self._metadata)

    def score_windows(
        self,
        window_paths: Sequence[Path],
        prompt_groups: Mapping[str, Mapping[str, str]],
    ) -> Mapping[str, Mapping[str, float]]:
        if not window_paths:
            raise AudioClassifierError("CLAP requires at least one deterministic window")
        import torch

        audio_embeddings = self._model.get_audio_embeddings(
            [str(path) for path in window_paths], resample=False
        )
        output: dict[str, dict[str, float]] = {}
        for group_name, prompts in prompt_groups.items():
            labels = list(prompts)
            if not labels:
                raise AudioClassifierError(f"empty prompt group: {group_name}")
            text_embeddings = self._model.get_text_embeddings(
                [str(prompts[label]) for label in labels]
            )
            logits = self._model.compute_similarity(audio_embeddings, text_embeddings)
            probabilities = torch.softmax(logits, dim=1).mean(dim=0)
            values = probabilities.detach().cpu().tolist()
            scores = {
                label: round(_finite_probability(value, label), 10)
                for label, value in zip(labels, values, strict=True)
            }
            output[str(group_name)] = scores
        return output


def _signal_loop_score(samples: Any, sample_rate: int) -> float:
    import numpy as np

    values = np.asarray(samples, dtype=np.float32).reshape(-1)
    if values.size < 16 or sample_rate <= 0:
        return 0.0
    edge_size = min(int(round(sample_rate * 0.5)), values.size // 4)
    if edge_size < 8:
        return 0.0
    head = values[:edge_size].astype(np.float64)
    tail = values[-edge_size:].astype(np.float64)
    head -= head.mean()
    tail -= tail.mean()
    head_rms = float(np.sqrt(np.mean(head * head)))
    tail_rms = float(np.sqrt(np.mean(tail * tail)))
    if max(head_rms, tail_rms) < 1e-8:
        return 0.0
    level_similarity = min(head_rms, tail_rms) / max(head_rms, tail_rms, 1e-12)
    window = np.hanning(edge_size)
    head_spectrum = np.abs(np.fft.rfft(head * window))
    tail_spectrum = np.abs(np.fft.rfft(tail * window))
    denominator = float(np.linalg.norm(head_spectrum) * np.linalg.norm(tail_spectrum))
    spectral_similarity = (
        float(np.dot(head_spectrum, tail_spectrum) / denominator)
        if denominator > 1e-12
        else 0.0
    )
    seam_scale = max((head_rms + tail_rms) * 0.5, 1e-8)
    seam_similarity = math.exp(-abs(float(tail[-1] - head[0])) / seam_scale)
    score = 0.50 * spectral_similarity + 0.30 * level_similarity + 0.20 * seam_similarity
    return round(max(0.0, min(1.0, score)), 10)


def compute_signal_quality(samples: Any, sample_rate: int) -> dict[str, Any]:
    """Measure full-file PCM contamination and endpoint loop compatibility."""

    import numpy as np

    values = np.asarray(samples, dtype=np.float32).reshape(-1)
    if values.size == 0:
        raise AudioClassifierError("decoded PCM is empty")
    if not np.isfinite(values).all():
        raise AudioClassifierError("decoded PCM contains non-finite samples")
    clipping_ratio = float(np.count_nonzero(np.abs(values) >= 0.999) / values.size)
    dc_mean = float(values.mean(dtype=np.float64))
    clipping = clipping_ratio > 1e-5
    dc_offset = abs(dc_mean) > 0.01
    return {
        "clipping_ratio": round(clipping_ratio, 12),
        "clipping": clipping,
        "clipping_sample_threshold": 0.999,
        "clipping_ratio_threshold": 1e-5,
        "dc_offset": dc_offset,
        "dc_offset_mean": round(dc_mean, 12),
        "dc_offset_abs_threshold": 0.01,
        "loop_score": _signal_loop_score(values, sample_rate),
        "signal_sample_rate_hz": int(sample_rate),
        "signal_sample_count": int(values.size),
    }


def _decode_full_signal(path: Path, *, ffmpeg_bin: str, timeout: float) -> Any:
    process = subprocess.run(
        [
            ffmpeg_bin,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-ac",
            "1",
            "-ar",
            str(SIGNAL_SAMPLE_RATE),
            "-f",
            "f32le",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if process.returncode:
        message = process.stderr.decode("utf-8", errors="replace")[-1200:]
        raise AudioClassifierError(f"ffmpeg PCM decode failed: {message}")
    import numpy as np

    return np.frombuffer(process.stdout, dtype="<f4")


def _window_starts(duration: float) -> list[float]:
    if duration <= MODEL_WINDOW_SECONDS:
        return [0.0]
    span = max(0.0, duration - MODEL_WINDOW_SECONDS)
    starts = [0.10 * span, 0.50 * span, 0.90 * span]
    result: list[float] = []
    for item in starts:
        rounded = round(item, 6)
        if not result or abs(rounded - result[-1]) > 1e-4:
            result.append(rounded)
    return result


def _extract_windows(
    source: Path,
    duration: float,
    directory: Path,
    *,
    ffmpeg_bin: str,
) -> tuple[list[Path], list[float]]:
    starts = _window_starts(duration)
    output: list[Path] = []
    for index, start in enumerate(starts):
        target = directory / f"window-{index:02d}.wav"
        process = subprocess.run(
            [
                ffmpeg_bin,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{start:.6f}",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-t",
                f"{MODEL_WINDOW_SECONDS:.6f}",
                "-ac",
                "1",
                "-ar",
                str(MODEL_SAMPLE_RATE),
                "-c:a",
                "pcm_s16le",
                "-y",
                str(target),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=max(60.0, MODEL_WINDOW_SECONDS * 5.0),
            check=False,
        )
        if process.returncode or not target.is_file() or target.stat().st_size <= 44:
            message = process.stderr.decode("utf-8", errors="replace")[-1200:]
            raise AudioClassifierError(f"ffmpeg window extraction failed: {message}")
        output.append(target)
    return output, starts


def _provider_asset_id(row: Mapping[str, Any]) -> str:
    provenance = row.get("provenance")
    if not isinstance(provenance, Mapping):
        provenance = {}
    value = str(provenance.get("provider_asset_id") or row.get("asset_id") or "").strip()
    if not value:
        raise AudioClassifierError("catalog asset is missing provider_asset_id")
    return value


def _duration(row: Mapping[str, Any]) -> float:
    media = row.get("media")
    if not isinstance(media, Mapping):
        media = {}
    try:
        value = float(row.get("duration") or media.get("duration_seconds") or 0.0)
    except (TypeError, ValueError) as exc:
        raise AudioClassifierError("catalog asset duration is invalid") from exc
    if not math.isfinite(value) or value <= 0.0:
        raise AudioClassifierError("catalog asset duration must be positive")
    return value


def _catalog_asset_path(catalog_root: Path, row: Mapping[str, Any]) -> Path:
    relative = str(row.get("canonical_path") or "").strip()
    if not relative or Path(relative).is_absolute():
        raise AudioClassifierError("canonical_path must be relative")
    root = catalog_root.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise AudioClassifierError("canonical_path escapes catalog root") from exc
    if not path.is_file():
        raise AudioClassifierError("catalog asset file is missing")
    expected = str(row.get("sha256") or "").strip().casefold()
    if not _SHA256_RE.fullmatch(expected) or sha256_file(path) != expected:
        raise AudioClassifierError("catalog asset SHA-256 mismatch")
    return path


def _catalog_taxonomy(row: Mapping[str, Any]) -> dict[str, Any]:
    semantic = row.get("semantic")
    if not isinstance(semantic, Mapping):
        semantic = {}
    raw_tags = semantic.get("taxonomy")
    tags = (
        [str(item).strip().casefold() for item in raw_tags if str(item).strip()]
        if isinstance(raw_tags, Sequence) and not isinstance(raw_tags, (str, bytes))
        else []
    )
    domain = str(semantic.get("domain") or row.get("kind") or "").strip().casefold()
    source_kind = {
        "ambience": "environment",
        "sfx": "foley",
    }.get(domain, domain)
    if source_kind not in {"music", "environment", "animal", "foley"}:
        source_kind = "other"
    result: dict[str, Any] = {"source_kind": source_kind, "labels": sorted(set(tags))}
    for tag in tags:
        parts = tag.split(".")
        if tag.startswith("animal.") and len(parts) >= 3:
            result.setdefault("family", parts[1].replace("_", " "))
            result.setdefault("species", parts[-1].replace("_", " "))
        elif tag.startswith("environment.") and len(parts) >= 2:
            environment = parts[1].replace("_", " ")
            if environment == "african savanna":
                environment = "savanna"
            if " or " not in environment:
                result.setdefault("environment", environment)
        elif tag.startswith("action.") and len(parts) >= 2:
            result["action"] = parts[1].replace("_", " ")
        elif tag == "foley.footsteps":
            result["action"] = "footstep"
        elif tag == "vocalization.trumpet":
            result["action"] = "trumpeting"
        elif tag == "vocalization.call":
            result["action"] = "calling"
        elif tag.startswith("distance.") and len(parts) >= 2:
            distance = parts[1].replace("_", " ")
            if distance != "unspecified":
                result["distance"] = distance
    action_tags = sorted(
        {
            tag.split(".", 1)[1]
            for tag in tags
            if tag.startswith("action.") and len(tag.split(".", 1)) == 2
        }
    )
    if len(action_tags) == 1:
        from .catalog_resolver import FOREGROUND_PHYSICAL_ACTIONS

        if action_tags[0] in FOREGROUND_PHYSICAL_ACTIONS:
            # These versioned physical profiles are exact cross-stage IDs,
            # not free text.  Preserve their underscores so resolver matching
            # stays byte-identical to the causal obligation.
            result["action"] = action_tags[0]
    foreground_contract = str(
        semantic.get("foreground_action_contract") or ""
    ).strip()
    if foreground_contract:
        # This is a narrow semantic identity supplied by the licensed source
        # inventory, not a label inferred by CLAP.  Keep it only when the
        # catalog row names one exact physical action and does not also claim
        # a biome/environment family.
        from .catalog_resolver import (
            FOREGROUND_ENVIRONMENT_ACTION_CONTRACT,
            FOREGROUND_ENVIRONMENT_ACTIONS,
        )

        if foreground_contract != FOREGROUND_ENVIRONMENT_ACTION_CONTRACT:
            raise AudioClassifierError("unsupported foreground action contract")
        if source_kind != "environment":
            raise AudioClassifierError(
                "foreground environment action requires environment source_kind"
            )
        if any(tag.startswith("environment.") for tag in tags):
            raise AudioClassifierError(
                "foreground environment action must not claim an ambience family"
            )
        if len(action_tags) != 1 or action_tags[0] not in FOREGROUND_ENVIRONMENT_ACTIONS:
            raise AudioClassifierError(
                "foreground environment action requires one allowlisted action tag"
            )
        result["action"] = action_tags[0]
        result["foreground_action_contract"] = foreground_contract
    result["catalog_basis"] = str(semantic.get("basis") or "catalog_explicit_tags")
    return result


def _source_kind_scores(scores: Mapping[str, float]) -> dict[str, float]:
    output = {name: 0.0 for name in ("music", "environment", "animal", "foley", "noise", "silence")}
    for label, value in scores.items():
        prefix = str(label).split(".", 1)[0]
        if prefix in output:
            output[prefix] += float(value)
    return {key: round(value, 10) for key, value in output.items()}


def _classified_taxonomy(
    row: Mapping[str, Any], scores: Mapping[str, float]
) -> dict[str, Any]:
    result = _catalog_taxonomy(row)
    checked = {
        str(key): round(_finite_probability(value, f"taxonomy.{key}"), 10)
        for key, value in scores.items()
    }
    if set(checked) != set(TAXONOMY_PROMPTS):
        raise AudioClassifierError("classifier returned an incomplete taxonomy prompt bank")
    source_scores = _source_kind_scores(checked)
    top_label = max(checked, key=checked.get)
    result.update(
        {
            "classifier_scores": checked,
            "classifier_source_kind_scores": source_scores,
            "classifier_top_label": top_label,
            "classifier_agrees_with_catalog_domain": (
                top_label.split(".", 1)[0] == result["source_kind"]
            ),
            "classifier_probability_semantics": "mean_window_relative_softmax_not_calibrated",
            "classifier_prompt_set_version": PROMPT_SET_VERSION,
        }
    )
    return result


def _classified_quality(
    scores: Mapping[str, float], signal: Mapping[str, Any], starts: Sequence[float]
) -> dict[str, Any]:
    checked = {
        str(key): round(_finite_probability(value, f"quality.{key}"), 10)
        for key, value in scores.items()
    }
    if set(checked) != set(QUALITY_PROMPTS):
        raise AudioClassifierError("classifier returned an incomplete quality prompt bank")
    result = dict(signal)
    result.update(
        {
            "speech_probability": checked["speech"],
            "music_probability": checked["music"],
            "noise_probability": checked["technical_noise"],
            "classifier_scores": checked,
            "classifier_version": CLASSIFIER_VERSION,
            "classifier_prompt_set_version": PROMPT_SET_VERSION,
            "probability_semantics": "mean_window_relative_softmax_not_calibrated",
            "classification_window_seconds": MODEL_WINDOW_SECONDS,
            "classification_window_starts_seconds": [round(float(item), 6) for item in starts],
            "short_asset_policy": "model_native_repeat_to_seven_seconds",
            "production_eligible": (
                checked["speech"] <= 0.05
                and checked["music"] <= 0.05
                and not bool(signal["clipping"])
                and not bool(signal["dc_offset"])
            ),
        }
    )
    return result


def _mapping_binding(portable_catalog_path: Path, catalog: Mapping[str, Any]) -> dict[str, Any]:
    rows = catalog.get("assets")
    if not isinstance(rows, list):
        raise AudioClassifierError("portable catalog assets must be a list")
    manifest = [
        {
            "provider_asset_id": _provider_asset_id(row),
            "sha256": str(row.get("sha256") or "").casefold(),
            "canonical_path": str(row.get("canonical_path") or ""),
        }
        for row in rows
        if isinstance(row, Mapping)
    ]
    manifest.sort(key=lambda item: item["provider_asset_id"])
    return {
        "catalog_id": str(catalog.get("catalog_id") or "portable-audio-catalog"),
        "portable_catalog_sha256": sha256_file(portable_catalog_path),
        "asset_manifest_sha256": canonical_hash(manifest),
        "asset_count": len(manifest),
    }


def validate_portable_mapping(
    mapping: Mapping[str, Any],
    portable_catalog: Mapping[str, Any],
    *,
    portable_catalog_sha256: str,
) -> None:
    """Validate completeness, catalog binding, metric shape, and portability."""

    if mapping.get("schema_version") != AUDIO_CLASSIFIER_SCHEMA_VERSION:
        raise AudioClassifierError("unsupported audio mapping schema_version")
    assert_no_host_paths(mapping, label="audio mapping")
    binding = mapping.get("catalog_binding")
    if not isinstance(binding, Mapping):
        raise AudioClassifierError("mapping requires catalog_binding")
    if binding.get("portable_catalog_sha256") != portable_catalog_sha256:
        raise AudioClassifierError("mapping portable catalog SHA-256 mismatch")
    rows = portable_catalog.get("assets")
    if not isinstance(rows, list):
        raise AudioClassifierError("portable catalog assets must be a list")
    expected = {
        _provider_asset_id(row): str(row.get("sha256") or "").casefold()
        for row in rows
        if isinstance(row, Mapping)
    }
    if int(binding.get("asset_count") or -1) != len(expected):
        raise AudioClassifierError("mapping catalog asset count mismatch")
    assets = mapping.get("assets")
    if not isinstance(assets, Mapping):
        raise AudioClassifierError("mapping assets must be a mapping")
    if mapping.get("status") == "needs_classifier":
        if assets:
            raise AudioClassifierError("needs_classifier mapping must not expose partial assets")
        return
    if mapping.get("status") != "ready" or set(assets) != set(expected):
        raise AudioClassifierError("ready mapping must cover every catalog asset exactly")
    for provider_id, expected_sha in expected.items():
        value = assets[provider_id]
        if not isinstance(value, Mapping):
            raise AudioClassifierError(f"invalid mapping row: {provider_id}")
        if value.get("sha256") != expected_sha:
            raise AudioClassifierError(f"mapping SHA-256 mismatch: {provider_id}")
        asset_id = str(value.get("asset_id") or "")
        if asset_id != f"audio-{expected_sha[:24]}":
            raise AudioClassifierError(f"mapping project asset_id mismatch: {provider_id}")
        taxonomy = value.get("taxonomy")
        quality = value.get("quality")
        if not isinstance(taxonomy, Mapping) or not str(taxonomy.get("source_kind") or ""):
            raise AudioClassifierError(f"mapping taxonomy missing: {provider_id}")
        if not isinstance(quality, Mapping):
            raise AudioClassifierError(f"mapping quality missing: {provider_id}")
        for key in ("speech_probability", "music_probability", "noise_probability", "loop_score"):
            _finite_probability(quality.get(key), f"{provider_id}.{key}")
        if not isinstance(quality.get("clipping"), bool):
            raise AudioClassifierError(f"mapping clipping flag invalid: {provider_id}")
        if not isinstance(quality.get("dc_offset"), bool):
            raise AudioClassifierError(f"mapping dc_offset flag invalid: {provider_id}")
        if str(quality.get("classifier_version") or "") != CLASSIFIER_VERSION:
            raise AudioClassifierError(f"mapping classifier_version mismatch: {provider_id}")


def _validate_through_catalog_resolver(
    portable_catalog: Mapping[str, Any],
    *,
    catalog_root: Path,
    assets: Mapping[str, Any],
) -> dict[str, Any]:
    from .catalog_resolver import resolve_sound_plan_from_catalog

    probe_plan = {
        "schema_version": 2,
        "status": "needs_sound",
        "timeline_duration": 1.0,
        "timeline_fingerprint": "classifier-catalog-contract-probe",
        "music_segments": [],
        "ambience_regions": [],
        "presence_layers": [],
        "event_cues": [],
        "acoustic_scenes": [],
    }
    resolved = resolve_sound_plan_from_catalog(
        probe_plan,
        portable_catalog,
        catalog_root=catalog_root,
        asset_mapping=assets,
    )
    diagnostics = resolved.get("resolution_diagnostics")
    rejections = (
        diagnostics.get("catalog_rejections", [])
        if isinstance(diagnostics, Mapping)
        else [{"reason_code": "resolver_missing_diagnostics"}]
    )
    if rejections:
        raise AudioClassifierError(
            "catalog_resolver rejected classifier mapping: "
            + json.dumps(rejections, ensure_ascii=False, sort_keys=True)
        )
    return {
        "status": "validated",
        "resolver_version": diagnostics.get("resolver_version") if isinstance(diagnostics, Mapping) else None,
        "catalog_fingerprint": diagnostics.get("catalog_fingerprint") if isinstance(diagnostics, Mapping) else None,
        "catalog_rejections": [],
    }


def _requested_classifier_metadata() -> dict[str, Any]:
    return {
        "provider": "Microsoft",
        "package": MODEL_PACKAGE,
        "package_version": MODEL_PACKAGE_VERSION,
        "model_repository": MODEL_REPOSITORY,
        "model_version": MODEL_VERSION,
        "weight_file": MODEL_WEIGHT_FILE,
        "prompt_set_version": PROMPT_SET_VERSION,
        "prompt_set_sha256": canonical_hash(PROMPT_GROUPS),
    }


def _failure_artifacts(
    *,
    output_dir: Path,
    binding: Mapping[str, Any],
    classifier_metadata: Mapping[str, Any],
    stage: str,
    error: BaseException,
) -> dict[str, Any]:
    diagnostic = {
        "code": "needs_classifier",
        "stage": stage,
        "error_type": type(error).__name__,
        "message": _portable_message(error),
    }
    mapping = {
        "schema_version": AUDIO_CLASSIFIER_SCHEMA_VERSION,
        "status": "needs_classifier",
        "catalog_binding": copy.deepcopy(dict(binding)),
        "classifier": copy.deepcopy(dict(classifier_metadata)),
        "methodology": _methodology(),
        "assets": {},
        "diagnostics": [diagnostic],
    }
    report = {
        "schema_version": AUDIO_CLASSIFIER_SCHEMA_VERSION,
        "status": "needs_classifier",
        "asset_count": int(binding.get("asset_count") or 0),
        "classified_asset_count": 0,
        "diagnostics": [diagnostic],
    }
    assert_no_host_paths(mapping, label="needs_classifier mapping")
    atomic_write_json(output_dir / "audio_asset_mapping.v1.json", mapping)
    atomic_write_json(output_dir / "classifier_run_report.v1.json", report)
    return mapping


def _methodology() -> dict[str, Any]:
    return {
        "probabilities": {
            "method": "Microsoft CLAP zero-shot cosine logits then per-window softmax and arithmetic mean",
            "semantics": "relative_to_versioned_prompt_bank_not_calibrated_confidence",
            "prompt_set_version": PROMPT_SET_VERSION,
            "prompt_set_sha256": canonical_hash(PROMPT_GROUPS),
            "prompt_groups": copy.deepcopy(PROMPT_GROUPS),
            "window_seconds": MODEL_WINDOW_SECONDS,
            "window_positions": "10%, 50%, and 90% of valid start range; one window for assets <=7s",
        },
        "signal": {
            "decode": "complete asset, ffmpeg mono float32 PCM at 48000 Hz",
            "clipping_ratio": "fraction of complete-file samples with abs(sample)>=0.999",
            "clipping_flag": "clipping_ratio>0.00001",
            "dc_offset_mean": "arithmetic mean of complete-file full-scale PCM samples",
            "dc_offset_flag": "abs(dc_offset_mean)>0.01",
            "loop_score": "0.50 endpoint spectral cosine + 0.30 endpoint RMS similarity + 0.20 seam continuity over 0.5s endpoints",
        },
        "taxonomy": {
            "identity_fields": "explicit catalog taxonomy tags; CLAP scores audit/validate rather than invent exact species",
            "classifier_scores": "real mean-window relative softmax values for every taxonomy prompt",
        },
    }


def classify_catalog(
    catalog_path: str | Path,
    output_dir: str | Path,
    *,
    classifier: ZeroShotAudioClassifier | None = None,
    ffmpeg_bin: str = "ffmpeg",
    model_path: str | Path | None = None,
) -> dict[str, Any]:
    """Classify every catalog asset or emit a fail-closed needs_classifier mapping."""

    source_path = Path(catalog_path).expanduser().resolve()
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    try:
        raw_catalog = json.loads(source_path.read_text(encoding="utf-8"))
        portable_catalog = make_portable_catalog(raw_catalog)
        portable_path = destination / "catalog.portable.v1.json"
        atomic_write_json(portable_path, portable_catalog)
        portable_sha = sha256_file(portable_path)
        binding = _mapping_binding(portable_path, portable_catalog)
    except Exception as exc:
        fallback_binding = {
            "catalog_id": "unreadable-catalog",
            "portable_catalog_sha256": "0" * 64,
            "asset_manifest_sha256": "0" * 64,
            "asset_count": 0,
        }
        return _failure_artifacts(
            output_dir=destination,
            binding=fallback_binding,
            classifier_metadata=_requested_classifier_metadata(),
            stage="portable_catalog",
            error=exc,
        )

    active_classifier = classifier
    try:
        if active_classifier is None:
            active_classifier = MSCLAPZeroShotClassifier(model_path=model_path)
        classifier_metadata = copy.deepcopy(dict(active_classifier.metadata))
        classifier_metadata["classifier_version"] = CLASSIFIER_VERSION
        classifier_metadata["prompt_set_version"] = PROMPT_SET_VERSION
        classifier_metadata["prompt_set_sha256"] = canonical_hash(PROMPT_GROUPS)
        assert_no_host_paths(classifier_metadata, label="classifier metadata")
    except Exception as exc:
        return _failure_artifacts(
            output_dir=destination,
            binding=binding,
            classifier_metadata=_requested_classifier_metadata(),
            stage="model_initialization",
            error=exc,
        )

    rows = portable_catalog.get("assets")
    assert isinstance(rows, list)
    assets: dict[str, Any] = {}
    progress = {
        "schema_version": AUDIO_CLASSIFIER_SCHEMA_VERSION,
        "status": "classifying",
        "asset_count": len(rows),
        "completed_asset_ids": [],
    }
    atomic_write_json(destination / "classifier_progress.v1.json", progress)
    try:
        with tempfile.TemporaryDirectory(prefix="windows-", dir=destination) as temp_name:
            temp_root = Path(temp_name)
            for index, row in enumerate(rows, 1):
                if not isinstance(row, Mapping):
                    raise AudioClassifierError("catalog asset row must be a mapping")
                provider_id = _provider_asset_id(row)
                digest = str(row.get("sha256") or "").strip().casefold()
                source = _catalog_asset_path(source_path.parent, row)
                duration = _duration(row)
                signal = _decode_full_signal(
                    source,
                    ffmpeg_bin=ffmpeg_bin,
                    timeout=max(90.0, duration * 2.0 + 30.0),
                )
                signal_quality = compute_signal_quality(signal, SIGNAL_SAMPLE_RATE)
                asset_temp = temp_root / f"asset-{index:03d}"
                asset_temp.mkdir(parents=True, exist_ok=True)
                windows, starts = _extract_windows(
                    source, duration, asset_temp, ffmpeg_bin=ffmpeg_bin
                )
                scores = active_classifier.score_windows(windows, PROMPT_GROUPS)
                quality_scores = scores.get("quality")
                taxonomy_scores = scores.get("taxonomy")
                if not isinstance(quality_scores, Mapping) or not isinstance(taxonomy_scores, Mapping):
                    raise AudioClassifierError("classifier returned incomplete prompt groups")
                assets[provider_id] = {
                    "asset_id": f"audio-{digest[:24]}",
                    "sha256": digest,
                    "taxonomy": _classified_taxonomy(row, taxonomy_scores),
                    "quality": _classified_quality(quality_scores, signal_quality, starts),
                }
                progress["completed_asset_ids"].append(provider_id)
                progress["completed_asset_count"] = len(assets)
                atomic_write_json(destination / "classifier_progress.v1.json", progress)
                print(f"[{index}/{len(rows)}] classified {provider_id}", flush=True)
    except Exception as exc:
        return _failure_artifacts(
            output_dir=destination,
            binding=binding,
            classifier_metadata=classifier_metadata,
            stage="asset_classification",
            error=exc,
        )

    mapping = {
        "schema_version": AUDIO_CLASSIFIER_SCHEMA_VERSION,
        "status": "ready",
        "catalog_binding": binding,
        "classifier": classifier_metadata,
        "methodology": _methodology(),
        "assets": assets,
        "diagnostics": [],
    }
    try:
        validate_portable_mapping(
            mapping, portable_catalog, portable_catalog_sha256=portable_sha
        )
        resolver_validation = _validate_through_catalog_resolver(
            portable_catalog,
            catalog_root=source_path.parent,
            assets=assets,
        )
    except Exception as exc:
        return _failure_artifacts(
            output_dir=destination,
            binding=binding,
            classifier_metadata=classifier_metadata,
            stage="catalog_resolver_validation",
            error=exc,
        )
    atomic_write_json(destination / "audio_asset_mapping.v1.json", mapping)
    report = {
        "schema_version": AUDIO_CLASSIFIER_SCHEMA_VERSION,
        "status": "ready",
        "asset_count": len(rows),
        "classified_asset_count": len(assets),
        "catalog_binding": binding,
        "classifier": classifier_metadata,
        "resolver_validation": resolver_validation,
        "diagnostics": [],
    }
    assert_no_host_paths(report, label="classifier report")
    atomic_write_json(destination / "classifier_run_report.v1.json", report)
    progress["status"] = "ready"
    atomic_write_json(destination / "classifier_progress.v1.json", progress)
    return mapping


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, help="Input audio catalog JSON")
    parser.add_argument("--output-dir", required=True, help="Classifier artifact directory")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg executable")
    parser.add_argument("--model-path", help="Optional pinned Microsoft CLAP weight path")
    parser.add_argument("--model-cache", help="Portable Hugging Face cache directory")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.model_cache:
        os.environ.setdefault("HF_HOME", str(Path(args.model_cache).expanduser().resolve()))
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    mapping = classify_catalog(
        args.catalog,
        args.output_dir,
        ffmpeg_bin=args.ffmpeg,
        model_path=args.model_path,
    )
    print(json.dumps({"status": mapping["status"], "asset_count": len(mapping["assets"])}, sort_keys=True))
    return 0 if mapping.get("status") == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
