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
    __slots__ = ("key", "label", "kind", "hint", "options", "off", "hide", "placeholder")

    def __init__(self, key, label, kind="text", hint="", options=None, off=None,
                 hide=None, placeholder=""):
        self.key = key
        self.label = label
        self.kind = kind                  # text | number | select | switch
        self.hint = hint
        # Варианты: функция от модуля кабинета — чтобы список жил там, где он живёт.
        self.options = options
        # Правило гашения: функция от текущих настроек → причина или "".
        self.off = off
        # Правило сокрытия: функция от настроек → True, если поля здесь быть не должно.
        #
        # Разница с `off` не косметическая. `off` — «настройку отменяет другая
        # настройка», и ТЗ §09 прямо требует показать её с объяснением: человек её
        # заполнил, и молча выбросить его работу нельзя. `hide` — «у этого провайдера
        # такой настройки не существует», и ТЗ §06 так же прямо требует её не
        # показывать. Красный текст на месте настройки, которой у Edge отродясь не
        # было, — это шум, а не честность.
        self.hide = hide
        self.placeholder = placeholder


def _engines(m):
    """Движки озвучки в порядке ТЗ §06: VoiceGenCore → Lumean → Voicer → Edge.

    Порядок словаря `TTS_ENGINES` другой (edge первым), и взять его как есть значило бы
    показать не тот список, который просили. Незнакомые движки, если появятся, идут
    следом: молча потерять пункт хуже, чем показать его не на своём месте.
    """
    order = ("voicegencore", "lumean", "voicer", "edge")
    items = dict(m.TTS_ENGINES)
    keys_ = [k for k in order if k in items] + [k for k in items if k not in order]
    return [(k, items[k][0] + (f" · {items[k][1]}" if items[k][1] else "")) for k in keys_]


def _image_models(m):
    return [(k, f"{v[0]} · {v[1]}") for k, v in m.IMAGE_MODELS.items()]


def _video_methods(m):
    return [(k, v[0] + (f" · {v[1]}" if v[1] else "")) for k, v in m.VIDEO_METHODS.items()]


def _quality(m):
    return [(q, q) for q in m._QUALITY_OK]


def _orders(m):
    """Порядок материалов. ТЗ §09 убирает случайный и алфавитный — их здесь нет.

    Список задан прямо, а не отфильтрован из `_MATERIAL_ORDERS`: движок обязан и дальше
    принимать старые значения из сохранённых пресетов, а вот предлагать их человеку
    больше нельзя. Это два разных вопроса, и решать их одним списком — ошибка.
    """
    titles = (("video_first", "сначала клипы"), ("images_first", "сначала кадры"),
              ("alternate", "вперемешку"))
    return [(k, t) for k, t in titles if k in m._MATERIAL_ORDERS]


def _sub_langs(m):
    return [(x, x) for x in sorted(m._SUB_LANG_OK)]


def _seed_off(m, s):
    """Seed принимают четыре модели семейства Nano Banana — у остальных провайдер
    отвергает запрос целиком (ТЗ §07, «починить»).

    Источник правды — движок (`googler_worker.V6_SEED_MODELS`), кабинет держит его
    отражение в `IMAGE_MODELS_WITH_SEED`. Здесь мы только спрашиваем.
    """
    model = str(s.get("model") or "").strip()
    allowed = getattr(m, "IMAGE_MODELS_WITH_SEED", None)
    if not model or allowed is None or model in allowed:
        return ""
    return "эта модель seed не принимает — провайдер отвергает запрос целиком"


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "on", "yes", "да")


def _sync_off(m, s):
    """Мин/макс секунд на кадр при включённом синхроне не действуют (ТЗ §09, «починить»).

    Синхрон заменяет расчёт длительностей целиком: в функции синхрона
    (`render_worker._compute_synced_image_durations`, 188 строк) мин и макс не
    упоминаются ни разу. Поле, которое доезжает до движка и там не читается, — ровно то,
    что запрещает правило 2.
    """
    if not _truthy(s.get("sync_audio")):
        return ""
    return "при синхроне длительность считает звук — эти поля не действуют"


def _tts_only(*supported):
    """Поле есть только у этих движков озвучки (ТЗ §06).

    «Показывать только те настройки, которые выбранный провайдер действительно
    поддерживает» — значит у остальных поля просто нет. Не гасить с объяснением, а не
    показывать: у Edge нет и не было ни stability, ни template_id, и сообщать об этом
    на каждом открытии формы незачем.
    """
    def rule(m, s):
        return str(s.get("engine") or "edge").strip().lower() not in supported
    return rule


# Убрано по ТЗ и сюда не возвращается:
#   §06 вторая строка Lumean — провайдер один, кнопка одна;
#       отдельный раздел «Голоса» — голос живёт в пресете правилом
#       «язык → провайдер → идентификатор»;
#   §07 источник «Поиск Bing» целиком, а вместе с ним поля `tags` и `num_images`;
#       галка «Авто-герой» (`auto_hero`) — дублирует разбор персонажей и конфликтует
#       с ручными референсами (в движке она и работает только когда ни того, ни другого
#       нет: googler_worker.py:8854);
#   §09 «Равномерное распределение»; режимы переходов «быстрые/плавные» вместо да/нет;
#       случайный и алфавитный порядок материалов; масштаб, смещение, интро, наложения;
#       автозвук — выключен до выкатки рендерера на Минск.
GROUPS = (
    ("Озвучка", (
        Field("engine", "Движок", "select", options=_engines),
        Field("speed", "Скорость", "text", placeholder="1.0", hint="1.0 — как есть"),
        Field("voice_by_lang", "Голоса по языкам", "text",
              placeholder="ru: тут-id, en: другой-id",
              hint="правило ТЗ §06: язык → провайдер → идентификатор. "
                   "Язык берётся у канала выпуска"),
        Field("voice", "Голос по умолчанию", "text",
              placeholder="пусто = подберётся по языку текста",
              hint="используется, когда языка нет в списке выше"),
        Field("model_id", "Модель Voicer", "text", hide=_tts_only("voicer")),
        Field("split_type", "Разбивка", "text", placeholder="по абзацам"),
        Field("stability", "Стабильность", "text", hide=_tts_only("voicegencore")),
        Field("voice_uuid", "Lumean template_id", "text", hide=_tts_only("lumean")),
    )),
    ("Картинки", (
        Field("model", "Модель", "select", options=_image_models),
        Field("aspect_ratio", "Соотношение", "text", placeholder="landscape"),
        Field("style", "Стиль", "text", placeholder="пусто = без стиля"),
        Field("sentences_per_prompt", "Предложений на кадр", "text", placeholder="3",
              hint="можно переменным шагом: 1x40,3 — первые сорок по одному, дальше по три"),
        Field("seed", "Seed — тот же результат", "text",
              placeholder="пусто = случайный", off=_seed_off),
        Field("analyze_story", "Разбор персонажей и мест", "switch",
              hint="канон внешности, чтобы герой не менялся от кадра к кадру"),
        Field("split_long_sentences", "Дробить длинные предложения", "switch"),
        Field("max_solo_scene_seconds", "Сек на кадр, примерно", "number"),
        Field("custom_prompts", "Кастом-промты", "text",
              placeholder="по одному в строке; пусто = промпты пишет модель"),
    )),
    ("Клипы", (
        Field("video_method", "Метод", "select", options=_video_methods),
        Field("first_n", "Кадров оживить", "number", placeholder="0 = все"),
        Field("prompt", "Промпт движения", "text"),
    )),
    ("Сборка", (
        Field("quality", "Качество", "select", options=_quality),
        Field("transitions", "Переходы", "select",
              options=lambda m: [("none", "нет"), ("fast", "да")]),
        Field("sync_audio", "Синхрон по озвучке", "switch",
              hint="длительность кадра считается по звуку; без него — по мин/макс"),
        Field("kenburns", "Плавный зум", "switch"),
        Field("subtitles", "Субтитры", "switch"),
        Field("subtitle_lang", "Язык субтитров", "select", options=_sub_langs),
        Field("chapters", "Тайм-коды в описание", "switch"),
        Field("material_order", "Порядок материалов", "select", options=_orders),
        Field("min_image_duration", "Сек на кадр, минимум", "number", off=_sync_off),
        Field("max_image_duration", "Сек на кадр, максимум", "number", off=_sync_off),
    )),
)


# Пятая группа. ТЗ §04: «пресеты 3 → 1» — человек настраивает ролик один раз, а не
# трижды в трёх сервисах. Варианты сюда приходят снаружи (`extra`), потому что списки
# пресетов рерайта и обложек принадлежат соседям, а не кабинету: спрашивать их надо у
# них, а не держать копию.
GROUPS = GROUPS + (
    ("Текст и обложка", (
        Field("preset_rewrite", "Пресет рерайта", "select",
              hint="каким пресетом Rewrite Studio делает текст"),
        Field("preset_cover", "Пресет обложки", "select",
              hint="каким пресетом Cover Studio делает обложку"),
    )),
)


def describe(mod, settings: dict, extra: dict = None) -> list:
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
                if f.key in (extra or {}):
                    options = [{"value": v, "title": v} for v in (extra or {})[f.key]]
                elif f.options and mod is not None:
                    options = [{"value": v, "title": t} for v, t in f.options(mod)]
                else:
                    options = []
            except Exception:                                      # noqa: BLE001
                options = []
            if f.kind == "select" and not options:
                continue          # список неоткуда взять — поля не будет, а не вранья
            try:
                if f.hide and f.hide(mod, s):
                    continue
            except Exception:                                      # noqa: BLE001
                pass          # правило сломалось — лучше показать поле, чем потерять
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


def parse_voice_map(raw) -> dict:
    """«ru: id, en: другой» → {"ru": "id", "en": "другой"}.

    Формат нарочно простой: список языков у канала короткий, а таблица в отдельном
    экране — это ровно тот раздел «Голоса», который ТЗ §06 велит убрать. Разделители
    принимаем и запятую, и перевод строки: человек напишет так, как ему удобнее, и
    угадывать его форму — не его работа.
    """
    out = {}
    for chunk in str(raw or "").replace("\n", ",").split(","):
        lang, sep, voice = chunk.partition(":")
        if not sep:
            continue
        lang, voice = lang.strip().lower(), voice.strip()
        if lang and voice:
            out[lang] = voice
    return out


def voice_for(settings: dict, lang: str) -> str:
    """Голос для этого языка, иначе голос по умолчанию, иначе пусто.

    Пусто — это не отказ: движок озвучки сам подберёт голос по языку текста. Отказ был
    бы неправ: у Edge голоса встроены, и требовать идентификатор незачем.
    """
    s = settings if isinstance(settings, dict) else {}
    code = str(lang or "").strip().lower()
    got = parse_voice_map(s.get("voice_by_lang")).get(code) if code else ""
    return got or str(s.get("voice") or "").strip()
