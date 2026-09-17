#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QA фазы 1 батча «Пресеты целые и частичные»: независимая проверка `preset_links.resolve`
поверх того, что уже покрыто `tests/test_preset_links.py`.

Здесь — сценарии, которых основной тест не проверяет напрямую через `resolve()`:
несколько одновременных ссылок сразу (одна рабочая + одна сломанная), подмена ключей
`PART_EXTRA_KEYS` (а не только полей формы `GROUPS`), неприкосновенность `SHARED_KEYS`
при подмене группы, `resolve()` без пресета (`None`/`0`) и ссылка сама на себя.

Без сети, база только в памяти.

    .venv/bin/python tests/test_preset_links_qa.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import preset_fields as PF                                        # noqa: E402
import preset_links                                                # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def fresh() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE presets(id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT, "
              "settings TEXT)")
    return c


def put(c, pid, settings, tenant="dev"):
    c.execute("INSERT INTO presets(id, tenant_id, name, settings) VALUES(?, ?, ?, ?)",
              (pid, tenant, f"пресет {pid}", json.dumps(settings, ensure_ascii=False)))


def main() -> int:
    print("Две ссылки одновременно (voice + images): каждая группа подменяется "
          "независимо, без взаимного влияния")
    c = fresh()
    put(c, 10, {"_scope": "voice", "engine": "lumean", "voice_uuid": "vvv"})
    put(c, 20, {"_scope": "images", "model": "NARWHAL", "style": "нуар"})
    put(c, 1, {"engine": "edge", "voice_uuid": "своё-стёртое", "model": "OPENAI",
               "style": "своё-стёртое-тоже", "quality": "1080p",
               "_links": {"voice": 10, "images": 20}})
    r = preset_links.resolve(c, 1)
    check("voice.engine — из частичного 10", r.get("engine"), "lumean")
    check("voice.voice_uuid — из частичного 10", r.get("voice_uuid"), "vvv")
    check("images.model — из частичного 20", r.get("model"), "NARWHAL")
    check("images.style — из частичного 20", r.get("style"), "нуар")
    check("render.quality — своё, эту группу ссылки не трогают", r.get("quality"), "1080p")
    check("проблем нет", r["_link_problems"], [])
    check("обе ссылки видны в _links", r["_links"], {"voice": 10, "images": 20})

    print("\nОдна ссылка рабочая, другая сломанная одновременно: рабочая применяется, "
          "сломанная даёт свои поля + отдельную причину, друг другу не мешают")
    c = fresh()
    put(c, 10, {"_scope": "voice", "engine": "lumean"})
    put(c, 1, {"engine": "edge", "model": "своё-осталось",
               "_links": {"voice": 10, "images": 999}})
    r = preset_links.resolve(c, 1)
    check("рабочая ссылка (voice) применилась", r.get("engine"), "lumean")
    check("сломанная ссылка (images) — своё поле осталось", r.get("model"), "своё-осталось")
    check("проблема только по images", [p["part"] for p in r["_link_problems"]], ["images"])
    check("причина — пресет удалён/повреждён", r["_link_problems"][0]["why"],
          "частичный пресет удалён или его настройки повреждены")

    print("\nСсылка подменяет и ключи PART_EXTRA_KEYS, а не только поля формы GROUPS "
          "(правило 3: группа = GROUPS ∪ PART_EXTRA_KEYS, а не только видимая форма)")
    c = fresh()
    put(c, 10, {"_scope": "voice", "voice_engine": "voicer-новый", "model_uuid": "muid-новый"})
    put(c, 1, {"voice_engine": "voicer-старый", "model_uuid": "muid-старый",
               "_links": {"voice": 10}})
    r = preset_links.resolve(c, 1)
    check("voice_engine (PART_EXTRA_KEYS) — из частичного", r.get("voice_engine"),
          "voicer-новый")
    check("model_uuid (PART_EXTRA_KEYS) — из частичного", r.get("model_uuid"), "muid-новый")

    c = fresh()
    put(c, 20, {"_scope": "images", "smart_mode": "1", "num_images": "9"})
    put(c, 1, {"smart_mode": "0", "num_images": "1", "_links": {"images": 20}})
    r = preset_links.resolve(c, 1)
    check("smart_mode (PART_EXTRA_KEYS images) — из частичного", r.get("smart_mode"), "1")
    check("num_images (PART_EXTRA_KEYS images) — из частичного", r.get("num_images"), "9")

    print("\nSHARED_KEYS (use_cache) — общий флаг нескольких этапов, ссылка на часть "
          "его не трогает: он всегда берётся от целого пресета целиком")
    c = fresh()
    # У частичного «Картинки» тоже есть use_cache — это мусор с точки зрения архитектуры
    # (use_cache не входит в group_keys('images')), и resolve() обязан его игнорировать.
    put(c, 20, {"_scope": "images", "model": "NARWHAL", "use_cache": "из-частичного-неважно"})
    put(c, 1, {"model": "своё-стёртое", "use_cache": "1", "_links": {"images": 20}})
    r = preset_links.resolve(c, 1)
    check("images.model — из частичного (сама группа подменилась)", r.get("model"), "NARWHAL")
    check("use_cache — от целого, ссылка на images его не подменяет",
          r.get("use_cache"), "1")
    check("use_cache не в group_keys('images')", "use_cache" in PF.part_group_keys("images"),
          False)

    print("\nresolve() без пресета: None и 0 не роняют разрешение, дают пустой целый")
    c = fresh()
    r_none = preset_links.resolve(c, None)
    check("resolve(None) — full, пустые поля", r_none["_scope"], "full")
    check("resolve(None) — _links пуст", r_none["_links"], {})
    check("resolve(None) — проблем нет", r_none["_link_problems"], [])
    r_zero = preset_links.resolve(c, 0)
    check("resolve(0) — тоже full, не падает", r_zero["_scope"], "full")

    print("\nСсылка на самого себя (_links.voice == собственный id): у самого целого "
          "нет _scope=='voice' (он full), поэтому это «не та часть», а не бесконечный "
          "цикл или падение")
    c = fresh()
    put(c, 1, {"engine": "своё", "_links": {"voice": 1}})
    r = preset_links.resolve(c, 1)
    check("своё поле осталось", r.get("engine"), "своё")
    check("причина — не та часть (сам целый, не voice)", r["_link_problems"],
          [{"part": "voice", "preset_id": 1, "why": "частичный пресет не той части"}])

    print("\nusers_of на пресете с несколькими держателями через одну и ту же часть — "
          "оба целых попадают в presets/via, без дублей и без пропусков")
    c = fresh()
    c.execute("CREATE TABLE pub_channels(id INTEGER PRIMARY KEY, tenant_id TEXT, name TEXT, "
              "preset_vsgen_id INTEGER)")
    c.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT, "
              "settings TEXT)")
    put(c, 10, {"_scope": "voice", "engine": "lumean"})
    put(c, 1, {"engine": "edge", "_links": {"voice": 10}})
    put(c, 2, {"engine": "edge", "_links": {"voice": 10}})
    c.execute("INSERT INTO pub_channels(id, tenant_id, name, preset_vsgen_id) "
              "VALUES(1, 'dev', 'Канал на 1', 1)")
    c.execute("INSERT INTO pub_channels(id, tenant_id, name, preset_vsgen_id) "
              "VALUES(2, 'dev', 'Канал на 2', 2)")
    u10 = preset_links.users_of(c, 10)
    check("оба целых пресета — держатели частичного", sorted(x["id"] for x in u10["presets"]),
          [1, 2])
    check("транзитивные каналы через оба целых", sorted(x["id"] for x in u10["via"]), [1, 2])

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
