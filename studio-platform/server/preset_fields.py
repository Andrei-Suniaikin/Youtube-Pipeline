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
        # Подпись обычно строка, но у «голосов по языкам» меняется вместе с движком:
        # для Lumean идентификатор — id пресета, а не голос, и заголовок поля обязан
        # сказать это так же прямо, как уже говорит подсказка.
        # Тогда сюда кладётся функция (модуль кабинета, настройки) → строка — тем же
        # контрактом, что и у `hint`/`off`/`hide`, а не своим отдельным.
        self.label = label
        self.kind = kind                  # text | number | select | switch | langmap
        # Подсказка обычно строка, но иногда зависит от другого поля — например у
        # голосов по языкам она разная для Lumean (там id — это template_id, а не
        # голос). Тогда сюда кладётся функция (модуль кабинета, настройки) → строка,
        # тем же контрактом, что и у `off`/`hide`, а не своим отдельным.
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


# Чёрный список, а не образец `_orders` (список для человека задан прямо, отфильтрован
# по константе для совместимости). Решение сознательное: у моделей картинок список
# кабинета живой и растёт, а тут убираются только эти два ключа по решению владельца —
# если завтра появится третья модель, она обязана
# показаться в форме сама, без новой правки этого файла. `_orders`, наоборот, закрытый
# список из трёх пунктов ТЗ §09, которому автопоявление новых значений не нужно и не
# обещано.
_HIDDEN_IMAGE_MODELS = {"GROK", "GROK_SPEED"}


def _fg_status_map(m):
    """id модели FastGen → текст живого статуса («работает»/«нестабильно»/«лежит»/
    «нет данных»), батч 16.09.2026 (пункт 2.1 ТЗ).

    Источник — только `m.fastgen_status`: свои пороги здесь не заводятся, `_fg_classify`
    уже посчитал их в кабинете. Ключ берётся тем же способом, что кабинет берёт
    серверный ключ `googler` (`keys()`) — без обращения к своим ключам пользователя,
    их здесь неоткуда взять (`describe()` не получает `request`/юзера).

    Всё под `getattr`+`try`: модуль-заглушка в тестах не обязан иметь ни `keys`, ни
    `fastgen_status`, а сеть или ключ могут молчать в проде — ни один из этих случаев не
    должен ронять форму (правило «форма обязана открываться»).
    """
    keys_fn = getattr(m, "keys", None)
    first_cred = getattr(m, "_first_credential", None)
    status_fn = getattr(m, "fastgen_status", None)
    if not (keys_fn and first_cred and status_fn):
        return {}
    try:
        key = first_cred(keys_fn(), "googler")
        if not key:
            return {}
        st = status_fn(key)
    except Exception:                                              # noqa: BLE001
        return {}
    return {row.get("id"): row.get("txt") for row in (st.get("models") or [])
            if row.get("id")}


def _image_models(m):
    """Модели картинок для выбора человеком.

    GROK и GROK_SPEED убраны из списка (батч 16.09.2026, решение владельца) — но не из
    движка: `main.py:3612` отвергает только по-настоящему незнакомые ключи, эти два в
    их числе не значатся, и старый пресет с GROK обязан продолжать работать. Ключ,
    которого нет в списке для человека, — не то же самое, что ключ, которого нет вовсе.

    Статус (пункт 2.1 ТЗ, батч 16.09.2026): у моделей с провайдером `recrafter`
    (третий элемент кортежа `IMAGE_MODELS` — тот же факт, что читает `_video_methods`
    выше) подпись дополняется живым статусом FastGen. У `GPT_IMAGE2` (`openai`) и
    `VEO_*` (`veo`) статуса у health-API нет вовсе — им ничего не дописываем, а не
    подставляем «нет данных»: это разные вещи («источник молчит» vs «источника нет»).
    """
    status = _fg_status_map(m)
    out = []
    for k, v in m.IMAGE_MODELS.items():
        if k in _HIDDEN_IMAGE_MODELS:
            continue
        label = f"{v[0]} · {v[1]}"
        provider = v[2] if len(v) > 2 else ""
        if provider == "recrafter":
            label += f" · {status.get(k, 'нет данных')}"
        out.append((k, label))
    return out


# Тот же чёрный список по той же причине, что и у моделей картинок выше: методы
# клипов в кабинете тоже растут, а решение владельца — про один конкретный ключ
# `grok`, не про то, что список методов впредь надо перечислять для человека вручную.
_HIDDEN_VIDEO_METHODS = {"grok"}
# Провайдер, который реально исполняет метод, — третий элемент кортежа
# `VIDEO_METHODS`, не наш отдельный список: два списка одного и того же факта
# разошлись бы первыми же (googler_worker.py:11610 — veo это VeoNonStop,
# recrafter.fun это FastGen).
#
# Подписи — свой короткий словарь, а не `main.PROVIDER_LABELS`: в кабинете
# (`main.py:3628`) эти подписи длинные и служебные — «VeoNonStop», «Recrafter /
# FastGen · кредиты» — они делают понятным весь список
# провайдеров кабинета. Здесь же подпись — это третье слово в узкой строке метода
# («Flower · Veo 3.1 · FastGen · 1 кр», ТЗ п. 3.2 просит именно короткие «NonStop» и
# «FastGen»), и подставить туда длинную форму значило бы захламить строку, которую и
# так читают в таблице. Это выбор человеческого текста для тесного места, а не второй
# список факта «кто провайдер» — сам факт (какой ключ у какого метода) по-прежнему
# берётся из кортежа `VIDEO_METHODS`, а не отсюда.
_VIDEO_PROVIDER_TITLE = {"veo": "NonStop", "recrafter": "FastGen"}


def _video_methods(m):
    """Методы клипов для выбора человеком, с подписанным провайдером (батч 16.09.2026).

    `grok` убран из списка тем же способом, что и модели картинок выше: движок обязан
    принимать его из старых пресетов и дальше.
    """
    out = []
    for k, v in m.VIDEO_METHODS.items():
        if k in _HIDDEN_VIDEO_METHODS:
            continue
        label, price = v[0], (v[1] if len(v) > 1 else "")
        provider = v[2] if len(v) > 2 else ""
        parts = [label, _VIDEO_PROVIDER_TITLE.get(provider, provider)]
        if price:
            parts.append(price)
        out.append((k, " · ".join(p for p in parts if p)))
    return out


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


def _aspect_ratio(m):
    """Три соотношения кадра, слово в слово как в форме кабинета.

    В кабинете (`main.py:4124`) эти три варианта вписаны прямо в HTML формы, а не в
    отдельную константу модуля — там неоткуда взять список через `options=`, как у
    остальных полей. Задаём его здесь напрямую и подписываем ссылкой на источник, чтобы
    расхождение было видно при следующей правке той строки.
    """
    return [("landscape", "Горизонтальное 16:9"), ("portrait", "Вертикальное 9:16"),
            ("square", "Квадрат")]


def _lang_options(m):
    """Языки для строк карты «голоса по языкам» (батч 16.09.2026).

    Задача называла `_SUB_LANGS_ALL` и `LANGUAGE_CODES` — ни один не подошёл:
    `_SUB_LANGS_ALL` в studio-platform хранит не двухбуквенные коды, а полные
    английские названия языков субтитров, а `LANGUAGE_CODES` здесь не существует
    вовсе. `_LANG_NAMES` (`main.py:7275`) — тот же формат «код → имя», в котором и
    хранится сам `voice_by_lang` («ru: id, en: id2»), поэтому источник — он.
    """
    names = getattr(m, "_LANG_NAMES", None) or {}
    return [(code, f"{code} — {name}") for code, name in sorted(names.items())]


def _voice_by_lang_label(m, s):
    """Подпись поля — тем же контрактом, что и подсказка ниже.

    Раньше про Lumean говорила только подсказка под полем, а заголовок оставался
    «Голоса по языкам» — человек, настроивший Lumean, читал в заголовке слово «голоса»,
    хотя строка — это id пресета, а не голос. Заголовок и подсказка обязаны говорить
    одно и то же.
    """
    if str(s.get("engine") or "edge").strip().lower() == "lumean":
        return "Пресеты Lumean по языкам"
    return "Голоса по языкам"


def _voice_by_lang_hint(m, s):
    """Подсказка меняется для Lumean: идентификатор там — не голос, а свой пресет.

    Провайдер один на строку — движок берёт `voice_uuid`/`voice_by_lang` как id шаблона
    Lumean, а не как голос TTS, и подсказка обязана сказать это прямо, а не оставить
    человека гадать, что писать в текстовое поле каждой строки.
    """
    if str(s.get("engine") or "edge").strip().lower() == "lumean":
        return ("для Lumean это id пресета Lumean (template_id), а не голос — то же "
                "правило «язык → провайдер → идентификатор» ТЗ §06")
    return ("правило ТЗ §06: язык → провайдер → идентификатор. "
            "Язык берётся у канала выпуска")


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
#   16.09.2026 «Кастом-промты» (custom_prompts) — только из формы: ключ в настройках не
#       трогаем, движок продолжает читать его у тех, кто уже заполнил, решение владельца
#       ещё не принято по остальным полям текста/обложки в проекте (см. task-spec.md);
#       GROK/GROK_SPEED и `grok` — из выпадающих списков (см. `_image_models`,
#       `_video_methods`), из движка нет — старые пресеты продолжают работать.
GROUPS = (
    ("Озвучка", (
        Field("engine", "Движок", "select", options=_engines),
        Field("speed", "Скорость", "text", placeholder="1.0", hint="1.0 — как есть"),
        # kind="langmap": строки «язык → идентификатор» вместо одной текстовой строки
        # (батч 16.09.2026). Хранение не меняется — на сервер по-прежнему уезжает строка
        # вида "ru: id, en: id2", её же продолжает разбирать parse_voice_map: заводить
        # вторую форму хранения одного и того же значило бы разойтись с движком.
        Field("voice_by_lang", _voice_by_lang_label, "langmap",
              options=_lang_options, hint=_voice_by_lang_hint),
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
        # kind="select": варианты и подписи дословно из формы кабинета (main.py:4124) —
        # своей константы под них в кабинете нет, задаём в `_aspect_ratio` (батч 16.09.2026).
        Field("aspect_ratio", "Соотношение", "select", options=_aspect_ratio),
        Field("style", "Стиль", "text", placeholder="пусто = без стиля"),
        Field("sentences_per_prompt", "Предложений на кадр", "text", placeholder="3",
              hint="можно переменным шагом: 1x40,3 — первые сорок по одному, дальше по три"),
        Field("seed", "Seed — тот же результат", "text",
              placeholder="пусто = случайный", off=_seed_off),
        Field("analyze_story", "Разбор персонажей и мест", "switch",
              hint="канон внешности, чтобы герой не менялся от кадра к кадру"),
        Field("split_long_sentences", "Дробить длинные предложения", "switch"),
        Field("max_solo_scene_seconds", "Сек на кадр, примерно", "number"),
        # "Кастом-промты" убраны из формы 16.09.2026 (решение владельца) — ключ
        # `custom_prompts` в настройках не трогаем, движок его читает как читал.
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
            # `langmap` — та же защита, что и у `select`: если модуль кабинета не
            # поднят или `_LANG_NAMES` не прочитался, вариантов языка
            # взять неоткуда, и раньше поле рисовалось пустым выпадающим списком, который
            # при открытии-сохранении формы вычищал сохранённую карту целиком (у select
            # такой дыры не было — там пустой список тоже прятал поле). Лучше не
            # показывать строки вовсе: без поля в форме ключ не уедет в `form.read()`, и
            # кабинет при сохранении (merge) сохранённую карту не тронет.
            if f.kind in ("select", "langmap") and not options:
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
            try:
                hint = f.hint(mod, s) if callable(f.hint) else f.hint
            except Exception:                                      # noqa: BLE001
                hint = ""          # правило подсказки сломалось — молчание лучше вранья
            try:
                label = f.label(mod, s) if callable(f.label) else f.label
            except Exception:                                      # noqa: BLE001
                label = f.key          # правило подписи сломалось — ключ лучше пустоты
            value = s.get(f.key)
            items.append({"key": f.key, "label": label, "kind": f.kind,
                          "hint": hint, "placeholder": f.placeholder,
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
