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

import os
import re
import sys
import threading
import time


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
# Кто исполняет модель картинок. Третий элемент кортежа `IMAGE_MODELS` — это ключ
# провайдера кабинета; здесь только человеческая подпись к нему, как у методов клипов.
_IMAGE_PROVIDER_TITLE = {"recrafter": "FastGen", "veo": "VeoNonStop", "openai": "OpenAI"}
# Насколько снимок статусов считается свежим. Больше — форма покажет старое; меньше —
# лишние обращения к health-API, который и так общий на платформу.
_FG_SNAPSHOT_TTL = float(os.environ.get("VS_FG_SNAPSHOT_TTL", "600"))


# Доработка 4 (Дополнение 16.09 к батчу 16.09.2026, фаза 4): у `fastgen_status` на
# холодном кэше — до трёх последовательных сетевых вызовов по 20с каждый (см.
# `main.py:_fg_get`), то есть до минуты, если провайдер завис. Открытие формы пресета
# не вправе ждать столько: бюджет ниже жёстко ограничивает, сколько форма готова
# подождать живой статус, прежде чем показать «нет данных» и открыться.
_FG_FORM_BUDGET_SEC = 3.0


_FG_INFLIGHT_LOCK = threading.Lock()
_FG_INFLIGHT = {"busy": False}


def _fg_status_map(m):
    """id модели FastGen → текст живого статуса («работает»/«нестабильно»/«лежит»).

    Источник — снимок `main.fastgen_models_snapshot()`: живой опрос health-API занимает
    около трёх секунд, а форме на всё про всё дано `_FG_FORM_BUDGET_SEC` (доработка 4,
    Дополнение 16.09), и до 18.09.2026 она поэтому ВСЕГДА показывала «нет данных».
    Снимок кладёт тот, кому ждать можно: экран ключей, `dev/alerts.py` и фоновое
    обновление ниже. Пороги по-прежнему считает кабинет (`_fg_classify`), своих здесь нет.

    Если снимок устарел (или его нет), форма всё равно открывается сразу: она отдаёт то,
    что есть, и просит обновить снимок в фоне — к следующему открытию он будет свежим.
    Поток демон и один на процесс: у зависшего FastGen иначе копились бы потоки ровно
    тогда, когда ему хуже всего.
    """
    snap = {}
    getter = getattr(m, "fastgen_models_snapshot", None)
    if getter:
        try:
            snap = getter() or {}
        except Exception:                                          # noqa: BLE001
            snap = {}
    models = snap.get("models") if isinstance(snap.get("models"), dict) else {}
    checked = snap.get("checked")
    fresh = isinstance(checked, (int, float)) and (time.time() - checked) < _FG_SNAPSHOT_TTL
    if not fresh:
        _fg_refresh_async(m)
    return dict(models)


def _fg_refresh_async(m) -> None:
    """Обновить снимок статусов в фоне, не задерживая форму."""
    keys_fn = getattr(m, "keys", None)
    first_cred = getattr(m, "_first_credential", None)
    status_fn = getattr(m, "fastgen_status", None)
    if not (keys_fn and first_cred and status_fn):
        return
    with _FG_INFLIGHT_LOCK:
        if _FG_INFLIGHT["busy"]:
            return
        _FG_INFLIGHT["busy"] = True

    def _call():
        try:
            key = first_cred(keys_fn(), "googler")
            if key:
                status_fn(key)          # сам положит снимок в admin_state
        except Exception:                                          # noqa: BLE001
            pass
        finally:
            with _FG_INFLIGHT_LOCK:
                _FG_INFLIGHT["busy"] = False

    threading.Thread(target=_call, daemon=True).start()


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
        provider = v[2] if len(v) > 2 else ""
        # Провайдер в подписи у ВСЕХ моделей (18.09.2026): раньше его было видно только
        # у Veo — и то потому, что он попал в само имя модели у кабинета. Имя чистим от
        # такой скобки, иначе выйдет «… (Veo) · VeoNonStop».
        name = re.sub(r"\s*\((Veo|VeoNonStop|OpenAI)\)\s*$", "", str(v[0])).strip()
        parts = [name, str(v[1]), _IMAGE_PROVIDER_TITLE.get(provider, provider)]
        if provider == "recrafter":
            # Статус есть только у FastGen: у Veo и OpenAI health-API нет вовсе, и
            # «нет данных» у них означало бы «источник молчит», а источника нет.
            parts.append(status.get(k) or "статуса нет")
        out.append((k, " · ".join(p for p in parts if p)))
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


# Код ISO 639-1 для каждого языка Rewrite Studio. Имена берутся у самого Rewrite
# (`rewrite_core.LANGUAGES`, «Русский» → "Russian"), а коды — отсюда: у Rewrite короткие
# коды получаются первыми двумя буквами названия («Armenian» → "ar", как у арабского;
# «Portuguese» → "po»), а карта голосов и язык канала хранят именно ISO ("pl", "pt").
REWRITE_LANG_ISO = {
    "Russian": "ru", "Ukrainian": "uk", "Belarusian": "be", "Polish": "pl", "Czech": "cs",
    "Slovak": "sk", "Bulgarian": "bg", "Serbian": "sr", "Croatian": "hr", "Slovenian": "sl",
    "Macedonian": "mk", "Bosnian": "bs", "English": "en", "German": "de", "French": "fr",
    "Spanish": "es", "Italian": "it", "Catalan": "ca", "Portuguese": "pt", "Dutch": "nl",
    "Swedish": "sv", "Norwegian": "no", "Danish": "da", "Finnish": "fi", "Lithuanian": "lt",
    "Latvian": "lv", "Estonian": "et", "Greek": "el", "Romanian": "ro", "Hungarian": "hu",
    "Turkish": "tr", "Kazakh": "kk", "Uzbek": "uz", "Azerbaijani": "az", "Armenian": "hy",
    "Georgian": "ka", "Arabic": "ar", "Hebrew": "he", "Persian": "fa", "Hindi": "hi",
    "Simplified Chinese": "zh", "Japanese": "ja", "Korean": "ko", "Vietnamese": "vi",
    "Thai": "th", "Indonesian": "id",
}


def lang_options_for_form(mod=None) -> list:
    """Список языков «код → подпись» для любой формы пульта: карта голосов в пресете и
    язык канала. Один источник на оба места (18.09.2026): канал с кодом, которого нет в
    списке пресета, остался бы без голоса и без рерайта."""
    return _lang_options(mod if mod is not None else sys.modules.get("main"))


def _lang_options(m):
    """Языки для строк карты «голоса по языкам».

    Источник — полный список Rewrite Studio (решение владельца 17.09.2026): те же
    русские названия, что человек видит при выборе языков текста, в том же порядке.
    Rewrite в платформе поднят в том же процессе, поэтому модуль уже загружен. Если его
    нет (кабинет отдельно, тесты) — прежний источник `_LANG_NAMES` кабинета, чтобы поле
    не пропало и не вычистило сохранённую карту. Язык в карте, которого нет в списке,
    не теряется: его показывает отдельным пунктом сама форма.
    """
    core = sys.modules.get("rewrite_core")
    langs = getattr(core, "LANGUAGES", None) if core is not None else None
    if isinstance(langs, dict) and langs:
        out = []
        for ru, canon in langs.items():
            code = REWRITE_LANG_ISO.get(canon)
            if code:
                out.append((code, f"{ru} ({code})"))
        if out:
            return out
    names = getattr(m, "_LANG_NAMES", None) or {}
    return [(code, f"{code} — {name}") for code, name in sorted(names.items())]


def _lumean_models(m):
    """Модели озвучки Lumean — список движка (`tts_worker.LUMEAN_MODEL_IDS`), а не свой.
    Пустой пункт — «как в шаблоне»: провайдер возьмёт то, что записано у шаблона."""
    ids = getattr(sys.modules.get("tts_worker"), "LUMEAN_MODEL_IDS", None)
    if not ids:
        ids = {"eleven_v3", "eleven_multilingual_v2", "eleven_flash_v2_5",
               "eleven_turbo_v2_5", "eleven_turbo_v2", "eleven_flash_v2"}
    return [("", "как в шаблоне")] + [(x, x) for x in sorted(ids)]


def _lumean_normalization(m):
    return [("", "как в шаблоне"), ("auto", "как решит сервис"),
            ("on", "всегда разворачивать"), ("off", "не трогать")]


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
    # Ссылки на пункты ТЗ с экрана сняты (просьба владельца 21.09.2026): человеку
    # нужно, что вписать и откуда берётся язык, а не номер раздела документа.
    if str(s.get("engine") or "edge").strip().lower() == "lumean":
        return ("для Lumean здесь id пресета Lumean (template_id), а не голос; "
                "язык берётся у канала выпуска")
    return ("идентификатор голоса на каждый язык; язык берётся у канала выпуска")


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
        # Настройки заказа Lumean (18.09.2026). Шаблон у провайдера остаётся выбором
        # голоса, а всё остальное задаётся здесь и уезжает на каждый заказ
        # (`main._lumean_config_override` → `config_override`). Имена и диапазоны — те
        # же, что у самого шаблона Lumean (сверено по `GET /templates`).
        # Пустое поле — «как в шаблоне»: в заказ оно не уходит вовсе.
        Field("lum_model", "Модель озвучки", "select", options=_lumean_models,
              hide=_tts_only("lumean"), hint="пусто — как в шаблоне Lumean"),
        Field("lum_stability", "Стабильность", "number", hide=_tts_only("lumean"),
              placeholder="0–1", hint="ниже — живее и рискованнее, выше — ровнее"),
        Field("lum_similarity", "Схожесть с голосом", "number", hide=_tts_only("lumean"),
              placeholder="0–1"),
        Field("lum_style", "Стиль", "number", hide=_tts_only("lumean"), placeholder="0–1"),
        Field("lum_speed", "Скорость", "number", hide=_tts_only("lumean"),
              placeholder="0.5–2.0", hint="1.0 — как есть"),
        Field("lum_speaker_boost", "Усиление голоса", "switch", hide=_tts_only("lumean")),
        Field("lum_normalization", "Нормализация текста", "select",
              options=_lumean_normalization, hide=_tts_only("lumean"),
              hint="как читать числа, даты и сокращения"),
        Field("lum_chunk", "Размер куска, знаков", "number", hide=_tts_only("lumean"),
              placeholder="2000"),
        Field("lum_pauses", "Паузы", "switch", hide=_tts_only("lumean")),
        Field("lum_stress", "Ударения", "switch", hide=_tts_only("lumean")),
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


# Пятая и шестая группы. ТЗ §04: «пресеты 3 → 1» — человек настраивает ролик один раз,
# а не трижды в трёх сервисах. Варианты сюда приходят снаружи (`extra`), потому что
# списки пресетов рерайта и обложек принадлежат соседям, а не кабинету: спрашивать их
# надо у них, а не держать копию.
#
# 18.09.2026, решение владельца: сценарий и обложка — РАЗНЫЕ компоненты, и группа у них
# тоже разная. Раньше это была одна группа «Текст и обложка», то есть один частичный
# пресет закрывал сразу оба сервиса: сменить студию обложек, не трогая текст, было
# нельзя.
GROUPS = GROUPS + (
    ("Сценарий", (
        Field("preset_rewrite", "Пресет рерайта", "select",
              hint="каким пресетом делается текст"),
    )),
    ("Обложка", (
        Field("preset_cover", "Пресет обложки", "select",
              hint="каким пресетом делается обложка"),
    )),
)


# Части пресета (батч «Пресеты целые и частичные»): служебный ключ `_scope`
# частичного пресета → заголовок группы `GROUPS`, которую он целиком закрывает.
# Задано один раз здесь, а не в `preset_links.py`: `describe()` и разрешение ссылок
# обязаны знать про одни и те же группы, а не про две копии этого списка.
# С 18.09.2026 частей шесть: «Сценарий» и «Обложка» разделены (см. GROUPS выше).
PARTS = (
    ("voice", "Озвучка"),
    ("images", "Картинки"),
    ("clips", "Клипы"),
    ("render", "Сборка"),
    ("text", "Сценарий"),
    ("cover", "Обложка"),
)


# PART_EXTRA_KEYS — ключи, которые реально читает сборщик параметров этапа
# (`_build_cmd`, `_render_config_from_params` в `server/main.py`), но которых нет
# среди полей `GROUPS`: форма пульта их не редактирует (историческое наследие,
# служебные флаги, поля, убранные из формы решением владельца), а движок
# продолжает их читать. Правило 3 архитектуры («группа берётся из одного
# источника целиком») без этой карты не выполняется: ссылка на частичный пресет
# подменила бы только видимые поля формы, а такой ключ остался бы от старого
# (своего) пресета — то самое смешение источников, которое правило запрещает.
#
# `group_keys(part)` = поля `GROUPS` этой части ∪ `PART_EXTRA_KEYS[part]`; список
# ниже выведен построчным чтением `server/main.py` (ответы на уточнения фазы 1,
# цикл 2, вопросы 1–2), а не придуман — у каждого ключа комментарий с источником.
# `stability` (Вопрос 2) в список не включён: это уже поле формы «Озвучка» в
# `GROUPS`, дублировать его здесь незачем — объединение множеств и так его туда
# относит.
#
# Рядом с картой — два списка того же происхождения (построчное чтение тех же
# двух функций), но по другой причине не попадающие ни в одну часть:
# `SHARED_KEYS` (ревью цикла 3, Issue 1/решение владельца) — ключ читают
# НЕСКОЛЬКО этапов сразу, и ни один из них не хозяин; такой ключ всегда берётся
# из целого пресета целиком, ссылка ни одной части его не подменяет. Правило 3
# это не нарушает: оно запрещает смешивать источники ВНУТРИ одной части, а
# общий ключ ни одной части не принадлежит вовсе.
# `TASK_LEVEL_KEYS` — ключи уровня ЗАДАЧИ, а не настройки пресета: сервер сам
# проставляет их при постановке конкретной джобы (контракт карточек,
# путь картинки/клипа для точечной перегенерации, легаси-флаг кэша одного
# запуска). Пресет их не хранит и не должен — это не «забыли отнести», а «не
# про пресет вовсе».
PART_EXTRA_KEYS = {
    "voice": {
        # _build_cmd, kind == "tts", ветка VoiceGenCore: main.py ≈7471–7481
        "voice_engine", "thread_count",
        # _build_cmd, kind == "tts", ветка Voicer: main.py ≈7458–7461
        "auto_pause_enabled", "auto_pause_duration", "auto_pause_frequency",
        # _build_cmd, kind == "tts", ветка Lumean: main.py ≈7506
        "model_uuid",
        # _build_cmd, kind == "tts", ветка FastGen (03.10.2026): инструкция стиля для
        # Gemini TTS (--scene воркера). Как в gen; пусто — умолчание воркера.
        "tts_scene",
        # _build_cmd, kind == "tts": запасной источник движка, main.py ≈7424,
        # если "engine" не задан. Вопрос 2 называл ещё "tts_provider" — этого ключа
        # в коде нет нигде (grep по main.py/pub_services.py/pub_engine.py пуст),
        # поэтому он сюда не включён: расхождение с ответом на вопрос 2, код прав.
        "tts_engine",
    },
    "images": {
        # `_IMAGE_CONTRACT_KEYS`, main.py ≈3525, без "model" — он уже поле формы
        # «Картинки» в GROUPS. Читаются `_image_field`/`_canonical_image_task_params`
        # (main.py ≈3569–3602).
        "image_mode", "img_source", "image_source", "googler_model",
        # _build_cmd, kind in ("images","prompts"): custom_prompts main.py ≈7546
        # (убран из формы 16.09.2026, но не из движка — preset_fields.py комментарий
        # выше), auto_hero main.py ≈7577, tags (источник Bing) main.py ≈7525/7616.
        "custom_prompts", "auto_hero", "tags",
        # smart_mode: _build_cmd, kind == "images" (обычный pipeline, не Bing и не
        # custom_prompts), main.py ≈7581. Issue 1 ревью цикла 3: форма «Картинки»
        # это поле не показывает, но пайплайн его читает — без явного отнесения
        # ссылка на частичный «Картинки» подменяла бы не все ключи этапа, а
        # только видимые в форме, и правило 3 нарушалось бы молча.
        "smart_mode",
        # num_images: _build_cmd, kind == "images", ветка Bing, старый режим тегов
        # 1:1, main.py ≈7528–7529 — тот же источник (Bing), что и у "tags" выше.
        "num_images",
    },
    "clips": {
        # _build_cmd, kind == "video", t2v-режим (text-to-video без исходных
        # картинок): main.py ≈7674–7676. Поля формы «Клипы» в GROUPS их не
        # показывают — вопрос 2 про clips вообще не упоминал, это расхождение,
        # код прав: t2v/t2v_first_n читает именно этап клипов.
        "t2v", "t2v_first_n",
    },
    "render": {
        # _render_config_from_params, main.py ≈12980–13078 — все ключи, которых
        # нет среди полей формы «Сборка» в GROUPS.
        "use_video_audio", "randomize_metadata", "vintage_effect",
        "fade_duration", "kenburns_zoom", "pause_duration", "pause_interval",
        "stock_video_limit", "stock_video_cycle",
        "main_audio_volume", "video_audio_volume",
        "media_scale_pct", "media_offset_x", "media_offset_y",
        "render_media",
    },
    "text": set(),
    "cover": set(),
}


# Общий флаг трёх этапов — ни один не хозяин (решения после ревью цикла 3,
# «use_cache — ничей (общий)»). Раньше жил в PART_EXTRA_KEYS["images"] по
# буквальному прочтению ответа на вопрос 2 — но правило 3 запрещает смешение
# источников ВНУТРИ части, а не владение общим ключом, поэтому решение владельца
# вынесло его отдельно: ссылка на частичный «Картинки» его больше не подменяет,
# он всегда идёт из целого пресета.
SHARED_KEYS = {
    # main.py: kind == "tts" ≈7436, kind == "images" ≈7513, kind == "video"
    # ≈7688 и ≈7734 — три разных этапа читают один и тот же флаг кэша.
    "use_cache",
}


# Ключи уровня задачи — не настройки пресета (см. комментарий над PART_EXTRA_KEYS).
TASK_LEVEL_KEYS = {
    # main.py: _build_cmd(kind="cards") ≈7373–7386 — контракт задачи карточек
    # редактора (хэши и контекст), server сам собирает его на постановке джобы.
    "contract_version", "narration_sha256", "request_sha256", "context",
    # main.py: _build_cmd(kind="regen") ≈7604 — путь картинки для точечной
    # перегенерации одного кадра, свой на каждый вызов, не поле пресета.
    "image_rel",
    # main.py: _build_cmd(kind="regen_video") ≈7652 — путь клипа для точечной
    # перегенерации, тем же образом.
    "video_rel",
    # main.py: _build_cmd(kind="images") ≈7514 — легаси-флаг ОДНОГО запуска
    # («перегенерировать» / «оставить» кэш конкретной задачи), не настройка
    # пресета, а разовое решение конкретной постановки в очередь.
    "legacy_cache_action",
    # main.py: _render_config_from_params ≈13408 — закреплённый на claim список
    # файлов основной озвучки. В форме пресета его нет и быть не может: его
    # пишет САМ кабинет в params задачи при её выдаче (render_claim ≈13529,
    # `_params["narration_files"] = _selected`), пересчитывая на каждый захват
    # по тому, что лежит на диске. Пресет такой список не хранит — это снимок
    # одного конкретного рендера, а не настройка производства.
    "narration_files",
}


def part_group_keys(part: str) -> set:
    """Ключи полей группы этой части пресета: поля формы `GROUPS` ∪
    `PART_EXTRA_KEYS` (ключи, которых форма не показывает, но этап читает).
    Часть неизвестна — пустое множество: это защита от опечатки в
    `_scope`/`_links`, а не рабочий случай."""
    title = dict(PARTS).get(part)
    if not title:
        return set()
    extra = PART_EXTRA_KEYS.get(part, set())
    for t, fields in GROUPS:
        if t == title:
            return {f.key for f in fields} | extra
    return extra


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
            item = {"key": f.key, "label": label, "kind": f.kind,
                    "hint": hint, "placeholder": f.placeholder,
                    "options": options, "off": off,
                    "value": "" if value is None else str(value)}
            if f.kind == "langmap":
                # Подпись и подсказка карты зависят от движка озвучки. Отдаём оба
                # варианта сразу: форма переключает их при смене «Движка», не дожидаясь
                # сохранения (решение владельца 17.09.2026). Тексты — те же функции,
                # что и выше, второго источника правды в браузере нет.
                item["by_engine"] = {}
                for eng in ("lumean", "other"):
                    s2 = dict(s, engine="lumean" if eng == "lumean" else "edge")
                    try:
                        item["by_engine"][eng] = {
                            "label": f.label(mod, s2) if callable(f.label) else f.label,
                            "hint": f.hint(mod, s2) if callable(f.hint) else f.hint}
                    except Exception:                              # noqa: BLE001
                        item["by_engine"][eng] = {"label": label, "hint": hint}
            items.append(item)
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
