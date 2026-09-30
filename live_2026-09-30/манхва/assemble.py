# -*- coding: utf-8 -*-
"""Сборка ролика: движение по картинке + склейка + звук.

Движение — наезд и проезд (Ken Burns), а не генерация видео: в разобранных
роликах ниши настоящей анимации нет ни в одном, везде медленное движение камеры.
Значит видеомодели этому формату не нужны.

Дрожание: zoompan шагает по целым пикселям, апскейл перед ним лишь мельчит шаг.
С 18.09.2026 кадры движения считает Pillow с дробным окном (MOTION_ENGINE ниже),
zoompan остался только как откат.
"""
from __future__ import annotations

import os
import math
import hashlib
import json
import subprocess
from pathlib import Path
from generation_receipts import atomic_json
from dataclasses import dataclass
from typing import List, Optional

W, H, FPS = 1920, 1080, 24
# Увеличение перед движением камеры лечит дрожание, но стоит времени: при 4x
# промежуточный кадр 7680x4320 и ffmpeg захлёбывается (проверено — сборка встала).
# 2x достаточно: дрожания нет, а кадр 3840x2160 считается вчетверо быстрее.
UPSCALE = 2
# Сколько ядер отдаём одному клипу. Общая загрузка = THREADS_PER_CLIP * workers
# и должна остаться в пределах числа ядер машины.
THREADS_PER_CLIP = 2
# Замер 09.09.2026, один клип 4,5 с на простаивающем VPS:
#   апскейл 2   / 30 к/с  128 с
#   апскейл 1.5 / 30 к/с  117 с
#   апскейл 1.25/ 24 к/с   74 с
#   апскейл 2   / 24 к/с  102 с
# ВАЖНО: все эти замеры сделаны ДО того, как нашлась ловушка zoompan, то есть
# мерили десятиминутные клипы. После починки клип на 4,5 с считается 3,7 с,
# поэтому апскейл вернули на 2 — он больше ничего не стоит.
# Стоимость определяется ЧИСЛОМ ВЫХОДНЫХ КАДРОВ, а не размером входа: апскейл
# почти не влияет, частота кадров влияет вдвое. Поэтому 24 к/с (это ещё и
# киношная частота, для слайдшоу с диктором её хватает с запасом) и апскейл 1.5,
# которого достаточно для наезда в 1.18x без потери резкости.

# ДВИЖОК ДВИЖЕНИЯ. Апскейл лишь уменьшал дрожание, а не убирал его: zoompan
# берёт окно кадра в ЦЕЛЫХ пикселях — и положение, и ширину/высоту окна
# (iw/zoom округляется отдельно по каждой оси). Замер 18.09.2026 на клипах
# пробного ролика 8ed08481: при наезде угол кадра должен ехать ровно на
# 0,86 px за кадр, а едет 0 / 0,5 / 1 / 1,5 px вперемешку — рывок на каждом
# кадре (апскейл 2x = шаг 0,5 px выходного кадра).
# «warp» — кадры считает Pillow (resize с дробным окном, бикубика, один
# пересчёт из исходника на кадр) и подаёт в ffmpeg трубой (rgb24). Траектории —
# те же формулы, что были у zoompan. «zoompan» — прежний путь, оставлен для
# отката (MANGA_MOTION_ENGINE=zoompan) и на случай, если PIL не импортируется.
MOTION_ENGINE = os.environ.get("MANGA_MOTION_ENGINE", "warp").strip().lower() or "warp"
MOTION_WARP_TAG = "warp2"     # попадает в подпись клипа: смена движка = пересборка


class AssembleError(RuntimeError):
    pass


# --- водяной знак генератора --------------------------------------------------
# nano-banana-pro ставит в правый нижний угол серую четырёхлучевую «звёздочку»
# (замер run_ceo 10.09.2026, 335 кадров; разметка глазами нашла 49, шаблонный
# детектор — заметно больше: глаз пропускает её на светлом и пёстром фоне).
# Форма одна для обоих размеров кадра — астроида радиусом 26.5 px (IoU 0.97
# с измеренной маской, 828 px), центр ~100 px от правого и нижнего края, но
# НЕ ровно: на 1376x768 — 97.5 px, на 896x1200 — 100.5 px. Поэтому детектор
# не верит константе, а ищет шаблон в окне ±12 px и ставит маску туда, где
# нашёл. Это ПИКСЕЛИ, а не проценты: обрезка одинаковых полей в 2.5% (19 px
# на 768) звёздочку (74–126 px от края) не задевает, а минимальный равномерный
# кроп, который её убирает, — 126/768 = 16.4% высоты для 16:9 (14% ширины
# для панели). Это уже не поля, а треть композиции, поэтому вместо кропа —
# заливка пятна по маске (cv2.inpaint): ~900 px на кадре, фактура вокруг
# дорисовывается, остальные пиксели не трогаются. Обратная раскраска по альфе
# (звёздочка = серый 229 при 34.5%) оставляла тёмный контур — проверено,
# отвергнуто.
WM_EDGE = 99         # ожидаемый центр звёздочки: px от правого и нижнего края
WM_RADIUS = 26.5     # радиус астроиды, px
WM_TPL = 72          # сторона шаблона (звёздочка 53 px + фон вокруг), px
WM_WIN = 96          # окно поиска: шаблон гуляет в нём на ±(96-72)/2 = ±12 px
# Два независимых признака, оба обязательны (замер run_ceo 10.09, 335 кадров):
#   форма — корреляция остатка с астроидой (TM_CCOEFF_NORMED, максимум по окну);
#   яркость — звёздочка = серый 229 при непрозрачности 34.5% (линейная
#   подгонка по 46 кадрам), т.е. поднимает фон на 79 − 0.345·фон уровней.
# Одна форма даёт ложные срабатывания до 0.49 на складках и линиях, одна
# яркость — на светлых предметах (рука, бумага) в углу. Вместе: 70 кадров
# из 335 (разметка глазами видела 49 и 3 из них ошибочно), ложных 2 — оба на
# ровной заливке, где пятно незаметно; пропущено 4: две бледные на почти
# белом фоне, одна с «дыркой» внутри, одна на бумаге. Порог формы 0.28:
# ниже него настоящие только 186 и 217, но с ними влезают три ложных на
# контурах (рука, кресло, край стола), где заливка смажет линии.
WM_SCORE_MIN = 0.28   # форма: чистые углы дают до 0.49, звёздочки от 0.25
WM_SCORE_SURE = 0.60  # форма настолько чёткая, что яркость сверять не надо
                      # (звёздочка на почти белом фоне: подъём всего 8–10 уровней)
WM_LIFT_MIN = 3.0     # подъём яркости в центре, уровни серого
WM_RATIO = (0.5, 2.0) # подъём / предсказание по модели: 1.0 у настоящей
WM_COLOR, WM_ALPHA = 229.0, 0.345
PREPARED_DIR = ".prepared"   # кэш подготовленных копий рядом с кадрами
_wm_cache = {}
_wm_warned = [False]


def _wm_template():
    """Маска звёздочки (uint8 0/255, WM_TPL×WM_TPL) и float32-шаблон для matchTemplate."""
    import numpy as np
    if "tpl" not in _wm_cache:
        c = WM_TPL / 2
        yy, xx = np.mgrid[0:WM_TPL, 0:WM_TPL]
        dx, dy = np.abs(xx + 0.5 - c), np.abs(yy + 0.5 - c)
        star = (dx ** (2 / 3) + dy ** (2 / 3)) <= WM_RADIUS ** (2 / 3)
        _wm_cache["tpl"] = (star.astype(np.uint8) * 255, star.astype(np.float32))
    return _wm_cache["tpl"]


def _wm_window(w: int, h: int):
    """Левый верхний угол окна поиска: звёздочка с ожидаемым центром WM_EDGE
    оказывается в середине окна."""
    off = WM_EDGE - WM_WIN // 2
    return w - off - WM_WIN, h - off - WM_WIN


def watermark_locate(gray):
    """(score, cx, cy, lift, pred): форма, центр звёздочки в координатах кадра,
    подъём яркости в её ядре и подъём, предсказанный моделью для этого фона.

    gray — двумерный uint8 массив всего кадра. Ищем на остатке «кадр минус
    медианный фон»: ядро медианы 71 больше звёздочки, поэтому фон под ней
    берётся из окружения, а не из неё самой (с ядром 31 в центре оставалась
    «дырка»). TM_CCOEFF_NORMED не зависит от контраста: бледная звёздочка на
    ровном светлом фоне даёт высокий балл, яркая на пёстром — низкий.
    """
    import numpy as np
    import cv2
    h, w = gray.shape[:2]
    x0, y0 = _wm_window(w, h)
    if x0 < 0 or y0 < 0:
        return 0.0, 0, 0, 0.0, 0.0
    win = gray[y0:y0 + WM_WIN, x0:x0 + WM_WIN]
    res = (win.astype(np.float32) - cv2.medianBlur(win, 71).astype(np.float32))
    star, tpl = _wm_template()
    m = cv2.matchTemplate(res, tpl, cv2.TM_CCOEFF_NORMED)
    _, score, _, (mx, my) = cv2.minMaxLoc(m)
    tile = win[my:my + WM_TPL, mx:mx + WM_TPL].astype(np.float32)
    core = star > 0
    ring = (cv2.dilate(star, np.ones((15, 15), np.uint8)) > 0) & ~(cv2.dilate(star, np.ones((7, 7), np.uint8)) > 0)
    bg = float(np.median(tile[ring]))
    lift = float(np.median(tile[core])) - bg
    pred = WM_ALPHA * (WM_COLOR - bg)
    return float(score), x0 + mx + WM_TPL // 2, y0 + my + WM_TPL // 2, lift, pred


def watermark_present(gray) -> bool:
    """Есть ли звёздочка: форма И яркость (см. константы выше)."""
    score, _, _, lift, pred = watermark_locate(gray)
    if score >= WM_SCORE_SURE and lift >= WM_LIFT_MIN:
        return True
    ratio = lift / max(pred, 1.0)
    return score >= WM_SCORE_MIN and lift >= WM_LIFT_MIN and WM_RATIO[0] <= ratio <= WM_RATIO[1]


def watermark_score(gray) -> float:
    return watermark_locate(gray)[0]


WATERMARK_FIX = True
# Звёздочка-водяной знак генератора (nano-banana-pro) стоит в фиксированном месте:
# центр ≈100 px от правого и нижнего края (замер 10.09 по 92 кадрам двух размеров),
# ≈40 px в поперечнике. Детектор формы (пробовали) пропускал каждую пятую и давал
# ложные, поэтому пятно заливается ВСЕГДА — inpaint Telea по кругу r=26: на ровных
# участках след невидим, на текстуре — мягкое пятно, куда менее заметное, чем звезда.
_WM_OFFSET = 100
_WM_RADIUS = 26
_GEN_SIZES = {(896, 1200), (1376, 768), (768, 1376), (1200, 896)}


def prepare_frame(path: str) -> str:
    """Копия кадра без водяного знака (кэш в .prepared рядом с кадром)."""
    if not WATERMARK_FIX or not path or not os.path.exists(path):
        return path
    d = os.path.dirname(path)
    if os.path.basename(d) == ".prepared":
        return path
    out_dir = os.path.join(d, ".prepared")
    out = os.path.join(out_dir, os.path.basename(path))
    try:
        source_sha = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        if os.path.exists(out):
            try:
                if json.loads(Path(out + ".input.json").read_text()).get("source_sha256") == source_sha:
                    return out
            except (OSError, ValueError):
                pass
        import cv2
        import numpy as np
        im = cv2.imread(path)
        if im is None:
            return path
        h, w = im.shape[:2]
        if (w, h) not in _GEN_SIZES:      # не наш размер — не трогаем
            return path
        # Заливаем только там, где детектор (images.watermark_offset) видит звезду:
        # слепая заливка на чистом кадре оставляла тёмный крест (проверка 10.09).
        try:
            import images as _im
            off = _im.watermark_offset(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY))
        except Exception:   # noqa: BLE001
            off = None
        if off is None:                   # звезды нет — кадр не трогаем
            return path
        mask = np.zeros((h, w), np.uint8)
        cv2.circle(mask, (w - _WM_OFFSET + off[0], h - _WM_OFFSET + off[1]), _WM_RADIUS, 255, -1)
        fixed = cv2.inpaint(im, mask, 3, cv2.INPAINT_TELEA)
        os.makedirs(out_dir, exist_ok=True)
        if not cv2.imwrite(out, fixed):
            return path
        atomic_json(out + ".input.json", {"source_sha256": source_sha})
        return out
    except Exception as e:   # noqa: BLE001 — сборка важнее водяного знака
        print(f"  ! prepare_frame {os.path.basename(path)}: {e}")
        return path

def _run(cmd, timeout=1800):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise AssembleError(f"{cmd[0]} упал: {(p.stderr or '')[-400:]}")
    return p


def _kenburns_filter(seconds: float, mode: int) -> str:
    """mode: 0 наезд, 1 отъезд, 2 проезд влево-вправо, 3 сверху вниз."""
    # Ровно round(seconds*FPS) кадров, минимум 1. Прежний пол в 2 кадра давал
    # клипу на 1 кадр лишний кадр, и сумма клипов уходила от звука (см. clip_seconds).
    frames = max(1, int(round(seconds * FPS)))
    big_w, big_h = int(W * UPSCALE), int(H * UPSCALE)
    pre = (f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase,"
           f"crop={big_w}:{big_h},setsar=1")
    z_in = f"min(1.0001+0.0009*on\\,1.18)"
    z_out = f"max(1.18-0.0009*on\\,1.0001)"
    if mode == 0:
        z, x, y = z_in, "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif mode == 1:
        z, x, y = z_out, "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif mode == 2:
        z = "1.12"
        x, y = f"(iw-iw/zoom)*on/{frames}", "ih/2-(ih/zoom/2)"
    else:
        z = "1.12"
        x, y = "iw/2-(iw/zoom/2)", f"(ih-ih/zoom)*on/{frames}"
    return (f"{pre},zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS},"
            f"format=yuv420p")


def mode_for(shot_size: str, gaze: str = "", prev_mode: int = -1, panel: str = "") -> int:
    """Направление движения выбирается по содержанию кадра, а не по его номеру.

    0 наезд, 1 отъезд, 2 проезд вниз, 3 проезд вверх (для вертикальной панели).
    Замер трёх топовых каналов: движение чередуют от плана к плану — вверх,
    вниз, наезд, отъезд, — и никогда не ставят одно и то же подряд. Прежняя
    версия не выдавала режим 3 вовсе, и половина ролика ехала в одну сторону.
      общий план / удар / деталь — наезд (зритель входит в пространство);
      крупный — отъезд (лицо не должно распухать);
      средний / в рост — проезд, вверх и вниз по очереди.
    """
    if (panel or "").lower() == "impact" or shot_size in ("wide", "insert"):
        m = 0
    elif shot_size in ("close", "extreme_close"):
        m = 1
    else:
        m = 3 if prev_mode == 2 else 2
    if m == prev_mode:
        m = {0: 2, 1: 3, 2: 3, 3: 2}[m]
    return m


@dataclass
class Clip:
    image: str
    seconds: float
    mode: int = 0


def clip_seconds(pairs, total: float, fps: int = FPS) -> List[float]:
    """Длительности клипов (с) по парам [начало, конец] из timing.json.

    Квантуем в кадры АБСОЛЮТНЫЕ границы, а не каждую длительность отдельно:
    иначе ошибки округления копятся, и стыки уезжают от звука. Прежний пол
    dur = max(0.6, ...) в pipeline.step_render УДЛИНЯЛ кадры короче 0,6 с и
    сдвигал все последующие стыки на 0,3–0,5 с к концу ролика (замер 09.09).
    Правила:
      * граница i = round(конец_i * fps), последняя = round(total * fps),
        поэтому сумма клипов == total с точностью до кадра;
      * минимум 1 кадр на клип — недостаток забирается у следующей пары
        (граница сдвигается вперёд не более чем на 1 кадр от pairs);
      * первый клип начинается с нуля: если pairs[0][0] > 0, вступительная
        тишина отдаётся первой картинке (дыра не сдвигает остальные, как и в
        step_render для потерянных картинок).
    Возвращает seconds = кадры / fps, и make_clip/make_clip_vertical дают
    ровно round(seconds * fps) кадров.
    """
    n = len(pairs)
    if n == 0:
        return []
    # timing.json без total (или total=null) — громкая ошибка, а не TypeError в глубине
    T = int(round(float(total or 0) * fps))
    if T < n:
        raise AssembleError(f"clip_seconds: {n} клипов не влезают в {T} кадров (total={total!r})")
    out, prev = [], 0
    for i, pr in enumerate(pairs):
        e = int(round(float(pr[1]) * fps))
        # не раньше prev+1 (минимум кадр), не позже T минус место для оставшихся клипов
        e = max(prev + 1, min(e, T - (n - 1 - i)))
        if i == n - 1:
            e = T
        out.append(e - prev)
        prev = e
    return [k / fps for k in out]


def _motion_engine() -> str:
    """'warp' (Pillow, дробные пиксели) или 'zoompan' (прежний путь)."""
    if MOTION_ENGINE == "zoompan":
        return "zoompan"
    try:
        from PIL import Image  # noqa: F401
    except Exception:   # noqa: BLE001 — без PIL ролик всё равно должен собраться
        return "zoompan"
    return "warp"


def _clip_signature(clip, crf=20, **options):
    sig = {"version": 1, "image_sha256": hashlib.sha256(Path(clip.image).read_bytes()).hexdigest(),
           "frames": max(1, round(clip.seconds * FPS)), "mode": clip.mode,
           "width": W, "height": H, "fps": FPS, "upscale": UPSCALE, "crf": crf, **options}
    # Прежний движок даёт прежнюю подпись — откат на zoompan берёт старые клипы.
    if _motion_engine() == "warp":
        sig["motion"] = MOTION_WARP_TAG
    return sig


# --- движение по картинке без округления до пикселя ---------------------------

def _ease(n: int, frames: int) -> float:
    return 0.5 - 0.5 * math.cos(math.pi * n / max(frames - 1, 1))


def _kenburns_path(frames: int, mode: int, layout: str = "horizontal"):
    """Окно камеры по кадрам: [(zoom, x0, y0)], x0/y0 — доля свободного хода.

    Окно = (1/zoom) картинки; левый верхний угол окна = x0*(1-1/zoom) ширины и
    y0*(1-1/zoom) высоты. Формулы — ровно те, что стояли в zoompan
    (_kenburns_filter и make_clip_vertical), только без округления.
    """
    out = []
    for n in range(frames):
        if layout == "vertical":
            e = _ease(n, frames)
            if mode == 0:
                z, fx, fy = 1.0001 + 0.22 * e, 0.5, 0.5
            elif mode == 1:
                z, fx, fy = 1.2201 - 0.22 * e, 0.5, 0.5
            elif mode == 2:
                z, fx, fy = 1.30, 0.5, e
            else:
                z, fx, fy = 1.30, 0.5, 1 - e
        else:
            # Амплитуда считается от ДОЛИ клипа, а не от номера кадра: короткий кадр
            # хука должен двигаться так же заметно, как длинный. Прежние 0,0009 за кадр
            # давали на клипе 2 секунды всего 4,5% наезда — почти стоячую картинку.
            e = _ease(n, frames)
            if mode == 0:
                z, fx, fy = 1.0001 + 0.12 * e, 0.5, 0.5
            elif mode == 1:
                z, fx, fy = 1.1201 - 0.12 * e, 0.5, 0.5
            elif mode == 2:
                z, fx, fy = 1.12, e, 0.5
            else:
                z, fx, fy = 1.12, 0.5, e
        out.append((z, fx, fy))
    return out


def _load_rgb(path: str):
    from PIL import Image
    try:
        with Image.open(path) as im:
            # rgb24, как PNG у ffmpeg: перевод в yuv420p тот же самый (проверено:
            # 0 отличий по всем плоскостям). Поворот по EXIF не применяем — ffmpeg
            # его тоже не применял.
            return im.convert("RGB")
    except Exception as e:   # noqa: BLE001
        raise AssembleError(f"картинка не читается: {path}: {e}")


def _warp_frames(src, out_w: int, out_h: int, path):
    """Кадры движения (RGB) по траектории path; картинка «cover» в out_w x out_h.

    Каждый кадр — один пересчёт из исходника: Image.resize(box=...) с дробным
    окном (координаты окна не округляются, фильтр строится по точным дробным
    центрам). Resize раздельный по осям и сам сглаживает при уменьшении, поэтому
    он и резче, и вдвое быстрее cv2.warpAffine с бикубикой (замер 18.09:
    15 против 30 мс на кадр 1920x1080, одно ядро).
    """
    from PIL import Image
    sw, sh = src.size
    s = max(out_w / sw, out_h / sh)                      # cover-масштаб
    cx, cy = (sw * s - out_w) / 2.0, (sh * s - out_h) / 2.0   # обрезка cover, px выхода
    path = list(path)
    # Если хоть один кадр клипа уменьшает исходник (панели 1086x1448 в окне
    # 810x1080) — Lanczos на весь клип: бикубика там мылила на 5–15% против
    # прежней сборки. Фильтр не меняется внутри клипа, иначе резкость прыгнет.
    zmin = min((z for z, _, _ in path), default=1.0)
    filt = Image.LANCZOS if s * zmin < 1.0 else Image.BICUBIC
    for z, fx, fy in path:
        x0 = fx * (out_w - out_w / z)       # угол окна, px выходного кадра при зуме 1
        y0 = fy * (out_h - out_h / z)
        box = ((x0 + cx) / s, (y0 + cy) / s,
               (x0 + out_w / z + cx) / s, (y0 + out_h / z + cy) / s)
        yield src.resize((out_w, out_h), filt, box=box)


def _pipe_render(cmd, frames_iter):
    """ffmpeg читает сырые кадры из stdin. Ошибка ffmpeg — громкая."""
    import tempfile
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=err)
        try:
            for fr in frames_iter:
                p.stdin.write(fr.tobytes())
            p.stdin.close()
        except BrokenPipeError:
            pass
        except BaseException:
            p.kill()
            p.wait()
            raise
        rc = p.wait(timeout=1800)
        if rc != 0:
            err.seek(0)
            tail = err.read().decode("utf-8", "replace")[-400:]
            raise AssembleError(f"ffmpeg упал: {tail}")


def _make_clip_warp(clip: "Clip", image: str, tmp: str, crf: int) -> None:
    frames = max(1, int(round(clip.seconds * FPS)))
    src = _load_rgb(image)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-threads", str(THREADS_PER_CLIP),
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-framerate", str(FPS),
           "-i", "pipe:0",
           "-vf", "setsar=1,format=yuv420p",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
           "-x264-params", f"threads={THREADS_PER_CLIP}",
           "-pix_fmt", "yuv420p", "-r", str(FPS), "-an", tmp]
    _pipe_render(cmd, _warp_frames(src, W, H, _kenburns_path(frames, clip.mode)))


def _cached_clip_ok(out_path: str, seconds: float, signature=None) -> bool:
    """Готовый клип годится, только если в нём РОВНО нужное число кадров.

    Раньше кэш проверял лишь размер файла: повторная сборка (force) после смены
    длительностей молча брала старые клипы из clips/. Проверка 10.09 на run_v:
    579 старых клипов = 61240 кадров при плане clip_seconds 61239 — стыки
    остались бы сдвинутыми, а правка выглядела бы неработающей. Цена — один
    ffprobe на уже готовый клип (~60 мс), для свежей сборки — ноль.
    """
    if not (os.path.exists(out_path) and os.path.getsize(out_path) > 8192):
        return False
    if signature is not None:
        try:
            if json.loads(Path(out_path + ".input.json").read_text()) != signature:
                return False
        except (OSError, ValueError):
            return False
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=nb_frames", "-of", "csv=p=0", out_path],
                       capture_output=True, text=True)
    try:
        have = int((p.stdout or "").strip().split(",")[0])
    except ValueError:
        return False     # неизвестное число кадров не доказывает целый клип
    return have == max(1, int(round(seconds * FPS)))


def make_clip(clip: Clip, out_path: str, crf: int = 20) -> str:
    signature = _clip_signature(clip, crf, layout="horizontal")
    if _cached_clip_ok(out_path, clip.seconds, signature):
        return out_path
    tmp = out_path + ".tmp.mp4"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    image = prepare_frame(clip.image)     # звёздочка генератора в углу — см. выше
    # -threads ОБЯЗАТЕЛЕН. Без него каждый ffmpeg забирает все ядра машины, и
    # шестнадцать параллельных клипов дают load average 89 на 20 ядрах —
    # замерено на минском боксе 09.09.2026, рендер встал колом.
    # ЛОВУШКА ZOOMPAN, из-за которой рендер был в 135 раз тяжелее нужного.
    # zoompan выдаёт d кадров на КАЖДЫЙ входной кадр. При «-loop 1 -t 4.5»
    # на вход приходит 4,5*fps кадров, и на выходе получалось 112*135 = 15120
    # кадров, то есть клип на десять минут вместо четырёх с половиной секунд.
    # Замерено 09.09.2026: 50 склеенных клипов дали 9 часов видео.
    # Лечение: подать РОВНО ОДИН входной кадр («-framerate 1 -t 1»), тогда
    # zoompan честно выдаёт d кадров, а длительность = d / fps.
    if _motion_engine() == "warp":
        _make_clip_warp(clip, image, tmp, crf)
        os.replace(tmp, out_path)
        atomic_json(out_path + ".input.json", signature)
        return out_path
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
          "-threads", str(THREADS_PER_CLIP),
          "-loop", "1", "-framerate", "1", "-t", "1", "-i", image,
          "-vf", _kenburns_filter(clip.seconds, clip.mode),
          "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
          "-x264-params", f"threads={THREADS_PER_CLIP}",
          "-pix_fmt", "yuv420p", "-r", str(FPS), "-an", tmp])
    os.replace(tmp, out_path)
    atomic_json(out_path + ".input.json", signature)
    return out_path


def make_clip_auto(clip: "Clip", out_path: str, crf: int = 20) -> str:
    """Горизонтальная картинка — обычный наезд; вертикальная — панель по центру
    с размытыми полями, как у топовых рекап-каналов."""
    import subprocess as _sp
    p = _sp.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
                 "-of", "csv=p=0", clip.image], capture_output=True, text=True)
    try:
        iw, ih = [int(x) for x in p.stdout.strip().split(",")[:2]]
    except ValueError:
        return make_clip(clip, out_path, crf)
    if ih > iw * 0.85:                       # портрет или квадрат — панель
        return make_clip_vertical(clip, out_path, crf)
    return make_clip(clip, out_path, crf)


def make_clips(clips: List[Clip], out_dir: str, workers: int = 4, log=print) -> List[str]:
    from concurrent.futures import ThreadPoolExecutor
    os.makedirs(out_dir, exist_ok=True)
    paths = [os.path.join(out_dir, f"clip_{i+1:05d}.mp4") for i in range(len(clips))]
    done = [0]

    def one(i):
        make_clip_auto(clips[i], paths[i])
        done[0] += 1
        if done[0] % 25 == 0:
            log(f"  клипов готово {done[0]}/{len(clips)}")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, range(len(clips))))
    return paths


def concat_video(paths: List[str], out_path: str) -> str:
    """Склейка БЕЗ пережатия — все клипы уже в одном формате."""
    lst = out_path + ".list"
    with open(lst, "w", encoding="utf-8") as f:
        for p in paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
    tmp = out_path + ".tmp.mp4"
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
          "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", tmp])
    os.unlink(lst)
    os.replace(tmp, out_path)
    return out_path


def mux(video: str, audio: str, out_path: str, music: Optional[str] = None) -> str:
    """Кладём голос на видео. Музыка — с автоприглушением под речь."""
    tmp = out_path + ".tmp.mp4"
    # НЕ -shortest: он режет по самому короткому потоку, а голос на десятки мс
    # короче видео (в ролик уходит хвост последнего кадра). Замер run_v 09.09:
    # silent.mp4 61240 кадров, после mux 61237 — три последних кадра потеряны.
    # Режем по длине ВИДЕО явно: -t на выходе, звук обрезается той же длиной
    # (и зацикленная музыка тоже — иначе -stream_loop -1 крутился бы вечно).
    vdur = probe(video).get("duration", 0) or 0
    if vdur <= 0:
        raise AssembleError(f"mux: ffprobe не дал длительность видео {video}")
    tlim = ["-t", f"{vdur:.6f}"]
    if music and os.path.exists(music):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-i", video, "-i", audio, "-stream_loop", "-1", "-i", music,
               "-filter_complex",
               "[2:a]volume=0.18[m];[1:a][m]sidechaincompress=threshold=0.03:ratio=8:attack=5:release=350[mix]",
               "-map", "0:v", "-map", "[mix]",
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", *tlim, tmp]
    else:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-i", video, "-i", audio, "-map", "0:v", "-map", "1:a",
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", *tlim, tmp]
    _run(cmd)
    os.replace(tmp, out_path)
    return out_path


def probe(path: str) -> dict:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration:stream=codec_type,width,height,r_frame_rate",
                        "-of", "json", path], capture_output=True, text=True)
    import json
    try:
        j = json.loads(p.stdout or "{}")
    except Exception:
        return {}
    streams = j.get("streams") or []
    out = {"duration": float((j.get("format") or {}).get("duration", 0) or 0)}
    for s in streams:
        if s.get("codec_type") == "video":
            out["width"], out["height"] = s.get("width"), s.get("height")
        if s.get("codec_type") == "audio":
            out["has_audio"] = True
    return out


def acceptance(mp4: str, expected_seconds: float, tol: float = 2.0) -> dict:
    """Машинная приёмка готового файла: длина, звук, чёрные провалы, тишина."""
    info = probe(mp4)
    probs = []
    if not info:
        return {"ok": False, "problems": ["ffprobe не прочитал файл"]}
    if not info.get("has_audio"):
        probs.append("в файле нет звуковой дорожки")
    if abs(info.get("duration", 0) - expected_seconds) > tol:
        probs.append(f"длина {info.get('duration',0):.1f}s против ожидаемых {expected_seconds:.1f}s")
    # 27.09.2026 (Codex A2-QA01): код возврата сканера не смотрели — упавший ffmpeg не
    # печатал ни одной строки, и это читалось как «чёрных 0, тишины 0» → ok. Теперь
    # сканер, вернувший не ноль, — сам по себе проблема: неизвестная проверка ≠ успех.
    def _scan_failed(name, p):
        tail = " ".join((p.stderr or "").strip().splitlines()[-2:])[:200]
        probs.append(f"сканер {name} упал (код {p.returncode}): {tail}")

    p = subprocess.run(["ffmpeg", "-hide_banner", "-i", mp4, "-vf",
                        "blackdetect=d=0.5:pix_th=0.10", "-an", "-f", "null", "-"],
                       capture_output=True, text=True)
    blacks = (p.stderr or "").count("black_start")
    if p.returncode != 0:
        _scan_failed("чёрных кадров", p)
    if blacks:
        probs.append(f"чёрных провалов дольше 0,5с: {blacks}")
    # Тишина: раньше считали штуки дольше 3 с и одну прощали — ролик с одной дырой
    # в 60 с проходил. Замер 27.09 на двух готовых роликах (75 и 80 мин): самая
    # длинная пауза 1,2 с, пауз от 2 с нет вовсе. Поэтому ловим от 1 с и судим по самой
    # длинной (> 5 с — брак) и по сумме пауз от 2 с (> 1% длины, но не меньше 10 с):
    # паузы между фразами (1–1,5 с) в сумму не идут, сколько бы их ни было.
    p = subprocess.run(["ffmpeg", "-hide_banner", "-i", mp4, "-af",
                        "silencedetect=n=-45dB:d=1", "-vn", "-f", "null", "-"],
                       capture_output=True, text=True)
    import re as _re
    err = p.stderr or ""
    sil = err.count("silence_start")
    durs = [float(x) for x in _re.findall(r"silence_duration:\s*([0-9.]+)", err)]
    starts = [float(x) for x in _re.findall(r"silence_start:\s*(-?[0-9.]+)", err)]
    if len(starts) > len(durs):          # тишина до самого конца файла: конца не напечатал
        durs.append(max(0.0, float(info.get("duration", 0) or 0) - starts[-1]))
    sil_max = round(max(durs, default=0.0), 2)
    sil_total = round(sum(durs), 2)
    sil_long = round(sum(d for d in durs if d >= 2.0), 2)
    total_limit = max(10.0, 0.01 * float(info.get("duration", 0) or 0))
    if p.returncode != 0:
        _scan_failed("тишины", p)
    if sil_max > 5.0:
        probs.append(f"тишина {sil_max:.1f}с подряд (норма — паузы до 5с)")
    if sil_long > total_limit:
        probs.append(f"пауз от 2с набралось {sil_long:.1f}с — больше {total_limit:.0f}с (1% длины)")
    return {"ok": not probs, "problems": probs, **info,
            "blacks": blacks, "silences": sil,
            "silence_max": sil_max, "silence_total": sil_total, "silence_long_total": sil_long}


# --- вертикальная панель в горизонтальном кадре ------------------------------
# Так устроены кадры у всех трёх топовых рекап-каналов (замер 09.09.2026,
# 90 кадров): вертикальная панель в центре 16:9, а поля залиты РАЗМЫТОЙ
# увеличенной копией той же картинки; у одного канала ещё тонкая рамка.
# Движение — наезд или вертикальный проезд по панели, как чтение вебтуна.

def _vertical_filter(seconds: float, mode: int, border: int = 0) -> str:
    """mode: 0 наезд, 1 отъезд, 2 проезд сверху вниз, 3 проезд снизу вверх."""
    frames = max(2, int(round(seconds * FPS)))
    # фон: размытая копия, растянутая на весь кадр
    bg = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
          f"boxblur=luma_radius=40:luma_power=2,eq=brightness=-0.08[bg]")
    # панель: по высоте кадра, с запасом под движение
    ph = int(H * 1.08)
    zi = "min(1.0001+0.0009*on\\,1.16)"
    zo = "max(1.16-0.0009*on\\,1.0001)"
    if mode == 0:
        z, x, y = zi, "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif mode == 1:
        z, x, y = zo, "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif mode == 2:
        z, x, y = "1.10", "iw/2-(iw/zoom/2)", f"(ih-ih/zoom)*on/{frames}"
    else:
        z, x, y = "1.10", "iw/2-(iw/zoom/2)", f"(ih-ih/zoom)*(1-on/{frames})"
    # ширина панели вычисляется из её пропорции: -2 сохраняет соотношение
    fg = (f"[0:v]scale=-2:{ph * UPSCALE}:flags=lanczos,"
          f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s=-1x{H}:fps={FPS}[fgz]")
    # s=-1 zoompan не понимает — задаём ширину панели явно через pad позже,
    # поэтому масштаб панели считаем в python: см. compose_vertical()
    return bg, fg


def make_clip_vertical(clip: "Clip", out_path: str, crf: int = 20,
                       border: int = 6, border_color: str = "#1d3fbf") -> str:
    """Клип из ВЕРТИКАЛЬНОЙ картинки: панель по центру, по бокам размытая копия."""
    import subprocess as _sp
    signature = _clip_signature(clip, crf, layout="vertical", border=border, border_color=border_color)
    if _cached_clip_ok(out_path, clip.seconds, signature):
        return out_path
    # размеры панели: по высоте кадра
    p = _sp.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
                 "-of", "csv=p=0", clip.image], capture_output=True, text=True)
    iw, ih = [int(x) for x in p.stdout.strip().split(",")[:2]]
    pw = int(H * iw / ih)
    if pw > W:                       # горизонтальная картинка — обычный путь
        return make_clip(clip, out_path, crf)
    # Ровно round(seconds*FPS) кадров, минимум 1 — иначе сумма клипов расходится
    # со звуком (см. clip_seconds). Пол в 2 кадра здесь и так резался trim'ом до 1.
    frames = max(1, int(round(clip.seconds * FPS)))
    big_w, big_h = int(pw * UPSCALE), int(H * UPSCALE)
    # Амплитуда движения — по замеру трёх топовых каналов (90 кадров, 3×180 с):
    # за план панель проходит 20–30% высоты экрана (60–95 px/с на 1080p), наезд
    # до +7%/с, у Маньюхи — с разгоном и торможением. Наш прежний проезд был
    # 9% высоты за клип (22 px/с) и наезд +2%/с — в 3–4 раза вялее. Фон при
    # этом у всех неподвижен: двигается только панель.
    ease = f"(0.5-0.5*cos(PI*on/{max(frames - 1, 1)}))"
    zi = f"(1.0001+0.22*{ease})"          # наезд 1.00 → 1.22 с плавным ходом
    zo = f"(1.2201-0.22*{ease})"          # отъезд 1.22 → 1.00
    m = clip.mode
    if m == 0:
        z, x, y = zi, "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif m == 1:
        z, x, y = zo, "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif m == 2:                          # проезд вниз: окно 1/1.3 высоты → путь 23%
        z, x, y = "1.30", "iw/2-(iw/zoom/2)", f"(ih-ih/zoom)*{ease}"
    else:                                 # проезд вверх
        z, x, y = "1.30", "iw/2-(iw/zoom/2)", f"(ih-ih/zoom)*(1-{ease})"
    bw = border
    # Фон — один входной кадр, поэтому его надо зациклить на всю длину клипа,
    # иначе overlay обрывается на первом же кадре (клип выходил длиной 1 с).
    fc = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
          f"boxblur=luma_radius=40:luma_power=2,eq=brightness=-0.08,"
          f"loop=loop={frames + FPS}:size=1:start=0,setpts=N/{FPS}/TB,fps={FPS}[bg];"
          f"[0:v]scale={big_w}:{big_h}:flags=lanczos,setsar=1,"
          f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={pw}x{H}:fps={FPS}[fg];"
          + (f"[fg]pad={pw + 2*bw}:{H + 2*bw}:{bw}:{bw}:{border_color}[fgb];" if bw else "[fg]copy[fgb];")
          + f"[bg][fgb]overlay=(W-w)/2:(H-h)/2:shortest=1,format=yuv420p,trim=end_frame={frames}")
    tmp = out_path + ".tmp.mp4"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    # тот же шаг подготовки, что и в make_clip: и панель, и размытый фон
    # берутся из одной очищенной копии (размер не меняется — pw/ih выше верны)
    image = prepare_frame(clip.image)
    if _motion_engine() == "warp":
        # Фон и рамка — прежним фильтром, панель (размер pw x H) едет из трубы.
        fc_warp = fc.replace(
            f"[0:v]scale={big_w}:{big_h}:flags=lanczos,setsar=1,"
            f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={pw}x{H}:fps={FPS}[fg];",
            "[1:v]setsar=1[fg];")
        assert fc_warp != fc
        panel = _warp_frames(_load_rgb(image), pw, H, _kenburns_path(frames, m, "vertical"))
        _pipe_render(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                      "-threads", str(THREADS_PER_CLIP),
                      "-loop", "1", "-framerate", "1", "-t", "1", "-i", image,
                      "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{pw}x{H}",
                      "-framerate", str(FPS), "-i", "pipe:0",
                      "-filter_complex", fc_warp,
                      "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                      "-x264-params", f"threads={THREADS_PER_CLIP}",
                      "-r", str(FPS), "-an", tmp], panel)
    else:
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
              "-threads", str(THREADS_PER_CLIP),
              "-loop", "1", "-framerate", "1", "-t", "1", "-i", image,
              "-filter_complex", fc,
              "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
              "-x264-params", f"threads={THREADS_PER_CLIP}",
              "-r", str(FPS), "-an", tmp])
    # trim по числу кадров, а не по секундам: duration=0.042 при 24 к/с впускал бы
    # 2 кадра вместо 1 (граница 1/24 = 0.0417 < 0.042).
    # Клип короче секунды: фон ровно из `frames` копий давал «No filtered frames»
    # и файл в 261 байт, а склейка молча теряла время (12 клипов, −3.6 с в ролике
    # 09.09). Запас копий фона в секунду (выше) + громкая ошибка вместо тишины.
    if os.path.getsize(tmp) < 8192:
        raise AssembleError(f"пустой клип {os.path.basename(out_path)} ({clip.seconds:.2f}s): "
                            f"ffmpeg не выдал ни кадра")
    os.replace(tmp, out_path)
    atomic_json(out_path + ".input.json", signature)
    return out_path
