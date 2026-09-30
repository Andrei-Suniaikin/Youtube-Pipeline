# -*- coding: utf-8 -*-
"""Профиль ниши и языка. Меняется файлом, а не правкой кода.

Здесь лежит то, что зависит от НИШИ и ЯЗЫКА, но не от конкретного текста:
темп начитки, стоп-лист мета-речи, границы формы. Имён персонажей и названий
мест тут нет и быть не может — они приходят из разбора конкретного выпуска.
"""

PROFILES = {
    "ru": {
        "name": "русский",
        "wpm": 126.0,                 # ЗАМЕР на своём голосе (edge-tts ru-RU-DmitryNeural):
                                      # 5207 слов -> 41.4 мин. Прежние 135 были взяты
                                      # с чужого ролика и оказались на 7% оптимистичны.
        "spacy_model": "ru_core_news_sm",
        "sentence_words": (10, 16),
        "sentence_hard_max": 24,
        "beat_words": (6, 14),
        "stop_meta": [
            r"\bв этой главе\b", r"\bглав[аеы]\b", r"\bэпизод",
            r"\bавтор\b", r"\bдавайте\b", r"\bпредставьте\b",
            r"\bподписывайтесь\b", r"\bстоит отметить\b",
            r"\bкак вы (?:видите|понимаете)\b",
        ],
        "stop_medium": [r"\bманхв", r"\bвебтун", r"\bманг[аиу]\b"],
    },
    "es": {
        "name": "испанский",
        "wpm": 150.0,                 # НЕ ЗАМЕРЕНО на своём голосе — оценка
        "spacy_model": "es_core_news_sm",
        "sentence_words": (10, 16),
        "sentence_hard_max": 24,
        "beat_words": (6, 14),
        "stop_meta": [r"\ben este cap[ií]tulo\b", r"\bcap[ií]tulo\b",
                      r"\bsuscr[ií]b", r"\bimag[ií]nat?e\b", r"\bimag[ií]nen\b", r"\bcomo ves\b"],
        "stop_medium": [r"\bmanhwa\b", r"\bwebtoon\b", r"\bmanga\b"],
    },
}

UNMEASURED = {"es"}   # у этих профилей темп речи не замерен на своём голосе


def get(lang: str = "ru") -> dict:
    if lang not in PROFILES:
        raise KeyError(f"нет профиля языка {lang!r}; есть: {sorted(PROFILES)}")
    return PROFILES[lang]


def stops(lang: str = "ru"):
    p = get(lang)
    return list(p["stop_meta"]) + list(p["stop_medium"])
