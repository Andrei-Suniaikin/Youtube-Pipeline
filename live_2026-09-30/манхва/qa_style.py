# -*- coding: utf-8 -*-
"""Метрика МАНЕРЫ рисунка, а не содержания.

Зачем отдельно от qa_clip: CLIP-эмбеддинг меряет, ЧТО на картинке, и «выбросом
по стилю» назначает кадр с необычным содержанием. Проверено: два кадра, крайние
по CLIP-метрике, стилистически неотличимы — оба чистый вебтун с плоской заливкой.

Здесь считаются низкоуровневые признаки, которые и есть манера:
  плоскость   — доля площади в крупных однотонных пятнах (cel-заливка против подкраски);
  линия       — сила и доля тёмного контура;
  палитра     — сколько различимых цветов в кадре;
  насыщенность и контраст.
Эти числа не зависят от сюжета кадра, поэтому по ним видно именно скачок манеры.
"""
from __future__ import annotations

import numpy as np
import cv2


def features(path: str) -> dict:
    im = cv2.imread(path)
    if im is None:
        return {}
    im = cv2.resize(im, (688, 384), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)

    # плоскость: доля пикселей, у которых окрестность почти однотонна
    blur = cv2.blur(im, (7, 7)).astype(np.int16)
    diff = np.abs(im.astype(np.int16) - blur).sum(axis=2)
    flat = float((diff < 12).mean())

    # линия: доля тёмных пикселей на сильных градиентах
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.sqrt(gx * gx + gy * gy)
    line = float(((mag > 90) & (g < 110)).mean())
    edge = float((mag > 60).mean())

    # палитра: сколько различимых цветов после огрубления
    q = (im // 24).astype(np.int32)
    codes = q[:, :, 0] * 121 + q[:, :, 1] * 11 + q[:, :, 2]
    vals, cnt = np.unique(codes, return_counts=True)
    keep = cnt > (codes.size * 0.0008)      # цвета, занимающие хотя бы 0.08% кадра
    palette = int(keep.sum())

    return {
        "flat": round(flat, 4),
        "line": round(line, 4),
        "edge": round(edge, 4),
        "palette": palette,
        "sat": round(float(hsv[:, :, 1].mean()), 1),
        "val": round(float(hsv[:, :, 2].mean()), 1),
        "contrast": round(float(g.std()), 1),
    }


KEYS = ["flat", "line", "edge", "palette", "sat", "contrast"]


def report(paths, log=print) -> dict:
    rows = []
    for p in paths:
        f = features(p)
        if f:
            f["path"] = p
            rows.append(f)
    if len(rows) < 3:
        return {}
    M = np.array([[r[k] for k in KEYS] for r in rows], dtype=float)
    mu, sd = M.mean(0), M.std(0) + 1e-6
    Z = (M - mu) / sd
    dist = np.abs(Z).mean(1)                 # насколько кадр далёк от общей манеры
    order = np.argsort(-dist)
    import os
    return {
        "кадров": len(rows),
        "разброс манеры (медиана отклонения)": round(float(np.median(dist)), 3),
        "выбросов (>2 сигмы)": int((dist > 2.0).sum()),
        "худшие по манере": [os.path.basename(rows[i]["path"]) for i in order[:10]],
        "значения": {k: (round(float(mu[i]), 3), round(float(sd[i]), 3))
                     for i, k in enumerate(KEYS)},
        "_dist": {os.path.basename(rows[i]["path"]): round(float(dist[i]), 3)
                  for i in range(len(rows))},
    }


def photoreal_score(path: str) -> float:
    """Насколько кадр похож на фотографию, а не на рисунок. Больше — хуже.

    Рисунок в нашей манере: крупные однотонные пятна, чёткий тёмный контур,
    небольшая палитра. Фотография: мелкая текстура везде, мягкие градиенты,
    контура нет, палитра огромная. Проверено на кадре 212 (вышел настоящей
    фотографией) против соседних 223 и 241 — числа расходятся втрое.
    """
    f = features(path)
    if not f:
        return 0.0
    # мало плоских участков + слабый контур + богатая палитра = фотография
    return round((1.0 - f["flat"]) * 2.0 + (0.02 - min(f["line"], 0.02)) * 50.0
                 + min(f["palette"], 120) / 120.0, 3)
