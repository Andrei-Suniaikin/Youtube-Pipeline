# -*- coding: utf-8 -*-
"""Привязка кадров к речи по РЕАЛЬНЫМ словам, а не делением поровну.

Механизм проверен в VideoStudio (render_worker.py:573): известный текст
выравнивается на слова распознавания через sequence-matching. Граница кадра
ставится по времени ЕГО СОБСТВЕННОГО первого слова, поэтому сдвиг не копится:
ошибка на одном кадре не тянет за собой все следующие.
"""
from __future__ import annotations

import difflib
import os
import re
import threading
from typing import List, Optional

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)

# Реэкспорт: pipeline ловит AL.NativeAlignmentError при откате на whisper.
from align_native import NativeAlignmentError  # noqa: E402


def _norm(s: str) -> List[str]:
    return [w.lower() for w in _WORD.findall(s or "")]


def whisper_language(lang: str) -> str:
    """Язык распознавания = язык проекта (одна таблица languages.py). Неизвестный язык — ошибка."""
    import languages as LG
    try:
        return LG.iso(lang)
    except ValueError:
        raise ValueError(f"распознавание речи: неизвестный язык проекта {lang!r}") from None


def blocks_without_word_marks(blocks_json_path: str) -> bool:
    """True, если озвучка сделана движком без меток слов (Lumean): ни у одного блока нет jsonl."""
    import json
    try:
        with open(blocks_json_path, encoding="utf-8") as f:
            records = json.load(f)
    except (OSError, ValueError):
        return False
    if isinstance(records, dict):
        records = records.get("blocks")
    return bool(records) and all(isinstance(r, dict) and not (r.get("word_metadata_path") or "")
                                 for r in records)


def _parallel() -> int:
    raw = (os.environ.get("MANGA_ALIGN_PARALLEL") or "").strip()
    try:
        value = int(raw) if raw else 2
    except ValueError:
        raise ValueError(f"MANGA_ALIGN_PARALLEL: нужно целое число, а не {raw!r}") from None
    if not 1 <= value <= 16:
        raise ValueError("MANGA_ALIGN_PARALLEL: от 1 до 16")
    return value


# Распознавание — самый тяжёлый шаг по памяти и процессору: ролик на 80 минут речи
# занимает ядра на десятки минут. Когда до него доходит вся пачка разом, сервер уходит
# в своп и ложится целиком. Поэтому одновременно распознаём не больше MANGA_ALIGN_PARALLEL
# роликов (по умолчанию 2), а модель держим одну на процесс, а не грузим на каждый ролик.
_SLOTS = threading.BoundedSemaphore(_parallel())
_MODELS: dict = {}
_MODEL_LOCK = threading.Lock()


def _engine() -> str:
    """faster | openai. По умолчанию faster, если библиотека есть: та же модель и те же
    метки слов, но вчетверо быстрее на процессоре. 20.09 ночью обычный whisper считал
    80 минут речи полтора часа и стал стеной для всей пачки."""
    want = (os.environ.get("MANGA_WHISPER_ENGINE") or "").strip().lower()
    if want not in ("", "faster", "openai"):
        raise ValueError("MANGA_WHISPER_ENGINE: faster или openai")
    if want == "openai":
        return "openai"
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        return "openai"
    return "faster"


def _model(model_size: str):
    with _MODEL_LOCK:
        if model_size not in _MODELS:
            if _engine() == "faster":
                from faster_whisper import WhisperModel
                _MODELS[model_size] = ("faster", WhisperModel(model_size, device="cpu", compute_type="int8"))
            else:
                import whisper
                _MODELS[model_size] = ("openai", whisper.load_model(model_size))
        return _MODELS[model_size]


def _words_from_faster(model, audio_path: str, language: Optional[str]) -> List[dict]:
    segments, _info = model.transcribe(audio_path, language=language, word_timestamps=True,
                                       vad_filter=False)
    out: List[dict] = []
    for seg in segments:
        for w in (getattr(seg, "words", None) or []):
            text = (getattr(w, "word", "") or "").strip()
            if not text:
                continue
            out.append({"word": text, "start": float(getattr(w, "start", 0.0) or 0.0),
                        "end": float(getattr(w, "end", 0.0) or 0.0)})
    return out


def whisper_words(audio_path: str, language: Optional[str] = "ru",
                  model_size: str = "base") -> List[dict]:
    """Плоский список слов с временами. Один прогон на весь ролик."""
    with _SLOTS:
        kind, model = _model(model_size)
        if kind == "faster":
            return _words_from_faster(model, audio_path, language)
        res = model.transcribe(audio_path, language=language, word_timestamps=True,
                               fp16=False, verbose=False)
    out: List[dict] = []
    for seg in res.get("segments") or []:
        for w in seg.get("words") or []:
            t = (w.get("word") or "").strip()
            if not t:
                continue
            out.append({"word": t,
                        "start": float(w.get("start", 0.0)),
                        "end": float(w.get("end", w.get("start", 0.0)))})
    return out


def words_receipt(audio_path: str, language: str) -> dict:
    """Приметы звука, по которым видно, что сохранённые слова относятся к нему же."""
    try:
        stat = os.stat(audio_path)
        # 27.09.2026, аудит (кэш распознавания): по одному размеру звук AAAA→BBBB того же
        # размера отдавал старые слова. Хэш содержимого — 0,3 с на 85 МБ; кэши старого вида
        # (без хэша) не совпадут, и распознавание один раз посчитается заново (медиана 2 мин).
        import hashlib
        h = hashlib.sha256()
        with open(audio_path, "rb") as f:
            for кусок in iter(lambda: f.read(1 << 20), b""):
                h.update(кусок)
        return {"bytes": stat.st_size, "language": language, "sha256": h.hexdigest()}
    except OSError:
        return {"bytes": 0, "language": language}


def cached_words(words_path: str, audio_path: str, language: str) -> Optional[List[dict]]:
    """Ранее распознанные слова этого же звука или None.

    Зачем. Распознавание 80 минут речи занимает полчаса и раньше нигде не сохранялось:
    любой перезапуск службы (выпуск, сбой) начинал его заново. 20.09 ночью это стоило бы
    часов на каждом ролике пачки.
    """
    import json
    try:
        with open(words_path, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(saved, dict) or saved.get("receipt") != words_receipt(audio_path, language):
        return None
    words = saved.get("words")
    return words if isinstance(words, list) and words else None


def save_words(words_path: str, audio_path: str, language: str, words: List[dict]) -> None:
    import json
    body = {"receipt": words_receipt(audio_path, language), "words": words}
    tmp = words_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False)
    os.replace(tmp, words_path)


def align(frame_texts: List[str], words: List[dict], total_seconds: float):
    """Возвращает (pairs, source), где source — КАК получены тайминги:

      'по словам'      — выравнивание на реальные слова распознавания (норма);
      'пропорционально'— запасной путь по длине текста, когда слов нет.

    Признак возвращается ЯВНО, а не прячется: пропорциональный тайминг заметно
    хуже, и вызывающий обязан это увидеть, а не догадаться.
    """
    n = len(frame_texts)
    if n == 0:
        return []

    clean = [(float(w["start"]), float(max(w["end"], w["start"])), w["word"].lower())
             for w in words if _WORD.search(w.get("word") or "")]
    clean = [(a, b, _WORD.findall(t)[0]) for a, b, t in clean if _WORD.findall(t)]

    script: List[tuple] = []
    for fi, txt in enumerate(frame_texts):
        for tok in _norm(txt):
            script.append((fi, tok))

    if not clean or not script:
        lens = [max(1, len("".join(_norm(t)))) for t in frame_texts]
        tot = sum(lens) or 1
        out, acc = [], 0.0
        for L in lens:
            d = total_seconds * L / tot
            out.append((round(acc, 3), round(acc + d, 3)))
            acc += d
        return out, "пропорционально"

    wtok = [c[2] for c in clean]
    stok = [s[1] for s in script]
    sm = difflib.SequenceMatcher(None, wtok, stok, autojunk=False)
    w_frame: List[Optional[int]] = [None] * len(clean)
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            w_frame[a + k] = script[b + k][0]

    last = 0
    for i in range(len(w_frame)):
        if w_frame[i] is None:
            w_frame[i] = last
        else:
            last = w_frame[i]

    first: List[Optional[float]] = [None] * n
    for i, (st, _en, _t) in enumerate(clean):
        fi = w_frame[i]
        if fi is not None and 0 <= fi < n and first[fi] is None:
            first[fi] = st
    if first[0] is None:
        first[0] = clean[0][0]

    known = [i for i in range(n) if first[i] is not None]
    for idx in range(len(known) - 1):
        a, b = known[idx], known[idx + 1]
        sa, sb = first[a], first[b]
        for j in range(1, b - a):
            first[a + j] = sa + (sb - sa) * j / (b - a)
    # Хвост после последнего узнанного слова: раскладываем по длине текста кадров.
    # Прежняя формула total - (n-j)*0.4 отдавала хвосту ровно 0,4 секунды на кадр,
    # а весь остальной ролик доставался последнему узнанному кадру — так и рождался
    # кадр на 70 минут, когда узнать удалось только первый (замер 26.09).
    lastk = known[-1]
    if lastk + 1 < n:
        хвост = [max(1, len("".join(_norm(t)))) for t in frame_texts[lastk:]]
        сумма = sum(хвост) or 1
        конец = max(float(first[lastk]) + 0.4 * (n - lastk), total_seconds)
        свободно = max(0.4 * (n - lastk), конец - float(first[lastk]))
        акк = float(first[lastk])
        for j in range(lastk + 1, n):
            акк += свободно * хвост[j - 1 - lastk] / сумма
            first[j] = min(акк, total_seconds - 0.4 * (n - j))

    out = []
    for i in range(n):
        s = max(0.0, float(first[i]))
        e = float(first[i + 1]) if i + 1 < n else total_seconds
        if e <= s:
            e = s + 0.4
        out.append((round(s, 3), round(min(e, total_seconds), 3)))
    return out, "по словам"


NATIVE_SOURCE = "по меткам синтезатора"


def native_pairs(blocks_json_path: str, storyboard_path: str, total_seconds: float,
                 source_path: str, voice_path: Optional[str] = None):
    """Тайминги из меток самого синтезатора (WordBoundary), без whisper.

    Возвращает (pairs, source), source == NATIVE_SOURCE. Любая неполнота данных —
    честное NativeAlignmentError с причиной, а не тихий откат: вызывающий сам
    решает, идти ли запасным путём (whisper), и пишет причину в timing.json.

    Почему native главный, а whisper запасной: замер 09.09 на 492 кадрах — whisper
    опаздывает на 0.3–1.8 с в 225 кадрах, потому что берёт первое РАСПОЗНАННОЕ
    слово, а имена героев не узнаёт. Метки синтезатора дают время первого слова
    каждого кадра точно, без интерполяции (проверено на 493 кадрах в лаборатории).

    Нужны: blocks.json с полями path и word_metadata_path у каждого блока,
    storyboard.json (beats + frames), текст final.txt, из которого резались
    и биты, и блоки.
    """
    from align_native import NativeAlignmentError, compile_files   # модули лежат плоско, как в pipeline.py
    try:
        res = compile_files(source_path, storyboard_path, blocks_json_path, voice_path)
    except NativeAlignmentError:
        raise
    except (KeyError, OSError, ValueError, TypeError) as e:
        # blocks.json без path/word_metadata_path, битый jsonl, нет ffprobe и т.п.
        raise NativeAlignmentError(f"{type(e).__name__}: {e}") from e
    if abs(float(res["total"]) - float(total_seconds)) > 0.12:
        raise NativeAlignmentError(f"длина дорожки {total_seconds:.3f} не сходится с "
                                   f"суммой блоков {res['total']:.3f}")
    pairs = [(round(float(a), 3), round(float(b), 3)) for a, b in res["pairs"]]
    if any(b <= a for a, b in pairs) or any(pairs[i][0] != pairs[i - 1][1]
                                            for i in range(1, len(pairs))):
        raise NativeAlignmentError("границы после округления не монотонны")
    return pairs, NATIVE_SOURCE


def check(pairs: List[tuple], total_seconds: float, tol: float = 0.5) -> dict:
    """Машинная приёмка тайминга: дыр нет, нахлёстов нет, конец сходится."""
    probs = []
    for i in range(1, len(pairs)):
        if pairs[i][0] < pairs[i - 1][1] - 0.01:
            probs.append(f"нахлёст на кадре {i+1}")
        if pairs[i][0] - pairs[i - 1][1] > tol:
            probs.append(f"дыра перед кадром {i+1}: {pairs[i][0]-pairs[i-1][1]:.2f}s")
    if pairs and abs(pairs[-1][1] - total_seconds) > tol:
        probs.append(f"конец не сходится: {pairs[-1][1]:.2f} против {total_seconds:.2f}")
    # Кадры короче секунды подряд — признак, что речи под ними нет (обрубок TTS,
    # блок 32 ролика 09.09: 13 кадров по 0.26 с), а не «быстрый монтаж».
    tiny = [i + 1 for i, (a, b) in enumerate(pairs) if b - a < 1.0]
    if len(tiny) >= 3:
        probs.append(f"кадров короче 1 с: {len(tiny)} ({tiny[:8]}…) — под ними нет речи?")
    # 27.09.2026, аудит (align.check): 30 с пустоты в начале и кадр (0, 0) проходили с ok.
    # Замер на 78 готовых роликах: начало везде 0,0 с (тишина в начале звука ≤ 0,36 с); самый
    # короткий кадр нормального ролика 0,7 с. Короче 0,3 с — вспышка: ccb6b7af кадр 728 0,12 с
    # (распознавание потеряло фразу), 3632631d кадр 665 0,2 с («Charlotte» → «Şarlot»).
    if pairs and pairs[0][0] > tol:
        probs.append(f"пустота в начале: первый кадр с {pairs[0][0]:.2f}s")
    вспышки = [i + 1 for i, (a, b) in enumerate(pairs) if round(b - a, 3) < 0.3]
    if вспышки:
        probs.append(f"кадров короче 0,3 с: {len(вспышки)} ({вспышки[:8]}) — вспышка вместо картинки")
    durs = sorted(b - a for a, b in pairs)
    return {"ok": not probs, "problems": probs[:10],
            "min": round(durs[0], 2) if durs else 0,
            "median": round(durs[len(durs) // 2], 2) if durs else 0,
            "max": round(durs[-1], 2) if durs else 0}
