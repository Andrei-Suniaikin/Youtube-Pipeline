#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Один пресет на канал: текст, обложка и голос берутся из него.

Решение владельца 09.09.2026 (ТЗ §04 «пресеты 3 → 1»). До него канал держал три ссылки
на три сервиса, и один ролик настраивался в трёх местах.

Сети здесь нет: проверяется разрешение настроек, а не поход к провайдерам.

    .venv/bin/python tests/test_preset_binding.py
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
from pub_engine import channel_presets, preset_settings           # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def fresh(settings=None) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE presets(id INTEGER PRIMARY KEY, settings TEXT)")
    if settings is not None:
        c.execute("INSERT INTO presets(id, settings) VALUES(1, ?)",
                  (json.dumps(settings, ensure_ascii=False),))
    return c


def main() -> int:
    print("Пресет решает, чем делаются текст и обложка")
    c = fresh({"preset_rewrite": "Быстрый", "preset_cover": "Тёмная"})
    check("оба взяты из пресета", channel_presets(c, 1, "стар-рерайт", "стар-обложка"),
          ("Быстрый", "Тёмная"))

    # Пустое поле в пресете — это «не задано», а не «убрать»: у каналов, заведённых до
    # переключения, пресет про соседей ещё ничего не знает, и обнулить им текст с
    # обложкой значило бы остановить выпуски.
    c = fresh({"preset_cover": "Тёмная"})
    check("чего в пресете нет — берётся у канала",
          channel_presets(c, 1, "стар-рерайт", "стар-обложка"), ("стар-рерайт", "Тёмная"))

    c = fresh({})
    check("пустой пресет ничего не ломает",
          channel_presets(c, 1, "стар-рерайт", "стар-обложка"),
          ("стар-рерайт", "стар-обложка"))

    c = fresh(None)
    check("удалённый пресет — тоже",
          channel_presets(c, 1, "стар-рерайт", "стар-обложка"),
          ("стар-рерайт", "стар-обложка"))
    check("канал без пресета", channel_presets(c, None, "р", "о"), ("р", "о"))

    c = fresh()
    c.execute("INSERT INTO presets(id, settings) VALUES(2, 'не json')")
    check("битые настройки не роняют разбор", preset_settings(c, 2), {})

    print("\nГолос по языку — правило «язык → провайдер → идентификатор»")
    check("запятыми", PF.parse_voice_map("ru: aaa, en: bbb"), {"ru": "aaa", "en": "bbb"})
    check("строками", PF.parse_voice_map("ru: aaa\nPL: ccc"), {"ru": "aaa", "pl": "ccc"})
    check("мусор пропускается", PF.parse_voice_map("ru: aaa, чепуха, : x, en:"),
          {"ru": "aaa"})
    check("пусто", PF.parse_voice_map(""), {})

    s = {"voice_by_lang": "ru: ru-id, pl: pl-id", "voice": "запасной"}
    check("язык найден", PF.voice_for(s, "ru"), "ru-id")
    check("регистр не важен", PF.voice_for(s, "PL"), "pl-id")
    check("языка нет — голос по умолчанию", PF.voice_for(s, "de"), "запасной")
    check("языка нет и умолчания нет",
          PF.voice_for({"voice_by_lang": "ru: ru-id"}, "de"), "")
    # Пусто — не отказ: движок сам подберёт голос по языку текста. У Edge голоса
    # встроены, требовать идентификатор там незачем.
    check("без карты работает старое поле", PF.voice_for({"voice": "один"}, "ru"), "один")

    print("\nНастройки чужого движка не показываются, а не гаснут")

    class Cabinet:
        """Заглушка кабинета: описанию нужны только списки вариантов."""
        TTS_ENGINES = {"edge": ("Edge", "бесплатно"), "voicegencore": ("VGC", "ключ"),
                       "voicer": ("Voicer", "ключ"), "lumean": ("Lumean", "ключ")}
        IMAGE_MODELS = {"OPENAI": ("OpenAI", "1 кр", "r"), "NARWHAL": ("NB2", "4 кр", "r")}
        VIDEO_METHODS = {"veo": ("Veo", "", "v")}
        _QUALITY_OK = ("720p", "1080p")
        _MATERIAL_ORDERS = {"video_first", "images_first", "alternate"}
        _SUB_LANG_OK = {"Russian"}
        IMAGE_MODELS_WITH_SEED = {"NARWHAL"}

    def voice_fields(engine):
        g = PF.describe(Cabinet, {"engine": engine})
        return [f["key"] for f in g[0]["fields"]]

    check("у Edge нет ни stability, ни template_id",
          [k for k in ("stability", "voice_uuid", "model_id") if k in voice_fields("edge")],
          [])
    check("у VoiceGenCore есть stability", "stability" in voice_fields("voicegencore"), True)
    check("у Lumean есть template_id", "voice_uuid" in voice_fields("lumean"), True)
    check("у Voicer есть своя модель", "model_id" in voice_fields("voicer"), True)

    # ТЗ различает два случая, и мы вместе с ним. Настройки чужого движка не существует —
    # её не показываем (§06). Настройку, отменённую другой настройкой, показываем с
    # объяснением: человек её заполнял, и молча выбросить его работу нельзя (§09).
    def build_field(key, settings):
        for g in PF.describe(Cabinet, settings):
            for f in g["fields"]:
                if f["key"] == key:
                    return f
        return None

    check("мин/макс при синхроне остаётся на месте",
          bool(build_field("min_image_duration", {"sync_audio": "1"})), True)
    check("и объясняет, почему не действует",
          "не действуют" in build_field("min_image_duration", {"sync_audio": "1"})["off"],
          True)
    check("без синхрона объяснения нет",
          build_field("min_image_duration", {"sync_audio": "0"})["off"], "")
    check("seed у неподходящей модели тоже объясняется, а не прячется",
          bool(build_field("seed", {"model": "OPENAI"})["off"]), True)

    print("\nФорма пресета знает эти поля")
    keys = PF.keys()
    for k in ("preset_rewrite", "preset_cover", "voice_by_lang", "voice"):
        check(f"поле {k} есть", k in keys, True)

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
