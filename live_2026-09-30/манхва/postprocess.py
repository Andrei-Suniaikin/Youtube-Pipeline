# -*- coding: utf-8 -*-
"""Пост-обработка кадров: то, что промптом не лечится.

Генератор впечатывает собственный водяной знак — полупрозрачную четырёхлучевую
звезду у правого края (подтверждено глазами на кадре 276 прогона 09.09.2026).
Промптом он не убирается: это метка модели, а не элемент рисунка.

Лечение — равномерная обрезка кромок с возвратом к исходному размеру. Обрезаем
ОДИНАКОВО у всех кадров, иначе разная композиция станет заметна на монтаже.
Побочно снимает остатки полей и рамок по краям.
"""
from __future__ import annotations

import os
from typing import List, Tuple

import cv2

TRIM_PCT = 3.0          # сколько процентов срезать с каждой стороны


def trim(path_in: str, path_out: str, pct: float = TRIM_PCT) -> bool:
    im = cv2.imread(path_in)
    if im is None:
        return False
    h, w = im.shape[:2]
    dx, dy = int(w * pct / 100.0), int(h * pct / 100.0)
    if dx <= 0 or dy <= 0:
        return False
    cut = im[dy:h - dy, dx:w - dx]
    out = cv2.resize(cut, (w, h), interpolation=cv2.INTER_LANCZOS4)
    tmp = path_out + ".tmp.png"
    cv2.imwrite(tmp, out)
    os.replace(tmp, path_out)
    return True


def trim_folder(folder: str, pct: float = TRIM_PCT, log=print) -> Tuple[int, int]:
    """Обрезает все кадры на месте. Идемпотентности НЕТ — повторный запуск
    срежет ещё раз, поэтому шаг помечается файлом-маркером."""
    marker = os.path.join(folder, "_trimmed.flag")
    if os.path.exists(marker):
        log("  кромки уже обрезаны, пропускаю")
        return 0, 0
    files = sorted(f for f in os.listdir(folder) if f.lower().endswith(".png"))
    ok = bad = 0
    for f in files:
        p = os.path.join(folder, f)
        if trim(p, p, pct):
            ok += 1
        else:
            bad += 1
    open(marker, "w").write(f"pct={pct}\n")
    log(f"  кромки обрезаны на {pct}%: {ok} кадров, не вышло {bad}")
    return ok, bad
