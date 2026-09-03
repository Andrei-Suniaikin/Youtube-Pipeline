"""Fast, deterministic quality gate for narration scripts.

The gate is deliberately lexical: it never calls an LLM and therefore can run
before TTS without network access, cost, or non-deterministic answers.  It is not
a grammar checker.  Its narrow job is to stop subtitle/ASR-like fragments from
being mistaken for a finished narration script and to return evidence a human
can act on.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable


SCHEMA_VERSION = 1

_WORD_RE = re.compile(
    r"[A-Za-zА-Яа-яЁё0-9]+(?:[-‑–—'][A-Za-zА-Яа-яЁё0-9]+)*"
)
_LETTER_RE = re.compile(r"[A-Za-zА-Яа-яЁё]")
_SENTENCE_RE = re.compile(
    r".+?(?:[.!?…]+(?:[»”\"'’)\]]+)?(?=\s|$)|$)", re.DOTALL
)
_LOWERCASE_LETTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzабвгдежзийклмнопрстуфхцчшщъыьэюяё"
)


@dataclass(frozen=True)
class ScriptQAConfig:
    """Conservative defaults tuned for documentary narration."""

    min_characters: int = 80
    min_words: int = 15
    min_sentences_for_ratios: int = 6
    lowercase_start_ratio: float = 0.14
    short_fragment_ratio: float = 0.09
    broken_punctuation_ratio: float = 0.08
    hard_lowercase_start_ratio: float = 0.20
    hard_broken_punctuation_ratio: float = 0.15
    blocking_score: float = 0.55
    max_examples: int = 5


@dataclass(frozen=True)
class QualitySignal:
    """One explainable measurement used by the gate."""

    code: str
    label: str
    triggered: bool
    value: float
    threshold: float
    unit: str
    count: int
    total: int
    weight: float
    explanation: str
    examples: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ScriptQualityReport:
    """JSON-friendly decision and the measurements behind it."""

    schema_version: int
    passed: bool
    blocked: bool
    risk_score: float
    character_count: int
    word_count: int
    sentence_count: int
    blocking_reasons: tuple[str, ...]
    signals: tuple[QualitySignal, ...]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # ``asdict`` already converts nested dataclasses but intentionally keeps
        # tuples.  Lists make the result directly JSON-schema friendly.
        payload["blocking_reasons"] = list(self.blocking_reasons)
        payload["signals"] = [signal.to_dict() for signal in self.signals]
        for signal in payload["signals"]:
            signal["examples"] = list(signal["examples"])
        return payload


class ScriptQualityError(ValueError):
    """Raised by :func:`require_script_quality` when TTS must not start."""

    def __init__(self, report: ScriptQualityReport):
        self.report = report
        super().__init__(report.summary)


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _sentences(text: str) -> list[str]:
    normalised = _compact(text)
    if not normalised:
        return []
    result = [_compact(match.group(0)) for match in _SENTENCE_RE.finditer(normalised)]
    return [item for item in result if _LETTER_RE.search(item)]


def _first_letter(sentence: str) -> str | None:
    match = _LETTER_RE.search(sentence)
    return match.group(0) if match else None


def _examples(values: Iterable[str], limit: int) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        compact = _compact(value)
        if len(compact) > 180:
            compact = compact[:177].rstrip() + "..."
        if compact and compact not in seen:
            result.append(compact)
            seen.add(compact)
        if len(result) >= limit:
            break
    return tuple(result)


def _ratio(count: int, total: int) -> float:
    return count / total if total else 0.0


def _broken_punctuation(sentence: str) -> bool:
    """Catch common joins left by subtitle/ASR concatenation.

    In ordinary Russian prose a comma/semicolon followed immediately by an
    uppercase word is rare.  It is common when separate subtitle captions were
    joined while their original capitalisation was retained.  The remaining
    expressions catch spacing and dangling punctuation damage.
    """

    return bool(
        re.search(r"[,;]\s+[А-ЯЁ]", sentence)
        or re.search(r"\s+[,;:.!?]", sentence)
        or re.search(r"[,;:]\s*[.!?…]", sentence)
        or re.search(r",\s*,", sentence)
    )


def analyze_script(
    text: str, *, config: ScriptQAConfig | None = None
) -> ScriptQualityReport:
    """Measure script integrity and return a fail-closed TTS decision.

    Ratios are only allowed to block once there are enough sentences for a
    meaningful sample.  Very small/empty input is blocked by a separate minimum
    content signal.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    cfg = config or ScriptQAConfig()
    if cfg.max_examples < 1:
        raise ValueError("max_examples must be positive")

    compact_text = _compact(text)
    sentences = _sentences(compact_text)
    words = _WORD_RE.findall(compact_text)
    sentence_count = len(sentences)
    ratios_reliable = sentence_count >= cfg.min_sentences_for_ratios

    lowercase_sentences: list[str] = []
    short_fragments: list[str] = []
    punctuation_damage: list[str] = []
    for sentence in sentences:
        first = _first_letter(sentence)
        starts_lowercase = bool(first and first in _LOWERCASE_LETTERS)
        sentence_words = _WORD_RE.findall(sentence)
        if starts_lowercase:
            lowercase_sentences.append(sentence)
        # Two/three-word standalone snippets are suspicious regardless of case;
        # a lowercase snippet up to seven words is usually a detached clause.
        if len(sentence_words) <= 3 or (starts_lowercase and len(sentence_words) <= 7):
            short_fragments.append(sentence)
        if _broken_punctuation(sentence):
            punctuation_damage.append(sentence)

    lowercase_ratio = _ratio(len(lowercase_sentences), sentence_count)
    fragment_ratio = _ratio(len(short_fragments), sentence_count)
    punctuation_ratio = _ratio(len(punctuation_damage), sentence_count)
    insufficient = (
        len(compact_text) < cfg.min_characters
        or len(words) < cfg.min_words
        or sentence_count < 2
    )

    signals = (
        QualitySignal(
            code="insufficient_text",
            label="Недостаточно текста для озвучки",
            triggered=insufficient,
            value=float(len(words)),
            threshold=float(cfg.min_words),
            unit="words_minimum",
            count=len(words),
            total=cfg.min_words,
            weight=1.0,
            explanation=(
                "Пустой или слишком короткий вход нельзя считать проверенным сценарием."
            ),
            examples=_examples(sentences, cfg.max_examples) if insufficient else (),
        ),
        QualitySignal(
            code="lowercase_sentence_starts",
            label="Предложения начинаются со строчной буквы",
            triggered=ratios_reliable and lowercase_ratio >= cfg.lowercase_start_ratio,
            value=round(lowercase_ratio, 4),
            threshold=cfg.lowercase_start_ratio,
            unit="sentence_ratio",
            count=len(lowercase_sentences),
            total=sentence_count,
            weight=0.45,
            explanation=(
                "Частые строчные начала указывают на оторванные части фраз или "
                "неправильно склеенные субтитры."
            ),
            examples=_examples(lowercase_sentences, cfg.max_examples),
        ),
        QualitySignal(
            code="short_fragments",
            label="Много коротких обрывков",
            triggered=ratios_reliable and fragment_ratio >= cfg.short_fragment_ratio,
            value=round(fragment_ratio, 4),
            threshold=cfg.short_fragment_ratio,
            unit="sentence_ratio",
            count=len(short_fragments),
            total=sentence_count,
            weight=0.20,
            explanation=(
                "Отдельные предложения из 1–3 слов и короткие фразы со строчной "
                "буквы часто являются кусками одного исходного предложения."
            ),
            examples=_examples(short_fragments, cfg.max_examples),
        ),
        QualitySignal(
            code="broken_punctuation",
            label="Повреждена пунктуация или склейка фраз",
            triggered=ratios_reliable and punctuation_ratio >= cfg.broken_punctuation_ratio,
            value=round(punctuation_ratio, 4),
            threshold=cfg.broken_punctuation_ratio,
            unit="sentence_ratio",
            count=len(punctuation_damage),
            total=sentence_count,
            weight=0.35,
            explanation=(
                "Запятая перед словом с заглавной буквы и другие аномалии типичны "
                "для механической склейки строк транскрипта."
            ),
            examples=_examples(punctuation_damage, cfg.max_examples),
        ),
    )

    risk_score = 0.0
    for signal in signals[1:]:
        if not ratios_reliable or signal.threshold <= 0:
            continue
        risk_score += signal.weight * min(1.0, signal.value / signal.threshold)
    risk_score = round(min(1.0, risk_score), 4)

    blocking_reasons: list[str] = []
    if insufficient:
        blocking_reasons.append("insufficient_text")
    if ratios_reliable and risk_score >= cfg.blocking_score:
        blocking_reasons.append("combined_fragmentation_score")
    if ratios_reliable and lowercase_ratio >= cfg.hard_lowercase_start_ratio:
        blocking_reasons.append("excessive_lowercase_sentence_starts")
    if ratios_reliable and punctuation_ratio >= cfg.hard_broken_punctuation_ratio:
        blocking_reasons.append("severe_punctuation_damage")

    blocked = bool(blocking_reasons)
    if blocked:
        triggered_labels = [signal.label for signal in signals if signal.triggered]
        details = "; ".join(triggered_labels) or ", ".join(blocking_reasons)
        summary = (
            f"TTS заблокирован: сценарий похож на сырой транскрипт "
            f"(риск {risk_score:.0%}). {details}."
        )
    else:
        summary = (
            f"Сценарий прошёл локальную проверку: {sentence_count} предложений, "
            f"риск фрагментации {risk_score:.0%}."
        )

    return ScriptQualityReport(
        schema_version=SCHEMA_VERSION,
        passed=not blocked,
        blocked=blocked,
        risk_score=risk_score,
        character_count=len(compact_text),
        word_count=len(words),
        sentence_count=sentence_count,
        blocking_reasons=tuple(blocking_reasons),
        signals=signals,
        summary=summary,
    )


def require_script_quality(
    text: str, *, config: ScriptQAConfig | None = None
) -> ScriptQualityReport:
    """Return a passing report or raise before any TTS network call starts."""

    report = analyze_script(text, config=config)
    if report.blocked:
        raise ScriptQualityError(report)
    return report


__all__ = [
    "QualitySignal",
    "ScriptQAConfig",
    "ScriptQualityError",
    "ScriptQualityReport",
    "analyze_script",
    "require_script_quality",
]
