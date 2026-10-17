#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обложка не нарисовалась — смягчить надпись и заказать заново, а не хоронить выпуск.

17.10.2026. «Po Ostatnim Strzale» и «Неймовірні Історії Війни»: Grok трижды подряд вернул
пустой ответ на обложки с надписями «NAZIŚCI PO HITLERZE» и «ЯК ЖИЛИ НІМЦІ ЗА ГІТЛЕРА?».
Причину провайдер не называет, но картина та же, что у фильтра озвучки: одни и те же слова
дают один и тот же молчаливый отказ, и повтор того же запроса бесполезен.

Решение владельца 17.10: как для озвучки — до трёх раундов смягчения надписи по возрастанию
силы, потом остановка с понятной причиной. Надпись переписывает подписка Codex (тот же вход
`chat-once`, что у названий и у смягчения озвучки), обложка заказывается в режиме «по
названию» с нейтральной подсказкой сцены — сервис обложек свой код не меняет.
"""
from __future__ import annotations

import re

import tts_soften

ROUNDS = 3

_ROUND_RULES = {
    1: ("Замени имена диктаторов, названия режимов и партий, слова «нацисты», «фашисты», "
        "«СС», «гестапо» на нейтральные: «Германия», «Третий рейх» → «тот режим», «немцы», "
        "«враг», «1945». Смысл и интригу сохрани."),
    2: ("Убери любые имена людей, названия режимов, партий, спецслужб и идеологий; оставь "
        "только событие, место или время: «после 1945», «на восточном фронте», «в тылу»."),
    3: ("Сделай общую историческую надпись без имён, режимов, насилия и оценок: о людях, "
        "времени, письмах, тайнах, судьбах. Пример духа: «ЧТО СКРЫВАЛИ ДНЕВНИКИ», "
        "«ЖИЗНЬ ПОСЛЕ ВОЙНЫ»."),
}

_SYSTEM = (
    "Ты редактор надписей для обложек исторических видео на YouTube. Генератор картинок "
    "молча отказался рисовать обложку с текущей надписью. Перепиши надпись так, чтобы она "
    "прошла фильтр, была на указанном языке, ЗАГЛАВНЫМИ буквами, короткой (2–6 слов, можно "
    "два ряда через перевод строки) и цепляла. На вход может прийти не надпись, а полное "
    "название ролика — тогда сделай из него короткую надпись по тем же правилам. Верни "
    "ТОЛЬКО надпись, без кавычек и пояснений."
)

_SCENE_HINT = {
    1: "WWII history documentary thumbnail; archival mood; soldiers' silhouettes, maps, "
       "letters; no flags, no symbols, no faces of real politicians.",
    2: "Mid-20th-century history documentary; archival photo mood; everyday life, ruins, "
       "roads, old letters; no military symbols, no weapons in focus, no real persons.",
    3: "Documentary history thumbnail; old paper, diaries, sepia and warm light; calm, "
       "mysterious; no symbols, no weapons, no real persons.",
}


class SoftenError(RuntimeError):
    """Смягчить не вышло; причина — человеку."""


_RESULT_RE = re.compile(r"Результат:\s*(.+?)\n\s*📝", re.S)
_BASE_TITLE_RE = re.compile(r"base_title=(['\"])(.+?)\1", re.S)


def adapted_text(log: str) -> str:
    """Надпись, которую сервис пытался нарисовать: из журнала задачи обложки.

    В режиме по ссылке это строка «Результат: …» после адаптации текста; в режиме по
    названию (наши повторные заказы) — `base_title=…` в заголовке журнала.
    """
    text = str(log or "")
    m = _RESULT_RE.search(text)
    if m:
        return " ".join(m.group(1).split())
    m = _BASE_TITLE_RE.search(text)
    if m:
        return " ".join(m.group(2).encode("utf-8").decode("unicode_escape", "ignore").split()) \
            if "\\u" in m.group(2) else " ".join(m.group(2).split())
    return ""


def messages_for(text: str, lang: str, round_no: int, video_title: str = "") -> list:
    rule = _ROUND_RULES.get(max(1, min(ROUNDS, int(round_no or 1))), _ROUND_RULES[ROUNDS])
    language = tts_soften.LANG_NAMES.get((lang or "").lower(), lang or "язык оригинала")
    user = (f"Язык надписи: {language}.\nРаунд смягчения: {round_no} из {ROUNDS}. {rule}\n"
            + (f"\nО чём ролик: {video_title.strip()[:200]}\n" if video_title else "")
            + f"\nНАДПИСЬ:\n{text}")
    return [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]


def clean_reply(reply: str, original: str) -> str:
    text = (reply or "").strip().strip('"«»\'')
    text = re.sub(r"^(?:НАДПИСЬ|Надпись)\s*:\s*", "", text).strip()
    if not text:
        raise SoftenError("модель вернула пустой ответ")
    if tts_soften._refused(text[:200]):
        raise SoftenError("модель отказалась переписывать надпись")
    words = text.split()
    if not 1 <= len(words) <= 8:
        raise SoftenError(f"надпись из {len(words)} слов не годится для обложки")
    if " ".join(words).casefold() == " ".join(original.split()).casefold():
        raise SoftenError("модель вернула надпись без изменений")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def soften_text(text: str, lang: str, round_no: int, key: str, base: str,
                *, video_title: str = "", chat=None) -> str:
    """Новая надпись для раунда `round_no`. Любая беда — SoftenError."""
    if not text.strip():
        raise SoftenError("надпись обложки в журнале задачи не найдена")
    call = chat or tts_soften.chat_once
    reply = call(messages_for(text, lang, round_no, video_title), key, base)
    return clean_reply(reply, text)


def scene_hint(round_no: int) -> str:
    return _SCENE_HINT.get(max(1, min(ROUNDS, int(round_no or 1))), _SCENE_HINT[ROUNDS])
