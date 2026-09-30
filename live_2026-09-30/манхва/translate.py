# -*- coding: utf-8 -*-
"""Перевод готового текста ролика на другой язык ДОЛЯМИ (битами).

Зачем доли. Кадр раскадровки ссылается не на предложение, а на пару номеров долей
(beat_from..beat_to). Перевести текст целиком нельзя: у другого языка другая нарезка,
и кадры поедут. Поэтому переводим каждую долю отдельно и склеиваем в том же порядке —
число долей и их границы сохраняются, раскадровка эталона годится как есть.

Что гарантируется (tests/test_translate.py):
1. Сколько долей отдали — столько и вернулось. Не сошлось — кусок делится пополам и
   переводится половинами, вплоть до одной доли; одна доля, разрезанная моделью на части,
   склеивается обратно в эту же долю. Молча терять или склеивать СОСЕДНИЕ доли нельзя:
   рассыплется тайминг. Готовые куски пишутся в cache — повтор ролика их не переспрашивает.
2. Пустая доля остаётся пустой, пробелы по краям восстанавливаются по исходной доле:
   склейка перевода = текст ролика без дыр и двойных пробелов.
3. Числа и имена собственные не переписываются: об этом просит промпт, а код проверяет
   только состав долей.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Optional

import claude_pool as CP
from llm import LLMError, call_json

# Деление пополам — сверх одного вызова на кусок. Запас на весь перевод: если модель сбивает
# счёт на каждом куске, 45 кусков до одной доли — это ~1700 вызовов Codex вместо 45.
EXTRA_CALLS = 16

PROMPT = """Translate every fragment below into {lang}. This is the narration of a manhwa recap video.

RULES
- Translate fragment by fragment. Return EXACTLY {n} fragments, in the same order.
- Never merge, split, reorder or drop a fragment: each one becomes one fragment of {lang}.
- Keep the meaning and the order of events. Natural spoken {lang}, the way a narrator reads it aloud.
- Keep every number as in the original (numbers written as words stay words).
- Names, titles and place names: keep the SAME name, but write it in the script of {lang},
  spelled the way a {lang} narrator would read it aloud. Never leave a name in the script
  of the source language: the voice reads these words out loud, and a foreign script is
  read as gibberish. Use the same spelling for the same name every time.
- No quotation marks around a fragment, no numbering, no comments — only the text of the fragment.

Answer in JSON only: {{"fragments": ["<fragment 1>", "<fragment 2>", ...]}}

FRAGMENTS ({n}):
{body}
"""


class TranslateError(RuntimeError):
    pass


def _flat(p: str) -> str:
    """Доля — ровно одна строка списка. Абзац ВНУТРИ доли модель читала как лишний кусок без
    номера: 20.09 доля 368 «…dopo il salto.\\n\\nNella villa accanto, » дала 21 кусок вместо 20."""
    return " ".join(p.split())


def _body(parts: List[str]) -> str:
    return "\n".join(f"{i + 1}. {_flat(p)}" for i, p in enumerate(parts))


def _fragments(data) -> Optional[List[str]]:
    """Куски из ответа модели. Она иногда отвечает списком без обёртки или называет поле иначе —
    это не повод ронять ролик: берём первый список строк, какой найдём."""
    if isinstance(data, list):
        return [str(x) for x in data]
    if isinstance(data, dict):
        got = data.get("fragments")
        if isinstance(got, list):
            return [str(x) for x in got]
        for value in data.values():
            if isinstance(value, list) and all(isinstance(x, (str, int, float)) for x in value):
                return [str(x) for x in value]
    return None


def _ask(parts: List[str], lang: str, model: str, timeout: int, tries: int = 2) -> List[str]:
    last = ""
    for attempt in range(1, tries + 1):
        data, _answer = call_json(PROMPT.format(lang=lang, n=len(parts), body=_body(parts)), model, timeout)
        got = _fragments(data)
        if got is not None:
            return got
        last = str(data)[:120]
    raise TranslateError(f"перевод: в ответе нет списка кусков ({tries} попытки). Ответ начинался так: {last}")


def chunk(parts: List[str], size: int) -> List[List[int]]:
    """Номера долей кусками по size: переводим кусками, а не по одной доле — так дешевле."""
    return [list(range(i, min(i + size, len(parts)))) for i in range(0, len(parts), size)]


def _restore_spacing(src: str, got: str) -> str:
    """Пробелы по краям — как у исходной доли: склейка долей должна дать текст без дыр."""
    left = src[:len(src) - len(src.lstrip())]
    right = src[len(src.rstrip()):]
    return left + got.strip() + right


def _key(lang: str, src: str) -> str:
    return hashlib.sha1(f"{lang}\n{src}".encode("utf-8")).hexdigest()


def _load_cache(path: Optional[str]) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        data = json.load(open(path, encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _translate(idxs: List[int], parts: List[str], lang: str, model: str, timeout: int, log: Callable,
               where: str, spare: Callable[[], bool], commit: Callable[[List[int], List[str]], None]) -> None:
    """Перевести доли idxs (номера в parts). Не сошлось число — пополам, до одной доли, пока есть
    запас вызовов. Каждая сошедшаяся часть сразу отдаётся в commit: если дальше кончится запас,
    переведённое не пропадёт и повтор ролика переспросит только остаток (проверка 21.09)."""
    ask = [parts[i] for i in idxs]
    got = _ask(ask, lang, model, timeout)
    if len(got) == len(ask):
        commit(idxs, got)
        return
    if len(ask) == 1:
        pieces = [g.strip() for g in got if str(g).strip()]
        if pieces:      # одну долю модель порезала — это всё ещё одна доля, границы целы
            commit(idxs, [" ".join(pieces)])
            return
        raise TranslateError(f"перевод: доля {where} вернулась пустой")
    if not spare():
        raise TranslateError(f"перевод: кусок {where} вернулся {len(got)} долями вместо {len(ask)}, "
                             f"запас повторов ({EXTRA_CALLS}) исчерпан — тайминг кадров рассыпался бы")
    half = len(idxs) // 2
    log(f"  перевод: кусок {where} вернулся {len(got)} долями вместо {len(ask)} — перевожу половинами")
    first = None
    for sub in (idxs[:half], idxs[half:]):
        try:
            _translate(sub, parts, lang, model, timeout, log, where, spare, commit)
        except TranslateError as e:     # вторая половина всё равно переводится и сохраняется
            first = first or e
    if first is not None:
        raise first


def translate_parts(parts: List[str], lang: str, model: str, size: int = 20, workers: int = 4,
                    timeout: int = 900, log: Callable = print, cache: Optional[str] = None) -> List[str]:
    """Перевести доли на язык lang. Длина ответа равна длине parts, порядок тот же.
    cache — файл готовых долей: упавший кусок не выбрасывает соседние, повтор ролика
    переводит только недостающее."""
    out: List[Optional[str]] = [None] * len(parts)
    done = _load_cache(cache)
    for i, p in enumerate(parts):
        hit = done.get(str(i))
        if isinstance(hit, dict) and hit.get("key") == _key(lang, p) and isinstance(hit.get("out"), str):
            out[i] = hit["out"]
    todo = [i for i, x in enumerate(out) if x is None]
    if cache and len(todo) < len(parts):
        log(f"  перевод: {len(parts) - len(todo)} долей уже переведены, осталось {len(todo)}")
    groups = [todo[i:i + size] for i in range(0, len(todo), size)]
    lock = threading.Lock()
    failed: List[str] = []
    errors: List[BaseException] = []
    stop = threading.Event()        # сбой связи/лимита: новые куски не начинаем
    left = [EXTRA_CALLS]

    def spare() -> bool:
        with lock:
            if left[0] < 2:
                return False
            left[0] -= 2
            return True

    def commit(idxs: List[int], got: List[str]) -> None:
        with lock:
            for i, g in zip(idxs, got):
                text = parts[i] if not parts[i].strip() else _restore_spacing(parts[i], g)
                out[i] = text
                done[str(i)] = {"key": _key(lang, parts[i]), "out": text}
            if cache:
                _save_cache(cache, done)

    def one(nums: List[int]) -> None:
        where = f"{nums[0] + 1}–{nums[-1] + 1}"
        if stop.is_set():
            return
        empty = [i for i in nums if not parts[i].strip()]
        if empty:
            commit(empty, [parts[i] for i in empty])     # пустая доля остаётся пустой
        ask = [i for i in nums if parts[i].strip()]
        try:
            if ask:
                _translate(ask, parts, lang, model, timeout, log, where, spare, commit)
        except (TranslateError, LLMError) as e:   # соседние куски доводим и сохраняем, ошибку — в конце
            if not isinstance(e, TranslateError):
                stop.set()                  # связь или лимит: остальным кускам то же самое
            with lock:
                failed.append(f"{where}: {e}")
                errors.append(e)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        # CP.carry: потоки видят задачу — остановка службы и потеря задачи прерывают ожидание лимита.
        list(ex.map(CP.carry(one), groups))
    if failed:
        other = next((e for e in errors if not isinstance(e, TranslateError)), None)
        if other is not None:
            # Лимит, связь, таймаут: наверх идёт ИСХОДНАЯ ошибка — её тип видят автоповтор (watch.classify)
            # и ожидание лимита, как до этой правки. Готовые куски уже в cache.
            raise other
        raise TranslateError(f"перевод: не переведены куски ({len(failed)}), готовые сохранены: "
                             + "; ".join(sorted(failed))[:600])
    missing = [i for i, x in enumerate(out) if x is None]
    if missing:
        raise TranslateError(f"перевод: не переведены доли {missing[:5]}")
    return [str(x) for x in out]


def beat_parts(text: str, beats) -> List[str]:
    """Куски текста по закладкам долей плюс хвосты между ними: склейка равна тексту побайтно."""
    parts, pos = [], 0
    for b in beats:
        if b.start > pos:
            parts.append(text[pos:b.start])
        parts.append(text[b.start:b.end])
        pos = b.end
    if pos < len(text):
        parts.append(text[pos:])
    return parts


def rebuild(parts: List[str]) -> str:
    return "".join(parts)


def offsets(parts: List[str], beats) -> List[dict]:
    """Границы долей в новом тексте: тот же порядок, пересчитанные номера символов."""
    out, pos, bi = [], 0, 0
    src_is_beat = []
    p = 0
    for b in beats:
        if b.start > p:
            src_is_beat.append(False)
        src_is_beat.append(True)
        p = b.end
    if len(src_is_beat) < len(parts):
        src_is_beat += [False] * (len(parts) - len(src_is_beat))
    for part, is_beat in zip(parts, src_is_beat):
        if is_beat:
            out.append({"idx": bi + 1, "start": pos, "end": pos + len(part)})
            bi += 1
        pos += len(part)
    return out


def valid_language(lang: str) -> str:
    if not re.fullmatch(r"[A-Za-zА-Яа-яЁё ]{2,32}", str(lang or "")):
        raise TranslateError("перевод: неизвестный язык")
    return lang
