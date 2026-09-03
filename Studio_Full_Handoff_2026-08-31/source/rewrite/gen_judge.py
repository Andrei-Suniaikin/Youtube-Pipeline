# -*- coding: utf-8 -*-
"""gen_judge — судья-редактор ретеншна (средний ярус), гейт GEN_JUDGE=report|edit (2026-07-20).

ОДИН вызов дешёвой модели (GEN_JUDGE_MODEL, дефолт claude-sonnet-4-6): оценка по
ретеншн-рубрике + якоря-цитаты (hook_end/payoff/signoff/cta) + delete-кластеры.
Всё остальное — детерминированный код:
  • якорь = ЦИТАТА, номер абзаца = подсказка: fuzzy-поиск с переприязкой n → n±1 →
    глобально (off-by-one судьи не пробивает protected);
  • вырезы применяются КЛАСТЕРАМИ целиком (ран или ничего); protected-конфликт
    выбрасывает весь ран; кэп 15% отбрасывает раны с конца;
  • матфакт: при кэпе 15% payoff сдвигается максимум ×1.176 ⇒ payoff < ~59.5%
    вырезанием не лечится → сразу ESCALATE; при раннем payoff (<LO, но досягаем)
    пред-payoff вырезы запрещены (двигают его назад); при позднем payoff (>HI)
    пост-payoff вырезы запрещены (двигают его ещё дальше);
  • после внутреннего re-clean — повторная проверка protected-цитат с откатом;
  • гейты: payoff 40–70% · финал один (якоря судьи ∪ regex cleaner'а) ·
    CTA только в хвосте · хук ≤300 слов.

Режимы (env GEN_JUDGE): report — только отчёт, вырезы НЕ применяются (фаза 1);
                        edit  — вырезы применяются, заменяет gen_editor (фаза 2).
Fail-open: любой сбой → текст как есть, джоба не падает.

CLI (нужен CLAUDE_CODE_OAUTH_TOKEN в env):
    python3 gen_judge.py file.txt [--edit] [--lang Polish]   # отчёт в stdout,
    # при --edit рядом пишется file.judged.txt
"""

import difflib
import json
import os
import re
import sys
import time
from typing import Dict, List, Optional, Tuple

JUDGE_VERSION = "1.0"

_CAP_FRAC = 0.15            # максимум вырезаемых абзацев (доля)
_MIN_WORDS = 1000           # короче — не судим (рубрика калибрована на длинные тексты)
# PATCH-2026-07-23-rules: окно по данным топ-vs-флоп 15 каналов (топы 62.8%, флопы 74.1%,
# бэклоад >85% = флоп-паттерн). Прежнее 70–92 толкало тексты в зону флопов.
_PAYOFF_LO, _PAYOFF_HI = 40.0, 70.0
_PAYOFF_REACHABLE = _PAYOFF_LO / (1.0 / (1.0 - _CAP_FRAC))   # ≈ 59.5
_FUZZ = 0.85                # порог SequenceMatcher для fuzzy-якоря
_CTA_TAIL_WORDS = 200       # CTA легален только в последних N словах

# ── промпт ────────────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are a ruthless YouTube RETENTION JUDGE for long faceless narration scripts. "
    "You never rewrite prose. You measure retention quality, anchor your findings with "
    "EXACT verbatim quotes, and mark whole paragraphs that hurt watch-time for deletion. "
    "Output ONLY one valid JSON object, first character '{', no markdown, no commentary."
)


def _user_prompt(numbered: str, language: str, n_paras: int) -> str:
    return (
        "Below is a finished narration script, one paragraph per line, prefixed [n] "
        f"(0..{n_paras - 1}). Language: {language}.\n\n"
        "TASK 1 — SCORE retention (integers): hook 0-25, loops 0-20 (open loops planted and "
        "paid off), tempo 0-20 (new fact/scene every 100-150 words, no lecturing), payoff 0-15 "
        "(title question answered in the 40-70% window; a payoff pushed past 85% kills retention), peaks 0-10 (emotional spikes).\n\n"
        "TASK 2 — ANCHORS. For each anchor give the paragraph number AND an EXACT VERBATIM "
        "quote copied character-for-character from that paragraph, 6-15 consecutive words:\n"
        "- hook_end: the sentence where the opening hook ends and normal narration begins;\n"
        "- payoff: the sentence where the TITLE's central question/promise is actually answered;\n"
        "- signoff: EVERY paragraph that reads as a closing/outro/sign-off (thanks, farewell, "
        "'subscribe and see you next time' endings) — list them all;\n"
        "- cta: EVERY subscribe/like/comment call-to-action, wherever it is.\n\n"
        "TASK 3 — DELETES. Paragraphs that KILL retention and must be cut (be surgical, when "
        "unsure KEEP). Each item = one CONSECUTIVE run of paragraph numbers + class:\n"
        "- thesis-repeat: the central paradox re-stated in other words (keep the single "
        "strongest statement, cut restatements; a scene ADDING a new fact is NOT a restatement);\n"
        "- epilogue: after the climax at most 2-3 aftermath paragraphs plus ONE outro may stay — "
        "cut extra epilogue, second/third 'real endings';\n"
        "- questions: runs of rhetorical questions to the viewer after the climax;\n"
        "- other: anything else that clearly kills watch-time (explain in reason).\n"
        "NEVER mark: unique facts/scenes/quotes, the hook, the payoff paragraph, the single "
        "final outro.\n\n"
        "TASK 4 — ESCALATE. Defects deletion can NOT fix (answer to the title spilled far too "
        "early, dead middle with no new information, missing hook): type + short verbatim quote.\n\n"
        "Output STRICTLY this JSON shape:\n"
        "{\"breakdown\": {\"hook\": 0, \"loops\": 0, \"tempo\": 0, \"payoff\": 0, \"peaks\": 0},\n"
        " \"anchors\": {\"hook_end\": {\"para\": 0, \"quote\": \"...\"},\n"
        "              \"payoff\": {\"para\": 0, \"quote\": \"...\"},\n"
        "              \"signoff\": [{\"para\": 0, \"quote\": \"...\"}],\n"
        "              \"cta\": [{\"para\": 0, \"quote\": \"...\"}]},\n"
        " \"deletes\": [{\"paras\": [1, 2], \"class\": \"epilogue\", \"reason\": \"...\"}],\n"
        " \"escalate\": [{\"type\": \"...\", \"quote\": \"...\"}]}\n\n"
        "SCRIPT:\n" + numbered
    )


# ── нормализация и fuzzy-якоря ────────────────────────────────────────────────

_TRANS = str.maketrans({
    "«": '"', "»": '"', "„": '"', "“": '"', "”": '"', "‟": '"',
    "‘": "'", "’": "'", "‚": "'",
    "—": "-", "–": "-", "‑": "-",
    "…": "...", " ": " ", " ": " ", " ": " ",
})


def _norm(s: str) -> str:
    """Нормализация для сравнения цитат: регистр, кавычки/тире/многоточия/NBSP, пробелы."""
    return " ".join((s or "").translate(_TRANS).casefold().split())


def _fuzzy_in(quote_n: str, para_n: str) -> bool:
    """quote_n входит в para_n: substring ИЛИ пословное окно с ratio ≥ _FUZZ.
    Окна len(qw)-1..len(qw)+1 — одно вставленное/пропущенное слово в цитате
    (граница предложения) не убивает матч (ревью 2026-07-20)."""
    if not quote_n or not para_n:
        return False
    if quote_n in para_n:
        return True
    qw = quote_n.split()
    pw = para_n.split()
    if len(qw) < 4:
        return False
    for wlen in (len(qw) - 1, len(qw), len(qw) + 1):
        if wlen < 4 or len(pw) < wlen:
            continue
        for i in range(len(pw) - wlen + 1):
            win = " ".join(pw[i:i + wlen])
            if difflib.SequenceMatcher(None, quote_n, win).ratio() >= _FUZZ:
                return True
    return False


def _find_para(quote: str, paras: List[str], hint: Optional[int]) -> Tuple[Optional[int], str]:
    """Ищет абзац с цитатой: подсказка → соседи ±1 → глобально (требуя уникальность).
    Возвращает (индекс | None, как_нашли)."""
    qn = _norm(quote)
    if not qn or len(qn.split()) < 4:
        return None, "quote-too-short"
    pn = [_norm(p) for p in paras]
    if hint is not None and 0 <= hint < len(paras) and _fuzzy_in(qn, pn[hint]):
        return hint, "hint"
    if hint is not None:
        for d in (-1, 1):
            j = hint + d
            if 0 <= j < len(paras) and _fuzzy_in(qn, pn[j]):
                return j, "hint±1"
    hits = [i for i, p in enumerate(pn) if _fuzzy_in(qn, p)]
    if len(hits) == 1:
        return hits[0], "global"
    if len(hits) > 1:
        return None, f"ambiguous({len(hits)})"
    return None, "not-found"


# ── разбиение и позиции ──────────────────────────────────────────────────────

def _split_paras(text: str) -> List[str]:
    return [p for p in text.split("\n\n") if p.strip()]


def _numbered(paras: List[str]) -> str:
    # внутренние \n схлопываем — «[n] один абзац = одна строка» для модели
    return "\n".join(f"[{i}] {' '.join(p.split())}" for i, p in enumerate(paras))


def _end_pct(paras: List[str], idx: int) -> float:
    """Позиция КОНЦА абзаца idx в % по кумулятивным словам."""
    total = sum(len(p.split()) for p in paras) or 1
    upto = sum(len(p.split()) for p in paras[: idx + 1])
    return upto * 100.0 / total


def _start_word(paras: List[str], idx: int) -> int:
    return sum(len(p.split()) for p in paras[:idx])


# ── парсинг ответа судьи ─────────────────────────────────────────────────────

def _parse(raw: str) -> Optional[Dict]:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    blob = m.group(0)
    try:
        return json.loads(blob)
    except Exception:
        pass
    try:
        obj, _ = json.JSONDecoder().raw_decode(blob)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


# ── кластеры вырезов ─────────────────────────────────────────────────────────

def _to_runs(paras_field) -> List[List[int]]:
    """Список номеров → непрерывные раны (некорректное разрезаем на подряд-куски).
    Битые элементы ("abc", None) пропускаем поэлементно — не выбрасывая весь кластер."""
    nums_set = set()
    for n in (paras_field or []):
        try:
            nums_set.add(int(n))
        except (TypeError, ValueError):
            continue
    nums = sorted(nums_set)
    runs, cur = [], []
    for n in nums:
        if cur and n != cur[-1] + 1:
            runs.append(cur)
            cur = []
        cur.append(n)
    if cur:
        runs.append(cur)
    return runs


def _plan_deletes(
    deletes_raw: List[Dict], n_paras: int, protected: set,
    payoff_idx: Optional[int], payoff_pct: Optional[float],
) -> Tuple[List[Dict], List[str]]:
    """Валидация delete-кластеров: границы, protected, ранний payoff, кэп 15%.
    Ран выбрасывается ЦЕЛИКОМ при любом конфликте. Возвращает (планы, лог)."""
    log: List[str] = []
    plans: List[Dict] = []
    early_payoff = payoff_pct is not None and payoff_pct < _PAYOFF_LO
    late_payoff = payoff_pct is not None and payoff_pct > _PAYOFF_HI
    for item in deletes_raw or []:
        if not isinstance(item, dict):
            continue
        klass = str(item.get("class", "other"))[:24]
        reason = str(item.get("reason", ""))[:120]
        for run in _to_runs(item.get("paras")):
            run = [n for n in run if 0 <= n < n_paras]
            if not run:
                continue
            if any(n in protected for n in run):
                log.append(f"судья: ран {run[0]}-{run[-1]} ({klass}) отклонён — protected")
                continue
            if early_payoff and payoff_idx is not None and run[0] <= payoff_idx:
                log.append(
                    f"судья: ран {run[0]}-{run[-1]} ({klass}) отклонён — пред-payoff вырез "
                    f"при раннем payoff ({payoff_pct:.0f}%) двигает его назад"
                )
                continue
            # PATCH-2026-07-23-rules: симметрично — вырез ПОСЛЕ позднего payoff увеличивает его %
            if late_payoff and payoff_idx is not None and run[0] > payoff_idx:
                log.append(
                    f"судья: ран {run[0]}-{run[-1]} ({klass}) отклонён — пост-payoff вырез "
                    f"при позднем payoff ({payoff_pct:.0f}%) двигает его дальше"
                )
                continue
            plans.append({"run": run, "class": klass, "reason": reason})
    # Приоритет при кэпе: самые ценные вырезы — хвост после кульминации (ww2: 11/11),
    # поэтому сортируем по позиции и отбрасываем РАННИЕ раны, сохраняя эпилог-вырезы.
    plans.sort(key=lambda p: p["run"][0])
    cap = int(n_paras * _CAP_FRAC)
    total = sum(len(p["run"]) for p in plans)
    while plans and total > cap:
        dropped = plans.pop(0)  # ранние раны первыми под нож кэпа, НЕ разрезая ран
        total -= len(dropped["run"])
        log.append(
            f"судья: ран {dropped['run'][0]}-{dropped['run'][-1]} ({dropped['class']}) "
            f"отброшен кэпом {cap} абзацев"
        )
    return plans, log


# ── гейты ────────────────────────────────────────────────────────────────────

def _signoff_regex_hits(paras: List[str], language: str) -> set:
    """Regex-кандидаты sign-off из cleaner'а (pl/cs/ro/other) в хвосте >65%. Fail-open."""
    try:
        from gen_output_cleaner import _SIGNOFF, _detect_lang
        lang = _detect_lang("\n\n".join(paras))
        sig_re = re.compile(_SIGNOFF.get(lang, _SIGNOFF["other"]), re.IGNORECASE)
    except Exception:
        return set()
    total = sum(len(p.split()) for p in paras) or 1
    acc, hits = 0, set()
    for i, p in enumerate(paras):
        acc += len(p.split())
        if acc > total * 0.65 and sig_re.search(p):
            hits.add(i)
    return hits


def _compute_gates(
    paras: List[str], language: str,
    hook_idx: Optional[int], payoff_idx: Optional[int],
    signoff_idx: List[int], cta_idx: List[int],
) -> Dict:
    total_words = sum(len(p.split()) for p in paras) or 1
    gates: Dict[str, Dict] = {}

    # payoff 70–92%
    if payoff_idx is None:
        gates["payoff_window"] = {"status": "unknown"}
    else:
        pct = _end_pct(paras, payoff_idx)
        gates["payoff_window"] = {
            "status": "pass" if _PAYOFF_LO <= pct <= _PAYOFF_HI else "fail",
            "pct": round(pct, 1),
        }

    # финал один: якоря судьи ∪ regex cleaner'а (обе разбивки в отчёт — видно расхождение)
    regex_hits = _signoff_regex_hits(paras, language)
    union = set(signoff_idx) | regex_hits
    gates["one_finale"] = {
        "status": "pass" if len(union) <= 1 else "fail",
        "count": len(union), "paras": sorted(union),
        "judge_paras": sorted(set(signoff_idx)), "regex_paras": sorted(regex_hits),
    }

    # CTA только в хвосте
    if not cta_idx:
        gates["cta_tail_only"] = {"status": "pass", "count": 0}
    else:
        bad = [i for i in cta_idx if _start_word(paras, i) < total_words - _CTA_TAIL_WORDS]
        gates["cta_tail_only"] = {
            "status": "pass" if not bad else "fail",
            "count": len(cta_idx), "in_body": bad,
        }

    # хук ≤300 слов
    if hook_idx is None:
        gates["hook_300w"] = {"status": "unknown"}
    else:
        upto = sum(len(p.split()) for p in paras[: hook_idx + 1])
        gates["hook_300w"] = {"status": "pass" if upto <= 300 else "fail", "words": upto}
    return gates


# ── главный вход ─────────────────────────────────────────────────────────────

def run_judge(
    text: str,
    language: str,
    oauth_token: str,
    mode: str = "report",
    log_queue=None,
    topic: str = "",
) -> Tuple[Optional[str], Optional[Dict], List[str]]:
    """Возвращает (edited_text | None, report | None, log_lines). Fail-open:
    (None, None, [почему]) — текст не менять, отчёт не писать."""
    log: List[str] = []
    words_before = len(text.split()) if text else 0
    if words_before < _MIN_WORDS:
        return None, None, [f"judge: пропуск — {words_before} слов < {_MIN_WORDS}"]
    try:
        from rewrite_core import call_claude_conversation
    except Exception as e:
        return None, None, [f"judge: импорт call_claude_conversation упал ({e})"]

    model = os.environ.get("GEN_JUDGE_MODEL", "claude-sonnet-4-6").strip() or "claude-sonnet-4-6"
    try:
        hard_cap = int(os.environ.get("GEN_JUDGE_TIMEOUT_SEC", "180") or 180)
    except (TypeError, ValueError):
        hard_cap = 180

    paras = _split_paras(text)
    if len(paras) < 8:
        return None, None, [f"judge: пропуск — {len(paras)} абзацев < 8"]

    try:
        ok, raws, err = call_claude_conversation(
            [_user_prompt(_numbered(paras), language or "the script's language", len(paras))],
            _SYSTEM, model, oauth_token, log_queue,
            task_info="gen-judge", target_words_per_turn=0, max_recovery_turns=0,
            hard_cap_sec=hard_cap,
        )
    except Exception as e:
        return None, None, [f"judge: вызов упал ({type(e).__name__}: {str(e)[:80]})"]
    if not raws:
        return None, None, [f"judge: пустой ответ ({str(err)[:80]})"]

    obj = _parse(raws[-1])
    if not obj:
        return None, None, ["judge: JSON не распарсен — отчёта нет"]

    # --- breakdown ---
    br = obj.get("breakdown") or {}
    def _int(v, hi):
        try:
            return max(0, min(int(v), hi))
        except Exception:
            return 0
    breakdown = {
        "hook": _int(br.get("hook"), 25), "loops": _int(br.get("loops"), 20),
        "tempo": _int(br.get("tempo"), 20), "payoff": _int(br.get("payoff"), 15),
        "peaks": _int(br.get("peaks"), 10),
    }
    score = sum(breakdown.values())  # из 90 (клик исключён — нужна ниша)

    # --- якоря: цитата = истина, номер = подсказка ---
    anch = obj.get("anchors") or {}
    unknown: List[str] = []

    def _hint(a):
        try:
            return int(a.get("para"))  # LLM часто отдаёт числа строками — коэрция
        except (TypeError, ValueError):
            return None

    def _one(name) -> Tuple[Optional[int], Optional[str]]:
        a = anch.get(name) or {}
        if not isinstance(a, dict):
            return None, None
        q = str(a.get("quote", ""))
        idx, how = _find_para(q, paras, _hint(a))
        if idx is None:
            unknown.append(f"{name}:{how}")
        elif how != "hint":
            log.append(f"судья: якорь {name} переприязан ({how}) → абзац {idx}")
        return idx, q

    def _many(name) -> List[Tuple[int, str]]:
        out = []
        for a in (anch.get(name) or []):
            if not isinstance(a, dict):
                continue
            q = str(a.get("quote", ""))
            idx, how = _find_para(q, paras, _hint(a))
            if idx is None:
                unknown.append(f"{name}:{how}")
            else:
                if how != "hint":
                    log.append(f"судья: якорь {name} переприязан ({how}) → абзац {idx}")
                out.append((idx, q))
        return out

    hook_idx, hook_q = _one("hook_end")
    payoff_idx, payoff_q = _one("payoff")
    signoffs = _many("signoff")
    ctas = _many("cta")
    signoff_idx = sorted({i for i, _ in signoffs})
    cta_idx = sorted({i for i, _ in ctas})

    payoff_pct = round(_end_pct(paras, payoff_idx), 1) if payoff_idx is not None else None
    gates_before = _compute_gates(paras, language, hook_idx, payoff_idx, signoff_idx, cta_idx)

    # --- protected: первый/последний абзац + якорные ---
    protected = {0, len(paras) - 1}
    if hook_idx is not None:
        protected.add(hook_idx)
    if payoff_idx is not None:
        protected.add(payoff_idx)
    if signoff_idx:
        protected.add(signoff_idx[-1])

    # --- план вырезов ---
    escalate = [e for e in (obj.get("escalate") or []) if isinstance(e, dict)][:10]
    unreachable = payoff_pct is not None and payoff_pct < _PAYOFF_REACHABLE
    if unreachable:
        plans, plan_log = [], [
            f"судья: payoff на {payoff_pct}% < {_PAYOFF_REACHABLE:.1f}% — вырезанием "
            f"не лечится (кэп {int(_CAP_FRAC * 100)}% даёт сдвиг ≤×{1 / (1 - _CAP_FRAC):.3f}) → ESCALATE"
        ]
    else:
        plans, plan_log = _plan_deletes(
            obj.get("deletes") or [], len(paras), protected, payoff_idx, payoff_pct)
    log.extend(plan_log)
    planned = [
        {"paras": p["run"], "class": p["class"], "reason": p["reason"],
         "words": sum(len(paras[i].split()) for i in p["run"])}
        for p in plans
    ]

    # --- применение (только edit) ---
    edited_text: Optional[str] = None
    applied: Dict = {"applied": False}
    gates_after: Optional[Dict] = None
    payoff_pct_after: Optional[float] = None
    words_after = words_before

    if mode == "edit" and plans and not unreachable and payoff_idx is None and payoff_q:
        # ревью 2026-07-20: payoff-цитата дана, но не разрешена (галлюцинация/дубль) —
        # кульминацию нечем защитить, вырезы НЕ применяем (деградация до report).
        log.append("судья: payoff-якорь не разрешён — вырезы не применены (только отчёт)")

    elif mode == "edit" and plans and not unreachable:
        drop = {i for p in plans for i in p["run"]}
        kept = [p for i, p in enumerate(paras) if i not in drop]
        after_cut = "\n\n".join(kept)
        # внутренний re-clean (склейки после вырезов) — fail-open, пропуск логируем
        cleaned = after_cut
        try:
            from gen_output_cleaner import clean_generated_text
            cleaned, _ = clean_generated_text(after_cut, language)
        except Exception as _rce:
            log.append(f"судья: re-clean пропущен ({type(_rce).__name__})")
        # protected-цитаты: ТОЛЬКО разрешённые якоря (idx найден ⇒ цитата реально
        # есть в исходнике; галлюцинированная цитата не может требоваться в финале).
        prot_quotes = [q for i, q in ((hook_idx, hook_q), (payoff_idx, payoff_q))
                       if i is not None and q]
        if signoffs:
            prot_quotes.append(max(signoffs, key=lambda t: t[0])[1])  # цитата max-idx пары
        final = cleaned
        rollback = ""
        missing = [q for q in prot_quotes if not _fuzzy_in(_norm(q), _norm(final))]
        if missing:
            final, rollback = after_cut, "re-clean"
            missing2 = [q for q in prot_quotes if not _fuzzy_in(_norm(q), _norm(final))]
            if missing2:
                final, rollback = None, "full"  # protected-раны отклонялись — аномалия
        if final is None:
            log.append("судья: protected-цитата пропала после вырезов — полный откат, текст не тронут")
            applied = {"applied": False, "rollback": "full"}
        else:
            words_after_c = len(final.split())
            fparas = _split_paras(final)
            # пере-поиск якорей в итоговом тексте
            def _refind(q):
                if not q:
                    return None
                i, _ = _find_para(q, fparas, None)
                return i
            h2, p2 = _refind(hook_q), _refind(payoff_q)
            s2 = sorted({i for i in (_refind(q) for _, q in signoffs) if i is not None})
            c2 = sorted({i for i in (_refind(q) for _, q in ctas) if i is not None})
            g_after = _compute_gates(fparas, language, h2, p2, s2, c2)
            # ревью 2026-07-20: гейт-регрессия (pass → fail от наших же вырезов,
            # напр. пред-payoff вырез утащил payoff из окна) → полный откат.
            regressed = [k for k, g in g_after.items()
                         if g.get("status") == "fail"
                         and gates_before.get(k, {}).get("status") == "pass"]
            if regressed:
                log.append(f"судья: вырезы ухудшили гейты {regressed} — откат, текст не тронут")
                applied = {"applied": False, "rollback": "gate-regression",
                           "regressed": regressed}
            else:
                edited_text = final
                words_after = words_after_c
                gates_after = g_after
                payoff_pct_after = round(_end_pct(fparas, p2), 1) if p2 is not None else None
                applied = {
                    "applied": True, "runs": len(plans),
                    "paras_cut": len(drop), "words_cut": words_before - words_after,
                    "rollback": rollback,
                }
                log.append(
                    f"судья: вырезано {len(drop)} абзацев (~{words_before - words_after} слов) "
                    f"в {len(plans)} кластерах" + (", откат re-clean" if rollback else "")
                )

    # --- вердикт (unknown-гейт ≠ fail: не пере-эскалируем из-за ненайденного якоря) ---
    # КАЛИБРОВКА 2026-07-20 (4/4 текста волны 5 давали ESCALATE): escalate-items судьи —
    # ИНФОРМАЦИОННЫЕ (советы полному прогону), вердикт ESCALATE ставит только КОД:
    # недостижимый вырезанием payoff (<59.5%) или остаточный гейт-fail после вырезов.
    ref_gates = gates_after or gates_before
    any_fail = any(g.get("status") == "fail" for g in ref_gates.values())
    all_pass = all(g.get("status") == "pass" for g in ref_gates.values())
    if unreachable:
        verdict = "ESCALATE"
    elif mode == "edit" and applied.get("applied"):
        verdict = "EDITED" if not any_fail else "ESCALATE"
    elif mode == "report" and planned:
        verdict = "EDIT_PLANNED"
    elif all_pass:
        verdict = "PASS"
    else:
        verdict = "FLAG"  # есть fail-гейты без вырезаемого, либо unknown-якоря

    report = {
        "judge_version": JUDGE_VERSION, "ts": time.time(), "mode": mode, "model": model,
        "verdict": verdict, "score_of_90": score, "breakdown": breakdown,
        "anchors": {
            "hook_end": {"para": hook_idx}, "payoff": {"para": payoff_idx, "pct": payoff_pct},
            "signoff_paras": signoff_idx, "cta_paras": cta_idx,
        },
        "unknown_anchors": unknown,
        "gates_before": gates_before, "gates_after": gates_after,
        "planned_deletes": planned, "escalate": escalate, "applied": applied,
        "words": {"before": words_before, "after": words_after},
        "payoff_pct": {"before": payoff_pct, "after": payoff_pct_after},
    }
    gate_str = ", ".join(f"{k}={v.get('status')}" for k, v in ref_gates.items())
    log.append(
        f"🏛️ Судья: {score}/90, вердикт {verdict} ({gate_str})"
        + (f", payoff {payoff_pct}%" + (f"→{payoff_pct_after}%" if payoff_pct_after else "")
           if payoff_pct is not None else "")
    )
    return edited_text, report, log


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    lang = "the script's language"
    argv = sys.argv[1:]
    if "--lang" in argv:
        _li = argv.index("--lang")
        lang = argv[_li + 1] if _li + 1 < len(argv) else lang
        argv = argv[:_li] + argv[_li + 2:]  # значение --lang не должно попасть в args
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(2)
    path = args[0]
    mode = "edit" if "--edit" in argv else "report"
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if not tok:
        print("CLAUDE_CODE_OAUTH_TOKEN не задан")
        sys.exit(2)
    body = open(path, encoding="utf-8").read()

    class _Q:  # лог вызова в stdout
        def put(self, m):
            print("  ", m)

    edited, rep, lines = run_judge(body, lang, tok, mode=mode, log_queue=_Q())
    for l in lines:
        print(l)
    if rep:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    if edited and mode == "edit":
        out = re.sub(r"\.txt$", "", path) + ".judged.txt"
        with open(out, "w", encoding="utf-8") as f:
            f.write(edited)
        print(f"→ {out}")
