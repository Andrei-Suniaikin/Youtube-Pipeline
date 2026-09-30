# -*- coding: utf-8 -*-
"""Облачка и звуковые надписи — рисует КОД, а не генератор.

Почему код. Генератор не пишет русский текст надёжно (проверено: буквы плывут),
а у конкурентов реплика в облачке — это отдельный кадр на весь экран, и звук
(«вжух», «бам») — надпись у действия. Обе вещи детерминированы: текст известен
из бланка, положение считается. Значит рисуем сами: точный текст, любой язык,
любой шрифт, ноль риска.
"""
from __future__ import annotations

import os
from typing import Optional, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageFilter

FONT_BUBBLE = "/System/Library/Fonts/Supplemental/Comic Sans MS Bold.ttf"
FONT_SFX = "/System/Library/Fonts/Supplemental/Impact.ttf"
FALLBACK = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"


def _font(path: str, size: int):
    for p in (path, FALLBACK, "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _split_word(draw, word, font, max_w):
    """Слово шире строки режем на куски с дефисом, каждый кусок влезает в max_w."""
    parts, rest = [], word
    while draw.textlength(rest, font=font) > max_w and len(rest) > 1:
        n = len(rest) - 1
        while n > 1 and draw.textlength(rest[:n] + "-", font=font) > max_w:
            n -= 1
        parts.append(rest[:n] + "-")
        rest = rest[n:]
    return parts + [rest]


def _wrap(draw, text, font, max_w, split_long=False):
    words, lines, cur = text.split(), [], ""
    if split_long:
        # 27.09.2026, аудит F07: «ВЗАИМОПОНИМАНИЕ.» (кадр 388 ролика 07d38531) целиком
        # шире облака даже крупным шрифтом, а перенос ставил его на строку как есть.
        words = [p for w in words for p in _split_word(draw, w, font, max_w)]
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _inside_ellipse(draw, lines, font, lh, cx, cy, a, b):
    """Все углы рамки букв каждой строки внутри эллипса с полуосями a, b.

    27.09.2026, аудит F07: прежний подбор смотрел только на число строк и высоту.
    Замер по 21 025 репликам: у 2025 буквы заходили за контур (углы крайних строк —
    эллипс там уже прямоугольника), у 43 строка была шире всего облака.
    """
    y = cy - len(lines) * lh / 2
    for ln in lines:
        w = draw.textlength(ln, font=font)
        l, t, r, bt = draw.textbbox((cx - w / 2, y), ln, font=font)
        for px, py in ((l, t), (r, t), (l, bt), (r, bt)):
            if ((px - cx) / a) ** 2 + ((py - cy) / b) ** 2 > 1:
                return False
        y += lh
    return True


def bubble_frame(text: str, backdrop: str, out_path: str,
                 size: Tuple[int, int] = (1376, 768), tail: str = "bottom-left") -> str:
    """Кадр-облачко: реплика крупно на весь экран поверх размытого фона.

    Так у топового канала (1,3 млн): облачко с репликой — самостоятельный
    кадр, а не подпись поверх лица.
    """
    W, H = size
    bg = Image.open(backdrop).convert("RGB").resize((W, H), Image.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(14))
    bg = Image.blend(bg, Image.new("RGB", (W, H), (255, 255, 255)), 0.18)
    draw = ImageDraw.Draw(bg)

    # эллипс облачка
    bw, bh = int(W * 0.62), int(H * 0.62)
    x0, y0 = (W - bw) // 2, (H - bh) // 2
    draw.ellipse([x0, y0, x0 + bw, y0 + bh], fill=(255, 255, 255), outline=(20, 20, 20), width=6)
    # хвостик
    cx, cy = W // 2, H // 2
    if tail == "bottom-left":
        pts = [(cx - bw // 4, y0 + bh - 12), (cx - bw // 4 - 90, y0 + bh + 110), (cx - bw // 4 + 70, y0 + bh - 6)]
    else:
        pts = [(cx + bw // 4, y0 + bh - 12), (cx + bw // 4 + 90, y0 + bh + 110), (cx + bw // 4 - 70, y0 + bh - 6)]
    draw.polygon(pts, fill=(255, 255, 255), outline=(20, 20, 20))
    draw.line([pts[0], pts[1]], fill=(20, 20, 20), width=6)
    draw.line([pts[1], pts[2]], fill=(20, 20, 20), width=6)

    # текст: подбираем размер, чтобы влез в 80% ширины эллипса
    txt = text.strip().upper()
    # 27.09.2026, аудит F07: размер принимаем, только если буквы целиком внутри эллипса
    # (контур 6 px рисуется внутрь + 8 px воздуха). Сначала весь ряд размеров без
    # разрезания слов, потом тот же ряд с дефисом: мельче шрифт лучше, чем «ВЗАИМОПОНИ-».
    ea, eb = bw / 2 - 14, bh / 2 - 14
    fitted = False
    for split_long in (False, True):
        for fs in (72, 64, 56, 48, 42, 36, 30):
            font = _font(FONT_BUBBLE, fs)
            lines = _wrap(draw, txt, font, int(bw * 0.78), split_long)
            lh = fs * 1.18
            if (len(lines) * lh <= bh * 0.72 and len(lines) <= 6
                    and _inside_ellipse(draw, lines, font, lh, cx, cy, ea, eb)):
                fitted = True
                break
        if fitted:
            break
    total_h = len(lines) * lh
    y = cy - total_h / 2
    for ln in lines:
        w = draw.textlength(ln, font=font)
        draw.text((cx - w / 2, y), ln, font=font, fill=(15, 15, 15))
        y += lh
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    bg.save(out_path)
    return out_path


def sfx_overlay(image_path: str, word: str, out_path: str,
                anchor: str = "right", tilt: float = -12.0, color=(255, 240, 60),
                scale: float = 0.11, vpos: float = 0.58) -> str:
    """Звуковая надпись поверх панели.

    Размер — 11% высоты панели, не больше: первый вариант (22%) закрывал
    голову героя (кадр с «ШШШ» в сцене 6). Ставится в нижней половине кадра,
    на стороне, ПРОТИВОПОЛОЖНОЙ герою в фокусе, — там обычно и есть действие,
    а лица нет. Так делают в корейских панелях: звук рядом с ударом, не над лицом.
    """
    im = Image.open(image_path).convert("RGBA")
    W, H = im.size
    txt = word.strip().upper()
    fs = max(28, int(H * scale))
    font = _font(FONT_SFX, fs)
    tmp = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(tmp)
    tw = d.textlength(txt, font=font)
    x = int(W * 0.06) if anchor == "left" else int(W - tw - W * 0.06)
    y = int(H * vpos)
    for dx in range(-6, 7, 2):
        for dy in range(-6, 7, 2):
            d.text((x + dx, y + dy), txt, font=font, fill=(0, 0, 0, 255))
    for dx in range(-2, 3, 1):
        for dy in range(-2, 3, 1):
            d.text((x + dx, y + dy), txt, font=font, fill=(255, 255, 255, 255))
    d.text((x, y), txt, font=font, fill=color + (255,))
    tmp = tmp.rotate(tilt, resample=Image.BICUBIC, center=(x + tw / 2, y + fs / 2))
    out = Image.alpha_composite(im, tmp).convert("RGB")
    out.save(out_path)
    return out_path
