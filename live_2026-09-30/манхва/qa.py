# -*- coding: utf-8 -*-
"""Приёмка кадров: смотрит готовую папку и говорит числами, что не так.

Ничего не перегенерирует и не правит — только читает. Это точка отсчёта:
любая последующая правка сравнивается с этими числами, а не с ощущением.

Мерим ДВА разных сходства с соседним кадром, а не одно:
  ЦВЕТ     — совпадение палитры и света. Норма 0.80-0.99.
             Ниже — свет уехал без причины, выше 0.99 — кадр буквально скопирован.
  ГЕОМЕТРИЯ— та ли это комната в той же планировке. Норма 0.25-0.75.
             Ниже — помещение перерисовано заново, выше — кадр застыл.
Поодиночке обе врут: замер по первому прогону дал цвет 0.85 (норма) при
геометрии 0.32 (рвётся), и по одному числу это читалось как «копируется фон».
"""
from __future__ import annotations

import argparse
import json
import os
import re as _re
import sys
from collections import Counter

import cv2
import numpy as np

# Пороги-коридоры. В конфиге, а не в коде: выведены из ОДНОГО ролика,
# на другой манхве поедут.
LIMITS = {
    # Калибровка 08.09.2026 на 486 кадрах первого прогона, замер моим же кодом:
    #   тот же кадр, слегка сдвинут   цвет 0.997  геометрия 0.693  <- потолок метрики
    #   соседи внутри сцены           цвет 0.607  геометрия 0.013
    #   случайные кадры разных сцен   цвет 0.198  геометрия 0.007  <- шум
    # Вывод: цвет держится (втрое выше шума), геометрия НЕ держится вовсе —
    # соседи внутри сцены отличаются от случайных пар в пределах погрешности.
    "color_lo": 0.50, "color_hi": 0.99,
    "geom_lo": 0.10, "geom_hi": 0.60,
    "bright_jump": 25.0,
    "bars_pct": 1.0,
}

_ORB = cv2.ORB_create(nfeatures=800)
_BF = cv2.BFMatcher(cv2.NORM_HAMMING)
# Детектор лиц — необязательный: на манхве он находит лицо в 16% кадров (замер
# 10.09, метрика всё равно не идёт в отбор), а на VPS сборка cv2 без него
# роняла всю приёмку на импорте. Нет детектора — считаем «лиц не найдено».
try:
    _FACE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
except Exception:   # noqa: BLE001 — headless/урезанный cv2
    _FACE = None


def bars_pct(img) -> float:
    """Доля площади, занятая ровными полями по краям.

    Считаем полем только СИММЕТРИЧНУЮ пару: сверху и снизу либо слева и справа,
    близких по цвету. Иначе в поля попадает обычная композиция: ровная белая
    стена, дверной косяк, небо. Проверено глазами на кадрах 346 и 472 — там
    ложные срабатывания были именно такие.
    """
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = g.shape

    def run(lines):
        n, tone = 0, None
        for ln in lines:
            if float(np.std(ln)) >= 3.0:
                break
            m = float(np.mean(ln))
            if tone is None:
                tone = m
            elif abs(m - tone) > 6:
                break
            n += 1
        return n, (tone if tone is not None else 0.0)

    top, t_tone = run(g[i] for i in range(h // 3))
    bot, b_tone = run(g[h - 1 - i] for i in range(h // 3))
    lef, l_tone = run(g[:, i] for i in range(w // 3))
    rig, r_tone = run(g[:, w - 1 - i] for i in range(w // 3))

    vert = (top + bot) if (top >= 2 and bot >= 2 and abs(t_tone - b_tone) <= 12) else 0
    horiz = (lef + rig) if (lef >= 2 and rig >= 2 and abs(l_tone - r_tone) <= 12) else 0
    area = vert * w + horiz * h - vert * horiz
    return round(100.0 * max(0, area) / (h * w), 2)


def color_sim(a, b) -> float:
    """Совпадение палитры: 2D-гистограмма по тону и насыщенности."""
    def hist(im):
        hsv = cv2.cvtColor(cv2.resize(im, (320, 180)), cv2.COLOR_BGR2HSV)
        h = cv2.calcHist([hsv], [0, 1], None, [50, 60], [0, 180, 0, 256])
        return cv2.normalize(h, h).flatten()
    return float(cv2.compareHist(hist(a), hist(b), cv2.HISTCMP_CORREL))


def geom_sim(a, b) -> float:
    """Совпадение геометрии: доля сошедшихся особых точек.

    Цвет намеренно выброшен — работаем по серому, иначе метрика повторит цвет.
    """
    ga = cv2.cvtColor(cv2.resize(a, (640, 360)), cv2.COLOR_BGR2GRAY)
    gb = cv2.cvtColor(cv2.resize(b, (640, 360)), cv2.COLOR_BGR2GRAY)
    ka, da = _ORB.detectAndCompute(ga, None)
    kb, db = _ORB.detectAndCompute(gb, None)
    if da is None or db is None or len(ka) < 10 or len(kb) < 10:
        return 0.0
    good = 0
    for m in _BF.knnMatch(da, db, k=2):
        if len(m) == 2 and m[0].distance < 0.75 * m[1].distance:
            good += 1
    return round(good / max(1, min(len(ka), len(kb))), 3)


def brightness(img) -> float:
    return float(np.mean(cv2.cvtColor(cv2.resize(img, (320, 180)), cv2.COLOR_BGR2GRAY)))


def faces(img) -> int:
    g = cv2.cvtColor(cv2.resize(img, (640, 360)), cv2.COLOR_BGR2GRAY)
    if _FACE is None:
        return 0
    return len(_FACE.detectMultiScale(g, 1.15, 4, minSize=(28, 28)))


def load_sb(path):
    if not path or not os.path.exists(path):
        return {}
    d = json.load(open(path, encoding="utf-8"))
    fr = d["frames"] if isinstance(d, dict) and "frames" in d else d
    return {int(f["idx"]): f for f in fr}


def run(frames_dir: str, sb_path: str = "", limit: int = 0) -> dict:
    # frames/NNNNN_speaker.png — крупный план говорящего, frames/NNNNN_extra.png — врезка;
    # это производные картинки кадра, а не кадры раскадровки:
    # номер из имени не читается, и строка отчёта легла бы на чужой кадр.
    files = sorted(f for f in os.listdir(frames_dir) if f.lower().endswith((".png", ".jpg"))
                   and not os.path.splitext(f)[0].endswith(("_speaker", "_extra")))
    if limit:
        files = files[:limit]
    sb = load_sb(sb_path)

    per = []
    prev_img = None
    prev_idx = None
    for fn in files:
        p = os.path.join(frames_dir, fn)
        img = cv2.imread(p)
        if img is None:
            per.append({"file": fn, "error": "не читается"})
            prev_img = None
            continue
        try:
            idx = int(os.path.splitext(fn)[0])
        except ValueError:
            idx = len(per) + 1
        meta = sb.get(idx, {})
        row = {
            "file": fn, "idx": idx,
            "scene": meta.get("scene_num"),
            "layer": meta.get("layer") or "now",
            "expect_people": len(meta.get("in_frame") or []),
            "camera": meta.get("camera", ""),
            "w": img.shape[1], "h": img.shape[0],
            "bars_pct": bars_pct(img),
            "bright": round(brightness(img), 1),
            "faces": faces(img),
        }
        hits = text_hits(p)
        row["text"] = [w for w, _ in hits] if hits else []
        row["text_unavailable"] = hits is None
        if prev_img is not None:
            same_scene = (meta.get("scene_num") is not None
                          and sb.get(prev_idx, {}).get("scene_num") == meta.get("scene_num"))
            row["prev_same_scene"] = bool(same_scene)
            row["color_prev"] = round(color_sim(prev_img, img), 3)
            row["geom_prev"] = geom_sim(prev_img, img)
            row["bright_jump"] = round(abs(row["bright"] - per[-1]["bright"]), 1)
        per.append(row)
        prev_img, prev_idx = img, idx

    # Яркость относительно медианы своей сцены: одна комната под одним светом не
    # должна давать кадр на 40 пунктов темнее соседей только потому, что камера
    # повернулась к тёмной стене.
    # Воспоминание внутри сцены — другой мир и другой свет (паспорт памяти), так
    # что сравниваем его только с воспоминаниями той же сцены, а не с «сейчас»:
    # иначе тёмная детская комната во флешбэке 99/100 «выпадала» из светлой
    # больницы и уходила на пересдачу по кругу.
    by_scene = {}
    for r in per:
        if r.get("scene") is not None and "bright" in r:
            by_scene.setdefault((r["scene"], r.get("layer") or "now"), []).append(r["bright"])
    for r in per:
        vals = by_scene.get((r.get("scene"), r.get("layer") or "now")) or []
        if len(vals) >= 5:
            vals = sorted(vals); med = vals[len(vals) // 2]
            r["bright_vs_scene"] = round(r["bright"] - med, 1)
    pairs = [r for r in per if r.get("prev_same_scene")]
    def med(key, rows):
        v = sorted(r[key] for r in rows if key in r)
        return round(v[len(v) // 2], 3) if v else None

    missing = sorted(set(range(1, max(sb) + 1)) - {r.get("idx") for r in per}) if sb else []

    summary = {
        "кадров на диске": len(files),
        "кадров в раскадровке": len(sb),
        "не сгенерировалось": len(missing),
        "номера дыр": missing[:20],
        "поля по краям >%.1f%%" % LIMITS["bars_pct"]:
            sum(1 for r in per if r.get("bars_pct", 0) > LIMITS["bars_pct"]),
        "пар внутри сцены": len(pairs),
        "цвет медиана": med("color_prev", pairs),
        "цвет ниже нормы": sum(1 for r in pairs if r.get("color_prev", 1) < LIMITS["color_lo"]),
        "цвет выше нормы (копия)": sum(1 for r in pairs if r.get("color_prev", 0) > LIMITS["color_hi"]),
        "геометрия медиана": med("geom_prev", pairs),
        "геометрия рвётся (<0.15)": sum(1 for r in pairs if r.get("geom_prev", 1) < 0.15),
        "геометрия ниже нормы": sum(1 for r in pairs if r.get("geom_prev", 1) < LIMITS["geom_lo"]),
        "геометрия застыла": sum(1 for r in pairs if r.get("geom_prev", 0) > LIMITS["geom_hi"]),
        "скачок яркости >%.0f" % LIMITS["bright_jump"]:
            sum(1 for r in pairs if r.get("bright_jump", 0) > LIMITS["bright_jump"]),
        "яркость далеко от медианы сцены (>35)":
            sum(1 for r in per if abs(r.get("bright_vs_scene", 0)) > 35),
        "надписей найдено (RapidOCR)": sum(1 for r in per if r.get("text")),
        "кадры с надписями": [r["idx"] for r in per if r.get("text")][:15],
        "лиц не найдено вовсе": sum(1 for r in per if r.get("faces") == 0),
        "лиц больше ожидаемого на 2+":
            sum(1 for r in per if r.get("faces", 0) >= (r.get("expect_people") or 0) + 2),
        "детектор нашёл хоть одно лицо, %":
            round(100.0 * sum(1 for r in per if r.get("faces", 0) > 0) / max(1, len(per))),
    }
    if sb:
        cams = Counter(r.get("camera", "") for r in per)
        trip = 0
        for i in range(len(per) - 2):
            a, b, c = per[i], per[i + 1], per[i + 2]
            if a.get("scene") == b.get("scene") == c.get("scene") and a.get("camera") and \
               a.get("camera") == b.get("camera") == c.get("camera"):
                trip += 1
        summary["троек подряд с одним планом"] = trip
        summary["ракурсы"] = dict(cams)
    return {"summary": summary, "frames": per, "limits": LIMITS}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("frames")
    ap.add_argument("--sb", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    rep = run(a.frames, a.sb, a.limit)
    for k, v in rep["summary"].items():
        print(f"{k:38} {v}")
    if a.out:
        tmp = a.out + ".tmp"
        json.dump(rep, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(tmp, a.out)
        print(f"\nполный отчёт: {a.out}")




# --- надписи: RapidOCR --------------------------------------------------------
# До этого детектора надписей не было вовсе: MSER не отличал кадр с CALCULUS от
# кадра без текста, CLIP zero-shot ошибался. RapidOCR (CPU, ONNX) на калибровке
# 09.09.2026: крупный текст (CALCULUS на обложке) — найден с уверенностью >0.6,
# четыре чистых кадра — ноль ложных тревог. Мелкий текст (табло аэропорта,
# бейдж) — НЕ находит даже при увеличении вдвое. То есть ловит то, что зритель
# точно заметит, и молчит там, где чисто. Этого достаточно для автопересдачи.
_OCR = None
# Потоки ONNX у OCR. По умолчанию 1: пул по числу ядер крутился вхолостую и съедал
# ~3/4 процессора всей приёмки при том же отчёте (замер 16.09, tasks/scale50/cycles/05).
# 0 — как было до 16.09: RapidOCR без параметров, ONNX сам берёт пул по ядрам.
# Больше числа ядер RapidOCR молча игнорирует и берёт тот же пул, поэтому это ошибка запуска.
OCR_THREADS = int(os.environ.get("MANGA_OCR_THREADS", "1"))
if not 0 <= OCR_THREADS <= (os.cpu_count() or 1):
    raise ValueError(f"MANGA_OCR_THREADS={OCR_THREADS}: допустимо 0 (пул ONNX по числу ядер) "
                     f"или от 1 до {os.cpu_count()}")


def text_hits(path: str, min_conf: float = 0.6):
    """Список (слово, уверенность) — только уверенные находки."""
    global _OCR
    try:
        if _OCR is None:
            from rapidocr_onnxruntime import RapidOCR
            _OCR = (RapidOCR(intra_op_num_threads=OCR_THREADS, inter_op_num_threads=1)
                    if OCR_THREADS else RapidOCR())
    except Exception:
        return None                     # детектора нет — честно возвращаем None
    res, _ = _OCR(path)
    out = []
    for r in (res or []):
        try:
            w = str(r[1]).strip()
            # «000» / «OOO» — так OCR читает ряд круглых ламп или кнопок
            # (кадры 32, 65, 86 прогона 09.09: надписей там нет). Слово из
            # одного повторённого знака — не надпись.
            if len(w) >= 2 and len(set(w)) == 1:
                continue
            # Одна-две буквы («A», «AV», «X») — это блики, пуговицы и складки, а не
            # надпись: замер Codex на 133 кадрах и наш прогон 10.09 (кадры 20/63/84/89/94
            # ушли бы в пересдачу зря). Считаем надписью только слово от 3 знаков.
            if len(_re.sub(r"[^\w]", "", w)) < 3:
                continue
            if float(r[2]) >= min_conf:
                out.append((w, round(float(r[2]), 2)))
        except (TypeError, ValueError, IndexError):
            continue
    return out


if __name__ == "__main__":
    main()
