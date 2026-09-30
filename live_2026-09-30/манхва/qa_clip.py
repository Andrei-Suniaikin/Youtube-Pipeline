# -*- coding: utf-8 -*-
"""Смысловые проверки через CLIP: стиль всего ролика и узнаваемость лиц.

Почему не детектор лиц: штатный каскад OpenCV нашёл лицо только в 58% кадров
(203 кадра из 486 — ни одного). Для рисованной манхвы он не годится, и строить
на нём приёмку нельзя. Замерено, а не предположено.

Что делаем вместо: CLIP-эмбеддинги, работающие на рисованном без детектора.
  СТИЛЬ    — каждый кадр против среднего по ролику. Выброс = кадр выпал из манеры.
  ЛИЦО     — только на крупных планах с ОДНИМ названным героем: там кадр и есть
             лицо, поэтому сравнение с эталоном осмысленно. На общих планах
             метрика бессмысленна, и мы её честно не считаем.
"""
from __future__ import annotations

import json
import os
from typing import Dict, List

import numpy as np
import torch
from PIL import Image

_MODEL = None
_PRE = None


def _load():
    global _MODEL, _PRE
    if _MODEL is None:
        import open_clip
        _MODEL, _, _PRE = open_clip.create_model_and_transforms(
            "ViT-B-32-quickgelu", pretrained="openai")
        _MODEL.eval()
    return _MODEL, _PRE


def embed(paths: List[str], batch: int = 32) -> np.ndarray:
    m, pre = _load()
    out = []
    for i in range(0, len(paths), batch):
        ims = []
        for p in paths[i:i + batch]:
            try:
                ims.append(pre(Image.open(p).convert("RGB")))
            except Exception:
                ims.append(torch.zeros(3, 224, 224))
        with torch.no_grad():
            e = m.encode_image(torch.stack(ims)).float()
            e = e / e.norm(dim=-1, keepdim=True)
        out.append(e.numpy())
    return np.concatenate(out) if out else np.zeros((0, 512), dtype=np.float32)


def style_report(frame_paths: List[str]) -> dict:
    """Насколько кадры держатся одной манеры."""
    e = embed(frame_paths)
    if len(e) < 3:
        return {}
    centre = e.mean(0)
    centre /= np.linalg.norm(centre)
    sims = e @ centre
    order = np.argsort(sims)
    return {
        "emb": e,
        "стиль медиана": round(float(np.median(sims)), 3),
        "стиль p10": round(float(np.percentile(sims, 10)), 3),
        "выпавших из стиля (<p10-0.05)": int((sims < np.percentile(sims, 10) - 0.05).sum()),
        "худшие кадры по стилю": [os.path.basename(frame_paths[i]) for i in order[:8]],
    }


def identity_report(frame_paths: List[str], sb: Dict[int, dict],
                    sheets_dir: str) -> dict:
    """Узнаваемость героя на крупных планах с одним человеком."""
    faces = {}
    if os.path.isdir(sheets_dir):
        for fn in os.listdir(sheets_dir):
            if fn.endswith("_face.png"):
                faces[fn[:-len("_face.png")]] = os.path.join(sheets_dir, fn)
    if not faces:
        return {"эталонов лиц": 0}

    ref_ids = sorted(faces)
    ref_emb = embed([faces[c] for c in ref_ids])

    take, cids = [], []
    for p in frame_paths:
        try:
            idx = int(os.path.splitext(os.path.basename(p))[0])
        except ValueError:
            continue
        f = sb.get(idx) or {}
        who = f.get("in_frame") or []
        if f.get("camera") == "close-up" and len(who) == 1 and who[0] in faces:
            take.append(p)
            cids.append(who[0])
    if not take:
        return {"эталонов лиц": len(faces), "подходящих кадров": 0}

    fe = embed(take)
    sims, wrong = [], 0
    for i, cid in enumerate(cids):
        row = fe[i] @ ref_emb.T
        own = float(row[ref_ids.index(cid)])
        sims.append(own)
        if len(ref_ids) > 1 and float(row.max()) > own + 0.02:
            wrong += 1
    sims_sorted = sorted(sims)
    return {
        "эталонов лиц": len(faces),
        "крупных планов с одним героем": len(take),
        "похожесть на своего героя, медиана": round(float(np.median(sims)), 3),
        "похожесть p10": round(float(np.percentile(sims, 10)), 3),
        "похож на ЧУЖОГО героя сильнее": wrong,
        "худшие кадры по лицу": [os.path.basename(take[i])
                                 for i in np.argsort(sims)[:8]],
    }


def neighbour_drift(frame_paths: List[str], sb: Dict[int, dict], emb=None) -> dict:
    """Смысловой разрыв между соседями внутри сцены (CLIP, а не пиксели)."""
    e = embed(frame_paths) if emb is None else emb
    idxs = []
    for p in frame_paths:
        try:
            idxs.append(int(os.path.splitext(os.path.basename(p))[0]))
        except ValueError:
            idxs.append(-1)
    vals = []
    for i in range(1, len(frame_paths)):
        a, b = idxs[i - 1], idxs[i]
        if a < 0 or b < 0:
            continue
        if (sb.get(a) or {}).get("scene_num") != (sb.get(b) or {}).get("scene_num"):
            continue
        vals.append((float(e[i - 1] @ e[i]), b))
    if not vals:
        return {}
    v = sorted(x[0] for x in vals)
    return {
        "смысловая связь с соседом, медиана": round(float(np.median(v)), 3),
        "разрывов (<0.75)": sum(1 for x in v if x < 0.75),
        "почти копий (>0.97)": sum(1 for x in v if x > 0.97),
        "худшие переходы": [n for s, n in sorted(vals)[:8]],
    }


# --- Ворота содержания: что вообще на картинке -------------------------------
# Нужны потому, что «крупный план героя X» в раскадровке НЕ означает, что лицо
# в кадре есть. Проверено глазами: кадр 282 помечен close-up shen_buyu, а на нём
# только сцепленные руки под одеялом — и это ПРАВИЛЬНАЯ картинка к тексту
# «нашла его ладонь и переплела пальцы». Без этих ворот метрика похожести лица
# считает такие кадры браком и завышает беду втрое.

PROBES = {
    "face": ["an illustration in which at least one person's face is visible",
             "an illustration with no visible face at all: only hands, objects, "
             "scenery, or people seen from behind"],
    "people": ["a scene with no people at all, only room, objects or landscape",
               "a scene with exactly one person",
               "a scene with two people",
               "a scene with a crowd of three or more people"],
    "text": ["an illustration with printed words, captions, subtitles or speech bubbles",
             "an illustration with no writing anywhere"],
    "panels": ["a comic page split into several separate panels with borders",
               "a single full-bleed illustration with no panel borders"],
}

_TXT_CACHE = {}


def _text_emb(key):
    if key not in _TXT_CACHE:
        import open_clip
        m, _ = _load()
        tok = open_clip.get_tokenizer("ViT-B-32-quickgelu")
        with torch.no_grad():
            t = m.encode_text(tok(PROBES[key])).float()
            t = t / t.norm(dim=-1, keepdim=True)
        _TXT_CACHE[key] = t.numpy()
    return _TXT_CACHE[key]


def content_flags(frame_paths: List[str], emb=None) -> List[dict]:
    """Для каждого кадра: есть ли лицо, сколько людей, есть ли надписи и панели."""
    e = embed(frame_paths) if emb is None else emb
    out = []
    for i in range(len(frame_paths)):
        row = {"file": os.path.basename(frame_paths[i])}
        for key in PROBES:
            t = _text_emb(key)
            s = e[i] @ t.T
            # softmax по маленькому набору — CLIP-логиты, температура 100
            p = np.exp(100 * (s - s.max()))
            p = p / p.sum()
            row[key] = {"i": int(np.argmax(p)), "p": round(float(p.max()), 3)}
        out.append(row)
    return out
