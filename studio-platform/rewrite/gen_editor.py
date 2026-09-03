# -*- coding: utf-8 -*-
"""gen_editor — ретеншн-редактор в пайплайне (2026-07-18), гейт GEN_EDITOR=1.

Автоматизирует ручной QA-цикл (судья находит → редактор режет по строкам) ОДНИМ
дешёвым вызовом. Принцип: редактор ТОЛЬКО удаляет/переносит абзацы, НЕ переписывает
прозу → ноль новых фабрикаций, ноль новых дублей. Операции применяет детерминированный
код. Ловит три ретеншн-киллера, которые не берёт чистильщик (они семантические):
  1. Повтор ЦЕНТРАЛЬНОГО ТЕЗИСА — один парадокс раскидан по 5-10 абзацам разными словами.
  2. Раздутый эпилог — после payoff десятки абзацев последствий + псевдо-финалы.
  3. Кластер вопросов к зрителю после кульминации.

Fail-open: любой сбой → текст возвращается как есть.
"""

import json
import re
from typing import List, Optional, Tuple

_EDITOR_SYSTEM = (
    "You are a ruthless YouTube RETENTION EDITOR. You NEVER rewrite or add prose — you only "
    "DELETE whole paragraphs that hurt watch-time. Output ONLY a JSON object."
)


def _editor_user(numbered: str, language: str) -> str:
    return (
        "Below is a finished narration script, one paragraph per line, prefixed by [n]. "
        "It is already good; find ONLY the paragraphs that KILL retention and must be cut. "
        "Cut targets (be surgical, when unsure KEEP):\n"
        "A. THESIS-REPEAT: the script states its central paradox/irony, then RE-STATES the SAME "
        "idea in other words in several later paragraphs. Keep the single strongest statement "
        "(ideally near the payoff); DELETE the other restatements. A scene that ADDS a new fact "
        "is NOT a restatement — keep it.\n"
        "B. EPILOGUE BLOAT: after the climax/payoff, at most 2-3 aftermath paragraphs plus one "
        "short outro may remain. DELETE extra epilogue paragraphs, second/third 'real endings', "
        "and any call-to-action in the body.\n"
        "C. QUESTION CLUSTER: a run of rhetorical questions to the viewer AFTER the climax — "
        "DELETE those paragraphs.\n"
        "Do NOT delete: unique facts, scenes, quotes, the hook, the single finale. Never cut "
        f"more than 15% of paragraphs. The script is in {language}.\n\n"
        "Output STRICTLY this JSON:\n"
        '{\"delete\": [list of paragraph numbers to remove], '
        '\"reason\": \"one short line\"}\n\n'
        "SCRIPT:\n" + numbered
    )


def _number(text: str) -> Tuple[List[str], str]:
    paras = [p for p in text.split("\n\n") if p.strip()]
    numbered = "\n".join(f"[{i}] {p}" for i, p in enumerate(paras))
    return paras, numbered


def edit_for_retention(
    text: str,
    language: str,
    model: str,
    oauth_token: str,
    log_queue=None,
    max_cut_frac: float = 0.15,
) -> Tuple[str, List[str]]:
    """Возвращает (edited_text, report). Fail-open."""
    report: List[str] = []
    if not text or len(text.split()) < 500:
        return text, report
    try:
        from rewrite_core import call_claude_conversation
    except Exception as e:
        return text, [f"editor: импорт call_claude_conversation упал ({e})"]

    paras, numbered = _number(text)
    if len(paras) < 8:
        return text, report

    try:
        ok, raws, err = call_claude_conversation(
            [_editor_user(numbered, language)], _EDITOR_SYSTEM, model, oauth_token,
            log_queue, task_info="gen-editor", target_words_per_turn=0, max_recovery_turns=0,
        )
    except Exception as e:
        return text, [f"editor: вызов упал ({type(e).__name__}: {e})"]
    if not ok or not raws:
        return text, [f"editor: пустой ответ ({str(err)[:60]})"]

    raw = raws[-1]
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return text, ["editor: JSON не найден в ответе"]
    try:
        obj = json.loads(m.group(0))
    except Exception:
        # спасение: вытащить числа из "delete":[...]
        dm = re.search(r'"delete"\s*:\s*\[([\d,\s]*)\]', raw)
        obj = {"delete": [int(x) for x in re.findall(r"\d+", dm.group(1))] if dm else []}

    to_del = sorted({int(n) for n in (obj.get("delete") or []) if 0 <= int(n) < len(paras)})
    # предохранитель: не резать больше max_cut_frac
    cap = int(len(paras) * max_cut_frac)
    if len(to_del) > cap:
        report.append(f"editor: предложено {len(to_del)} удалений > лимита {cap} — применяю первые {cap}")
        to_del = to_del[:cap]
    if not to_del:
        return text, ["editor: удалять нечего (чисто)"]

    kept = [p for i, p in enumerate(paras) if i not in set(to_del)]
    edited = "\n\n".join(kept)
    cut_words = sum(len(paras[i].split()) for i in to_del)
    report.append(
        f"editor: вырезано {len(to_del)} абзацев (~{cut_words} слов) — {obj.get('reason', '')[:80]}"
    )
    return edited, report
