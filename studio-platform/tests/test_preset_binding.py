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
        # GROK/GROK_SPEED и grok — как в реальном main.py: остаются ключами движка,
        # батч 16.09.2026 убирает их только из списка для человека (ниже).
        IMAGE_MODELS = {"OPENAI": ("OpenAI", "1 кр", "r"), "NARWHAL": ("NB2", "4 кр", "r"),
                        "GROK": ("Grok", "3 кр", "recrafter"),
                        "GROK_SPEED": ("Grok Speed", "1 кр", "recrafter")}
        VIDEO_METHODS = {"veo": ("Veo 3.1 — по умолчанию", "", "veo"),
                         "flower": ("Flower · Veo 3.1", "1 кр", "recrafter"),
                         "grok": ("Grok", "2–4 кр", "recrafter")}
        _QUALITY_OK = ("720p", "1080p")
        _MATERIAL_ORDERS = {"video_first", "images_first", "alternate"}
        _SUB_LANG_OK = {"Russian"}
        IMAGE_MODELS_WITH_SEED = {"NARWHAL"}
        _LANG_NAMES = {"ru": "Русский", "en": "English", "pl": "Polski"}

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

    print("\nБатч 16.09.2026: убрать из выбора не значит убрать из движка")

    model_field = build_field("model", {"model": "GROK"})
    model_values = [o["value"] for o in model_field["options"]]
    check("GROK нет в списке моделей", "GROK" in model_values, False)
    check("GROK_SPEED нет в списке моделей",
          any(o["value"] == "GROK_SPEED" for o in model_field["options"]), False)
    check("сохранённый GROK не потерялся описанием (value)",
          model_field["value"], "GROK")

    method_field = build_field("video_method", {"video_method": "grok"})
    method_values = [o["value"] for o in method_field["options"]]
    check("grok нет в списке методов клипов", "grok" in method_values, False)
    method_titles = {o["value"]: o["title"] for o in method_field["options"]}
    check("veo подписан провайдером NonStop", "NonStop" in method_titles["veo"], True)
    check("flower подписан провайдером FastGen", "FastGen" in method_titles["flower"], True)
    check("сохранённый grok не потерялся описанием (value)", method_field["value"], "grok")

    aspect_field = build_field("aspect_ratio", {})
    check("aspect_ratio — выпадающий список", aspect_field["kind"], "select")
    check("aspect_ratio приходит с непустым списком вариантов",
          len(aspect_field["options"]) > 0, True)
    check("подписи ровно как в кабинете (main.py:4124)",
          {o["value"]: o["title"] for o in aspect_field["options"]},
          {"landscape": "Горизонтальное 16:9", "portrait": "Вертикальное 9:16",
           "square": "Квадрат"})

    check("custom_prompts убран из формы", "custom_prompts" in keys, False)
    check("custom_prompts не встречается ни в одной группе",
          any(f["key"] == "custom_prompts"
              for g in PF.describe(Cabinet, {}) for f in g["fields"]), False)

    voice_field = build_field("voice_by_lang", {"engine": "edge"})
    check("voice_by_lang — карта по языкам", voice_field["kind"], "langmap")
    check("языки берутся из _LANG_NAMES кабинета",
          {o["value"] for o in voice_field["options"]}, {"ru", "en", "pl"})
    check("подсказка для обычного движка — про язык→провайдер→идентификатор",
          "идентификатор" in voice_field["hint"], True)
    lumean_hint = build_field("voice_by_lang", {"engine": "lumean"})["hint"]
    check("подсказка для Lumean говорит про template_id, а не голос",
          "template_id" in lumean_hint, True)
    # Подпись поля обязана меняться вместе с подсказкой — иначе человек с Lumean
    # по-прежнему читает в заголовке слово «голоса».
    check("подпись поля для обычного движка — «Голоса по языкам»",
          build_field("voice_by_lang", {"engine": "edge"})["label"], "Голоса по языкам")
    check("подпись поля для Lumean — про пресеты, а не про голоса",
          build_field("voice_by_lang", {"engine": "lumean"})["label"],
          "Пресеты Lumean по языкам")

    # Язык, введённый в строку карты до появления этого выпадающего списка (или код,
    # которого больше нет в _LANG_NAMES), не должен теряться на sync()/сохранении
    # формы. Проверка со стороны описания — value поля
    # остаётся сырой строкой без разбора и фильтрации по известным языкам: фильтрует
    # список выбора JS (langOptionsHtml — стрей-пункт), а не describe().
    stray_raw = "ru: id-1, pt-br: id-2"
    stray_field = build_field("voice_by_lang", {"engine": "edge", "voice_by_lang": stray_raw})
    check("значение с языком вне _LANG_NAMES не фильтруется описанием",
          stray_field["value"], stray_raw)
    check("parse_voice_map принимает код языка вне _LANG_NAMES как есть",
          PF.parse_voice_map(stray_raw), {"ru": "id-1", "pt-br": "id-2"})

    # Второй случай: если вариантов языка вообще нет
    # (кабинет не поднят / _LANG_NAMES не прочитался), поле не должно рисоваться с
    # пустым списком — иначе открытие и сохранение формы вычистило бы карту целиком.
    # Защита та же, что уже стоит у kind == "select" (см. describe()).
    class CabinetNoLangs(Cabinet):
        _LANG_NAMES = {}

    check("без вариантов языка поле langmap не рисуется (не вычищает карту при сохранении)",
          "voice_by_lang" in {f["key"] for g in PF.describe(
              CabinetNoLangs, {"voice_by_lang": stray_raw}) for f in g["fields"]},
          False)

    # Браузер собирает те же строки, что и раньше: два ряда виджета «язык + id» → та же
    # строка "ru: id-1, en: id-2", которую как всегда разбирает parse_voice_map.
    assembled = ", ".join(f"{lang}: {vid}" for lang, vid in
                          (("ru", "id-1"), ("en", "id-2")))
    check("карта голосов по-прежнему разбирается parse_voice_map после сборки браузером",
          PF.parse_voice_map(assembled), {"ru": "id-1", "en": "id-2"})

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
