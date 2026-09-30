# -*- coding: utf-8 -*-
"""Описание и ключевые слова каналов из задания оператору → поля description/keywords в channels.json.

Задание (~/niches/манхва_2026-09/оформление/ЗАДАНИЕ_ОПЕРАТОРУ_оформить_20_каналов.md) — блоки
«## N. Название · ЯЗ · …» с «**Хендл:** `@ключ`», «**Описание:**» и «**Ключевые слова:**» в ```…```.
Канал ищется по хендлу (он же ключ в channels.json), а без совпадения — по названию.

    python3 app/channel_blurbs.py ЗАДАНИЕ.md channels.json -o channels.new.json

Исходный channels.json не меняется; новый файл проверяется так же, как его читает студия.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    from . import channels as CH
except ImportError:
    import channels as CH

BLOCK = re.compile(r"^## \d+\.\s+(?P<name>[^·\n]+?)\s+·", re.M)
FENCED = r"\*\*{label}:\*\*\s*\n```[^\n]*\n(?P<body>.*?)\n```"


def parse(text: str) -> list:
    """[{name, handle, description, keywords}] по блокам задания."""
    heads = list(BLOCK.finditer(text))
    out = []
    for i, head in enumerate(heads):
        body = text[head.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        handle = re.search(r"\*\*Хендл:\*\*\s*`@?([^`]+)`", body)
        desc = re.search(FENCED.format(label="Описание"), body, re.S)
        keys = re.search(FENCED.format(label="Ключевые слова"), body, re.S)
        out.append(dict(name=head.group("name").strip(), handle=handle.group(1).strip().lower() if handle else "",
                        description=desc.group("body").strip() if desc else "",
                        keywords=keys.group("body").strip() if keys else ""))
    return out


def merge(channels_data: dict, blurbs: list):
    """(новые данные channels.json, список каналов без описания)."""
    items = channels_data.get("channels") if isinstance(channels_data, dict) else channels_data
    by_handle = {b["handle"]: b for b in blurbs if b["handle"]}
    by_name = {b["name"].casefold(): b for b in blurbs}
    missing, merged = [], []
    for item in items:
        blurb = by_handle.get(str(item.get("key", "")).lower()) or by_name.get(str(item.get("name", "")).casefold())
        item = dict(item)
        if blurb and blurb["description"]:
            item["description"] = blurb["description"]
            if blurb["keywords"]:
                item["keywords"] = blurb["keywords"]
        else:
            missing.append(item.get("name") or item.get("key"))
        merged.append(item)
    data = {"channels": merged}
    CH.validate(data)
    return data, missing


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("task_md")
    ap.add_argument("channels_json")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args(argv)
    blurbs = parse(Path(args.task_md).read_text(encoding="utf-8"))
    data, missing = merge(json.loads(Path(args.channels_json).read_text(encoding="utf-8-sig")), blurbs)
    Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    done = len(data["channels"]) - len(missing)
    print(f"Описание добавлено каналам: {done} из {len(data['channels'])}. Файл: {args.out}")
    if missing:
        print("Без описания: " + ", ".join(missing), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
