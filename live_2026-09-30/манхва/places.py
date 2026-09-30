# -*- coding: utf-8 -*-
"""Паспорт локации: одно место — один эталон и один неизменный текстовый блок.

Зачем: в первом прогоне у локации не было ничего, кроме слова в описании кадра.
Модель проектировала помещение заново каждый раз — в одной сцене магазина вышло
пять разных подсобок, шесть комплектов одежды на герое и три перехода палитры
из тёплой в холодную. Замер по всему ролику: 39% переходов между соседними
кадрами дают скачок яркости и насыщенности больше 45 пунктов.

Лечение структурное: у места появляется паспорт — картинка общего плана плюс
дословно один и тот же текст про интерьер и свет. И то и другое уходит в КАЖДЫЙ
кадр этой локации, поэтому переизобретать помещение модели больше не из чего.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import images
import sheets as SH

PLACE_STYLE = ("wide establishing shot of an empty interior, no people, no characters, "
               "no figures. Clean flat-shaded background art.")


@dataclass
class Place:
    lid: str
    name: str
    look_en: str
    path: str = ""
    status: str = "ждёт"

    def ref(self) -> Optional[images.Ref]:
        if self.path and os.path.exists(self.path) and os.path.getsize(self.path) > 4096:
            return images.Ref(name=f"place_{self.lid}.png",
                              data_uri=images.data_uri(self.path))
        return None

    def block(self) -> str:
        """Дословно один и тот же текст во все кадры этой локации."""
        return (f"THE PLACE (identical in every shot of this scene, never redesign it): "
                f"{self.look_en} The layout, wall colour, furniture, window positions and "
                f"light sources stay exactly as in the attached place_{self.lid}.png. "
                f"Keep the same light temperature and the same overall brightness.")


def build(locations: List[dict], out_dir: str, style_line: str,
          seed_base: int = 31000, log=print) -> Dict[str, Place]:
    """По одному общему плану на локацию. Дёшево: мест обычно 8-12 на ролик."""
    os.makedirs(out_dir, exist_ok=True)
    places: Dict[str, Place] = {}
    jobs = []
    for i, l in enumerate(locations):
        lid = l.get("id") or f"loc{i}"
        p = Place(lid=lid, name=l.get("name", lid), look_en=SH.plain(l.get("look_en")).strip(),
                  path=os.path.join(out_dir, f"{lid}.png"))
        places[lid] = p
        jobs.append({"key": lid, "path": p.path,
                     "prompt": f"{p.look_en}. {PLACE_STYLE} {style_line}",
                     "seed": seed_base + i})
    log(f"  мест: {len(jobs)}")
    res = images.generate_many(jobs, threads=6,
                               on_done=lambda r: log(f"    {r['key']}: {r['status']}"))
    for r in res:
        pl = places.get(r["key"])
        if pl:
            pl.status = r["status"]
    bad = [r["key"] for r in res if r["status"] == "ошибка"]
    if bad:
        log(f"  ! не нарисовались: {bad}")
    return places


def resolve_frame_place(frame, scene_where: Optional[str], places: Dict[str, Place]):
    """Место кадра: сначала из состояния сцены, потом из скелета сцены.

    Раньше место знали лишь у половины кадров: состояние сцены заполняет его
    только после того, как текст место НАЗВАЛ, а первая половина ролика его
    не называет. Но в скелете у КАЖДОЙ сцены место проставлено — значит
    непокрытых кадров быть не должно вовсе.
    """
    by_name = {p.name.lower(): p for p in places.values()}
    loc = getattr(frame, "location", None) or (frame.get("location") if isinstance(frame, dict) else None)
    if loc:
        p = by_name.get(str(loc).lower()) or places.get(str(loc))
        if p:
            return p
    if scene_where:
        return places.get(scene_where) or by_name.get(str(scene_where).lower())
    return None
