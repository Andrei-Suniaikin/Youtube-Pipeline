"""CLIP-ранжирование кандидатов: косинус (текст сцены, картинка). Локально, бесплатно.

Модель грузится лениво один раз (первый запуск скачает веса ~600МБ в кэш).
"""
import os
import threading

import numpy as np

_MODEL = None
_PREPROCESS = None
_TOKENIZER = None
_DEVICE = "cpu"
_LOCK = threading.Lock()  # MPS/torch не для конкурентных вызовов — сериализуем


def _load(model_name: str, pretrained: str):
    global _MODEL, _PREPROCESS, _TOKENIZER, _DEVICE
    if _MODEL is not None:
        return
    import torch
    import open_clip
    _DEVICE = "mps" if torch.backends.mps.is_available() else "cpu"
    _MODEL, _, _PREPROCESS = open_clip.create_model_and_transforms(
        model_name, pretrained=pretrained, device=_DEVICE)
    _MODEL.eval()
    _TOKENIZER = open_clip.get_tokenizer(model_name)


def _cfg_load(niche: dict = None):
    cfg = (niche or {}).get("clip", {})
    _load(cfg.get("model", "ViT-B-32"), cfg.get("pretrained", "laion2b_s34b_b79k"))


def embed_text(text: str, niche: dict = None) -> np.ndarray:
    """Нормированный CLIP-эмбеддинг текста, float32 shape (D,). Потокобезопасно."""
    import torch
    with _LOCK:
        _cfg_load(niche)
        return _embed_text_locked(text, torch)


def _embed_text_locked(text, torch):
    with torch.no_grad():
        toks = _TOKENIZER([text[:300]]).to(_DEVICE)
        feat = _MODEL.encode_text(toks)
        feat /= feat.norm(dim=-1, keepdim=True)
    return feat.squeeze(0).cpu().numpy().astype(np.float32)


def embed_images(paths: list, niche: dict = None) -> dict:
    """{path: нормированный эмбеддинг float32}. Битые файлы пропускаются. Потокобезопасно."""
    import torch
    from PIL import Image
    with _LOCK:
        _cfg_load(niche)
        return _embed_images_locked(paths, torch, Image)


def _embed_images_locked(paths, torch, Image):
    out = {}
    with torch.no_grad():
        batch, keep = [], []

        def flush():
            if not batch:
                return
            imgs = torch.stack(batch).to(_DEVICE)
            feat = _MODEL.encode_image(imgs)
            feat /= feat.norm(dim=-1, keepdim=True)
            arr = feat.cpu().numpy().astype(np.float32)
            for p, v in zip(keep, arr):
                out[p] = v
            batch.clear(); keep.clear()

        for p in paths:
            try:
                img = Image.open(p).convert("RGB")
                batch.append(_PREPROCESS(img))
                keep.append(p)
            except Exception:  # noqa: BLE001
                continue
            if len(batch) >= 16:
                flush()
        flush()
    return out


def rank(scene_text: str, image_paths: list, niche: dict, log=print) -> list:
    """-> [(path, score)] по убыванию. Битые файлы получают score -1."""
    tfeat = embed_text(scene_text, niche)
    embs = embed_images(image_paths, niche)
    scores = {p: (float(embs[p] @ tfeat) if p in embs else -1.0) for p in image_paths}
    return sorted(scores.items(), key=lambda kv: -kv[1])
