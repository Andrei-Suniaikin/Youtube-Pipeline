#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Собрать публичные страницы для домена: подставить имя, домен и почту.

Шаблоны (`index.html`, `privacy.html`, `terms.html`) остаются с подстановками — их
проверяет `check_pages.py`. Сюда значения приходят из `site.json` (04.10.2026: домен
vovastudio.work), а на выходе НЕ должно остаться ни одной `{{…}}`: незаменённая
подстановка на живой странице — отказ в проверке приложения Google.

    .venv/bin/python public/build_pages.py --out /куда/положить
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PAGES = ("index.html", "privacy.html", "terms.html")
KEYS = {"{{НАЗВАНИЕ}}": "name", "{{ДОМЕН}}": "domain", "{{ПОЧТА}}": "mail"}


def build(out_dir: str, site: dict) -> list:
    missing = [k for k in KEYS.values() if not str(site.get(k) or "").strip()]
    if missing:
        raise SystemExit(f"в site.json не заполнено: {', '.join(missing)}")
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for page in PAGES:
        text = open(os.path.join(HERE, page), encoding="utf-8").read()
        for mark, key in KEYS.items():
            text = text.replace(mark, str(site[key]).strip())
        left = sorted(set(re.findall(r"\{\{[^}]{1,40}\}\}", text)))
        if left:
            raise SystemExit(f"{page}: остались подстановки {left}")
        path = os.path.join(out_dir, page)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        written.append(path)
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--site", default=os.path.join(HERE, "site.json"))
    a = ap.parse_args(argv)
    site = json.load(open(a.site, encoding="utf-8"))
    for path in build(a.out, site):
        print("собрано:", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
