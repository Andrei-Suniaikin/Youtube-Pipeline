#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Публичные страницы для Google собираются под домен без единой подстановки (04.10.2026).

Домен vovastudio.work куплен; визитка, политика и условия лежат на нём для настройки
согласия Google. Шаблоны остаются с подстановками (их проверяет public/check_pages.py),
а собранные страницы — нет: незаменённая `{{ПОЧТА}}` на живой странице — отказ в проверке.

    .venv/bin/python tests/test_public_pages_build.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "public"))

import build_pages as B                                           # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def main() -> int:
    site = json.load(open(os.path.join(BASE, "public", "site.json"), encoding="utf-8"))
    print("Значения сайта")
    check("домен", site["domain"], "vovastudio.work")
    check("почта на том же домене", site["mail"].endswith("@" + site["domain"]), True)

    print("\nСборка")
    out = tempfile.mkdtemp(prefix="pub-")
    written = B.build(out, site)
    check("три страницы", sorted(os.path.basename(p) for p in written),
          ["index.html", "privacy.html", "terms.html"])
    for p in written:
        t = open(p, encoding="utf-8").read()
        name = os.path.basename(p)
        check(f"{name}: подстановок не осталось", "{{" in t, False)
        check(f"{name}: имя приложения", site["name"] in t, True)
        check(f"{name}: почта для связи", f"mailto:{site['mail']}" in t, True)
    idx = open(os.path.join(out, "index.html"), encoding="utf-8").read()
    check("визитка ведёт на /privacy и /terms",
          'href="/privacy"' in idx and 'href="/terms"' in idx, True)

    print("\nПустое значение — сборка отказывает")
    try:
        B.build(tempfile.mkdtemp(), {**site, "mail": ""})
        got = "собрано"
    except SystemExit as exc:
        got = "mail" in str(exc)
    check("отказ с названием поля", got, True)

    print("\nШаблоны по-прежнему годны")
    r = subprocess.run([sys.executable, os.path.join(BASE, "public", "check_pages.py")],
                       capture_output=True, text=True)
    check("check_pages.py проходит", r.stdout.strip().splitlines()[-1], "все проверки прошли")
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
