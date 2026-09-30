# -*- coding: utf-8 -*-
"""Нарезка текста на действия (биты).

Бит — это ЗАКЛАДКА поверх неизменного текста: пара номеров символов.
Модуль ничего не переписывает. Отсюда главная гарантия: склейка всех
закладок побайтово равна исходнику, значит потерять кусок или выдумать
слово физически нельзя, а проверка полноты — арифметика, а не доверие.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

MIN_WORDS = 6
MAX_WORDS = 14
HARD_MIN = 3
HARD_MAX = 18

# Части речи, после которых шов запрещён: разрыв оставит кусок без опоры.
_NO_CUT_AFTER = {"ADP", "CCONJ", "SCONJ", "DET", "PART", "AUX", "PRON"}
# Части речи, перед которыми шов запрещён.
_NO_CUT_BEFORE = {"PUNCT"}

_SPACY = None


def _nlp():
    global _SPACY
    if _SPACY is None:
        import spacy
        _SPACY = spacy.load("ru_core_news_sm", disable=["ner", "lemmatizer"])
    return _SPACY


@dataclass
class Beat:
    idx: int
    start: int
    end: int
    words: int
    forced: bool = False           # шов поставлен принудительно, не по грамматике
    subject: Optional[str] = None  # явное подлежащее, если есть
    verbs: int = 0                 # спрягаемых глаголов

    def text(self, src: str) -> str:
        return src[self.start:self.end]


@dataclass
class Report:
    beats: List[Beat] = field(default_factory=list)
    assembled_ok: bool = False
    total_words: int = 0
    corridor_pct: float = 0.0
    median_words: float = 0.0
    forced_pct: float = 0.0
    too_long: int = 0
    too_short: int = 0
    problems: List[str] = field(default_factory=list)


def _word_count(s: str) -> int:
    return len(re.findall(r"\w+", s, flags=re.UNICODE))


def _cut_candidates(sent) -> List[tuple]:
    """Точки разреза внутри предложения: (позиция_в_документе, вес).

    Вес тем больше, чем «честнее» граница: сначала сильная пунктуация,
    потом сочинительные союзы при новом сказуемом, потом запятые.
    """
    out = []
    toks = list(sent)
    for i, t in enumerate(toks[:-1]):
        nxt = toks[i + 1]
        if nxt.pos_ in _NO_CUT_BEFORE:
            continue
        if t.pos_ in _NO_CUT_AFTER:
            continue
        w = 0
        txt = t.text
        if txt in (";", ":", "—", "–"):
            w = 100
        elif txt == ",":
            # запятая ценнее, если дальше начинается новое сказуемое
            tail = toks[i + 1:i + 6]
            w = 70 if any(x.morph.get("VerbForm") == ["Fin"] for x in tail) else 45
        elif t.pos_ == "VERB" and nxt.pos_ in ("CCONJ", "SCONJ"):
            w = 60
        elif nxt.pos_ in ("CCONJ", "SCONJ") and nxt.text.lower() in ("и", "а", "но", "чтобы", "когда", "пока", "потому"):
            w = 55
        if w:
            out.append((nxt.idx, w))
    return out


def _split_sentence(sent, src: str) -> List[tuple]:
    """Возвращает список (start, end, forced) для одного предложения."""
    s0, s1 = sent.start_char, sent.end_char
    n = _word_count(src[s0:s1])
    if n <= MAX_WORDS:
        return [(s0, s1, False)]

    cands = sorted(_cut_candidates(sent), key=lambda x: -x[1])
    pieces = [(s0, s1)]
    changed = True
    while changed:
        changed = False
        new_pieces = []
        for a, b in pieces:
            if _word_count(src[a:b]) <= MAX_WORDS:
                new_pieces.append((a, b))
                continue
            # ищем кандидата ближе к середине куска среди самых весомых
            inner = [p for p, _w in cands if a < p < b]
            if not inner:
                new_pieces.append((a, b))
                continue
            mid = (a + b) / 2
            best = min(inner, key=lambda p: abs(p - mid))
            new_pieces.append((a, best))
            new_pieces.append((best, b))
            changed = True
        pieces = new_pieces

    out = []
    for a, b in pieces:
        if _word_count(src[a:b]) > HARD_MAX:
            # грамматических кандидатов не осталось — принудительный дорез по словам
            words = list(re.finditer(r"\w+", src[a:b], flags=re.UNICODE))
            if len(words) > HARD_MAX:
                k = len(words) // 2
                cut = a + words[k].start()
                out.append((a, cut, True))
                out.append((cut, b, True))
                continue
        out.append((a, b, False))
    return out


# Кавычки и перевёрнутые знаки, которые могут оказаться на шве: „…” “…” «…» ‚…‘ ¿ ¡.
# Тире диалога, приклеенное к слову («—No puedo»), считается открывающим знаком.
_QUOTES = "„“”«»‚‘’\"¿¡"


def _quote_safe(src: str, p: int) -> int:
    """Шов не отрывает кавычку от её слов.

    Открывающие знаки (перед ними пробел, после — буква) уходят в СЛЕДУЮЩИЙ бит:
    «…ruchu. „|Nie mogę» → «…ruchu. |„Nie mogę». Закрывающая (перед ней не пробел,
    после — пробел или конец) остаётся в ПРЕДЫДУЩЕМ: «|” – krzyknęła» → «”| – …».
    На русских текстах четырёх прогонов (run1, run_ceo, run_isagi, full_text_audit)
    шов на кавычке не встретился ни разу — там границы не меняются."""
    n = len(src)
    if 1 <= p < n and not src[p].isspace():
        k = p
        while k >= 1 and src[k - 1] in _QUOTES + "—":
            k -= 1
        if k < p and (k == 0 or src[k - 1].isspace()):
            return k
    q = p
    while q < n and src[q] in _QUOTES and q > 0 and not src[q - 1].isspace() \
            and (q + 1 == n or src[q + 1].isspace() or src[q + 1] in ",.;:!?…–—"):
        q += 1
    return q


def segment(src: str) -> Report:
    doc = _nlp()(src)
    raw: List[tuple] = []
    for sent in doc.sents:
        raw.extend(_split_sentence(sent, src))

    # склейка слишком коротких с соседом
    merged: List[tuple] = []
    for a, b, f in raw:
        if merged and _word_count(src[a:b]) < HARD_MIN:
            pa, pb, pf = merged[-1]
            merged[-1] = (pa, b, pf or f)
        else:
            merged.append((a, b, f))

    # закладки должны покрывать ВЕСЬ исходник без дыр: тянем границы встык
    beats: List[Beat] = []
    cuts = [_quote_safe(src, merged[i + 1][0]) for i in range(len(merged) - 1)]
    for i, (a, b, f) in enumerate(merged):
        start = 0 if i == 0 else beats[-1].end
        end = len(src) if i == len(merged) - 1 else max(start + 1, cuts[i])
        seg = src[start:end]
        beats.append(Beat(idx=i + 1, start=start, end=end,
                          words=_word_count(seg), forced=f))

    # морфология по каждому биту: подлежащее и число сказуемых
    for bt in beats:
        d = doc.char_span(bt.start, bt.end, alignment_mode="expand")
        if d is None:
            continue
        bt.verbs = sum(1 for t in d if t.morph.get("VerbForm") == ["Fin"])
        subj = [t.text for t in d if t.dep_ in ("nsubj", "nsubj:pass")]
        bt.subject = subj[0] if subj else None

    rep = Report(beats=beats)
    rep.assembled_ok = "".join(src[b.start:b.end] for b in beats) == src
    ws = [b.words for b in beats]
    rep.total_words = _word_count(src)
    if ws:
        inside = sum(1 for w in ws if MIN_WORDS <= w <= MAX_WORDS)
        rep.corridor_pct = inside / len(ws) * 100
        rep.median_words = sorted(ws)[len(ws) // 2]
        rep.forced_pct = sum(1 for b in beats if b.forced) / len(beats) * 100
        rep.too_long = sum(1 for w in ws if w > HARD_MAX)
        rep.too_short = sum(1 for w in ws if w < HARD_MIN)

    if not rep.assembled_ok:
        rep.problems.append("СКЛЕЙКА НЕ СОШЛАСЬ — закладки не покрывают исходник")
    if rep.corridor_pct < 85:
        rep.problems.append(f"в коридор {MIN_WORDS}-{MAX_WORDS} слов попало {rep.corridor_pct:.1f}% при пороге 85%")
    if rep.too_long:
        rep.problems.append(f"битов длиннее {HARD_MAX} слов: {rep.too_long}")
    return rep
