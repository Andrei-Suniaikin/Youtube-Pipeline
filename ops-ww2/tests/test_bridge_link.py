#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Мост ВВ2 берёт готовый ролик из кабинета Гринда для канала кабинета testpartner (А5, 04.10).

Проверяется ссылка на папку проекта: появляется ровно там, где её ищет движок
(`<tenants>/<кабинет канала>/projects/<pid>`), повтор безопасен, чужую папку или ссылку
не туда не трогает; брать можно только из разрешённых кабинетов. И что мост заводит выпуск
в кабинете канала и сообщает, если ссылка не легла.

    python3 ops-ww2/tests/test_bridge_link.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import bridge_link as L                                            # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def main() -> int:
    print("Из какого кабинета можно брать")
    check("Гринд (18) → testpartner (3)", L.foreign_allowed("18", "3", {"18"}), True)
    check("чужой кабинет (7) — нет", L.foreign_allowed("7", "3", {"18"}), False)
    check("тот же кабинет — это не «чужой»", L.foreign_allowed("3", "3", {"3"}), False)

    print("\nСсылка на папку проекта")
    root = tempfile.mkdtemp(prefix="tenants-")
    os.makedirs(os.path.join(root, "18", "projects", "p_ready", "render"))
    open(os.path.join(root, "18", "projects", "p_ready", "render", "1.mp4"), "wb").write(b"mp4")
    check("легла", L.ensure_project_link(root, "18", "3", "p_ready"), "")
    place = os.path.join(root, "3", "projects", "p_ready")
    check("движок видит файл через кабинет канала",
          open(os.path.join(place, "render", "1.mp4"), "rb").read(), b"mp4")
    check("повтор — тихо", L.ensure_project_link(root, "18", "3", "p_ready"), "")
    shutil.rmtree(place, ignore_errors=True)
    check("«удаление проекта» в кабинете канала настоящую папку не трогает",
          os.path.exists(os.path.join(root, "18", "projects", "p_ready", "render", "1.mp4")), True)

    print("\nЧужое не трогаем")
    os.makedirs(os.path.join(root, "3", "projects", "p_own"))
    os.makedirs(os.path.join(root, "18", "projects", "p_own"))
    why = L.ensure_project_link(root, "18", "3", "p_own")
    check("своя папка кабинета канала — отказ словами", "уже есть своя папка" in why, True)
    os.makedirs(os.path.join(root, "18", "projects", "p_x"))
    os.makedirs(os.path.join(root, "9", "projects", "p_x"))
    os.symlink(os.path.join(root, "9", "projects", "p_x"), os.path.join(root, "3", "projects", "p_x"))
    check("ссылка не туда — отказ", "в другое место" in L.ensure_project_link(root, "18", "3", "p_x"), True)
    check("нет проекта — отказ", "папки проекта нет" in L.ensure_project_link(root, "18", "3", "p_none"), True)

    print("\nМост устроен так")
    src = open(os.path.join(os.path.dirname(HERE), "pub_bridge.py"), encoding="utf-8").read()
    check("чужой кабинет проверяется, а не отсекается всегда",
          'bridge_link.foreign_allowed(prj["tenant_id"], ch["tenant_id"])' in src, True)
    check("выпуск — в кабинете канала", '"tenant_id": ch["tenant_id"], "channel_id": ch["id"]' in src, True)
    check("ссылка кладётся под замком записи, до выпуска",
          src.find("bridge_link.ensure_project_link(") < src.find("INSERT INTO pub_plan_items"), True)
    check("не легла — причина в отчёте прогона", '"code": "link"' in src, True)

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
