"""Pinned local CLIP embeddings for the portable montage package.

This module intentionally lives inside :mod:`semantic_montage`: an installed
production runner must not depend on the legacy ``parser`` package or its
niche-specific configuration.
"""
from __future__ import annotations

import hashlib
import json
import threading
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Mapping

import numpy as np


MODEL_NAME = "ViT-B-32"
PRETRAINED = "laion2b_s34b_b79k"
# SHA-256 of the pinned LAION checkpoint payload.  Broad/lazy cache identities
# include the exact weights, not only the human-readable model alias.
WEIGHTS_SHA256 = "ac4f8c4b88af6d963118cbf40ad93176d092abbedfcb752601ae1866352656e6"

_MODEL = None
_PREPROCESS = None
_TOKENIZER = None
_DEVICE = "cpu"
_LOCK = threading.Lock()


def _package_version(distribution: str) -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return "missing"


def clip_model_contract() -> dict[str, Any]:
    """Return the pinned, hashable model contract without loading weights."""

    payload: dict[str, Any] = {
        "provider": "open_clip",
        "model_name": MODEL_NAME,
        "pretrained": PRETRAINED,
        "weights_sha256": WEIGHTS_SHA256,
        "open_clip_version": _package_version("open-clip-torch"),
        "torch_version": _package_version("torch"),
        "pooling": "four_temporal_cells_mean_normalized",
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    payload["contract_sha256"] = hashlib.sha256(encoded).hexdigest()
    return payload


def _verify_downloaded_weights(
    open_clip_module: Any,
    model_name: str,
    pretrained: str,
    *,
    expected_sha256: str = WEIGHTS_SHA256,
) -> Path:
    """Resolve the pinned checkpoint and verify its bytes before model load."""

    config = open_clip_module.get_pretrained_cfg(model_name, pretrained)
    if not isinstance(config, Mapping) or not config:
        raise RuntimeError("pinned CLIP pretrained configuration is unavailable")
    raw_path = open_clip_module.download_pretrained(config)
    path = Path(str(raw_path or "")).expanduser()
    if not path.is_file():
        raise RuntimeError("pinned CLIP checkpoint is unavailable")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != str(expected_sha256).strip().casefold():
        raise RuntimeError(
            "pinned CLIP checkpoint SHA-256 mismatch; refusing stale or substituted weights"
        )
    return path


def _load(model_name: str, pretrained: str) -> None:
    global _MODEL, _PREPROCESS, _TOKENIZER, _DEVICE
    if _MODEL is not None:
        return
    import open_clip
    import torch

    _verify_downloaded_weights(open_clip, model_name, pretrained)
    _DEVICE = _select_device(torch)
    _MODEL, _, _PREPROCESS = open_clip.create_model_and_transforms(
        model_name, pretrained=pretrained, device=_DEVICE
    )
    _MODEL.eval()
    _TOKENIZER = open_clip.get_tokenizer(model_name)


def _select_device(torch_module: Any) -> str:
    """Prefer the production render node's CUDA GPU, then Apple MPS."""

    cuda = getattr(torch_module, "cuda", None)
    if cuda is not None and callable(getattr(cuda, "is_available", None)):
        if cuda.is_available():
            return "cuda"
    backends = getattr(torch_module, "backends", None)
    mps = getattr(backends, "mps", None) if backends is not None else None
    if mps is not None and callable(getattr(mps, "is_available", None)):
        if mps.is_available():
            return "mps"
    return "cpu"


def _resolve_contract(config: Mapping[str, Any] | None) -> tuple[str, str]:
    if not config:
        return MODEL_NAME, PRETRAINED
    clip = config.get("clip") if isinstance(config, Mapping) else None
    if not isinstance(clip, Mapping):
        return MODEL_NAME, PRETRAINED
    model_name = str(clip.get("model") or MODEL_NAME)
    pretrained = str(clip.get("pretrained") or PRETRAINED)
    if (model_name, pretrained) != (MODEL_NAME, PRETRAINED):
        raise ValueError(
            "production CLIP is pinned; changing model/pretrained requires a new contract"
        )
    return model_name, pretrained


def embed_texts(
    texts: list[str],
    niche: Mapping[str, Any] | None = None,
    batch_size: int = 128,
) -> np.ndarray:
    """Return normalized float32 text embeddings in one bounded GPU batch loop."""

    if not texts:
        return np.empty((0, 0), dtype=np.float32)
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
        raise ValueError("batch_size must be a positive integer")

    import torch

    model_name, pretrained = _resolve_contract(niche)
    with _LOCK:
        _load(model_name, pretrained)
        batches: list[np.ndarray] = []
        with torch.no_grad():
            for offset in range(0, len(texts), batch_size):
                values = [str(value)[:300] for value in texts[offset : offset + batch_size]]
                tokens = _TOKENIZER(values).to(_DEVICE)
                features = _MODEL.encode_text(tokens)
                features /= features.norm(dim=-1, keepdim=True)
                batches.append(features.cpu().numpy().astype(np.float32))
        return np.concatenate(batches, axis=0)


def embed_text(text: str, niche: Mapping[str, Any] | None = None) -> np.ndarray:
    """Return one normalized float32 text embedding."""

    return embed_texts([text], niche=niche, batch_size=1)[0]


def embed_images(
    paths: list[str],
    niche: Mapping[str, Any] | None = None,
    batch_size: int = 16,
) -> dict[str, np.ndarray]:
    """Return normalized embeddings; unreadable files are omitted."""

    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
        raise ValueError("batch_size must be a positive integer")

    import torch
    from PIL import Image

    model_name, pretrained = _resolve_contract(niche)
    with _LOCK:
        _load(model_name, pretrained)
        output: dict[str, np.ndarray] = {}
        with torch.no_grad():
            batch = []
            keep: list[str] = []

            def flush() -> None:
                if not batch:
                    return
                images = torch.stack(batch).to(_DEVICE)
                features = _MODEL.encode_image(images)
                features /= features.norm(dim=-1, keepdim=True)
                for path, vector in zip(
                    keep, features.cpu().numpy().astype(np.float32)
                ):
                    output[path] = vector
                batch.clear()
                keep.clear()

            for raw_path in paths:
                path = str(raw_path)
                try:
                    with Image.open(path) as image:
                        batch.append(_PREPROCESS(image.convert("RGB")))
                    keep.append(path)
                except (OSError, ValueError):
                    continue
                if len(batch) >= batch_size:
                    flush()
            flush()
        return output


__all__ = [
    "MODEL_NAME",
    "PRETRAINED",
    "WEIGHTS_SHA256",
    "clip_model_contract",
    "embed_images",
    "embed_text",
    "embed_texts",
]
