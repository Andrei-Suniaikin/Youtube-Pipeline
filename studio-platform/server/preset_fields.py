#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Из чего состоит пресет производства — одним описанием на всех.

Зачем модуль. Форма редактора пресета в кабинете собрана из ста семидесяти строк
f-строк: список полей, их подписи, варианты выбора и правила «когда поле гаснет»
существуют там ровно один раз — внутри HTML. Повторить эту форму в пульте значило бы
завести вторую правду о том, что такое пресет: добавили поле в одном месте, забыли в
другом, и человек настраивает не то, что уезжает в движок.

Поэтому описание вынесено сюда, а варианты выбора **не копируются**, а берутся из тех
же констант кабинета (`TTS_ENGINES`, `IMAGE_MODELS`, `VIDEO_METHODS`, `_QUALITY_OK`…).
Появилась новая модель картинок — она появляется в форме сама.

Чего здесь НЕТ и не будет: проверок и умолчаний. Значения проверяет кабинет своими
контрактами (`_canonical_image_params`, `_normalize_lumean_settings`) на сохранении —
там же, где всегда. Описание отвечает только на вопрос «какие поля показать и что о
них сказать человеку».

`off` — правило «поле гаснет с объяснением» (правило 2 ТЗ). Пустая строка = поле живо.
"""
from __future__ import annotations


class Field:
    __slots__ = ("key", "label", "kind", "hint", "options", "off", "placeholder")

    def __init__(self, key, label, kind="text", hint="", options=None, off=None,
                 placeholder=""):
        self.key = key
        self.label = label
        self.kind = kind                  # text | number | select | switch
        self.hint = hint
        # Варианты: функция от модуля кабинета — чтобы список жил там, где он живёт.
        self.options = options
        # Правило гашения: функция от текущих настроек → причина или "".
        self.off = off
        self.placeholder = placeholder


def _engines(m):
    return [(k, v[0] + (f" · {v[1]}" if v[1] else "")) for k, v in m.TTS_ENGINES.items()]


def _image_models(m):
    return [(k, f"{v[0]} · {v[1]}") for k, v in m.IMAGE_MODELS.items()]


def _video_methods(m):
    return [(k, v[0] + (f" · {v[1]}" if v[1] else "")) for k, v in m.VIDEO_METHODS.items()]


def _quality(m):
    return [(q, q) for q in m._QUALITY_OK]


def _orders(m):
    titles = {"video_first": "сначала клипы", "images_first": "сначала кадры",
              "alternate": "вперемешку", "alphabetical": "по имени файла",
              "random": "случайно"}
    return [(k, titles.get(k, k)) for k in sorted(m._MATERIAL_ORDERS)]


def _sub_langs(m):
    return [(x, x) for x in sorted(m._SUB_LANG_OK)]


def _seed_off(m, s):
    """Seed принимают три модели из двенадцати — у остальных провайдер отвергает запрос.

    Источник правды — движок (`googler_worker.V6_SEED_MODELS`), кабинет держит его
    отражение в `IMAGE_MODELS_WITH_SEED`. Здесь мы только спрашиваем.
    """
    model = str(s.get("model") or "").strip()
    allowed = getattr(m, "IMAGE_MODELS_WITH_SEED", None)
    if not model or allowed is None or model in allowed:
        return ""
    return "эта модель seed не принимает — провайдер отвергает запрос целиком"


def _bing_off(m, s):
    if str(s.get("image_mode") or "ai") == "bing":
        return ""
    return "только для источника «Поиск Bing»"


def _ai_off(m, s):
    if str(s.get("image_mode") or "ai") == "ai":
        return ""
    return "только для источника «AI-генерация»"


GROUPS = (
    ("Озвучка", (
        Field("engine", "Движок", "select", options=_engines),
        Field("speed", "Скорость", "text", placeholder="1.0", hint="1.0 — как есть"),
        Field("voice", "Голос / Voice ID", "text", placeholder="пусто = по языку"),
        Field("model_id", "Модель озвучки", "text", placeholder="пусто = по умолчанию"),
        Field("split_type", "Разбивка", "text", placeholder="по абзацам"),
        Field("stability", "Стабильность", "text"),
    )),
    ("Картинки", (
        Field("image_mode", "Источник", "select",
              options=lambda m: [("ai", "AI-генерация"), ("bing", "Поиск Bing")]),
        Field("model", "Модель", "select", options=_image_models, off=_ai_off),
        Field("aspect_ratio", "Соотношение", "text", placeholder="landscape"),
        Field("style", "Стиль", "text", placeholder="пусто = без стиля"),
        Field("sentences_per_prompt", "Предложений на кадр", "number", placeholder="3"),
        Field("seed", "Seed — тот же результат", "text",
              placeholder="пусто = случайный", off=_seed_off),
        Field("auto_hero", "Разбор персонажей и мест", "switch",
              hint="континуити между кадрами"),
        Field("max_solo_scene_seconds", "Сек на кадр, примерно", "number"),
        Field("tags", "Теги для Bing", "text", off=_bing_off),
        Field("custom_prompts", "Кастом-промты", "text"),
    )),
    ("Клипы", (
        Field("video_method", "Метод", "select", options=_video_methods),
        Field("first_n", "Кадров оживить", "number", placeholder="0 = все"),
        Field("prompt", "Промпт движения", "text"),
    )),
    ("Сборка", (
        Field("quality", "Качество", "select", options=_quality),
        Field("transitions", "Переходы", "select",
              options=lambda m: [("none", "без переходов"), ("fast", "плавные")]),
        Field("sync_audio", "Синхрон по озвучке", "switch",
              hint="длительность кадра считается по звуку"),
        Field("kenburns", "Плавный зум", "switch"),
        Field("subtitles", "Субтитры", "switch"),
        Field("subtitle_lang", "Язык субтитров", "select", options=_sub_langs),
        Field("chapters", "Тайм-коды в описание", "switch"),
        Field("material_order", "Порядок материалов", "select", options=_orders),
        Field("min_image_duration", "Сек на кадр, минимум", "number"),
        Field("max_image_duration", "Сек на кадр, максимум", "number"),
    )),
)


def describe(mod, settings: dict) -> list:
    """Группы полей с текущими значениями — то, из чего рисуется форма.

    `mod` — модуль кабинета (`main`). Без него варианты выбора неоткуда взять, и это
    намеренно: пусть форма будет пустой, чем покажет выдуманный список моделей.
    """
    s = settings if isinstance(settings, dict) else {}
    out = []
    for title, fields in GROUPS:
        items = []
        for f in fields:
            try:
                options = [{"value": v, "title": t} for v, t in f.options(mod)] \
                    if (f.options and mod is not None) else []
            except Exception:                                      # noqa: BLE001
                options = []
            if f.kind == "select" and not options:
                continue          # список неоткуда взять — поля не будет, а не вранья
            try:
                off = f.off(mod, s) if f.off else ""
            except Exception:                                      # noqa: BLE001
                off = ""
            value = s.get(f.key)
            items.append({"key": f.key, "label": f.label, "kind": f.kind,
                          "hint": f.hint, "placeholder": f.placeholder,
                          "options": options, "off": off,
                          "value": "" if value is None else str(value)})
        if items:
            out.append({"title": title, "fields": items})
    return out


def keys() -> set:
    """Все поля, которыми управляет эта форма. Кабинет на сохранении делает merge,
    поэтому чужие ключи пресета (их там больше) не затираются."""
    return {f.key for _t, fields in GROUPS for f in fields}
