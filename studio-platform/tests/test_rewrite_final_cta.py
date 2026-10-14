#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Последняя часть донора — только призыв «лайк, подписка, откуда смотрите» (14.10.2026).

Выпуск «Oțel și Aripi»: донор на 32 650 знаков, пятая часть — 884 знака сплошного
призыва. Рерайт честно выкидывает его, ответ короче четверти исходника, проверка длины
считала это браком, и весь текст трижды падал «no output files saved (0/1)». Исключение
для такого хвоста уже было (`source_is_donor_cta_only`), но знало не все формы: «We
rescue…», просьбу и лайк в одной фразе, пять слов в именах, «do not disappear».

Проверяется: этот хвост узнаётся; история с подпиской в конце — нет; короткий ответ с
прощанием канала принимается только для последней части.

    .venv/bin/python tests/test_rewrite_final_cta.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "rewrite"))
sys.path.insert(0, os.path.join(BASE, "shared"))

import rewrite_core as R                                           # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


TAIL = (
    "If this story moved you the way it moved us, do me a favor, hit that like button. "
    "Every single like tells YouTube to show this story to more people. Hit subscribe and "
    "turn on notifications. We rescue forgotten stories from dusty archives every single day. "
    "Stories about pilots who risked everything for their comrades. Real people, real heroism. "
    "Drop a comment right now and tell us where you're watching from. Are you watching from "
    "the United States, United Kingdom, Canada, Australia? Our community stretches across the "
    "entire world. You are not just a viewer. You are part of keeping these memories alive. "
    "Tell us your location. Tell us if someone in your family served. Just let us know you are "
    "here. Thank you for watching and thank you for making sure Royce Priest and Bert Marshall "
    "do not disappear into silence. These men deserve to be remembered and you are helping make that")

print("Хвост донора — только призыв")
check("хвост «Oțel și Aripi» узнан", R.source_is_donor_cta_only(TAIL), True)
check("с историей впереди — уже не призыв",
      R.source_is_donor_cta_only("The squadron flew home over the frozen lake that night. " + TAIL),
      False)
check("одна просьба — мало", R.source_is_donor_cta_only(
    "If this story moved you the way it moved us, do me a favor, hit that like button. "
    "Real people, real heroism. You are not just a viewer. Our community stretches across "
    "the entire world."), False)
check("незнакомая фраза — не узнаём", R.source_is_donor_cta_only(
    TAIL.replace("Real people, real heroism.", "The colonel later denied every word.")), False)

print("\nКороткий ответ вместо хвоста")
OUT = ("Eu sunt Radu, iar acesta a fost canalul Oțel și Aripi. Abonați-vă și apreciați "
       "povestea, ca să nu se piardă.")
check("последняя часть — принимается",
      R.final_cta_short_output_ok(TAIL, OUT, 4, 5, "Oțel și Aripi", "Radu"), True)
check("не последняя — нет", R.final_cta_short_output_ok(TAIL, OUT, 3, 5, "Oțel și Aripi", "Radu"),
      False)
check("без имени канала — нет",
      R.final_cta_short_output_ok(TAIL, OUT.replace("Oțel și Aripi", "canalul"), 4, 5,
                                  "Oțel și Aripi", "Radu"), False)
check("план вместо текста — нет", R.final_cta_short_output_ok(
    TAIL, "Let me rewrite: Radu, Oțel și Aripi, abonați-vă la canal acum.", 4, 5,
    "Oțel și Aripi", "Radu"), False)

print(f"\nпровалов: {len(FAILS)}")
sys.exit(1 if FAILS else 0)
