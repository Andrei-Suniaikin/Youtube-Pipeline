#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Озвучка упёрлась в фильтр провайдера — смягчить текст блока, а не заказывать его снова.

17.10.2026. Ролик «Nevjerojatne Priče Rata» дважды упал на озвучке: FastGen на блок 6 из 10
отвечал «Request blocked by provider safety filters». Воркер при этом делал ровно одно —
заказывал ТОТ ЖЕ текст второй раз, а движок считал падение временным и возвращал выпуск
ещё дважды. Итого шесть одинаковых заказов, и ни один не мог пройти: отказ фильтра
детерминирован, тот же текст даёт тот же отказ.

Правило ТЗ (раздел «Отказ фильтра»): вместо повторов — до трёх раундов смягчения по
возрастанию силы. Для картинок оно реализовано (`prompt_failover`), для озвучки не было.

Что делает модуль:
  • по журналу упавшей задачи находит номер блока, который не пропустил фильтр;
  • режет `source.txt` на блоки ТЕМ ЖЕ кодом, что воркер (`fastgen_tts_worker.split_text`),
    поэтому «блок 6» здесь — это тот же блок 6, что у него;
  • просит сервис текста (подписка Codex, тот же вход `chat-once`, что у названий)
    переписать только этот блок — раунд 1 бережно, раунд 3 нейтральным пересказом;
  • проверяет ответ (не отказ модели, не обрывок, не разросся вдвое) и пишет новый
    `source.txt`, сохранив прежний рядом (`source.txt.before-soften-N`).

Чего модуль НЕ делает: не трогает готовые блоки (у них квитанции по отпечатку текста,
воркер их не перезаказывает), не меняет движок озвучки и не решает, что делать после
трёх раундов — это говорит движок выпуска, а решает человек.
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

ROUNDS = 3                                   # ТЗ: «до трёх раундов смягчения»
MODEL = os.environ.get("VS_SOFTEN_CODEX_MODEL") or os.environ.get("VS_TITLE_CODEX_MODEL", "gpt-5.6-sol")
TIMEOUT_SEC = float(os.environ.get("VS_SOFTEN_TIMEOUT_SEC", "480") or 480)

# Как воркер FastGen называет отказ фильтра. Список закрытый: другие отказы (нет связи,
# лимит, битый звук) смягчением не лечатся, и путать их нельзя.
FILTER_MARKS = ("blocked by provider safety filters", "safety filter", "safety_filter",
                "blocked_reason", "prohibited_content", "content policy")
_BLOCK_RE = re.compile(r"блок\s+(\d+)\b", re.I)


class SoftenError(RuntimeError):
    """Смягчение не удалось; причина — человеку, без текста блока."""


def failed_block(lines) -> int | None:
    """Номер блока, который не пропустил фильтр провайдера, по строкам журнала задачи.

    Берём строку с отказом фильтра, где назван блок. Нет отказа фильтра — None: это
    другая беда, и смягчать текст за неё бессмысленно.
    """
    for line in lines:
        text = " ".join(str(line or "").split())
        low = text.lower()
        if not any(m in low for m in FILTER_MARKS):
            continue
        m = _BLOCK_RE.search(text)
        if m:
            return int(m.group(1))
    return None


_worker = None


def _split_text():
    """`split_text` самого воркера — чтобы границы блоков совпали один в один."""
    global _worker
    if _worker is None:
        path = Path(__file__).resolve().parents[1] / "workers" / "fastgen_tts_worker.py"
        spec = importlib.util.spec_from_file_location("fastgen_tts_worker_for_soften", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module          # dataclass'ы воркера ищут свой модуль
        spec.loader.exec_module(module)
        _worker = module
    return _worker.split_text


def blocks_of(source: str, max_chars: int) -> list:
    paragraphs = [p.strip() for p in source.splitlines() if p.strip()]
    if not paragraphs:
        return []
    return _split_text()(paragraphs, max_chars)


def max_chars_default() -> int:
    """Тот же предел, что кабинет передаёт воркеру (`FASTGEN_TTS_MAX_CHARS` в main.py)."""
    try:
        return max(1000, min(9000, int(os.environ.get("FASTGEN_TTS_MAX_CHARS", "6000"))))
    except (TypeError, ValueError):
        return 6000


# ─────────────────────────── просьба к модели ───────────────────────────

# Три раунда по возрастанию силы. Язык, объём и смысл — те же; меняется только то, что
# способно задеть фильтр. Модель не знает, что именно не понравилось провайдеру, поэтому
# первый раунд трогает только резкие формулировки, третий — пересказывает нейтрально.
_ROUND_RULES = {
    1: ("Перефразируй ТОЛЬКО самые резкие и натуралистичные формулировки (описания ран, "
        "крови, казней, пыток, оскорбления, прямые призывы к насилию), заменив их на "
        "сдержанные. Всё остальное оставь дословно."),
    2: ("Убери натуралистичные подробности насилия и жестокости, смягчи описания "
        "страданий до уровня школьного учебника истории, замени брань и оскорбления. "
        "Факты, имена, даты и ход событий сохрани."),
    3: ("Перескажи фрагмент нейтральным документальным языком без натурализма, брани, "
        "унижения групп людей и подробностей насилия. Факты, имена, даты и ход событий "
        "сохрани, объём — примерно тот же."),
}

_SYSTEM = (
    "Ты редактор закадрового текста документального видео. Фрагмент текста не пропустил "
    "автоматический фильтр безопасности сервиса озвучки. Твоя задача — переписать фрагмент "
    "так, чтобы он прошёл фильтр, сохранив язык оригинала, смысл, последовательность фактов "
    "и примерно тот же объём. Верни ТОЛЬКО переписанный фрагмент тем же языком, без "
    "заголовков, кавычек, пояснений и пометок. Абзацы сохраняй как в оригинале."
)

LANG_NAMES = {"ru": "русский", "en": "английский", "pl": "польский", "cs": "чешский",
              "hu": "венгерский", "ro": "румынский", "el": "греческий", "pt": "португальский",
              "es": "испанский", "de": "немецкий", "fr": "французский", "it": "итальянский",
              "uk": "украинский", "hr": "хорватский", "sk": "словацкий", "sr": "сербский",
              "bg": "болгарский", "tr": "турецкий", "nl": "нидерландский", "ja": "японский"}


def messages_for(block_text: str, lang: str, round_no: int) -> list:
    rule = _ROUND_RULES.get(max(1, min(ROUNDS, int(round_no or 1))), _ROUND_RULES[ROUNDS])
    language = LANG_NAMES.get((lang or "").lower(), lang or "язык оригинала")
    return [{"role": "system", "content": _SYSTEM},
            {"role": "user", "content": f"Язык текста: {language}.\nРаунд смягчения: "
                                        f"{round_no} из {ROUNDS}. {rule}\n\nФРАГМЕНТ:\n{block_text}"}]


def chat_once(messages: list, key: str, base: str, *, model: str = "", post=None) -> str:
    """Один вызов `chat-once` сервиса текста подпиской Codex — как у названий."""
    if not key:
        raise SoftenError("нет ключа сервиса текста")
    if post is None:
        import requests
        post = requests.post
    payload = {"provider": "codex", "model": model or MODEL, "images": [], "messages": messages}
    try:
        resp = post(base.rstrip("/") + "/api/v1/chat-once", json=payload,
                    headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT_SEC)
    except Exception as exc:                                  # noqa: BLE001 — сеть
        raise SoftenError(f"сервис текста не ответил: {type(exc).__name__}") from None
    if getattr(resp, "status_code", 0) != 200:
        try:
            detail = str((resp.json() or {}).get("detail") or "")
        except Exception:                                      # noqa: BLE001
            detail = str(getattr(resp, "text", ""))[:160]
        raise SoftenError(f"подписка Codex не ответила ({resp.status_code}): {detail[:160]}")
    try:
        return str((resp.json() or {}).get("text") or "")
    except Exception:                                          # noqa: BLE001
        raise SoftenError("не разобрал ответ сервиса текста") from None


def _refused(text: str) -> bool:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from pub_publish import refusal_in
        return refusal_in(text)
    except Exception:                                          # noqa: BLE001
        return False


def clean_reply(reply: str, original: str) -> str:
    """Ответ модели как текст блока — или SoftenError, если это не текст блока."""
    text = (reply or "").strip()
    text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text).strip()
    text = re.sub(r"^(?:ФРАГМЕНТ|FRAGMENT|Переписанный фрагмент)\s*:\s*", "", text, flags=re.I).strip()
    if not text:
        raise SoftenError("модель вернула пустой ответ")
    if _refused(text[:200]):
        raise SoftenError("модель отказалась переписывать фрагмент")
    words = len(text.split())
    was = max(1, len(original.split()))
    if words < was * 0.5:
        raise SoftenError(f"ответ слишком короткий ({words} слов вместо ~{was})")
    if words > was * 1.6:
        raise SoftenError(f"ответ разросся ({words} слов вместо ~{was})")
    return text


# ─────────────────────────── запись в проект ───────────────────────────

def replace_block(source: str, blocks: list, index: int, new_text: str) -> str:
    """Новый `source.txt`: блок `index` (с единицы) заменён, остальные — как были.

    Блоки воркера — это целые абзацы (через перевод строки) либо куски одного длинного
    абзаца (через пробел). Собираем текст обратно из блоков через перевод строки: там,
    где граница блока прошла внутри абзаца, появится лишний перевод строки — для озвучки
    это пауза между предложениями, не потеря.
    """
    if not 1 <= index <= len(blocks):
        raise SoftenError(f"блока {index} нет: всего блоков {len(blocks)}")
    parts = list(blocks)
    parts[index - 1] = new_text.strip()
    return "\n".join(parts) + "\n"


def write_source(project_dir: str, new_source: str, round_no: int) -> str:
    """Записать новый текст, прежний сохранить рядом. Возвращает путь копии."""
    path = os.path.join(project_dir, "source.txt")
    backup = f"{path}.before-soften-{int(round_no)}"
    with open(path, "rb") as f:
        old = f.read()
    if not os.path.exists(backup):
        with open(backup, "wb") as f:
            f.write(old)
    tmp = path + ".soften.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_source)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return backup


def soften_project(project_dir: str, block_index: int, lang: str, round_no: int,
                   key: str, base: str, *, max_chars: int = None, chat=None) -> dict:
    """Весь шаг: прочитать текст → найти блок → попросить модель → записать.

    Возвращает {"blocks": N, "words_before": a, "words_after": b, "backup": путь}.
    Любая беда — SoftenError с причиной без текста блока.
    """
    path = os.path.join(project_dir, "source.txt")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            source = f.read()
    except OSError as exc:
        raise SoftenError(f"текст проекта не прочитан: {exc.strerror or exc}") from None
    blocks = blocks_of(source, max_chars or max_chars_default())
    if not 1 <= block_index <= len(blocks):
        raise SoftenError(f"в тексте {len(blocks)} блоков, а фильтр назвал блок {block_index}")
    original = blocks[block_index - 1]
    call = chat or chat_once
    reply = call(messages_for(original, lang, round_no), key, base)
    new_text = clean_reply(reply, original)
    if " ".join(new_text.split()) == " ".join(original.split()):
        raise SoftenError("модель вернула текст без изменений")
    new_source = replace_block(source, blocks, block_index, new_text)
    backup = write_source(project_dir, new_source, round_no)
    return {"blocks": len(blocks), "words_before": len(original.split()),
            "words_after": len(new_text.split()), "backup": backup}
