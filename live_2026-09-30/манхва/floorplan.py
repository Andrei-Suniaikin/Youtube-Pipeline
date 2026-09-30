# -*- coding: utf-8 -*-
"""Чертёж помещения видом сверху — рисуется кодом, генератор не нужен.

Зачем именно чертёж, а не готовая фотография комнаты.
Замер по 486 кадрам первого прогона: совпадение по геометрии между соседними
кадрами 0.013 при шуме 0.007 — то есть комната перерисовывается заново почти
каждый кадр. Причина структурная: локации выдана ОДНА картинка общего плана и
подпись «планировка точно как здесь». Такая картинка отвечает на вопрос
«как выглядит комната ОТСЮДА», а нужен ответ «что в комнате есть и где стоит».
В студии это два РАЗНЫХ документа: 美術設定 (чертёж без цвета) и 美術ボード
(эталон покраски). Мы отдавали второй вместо первого.

Здесь — первый: план без цвета, с подписями и сторонами света. По нему модель
может поставить камеру в любую точку и получить ту же комнату с новой стороны.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

PX = 1024          # сторона картинки
MARGIN = 96
BG = (255, 255, 255)
INK = (20, 20, 20)
SOFT = (130, 130, 130)


def _font(size: int):
    for p in ("/System/Library/Fonts/Supplemental/Arial.ttf",
              "/System/Library/Fonts/Helvetica.ttc",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


@dataclass
class Item:
    name: str          # английское имя предмета, оно же уйдёт в промпт
    x: float           # метры от левой стены до ЛЕВОГО края предмета
    y: float           # метры от верхней стены до ВЕРХНЕГО края
    w: float
    d: float
    kind: str = "furniture"   # furniture | door | window | fixture
    look: str = ""     # цвет и материал: доска, ставшая из зелёной белой,
    # читается зрителем как ДРУГАЯ комната


@dataclass
class Plan:
    lid: str
    width: float               # метры, слева направо
    depth: float               # метры, сверху вниз
    items: List[Item] = field(default_factory=list)
    path: str = ""

    # ---- чертёж ----------------------------------------------------------
    def draw(self, path: str) -> str:
        im = Image.new("RGB", (PX, PX), BG)
        d = ImageDraw.Draw(im)
        f_big, f_mid, f_sml = _font(30), _font(24), _font(19)

        inner = PX - 2 * MARGIN
        scale = min(inner / max(self.width, 0.1), inner / max(self.depth, 0.1))
        w_px, d_px = self.width * scale, self.depth * scale
        x0 = (PX - w_px) / 2
        y0 = (PX - d_px) / 2

        def to_px(mx, my):
            return x0 + mx * scale, y0 + my * scale

        # стены
        d.rectangle([x0, y0, x0 + w_px, y0 + d_px], outline=INK, width=6)

        for it in self.items:
            ax, ay = to_px(it.x, it.y)
            bx, by = to_px(it.x + it.w, it.y + it.d)
            if it.kind == "door":
                d.rectangle([ax, ay, bx, by], fill=BG, outline=INK, width=5)
                d.line([ax, ay, bx, by], fill=INK, width=3)
            elif it.kind == "window":
                d.rectangle([ax, ay, bx, by], fill=BG, outline=INK, width=5)
                mx, my = (ax + bx) / 2, (ay + by) / 2
                if (bx - ax) > (by - ay):
                    d.line([ax, my, bx, my], fill=INK, width=3)
                else:
                    d.line([mx, ay, mx, by], fill=INK, width=3)
            else:
                d.rectangle([ax, ay, bx, by], outline=INK, width=4)
            # Подпись. Длинные имена не влезают в узкие предметы у стен —
            # тогда шрифт мельчает, а сама подпись уезжает ВНУТРЬ комнаты и
            # прижимается к её границам, иначе текст вылезает за чертёж
            # (проверено: STREET WINDOW наезжал на слово EAST).
            label = it.name.upper()
            box_w, box_h = bx - ax, by - ay
            fnt = f_sml
            for size in (19, 16, 13, 11):
                fnt = _font(size)
                tb0 = d.textbbox((0, 0), label, font=fnt)
                if (tb0[2] - tb0[0]) <= max(box_w, box_h) - 8:
                    break
            tb = d.textbbox((0, 0), label, font=fnt)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
            lx, ly = (ax + bx) / 2, (ay + by) / 2
            if tw > box_w - 6:                      # не влезло по ширине предмета
                if box_h > box_w:                   # предмет вертикальный, у боковой стены
                    lx = lx + (tw / 2 + 10) * (1 if it.x < self.width / 2 else -1)
                else:                               # горизонтальный, у верхней/нижней стены
                    ly = ly + (th / 2 + 12) * (1 if it.y < self.depth / 2 else -1)
            lx = min(max(lx, x0 + tw / 2 + 4), x0 + w_px - tw / 2 - 4)
            ly = min(max(ly, y0 + th / 2 + 4), y0 + d_px - th / 2 - 4)
            d.rectangle([lx - tw / 2 - 4, ly - th / 2 - 3,
                         lx + tw / 2 + 4, ly + th / 2 + 5], fill=BG)
            d.text((lx, ly), label, font=fnt, fill=INK, anchor="mm")

        # Стороны света и углы. Проверено: модель реально считывает с плана
        # компас, подписи стен, углы NW/NE/SW/SE и подписи предметов латиницей
        # заглавными — по ним она и раскладывает мебель. А вот нарисованную
        # стрелку камеры игнорирует, поэтому стрелку не рисуем.
        d.text((PX / 2, y0 - 34), "NORTH WALL", font=f_mid, fill=SOFT, anchor="mm")
        d.text((PX / 2, y0 + d_px + 34), "SOUTH WALL", font=f_mid, fill=SOFT, anchor="mm")
        d.text((x0 - 46, PX / 2), "WEST", font=f_mid, fill=SOFT, anchor="mm")
        d.text((x0 + w_px + 46, PX / 2), "EAST", font=f_mid, fill=SOFT, anchor="mm")
        for tag, (cx, cy) in (("NW", (x0 + 30, y0 + 22)), ("NE", (x0 + w_px - 30, y0 + 22)),
                              ("SW", (x0 + 30, y0 + d_px - 22)),
                              ("SE", (x0 + w_px - 30, y0 + d_px - 22))):
            d.text((cx, cy), tag, font=f_sml, fill=SOFT, anchor="mm")
        # компас: N вверху
        cx, cy, r = PX - 74, 78, 26
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=INK, width=3)
        d.line([cx, cy + r - 6, cx, cy - r + 6], fill=INK, width=3)
        d.polygon([(cx, cy - r + 2), (cx - 7, cy - r + 14), (cx + 7, cy - r + 14)], fill=INK)
        d.text((cx, cy - r - 16), "N", font=f_mid, fill=INK, anchor="mm")
        d.text((PX / 2, 34), f"FLOOR PLAN — {self.lid.upper()}  "
                             f"({self.width:g} x {self.depth:g} m)",
               font=f_big, fill=INK, anchor="mm")
        d.text((PX / 2, PX - 30),
               "TOP-DOWN SCHEMATIC — REFERENCE ONLY, NEVER DRAW THIS",
               font=f_mid, fill=SOFT, anchor="mm")

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp.png"
        im.save(tmp)
        os.replace(tmp, path)
        self.path = path
        return path

    # ---- текст в промпт --------------------------------------------------
    def layout_line(self) -> str:
        """Планировка словами. Картинка одна не тянет: модель читает и текст.

        Стороны называем как на чертеже — тогда «камера у южной стены» из
        поля кадра совпадает с тем, что модель видит на плане.
        """
        parts = []
        for it in self.items:
            cx, cy = it.x + it.w / 2, it.y + it.d / 2
            ns = "north" if cy < self.depth / 3 else ("south" if cy > 2 * self.depth / 3 else "middle")
            we = "west" if cx < self.width / 3 else ("east" if cx > 2 * self.width / 3 else "centre")
            where = "centre of the room" if (ns == "middle" and we == "centre") else f"{ns} {we}".strip()
            look = f" ({it.look})" if it.look else ""
            parts.append(f"{it.name}{look} at the {where}")
        return "; ".join(parts)

    def anchors(self) -> List[str]:
        """Готовые точки съёмки — из них раскадровка выбирает по одной на кадр."""
        out = ["from the south wall looking north",
               "from the north wall looking south",
               "from the west wall looking east",
               "from the east wall looking west"]
        for it in self.items:
            if it.kind == "door":
                out.append(f"from just inside the {it.name} looking into the room")
            if it.kind == "window":
                out.append(f"from beside the {it.name} looking across the room")
        return out


def from_json(lid: str, data: dict) -> Plan:
    """data — то, что вернула модель по локации."""
    items = []
    for raw in (data.get("items") or []):
        try:
            items.append(Item(
                name=str(raw.get("name", "object"))[:28],
                look=str(raw.get("look", ""))[:40],
                x=float(raw.get("x", 0)), y=float(raw.get("y", 0)),
                w=max(0.2, float(raw.get("w", 1))), d=max(0.2, float(raw.get("d", 1))),
                kind=str(raw.get("kind", "furniture")),
            ))
        except (TypeError, ValueError):
            continue
    return Plan(lid=lid,
                width=max(2.0, float(data.get("width", 6))),
                depth=max(2.0, float(data.get("depth", 5))),
                items=items)


# --- что видно из точки съёмки ------------------------------------------------
# Раньше в промпт уходили два несвязанных куска: план в сторонах света («доска
# на севере, дверь на юго-западе») и направление камеры («от южной стены на
# север»). Свести их модель обязана была в уме — и не сводила: доска оказывалась
# позади камеры, комната зеркалилась. Считаем разность мы, кодом.

_DIRS = {"north": (0.0, -1.0), "south": (0.0, 1.0),
         "east": (1.0, 0.0), "west": (-1.0, 0.0)}


def _anchor_camera(plan: "Plan", anchor: str):
    """Возвращает (позиция камеры, направление взгляда) в метрах плана."""
    a = (anchor or "").lower()
    cx, cy = plan.width / 2.0, plan.depth / 2.0
    for wall, (dx, dy) in (("south wall", (0.0, -1.0)), ("north wall", (0.0, 1.0)),
                           ("west wall", (1.0, 0.0)), ("east wall", (-1.0, 0.0))):
        if wall in a:
            # Камеру ставим НА ШАГ внутрь комнаты, а не вплотную к стене:
            # вплотную предмет, висящий на этой же стене, попадал в «прямо
            # перед собой» вместо «за спиной» (окно на восточной стене при
            # взгляде на запад).
            pos = {"south wall": (cx, plan.depth - 0.9), "north wall": (cx, 0.9),
                   "west wall": (0.9, cy), "east wall": (plan.width - 0.9, cy)}[wall]
            return pos, (dx, dy)
    for it in plan.items:                       # «от двери», «у окна»
        if it.name.lower() in a:
            px, py = it.x + it.w / 2, it.y + it.d / 2
            vx, vy = cx - px, cy - py
            n = max(0.001, (vx * vx + vy * vy) ** 0.5)
            return (px, py), (vx / n, vy / n)
    return (cx, plan.depth - 0.4), (0.0, -1.0)


# Предметы, на которых по смыслу есть надпись: генератор пишет на них слова
# (run_ceo круг 3: «directory plaque» в паспорте сцены 5 → DIRECTORY на стене в
# 9 кадрах, газета сцены 3 → читаемые заголовки в 6, бейдж → SERVICE PASS).
# Общий запрет текста в NEG это не остановил — предмет надо подписывать на месте.
_SIGN_ITEM = re.compile(r"\b(plaques?|signs?|signboards?|signage|directory|directories|posters?|banners?|"
                        r"name ?plates?|placards?|notices?|bulletin|whiteboard|blackboard|chalkboard|"
                        r"billboard|logos?|monitors?|screens?|displays?|tv|television|clock|calendar)\b", re.I)
_PRINT_ITEM = re.compile(r"\b(newspapers?|newsprint|magazines?|planners?|documents?|sheets?|papers?|"
                         r"letters?|badges?|pass(?:es)?|books?|menus?|receipts?|tickets?|maps?|files?|"
                         r"folders?|envelopes?|notes?|cards?|forms?|contracts?|agreements?)\b", re.I)


def blank_writing(name: str) -> str:
    """«directory plaque» → «directory plaque (blank, no lettering)»;
    «folded newspaper» → «folded newspaper (its print drawn as illegible grey strokes, no readable words)»."""
    n = str(name or "")
    if not n or "(blank" in n or "illegible" in n:
        return n
    tail = ""
    if _SIGN_ITEM.search(n):
        tail = "blank, no lettering"
    elif _PRINT_ITEM.search(n):
        tail = "its print drawn as illegible grey strokes, no readable words"
    if not tail:
        return n
    if n.endswith(")") and "(" in n:            # «newspaper (grey newsprint)» → одна скобка
        return n[:-1] + "; " + tail + ")"
    return n + " (" + tail + ")"


def view_from(plan: "Plan", anchor: str) -> str:
    """Словами: что впереди, что слева, что справа, что за спиной."""
    (px, py), (fx, fy) = _anchor_camera(plan, anchor)
    rx, ry = -fy, fx                             # правая рука наблюдателя
    ahead, left, right, behind = [], [], [], []
    for it in plan.items:
        vx, vy = (it.x + it.w / 2) - px, (it.y + it.d / 2) - py
        depth = vx * fx + vy * fy
        lat = vx * rx + vy * ry
        if depth < -0.15:
            behind.append(it.name)
        elif abs(lat) < max(0.8, plan.width * 0.15):
            ahead.append(blank_writing(it.name))
        elif lat > 0:
            right.append(blank_writing(it.name))
        else:
            left.append(blank_writing(it.name))
    bits = []
    if ahead:
        bits.append("straight ahead: " + ", ".join(ahead))
    if left:
        bits.append("on the left: " + ", ".join(left))
    if right:
        bits.append("on the right: " + ", ".join(right))
    if behind:
        bits.append("behind the camera and therefore NOT visible: " + ", ".join(behind))
    return "From this camera position you see " + "; ".join(bits) + "." if bits else ""
