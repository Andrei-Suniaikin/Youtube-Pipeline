# -*- coding: utf-8 -*-
"""Языки студии: одна таблица для сайта, конвейера и промптов.

Темп (слов в минуту) задаёт объём текста и плотность кадров: минуты ролика × темп.
Темп — у голоса, а не у языка: VOICE_WPM ниже (замер 18.09.2026).

Примеры в промптах («said», «sfx») даны на самом языке: иначе модель рисует
в польском ролике русское «БАМ». Русские строки совпадают с прежними побайтно.
"""
from __future__ import annotations

import os

# Куски промптов, которые раньше были написаны под русский. Русские значения —
# прежний текст побайтно; у остальных языков — нейтральная формулировка с именем
# языка (sfx_match/gender_why/labels ниже), без русских слов-образцов: иначе
# модель пишет в польском ролике русское «ВЖУХ».
SFX_MATCH_RU = (
    "The word must match THAT sound: a blow on a body — БАМ / ХРЯСЬ, a slap — ШЛЁП, a fall —\n"
    "  БУХ, a footstep — ТУК, glass — ДЗЫНЬ, cloth tearing — ТРЕСК, a door — ХЛОП, a blade\n"
    "  cutting air — ВЖУХ (only for something actually swishing through the air). Do not reuse\n"
    "  one favourite word for different kinds of action — 33 of 62 sounds in one run were ВЖУХ.")
SFX_MATCH_ANY = (
    "The word must match THAT sound: a blow on a body, a slap, a fall, a footstep, glass,\n"
    "  cloth tearing, a door and a blade cutting air (only for something actually swishing\n"
    "  through the air) each get their own {lang} sound word. Do not reuse one favourite word\n"
    "  for different kinds of action — in one run 33 of 62 sounds were the same word.")
GENDER_WHY_RU = ("In Russian, Chinese names do not decline,\n"
                 "  so the subject of a sentence is recognised only by the gender of the verb.")
GENDER_WHY_ANY = ("The {lang} narration needs it for pronouns and for\n"
                  "  gendered verb and adjective forms.")
# Подписи в глоссарии сцены (rewrite._fmt_chars) и заглушки соседних сцен.
LABELS_RU = {"m": "мужчина", "f": "женщина", "variants": "в источнике также",
             "none": "(нет)", "start": "— это начало", "end": "— это конец"}
LABELS_ANY = {"m": "male", "f": "female", "variants": "also in the source",
              "none": "(none)", "start": "— this is the beginning", "end": "— this is the end"}

# Зацепки «звук за кадром при неподвижном герое» (state.resolve): место не меняется, если в
# бите есть звук из-за стены, герой сидит/стоит на месте и никуда не идёт. Порядок: за кадром,
# на месте, движение. Ищутся подстрокой в тексте нижнего регистра, как русские в state.py.
# Короткие намеренно; без носителя языка. «ruszył» не взят: он внутри «nie poruszył się».
CUES_PL = (
    ("za drzwiami", "zza drzwi", "zza ściany", "z korytarza", "na korytarzu", "z pokoju obok",
     "z sąsiedniego", "z zewnątrz", "dobiegł", "dobiegła", "dobiegło", "dobiegły", "rozległ się",
     "rozległo się", "rozległy się", "słychać", "usłyszał", "hałas", "odgłos", "kroki", "pukanie",
     "gdzieś"),
    ("siedział", "siedziała", "siedzi", "przy biurku", "przy stole", "na krześle", "na łóżku",
     "leżał", "leżała", "leży", "nie poruszył", "nie ruszał", "został", "została", "zamarł",
     "zamarła", "stał w miejscu", "stała w miejscu"),
    ("wszedł", "weszła", "weszli", "wchodzi", "wyszedł", "wyszła", "wyszli", "wychodzi",
     "poszedł", "poszła", "poszli", "podszedł", "podeszła", "przeszedł", "przeszła", "pobiegł",
     "pobiegła", "wstał", "wstała", "przyjechał", "przyjechała", "dotarł", "dotarła", "wrócił",
     "wróciła", "wrócili"),
)
CUES_CS = (
    ("za dveřmi", "zpoza dveří", "za zdí", "z chodby", "na chodbě", "z vedlejšího", "zvenku",
     "ozval", "ozvala", "ozvalo", "ozvaly", "bylo slyšet", "zaslechl", "hluk", "zvuk", "kroky",
     "klepání", "někde"),
    ("seděl", "seděla", "sedí", "u stolu", "na židli", "na posteli", "ležel", "ležela", "leží",
     "nehýbal", "nepohnul", "zůstal", "zůstala", "ztuhl", "ztuhla", "stál na místě", "stála na místě"),
    ("vešel", "vešla", "vešli", "vstoupil", "vstoupila", "vyšel", "vyšla", "vyšli", "odešel",
     "odešla", "přišel", "přišla", "přešel", "přešla", "vydal se", "vydala se", "běžel", "běžela",
     "vstal", "vstala", "přijel", "přijela", "dorazil", "dorazila", "vrátil", "vrátila",
     "vrátili"),
)

LANGS = {
    "Russian": {"label": "Русский", "iso": "ru",
                "voices": [("ru-RU-DmitryNeural", "Дмитрий"), ("ru-RU-SvetlanaNeural", "Светлана")],
                "quotes": "«...»", "said": "«сказал», «крикнула»",
                "sfx": "'БАМ', 'ВЖУХ', 'ХЛОП', 'ТУК', 'ШШШ'",
                "script": "Cyrillic", "gender_morph": True,
                "memory_words": '"вспомнил", "в детстве", "тогда", "маленький"',
                "sfx_match": SFX_MATCH_RU,
                "gender_why": GENDER_WHY_RU, "labels": LABELS_RU},
    "English": {"label": "Английский", "iso": "en",
                "voices": [("en-US-GuyNeural", "Гай"), ("en-US-JennyNeural", "Дженни")],
                "quotes": "\"...\"", "said": "\"said\", \"shouted\"",
                "sfx": "'BAM', 'WHOOSH', 'SLAM', 'THUD', 'SHHH'",
                "script": "Latin", "memory_words": '"remembered", "as a child", "back then", "little"'},
    "Polish": {"label": "Польский", "iso": "pl",
               "voices": [("pl-PL-MarekNeural", "Marek"), ("pl-PL-ZofiaNeural", "Zofia")],
               "quotes": "„...”", "said": "„powiedział”, „krzyknęła”",
               "sfx": "'BUM', 'ŁUP', 'TRZASK', 'PUK', 'SZSZ'",
               "declines": True, "cues": CUES_PL,
               "script": "Latin", "memory_words": '"przypomniał sobie", "w dzieciństwie", "wtedy", "mały"'},
    "Czech": {"label": "Чешский", "iso": "cs",
              "voices": [("cs-CZ-AntoninNeural", "Antonín"), ("cs-CZ-VlastaNeural", "Vlasta")],
              "quotes": "„...“", "said": "„řekl“, „vykřikla“",
              "sfx": "'BUM', 'PRÁSK', 'BOUCH', 'ŤUK', 'ŠŠŠ'",
              "declines": True, "cues": CUES_CS,
              "script": "Latin", "memory_words": '"vzpomněl si", "v dětství", "tehdy", "malý"'},
    "Romanian": {"label": "Румынский", "iso": "ro",
                 "voices": [("ro-RO-EmilNeural", "Emil"), ("ro-RO-AlinaNeural", "Alina")],
                 "quotes": "„...”", "said": "„a spus”, „a strigat”",
                 "sfx": "'BUM', 'PAC', 'ZDRANG', 'CIOC', 'ȘȘȘ'",
                 "declines": True,
                 "script": "Latin", "memory_words": '"și-a amintit", "în copilărie", "atunci", "mic"'},
    "Portuguese": {"label": "Португальский", "iso": "pt",
                   "voices": [("pt-BR-AntonioNeural", "Antônio"), ("pt-BR-FranciscaNeural", "Francisca")],
                   "quotes": "“...”", "said": "“disse”, “gritou”",
                   "sfx": "'BUM', 'VUPT', 'PAF', 'TOC', 'SHHH'",
                   "script": "Latin", "memory_words": '"lembrou", "na infância", "naquela época", "pequeno"'},
    "Spanish": {"label": "Испанский", "iso": "es",
                "voices": [("es-MX-JorgeNeural", "Jorge"), ("es-MX-DaliaNeural", "Dalia")],
                "quotes": "«...»", "said": "«dijo», «gritó»",
                "sfx": "'PUM', 'ZAS', 'PLAF', 'TOC', 'SHHH'",
                "script": "Latin", "memory_words": '"recordó", "de niño", "entonces", "pequeño"'},
    "Italian": {"label": "Итальянский", "iso": "it",
                "voices": [("it-IT-DiegoNeural", "Diego"), ("it-IT-ElsaNeural", "Elsa")],
                "quotes": "«...»", "said": "«disse», «gridò»",
                "sfx": "'BUM', 'SBAM', 'CRASH', 'TOC', 'SSST'",
                "script": "Latin", "memory_words": '"ricordò", "da bambino", "allora", "piccolo"'},
}
DEFAULT = "Russian"

# Темп голоса, слов в минуту — по нему минуты заказа переводятся в объём текста (и плотность
# кадров). Замер 18.09.2026, тем же путём, что в студии (биты → блоки ~55 с → edge-tts → склейка):
# - ru-RU-DmitryNeural, pl-PL-MarekNeural — готовые ролики на VPS: Dmitry 7920 слов за 66,42 мин
#   (ролики 40, 10 и 10 мин), Marek 1625 слов за 11,76 мин;
# - остальные — один и тот же пересказ (~430–550 слов, имена, реплики в кавычках) на каждом языке,
#   начитан с Мака, × 0,971: на живом польском тексте Marek читает на 2,9% медленнее, чем на
#   пробном (138,2 против 142,3). Прежняя таблица (замер 17.09, приведённый к 135 для русского)
#   завышала темп: русский ролик 40 мин вышел 46,9 мин, польский 10 мин — 11,8 мин.
# Голоса одного языка читают по-разному (Antonín 150, Vlasta 127), поэтому темп — у голоса.
VOICE_WPM = {
    "ru-RU-DmitryNeural": 119.2, "ru-RU-SvetlanaNeural": 129.8,
    "en-US-GuyNeural": 174.0, "en-US-JennyNeural": 168.4,
    "pl-PL-MarekNeural": 138.2, "pl-PL-ZofiaNeural": 137.2,
    "cs-CZ-AntoninNeural": 150.1, "cs-CZ-VlastaNeural": 126.6,
    "ro-RO-EmilNeural": 154.0, "ro-RO-AlinaNeural": 151.7,
    "pt-BR-AntonioNeural": 154.0, "pt-BR-FranciscaNeural": 177.5,
    "es-MX-JorgeNeural": 151.7, "es-MX-DaliaNeural": 153.2,
    "it-IT-DiegoNeural": 139.8, "it-IT-ElsaNeural": 156.6,
}

# Прежний темп по языку (до 18.09.2026). Нужен только роликам, начатым до таблицы голосов:
# их текст уже написан под этот темп, а границы блоков озвучки считаются из темпа — другой темп
# перерезал бы блоки и озвучил весь ролик заново (с Lumean — вторая покупка).
LEGACY_WPM = {"Russian": 135.0, "English": 135.0, "Polish": 151.0, "Czech": 157.0,
              "Romanian": 179.0, "Portuguese": 166.0, "Spanish": 167.0, "Italian": 160.0}


def voice_pace() -> bool:
    """MANGA_VOICE_PACE — брать темп новых роликов из таблицы голосов (VOICE_WPM).
    Пусто, 0 или off — прежний темп языка (LEGACY_WPM), как до 18.09: без настройки студия
    пишет тексты того же объёма, что и раньше. 1 или on — таблица голосов. Иное — ошибка."""
    raw = (os.environ.get("MANGA_VOICE_PACE") or "").strip().lower()
    if raw in ("", "0", "off"):
        return False
    if raw in ("1", "on"):
        return True
    raise ValueError(f"MANGA_VOICE_PACE: нужно on или off, а не {raw!r}")


def get(lang: str) -> dict:
    """Неизвестный язык — ошибка: иначе ролик тихо получит русский темп и русские примеры."""
    try:
        return LANGS[lang]
    except KeyError:
        raise ValueError(f"язык не поддерживается студией: {lang!r}; есть: {', '.join(LANGS)}") from None


def wpm(lang: str, voice: str | None = None) -> float:
    """Темп голоса проекта (VOICE_WPM); без голоса — первый голос языка. Голос чужого языка — ошибка."""
    spec = get(lang)
    voice = voice or spec["voices"][0][0]
    if voice not in {code for code, _ in spec["voices"]}:
        raise ValueError(f"голос {voice!r} не относится к языку {lang}")
    return VOICE_WPM[voice]
def iso(lang: str) -> str: return get(lang)["iso"]
def quotes(lang: str) -> str: return get(lang)["quotes"]
def said(lang: str) -> str: return get(lang)["said"]
def sfx(lang: str) -> str: return get(lang)["sfx"]
def script(lang: str) -> str: return get(lang)["script"]
def memory_words(lang: str) -> str: return get(lang)["memory_words"]
def default_voice(lang: str) -> str: return get(lang)["voices"][0][0]


def gender_morph(lang: str) -> bool:
    """Угадывать героя по роду глагола — только там, где морфология языка настоящая.
    Модель spaCy на сервере одна, русская (ru_core_news_sm): на польском и
    итальянском её род — случайность, и неверный род уводил бы кадр к другому герою."""
    return bool(get(lang).get("gender_morph"))


def declines(lang: str) -> bool:
    """Склоняет ли язык имена и названия мест: Shen → Shena, Shenowi; Lina → Linę, Linie;
    gabinet → w gabinecie. Тогда модель глоссария выписывает падежные формы в variants
    (variants_rule), и имя/место ищется целым словом по этим формам (state._word_rx);
    основу слова не угадываем. Русский сюда не входит: там китайские имена не склоняются, а
    поиск русских мест оставлен как был (побайтно прежнее поведение)."""
    return bool(get(lang).get("declines"))


def word_search(lang: str) -> bool:
    """Искать имена и места целым словом (state._name_spans/_find_place) — все языки, кроме
    русского: подстрока находила «Casa» в «casamento», «Leon» в «Leona». Русский — побайтно
    прежний поиск подстрокой."""
    return get(lang) is not LANGS["Russian"]


def scene_cues(lang: str):
    """(за кадром, на месте, движение) для правила «звук за кадром»; None — своих нет."""
    return get(lang).get("cues")


# Просьба к модели глоссария дать падежные формы имён. Для русского — пусто:
# промпт глоссария побайтно прежний. LOC_VARIANTS — поле variants в образце мест.
VARIANTS_RULE = ("- {lang} declines names: in \"variants\" also list the case forms of every name as\n"
                 "  they will appear in {lang} narration (e.g. genitive, dative, accusative, locative).\n"
                 "  Give every location a \"variants\" list of its case forms too.\n"
                 "  Spell each form exactly as in the narration, hyphens included (e.g. Min-ho, Min-hoa).\n")


LOC_VARIANTS = '\n      "variants": ["<case forms of the place name>"],'


def loc_variants(lang: str) -> str:
    return LOC_VARIANTS if declines(lang) else ""


def variants_rule(lang: str) -> str:
    return VARIANTS_RULE.format(lang=lang) if declines(lang) else ""


def sfx_match(lang: str) -> str:
    return get(lang).get("sfx_match") or SFX_MATCH_ANY.format(lang=lang)


def gender_why(lang: str) -> str:
    return get(lang).get("gender_why") or GENDER_WHY_ANY.format(lang=lang)


def labels(lang: str) -> dict:
    return get(lang).get("labels") or LABELS_ANY
