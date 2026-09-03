# -*- coding: utf-8 -*-
"""gen_v7 — конвейер генерации v7 (2026-07-18), включается env GEN_V7=1.

Отличия от v5/v6-пути:
  1. ВОРОНКА = ход 0 ТОГО ЖЕ разговора: модель пишет по плану, который сама
     спроектировала и помнит (идея владельца: «зачем перекладывать план,
     если у чата есть контекст»).
  2. Части возвращаются СЫРЫМ ТЕКСТОМ между маркерами <<<SCRIPT>>>...<<<END>>>
     (JSON только у воронки) — класс «швов»/экранирования вымирает физически,
     проза не скована JSON-строкой.
  3. Промпты частей компактные (~2К символов): пресет и тема уже в контексте
     хода 0 — стена в 40К символов не повторяется.
  4. План живой: части разрешено мягко адаптировать биты, окна петель — нет.
  5. Внутренний length-recovery ОТКЛЮЧЁН (target_words_per_turn=0): длину держит
     абзацная арифметика (v6) — дожимов-заплаток на этом пути не существует.

Fail-open: любой сбой → server.py откатывается на обычный conversation-путь.
"""

import os
import re
from typing import Dict, List, Optional, Tuple

_SCRIPT_RE = re.compile(r"<<<SCRIPT>>>(.*?)<<<END>>>", re.S)
_OPS_RE = re.compile(r"<<<OPS>>>(.*?)<<<END>>>", re.S)

# ── ревизионный ход (PATCH-2026-07-20-review, гейт GEN_V7_REVIEW=1) ──────────
# Идея владельца: проверяющий — В ТОМ ЖЕ чате, что план и текст. Модель, дописав
# последнюю часть, перечитывает ВСЁ (помня свою воронку) и выдаёт операции-правки.
# Лечит ровно то, что внешний DELETE-only судья не может: дубли МЕЖДУ частями
# (4-way сравнение 20.07: 8-18 дублей на 25-65%) и ранний слив титульной петли
# (payoff-паттерн 58.5→58.7→23.9%) — последний чинится ТОЛЬКО переписыванием.
# Применяет операции детерминированный код с жёсткими лимитами; внешний судья
# остаётся независимым приёмщиком после.

_REV_MAX_DEL = 18          # максимум удаляемых абзацев (4-way: 8-18 дублей)
_REV_MAX_REPL = 3          # максимум переписываемых абзацев
_REV_MAX_CUT_FRAC = 0.15   # суммарно удалять не больше 15% слов (главный ограничитель урона)
# REPL — это payoff-дефер (полный ответ → тизер): почти всегда КОРОЧЕ оригинала,
# поэтому нижней границы длины нет; верхняя есть — дефер не должен РАЗДУВАТЬ (=добавлять).
_REV_REPL_MAX_GROW = 1.2   # replacement не длиннее +20% оригинала


def _tokens_num(s: str):
    """Числа и Имена-с-заглавной-в-СЕРЕДИНЕ-предложения (не первое слово) — маркеры фактов."""
    nums = set(re.findall(r"\d[\d\s.,/–-]*\d|\d", s))
    caps = set()
    for sent in re.split(r"(?<=[.!?…])\s+", s):
        for j, w in enumerate(sent.split()):
            core = w.strip(".,!?;:—–\"'«»„…()")
            if j > 0 and len(core) > 1 and core[:1].isupper() and core[1:].islower():
                caps.add(core.casefold())
    return nums, caps


def _check_prompt(part_no: int, num_chunks: int) -> str:
    """Проверка-заметка сразу после части (interleave). Короткий вывод, НЕ скрипт —
    служит инвентарём покрытия для следующей части и финального OPS-хода."""
    return (
        f"Pause writing. You just finished PART {part_no} of {num_chunks}. Do a fast COVERAGE "
        "CHECK of THIS part only, so the next part does not repeat it. Output exactly this, nothing "
        "else (no <<<SCRIPT>>>, this is a note to yourself):\n"
        f"COVERAGE part {part_no}: <3-5 bullets, each a key fact/scene/image this part delivered, "
        "max 5 words each>\n"
        "REPEAT-RISK: <name any fact/scene here that you already told in an earlier part, or 'none'>\n"
        "TITLE-ANSWER-LEAKED: <yes/no — did this part already FULLY answer the title's promise? "
        "It should stay open until the mapped payoff window at 70-92%>"
    )


# ── полировочный ход (PATCH-2026-07-21-polish, гейт GEN_V7_POLISH=1) ─────────
# Идея владельца: «по-простому — в том же чате последовательно прокачать чанки
# для максимального удержания, объяснив принципы в промпте». Часть пишется →
# СРАЗУ прокачивается тем же чатом (помнит план) → следующая часть рождается
# уже от прокачанной версии (связность сохраняется сама). Полный редрафт может
# то, что не может вырезание: переписать ранний слив титула, усилить хук.
# Guard'ы кодом: длина ±20% от драфта, никаких НОВЫХ чисел/имён вне драфта∪семени
# (анти-фабрикация). Нарушение → остаётся драфт (fail-open per-part).

_POLISH_PRINCIPLES = (
    "RETENTION PRINCIPLES to apply (rewrite, do not just touch up):\n"
    "1. OPEN LOOPS carry the viewer: a question raised and NOT answered keeps them; "
    "kill any paragraph that closes a loop earlier than your map planned. If this part "
    "is BEFORE the mapped payoff window, it must NOT fully answer the title's promise — "
    "tease, escalate, withhold.\n"
    "2. CONCRETE beats abstract: replace every general claim with a number, a name, a "
    "physical scene already present in the brief or this draft. No new facts.\n"
    "3. TEMPO: a NEW element every 100-150 words; cut explainer passages ('стоит "
    "отметить', 'позвольте объяснить', lecture asides) — show the thing instead.\n"
    "4. STAKES up front: within the first two sentences of the part the viewer must "
    "feel what is at risk.\n"
    "5. RHYTHM: vary sentence length; after three long sentences land a short one; "
    "no run of paragraphs opening with the same movement verb.\n"
    "6. Never signal withholding ('об этом позже') and never re-tell what an earlier "
    "part delivered."
)


def _polish_prompt(part_no: int, num_chunks: int, is_last: bool) -> str:
    tail = (
        "Keep the finale structure intact: payoff where mapped, at most 2-3 aftermath "
        "paragraphs, the SINGLE outro with its exact wording preserved."
        if is_last else
        "This part must still END mid-tension (no closing, no outro) so the next part "
        "picks up momentum."
    )
    return (
        f"Now POLISH the part you just wrote (part {part_no} of {num_chunks}) for maximum "
        "retention, as a ruthless editor of your own draft.\n\n" + _POLISH_PRINCIPLES + "\n\n"
        "HARD LIMITS: keep every fact, name, number, date and quote EXACTLY as in the draft "
        "(you may drop weak ones, never invent new); keep length within ±10% of the draft; "
        "keep the part's role in your retention map. " + tail + "\n\n"
        "Output ONLY the full polished part between the same markers:\n"
        "<<<SCRIPT>>>\n...polished part...\n<<<END>>>"
    )


_NUM_RE = re.compile(r"\d[\d\s.,/–-]*\d|\d")


def _facts_of(s: str):
    """Числа + Имена-с-заглавной не в начале предложения — маркеры фактов."""
    nums = set(_NUM_RE.findall(s or ""))
    caps = set()
    for sent in re.split(r"(?<=[.!?…])\s+", s or ""):
        for j, w in enumerate(sent.split()):
            core = w.strip(".,!?;:—–\"'«»„…()")
            if j > 0 and len(core) > 1 and core[:1].isupper() and core[1:].islower():
                caps.add(core.casefold())
    return nums, caps


def _accept_polish(draft: str, polished: str, seed: str, log_queue, part_no: int) -> str:
    """Guard полировки: длина ±20% драфта; новые числа/имена только из драфта∪семени.
    Нарушение → драфт (fail-open per-part, с логом)."""
    if not (polished or "").strip():
        return draft
    dw, pw = len(draft.split()), len(polished.split())
    if not dw or abs(pw - dw) > dw * 0.20:
        if log_queue:
            log_queue.put(f"💅 polish part {part_no}: длина вне допуска ({dw}→{pw} слов) — драфт")
        return draft
    d_nums, d_caps = _facts_of(draft)
    s_nums, s_caps = _facts_of(seed)
    p_nums, p_caps = _facts_of(polished)
    new_facts = (p_nums - d_nums - s_nums) | (p_caps - d_caps - s_caps)
    if new_facts:
        if log_queue:
            log_queue.put(
                f"💅 polish part {part_no}: новые факты {sorted(new_facts)[:3]} — драфт")
        return draft
    if log_queue:
        log_queue.put(f"💅 polish part {part_no}: принята ({dw}→{pw} слов)")
    return polished


def _review_prompt(num_chunks: int) -> str:
    return (
        "Now switch roles: you are a ruthless RETENTION REVIEWER of YOUR OWN script above. "
        "Re-read every part you have written, against YOUR OWN retention map from your first "
        "answer. Find ONLY these defects:\n"
        "A. CROSS-PART REPEATS: a fact, scene, image or near-identical sentence delivered in MORE "
        "than one part (viewer already heard it — the LATER copy must go; a passing half-sentence "
        "callback is fine, a re-telling is not). List every later copy.\n"
        "B. TITLE-LOOP TIMING: your title makes a promise. Locate where the script FULLY answers "
        "it. If the full answer lands before ~65% of the whole script AND the same answer is ALSO "
        "reachable later (or you can move its full delivery into the 70-92% window), REWRITE the "
        "early paragraph into a deferral — a tease/partial reveal, NO new facts, names or numbers, "
        "shorter is fine. CRITICAL: if that early paragraph is the ONLY place the answer is given, "
        "do NOT defer it (you would strip the reveal) — leave it and skip B.\n"
        "C. ENDING: exactly ONE finale/sign-off; CTA only in the very tail. Extra endings must go.\n\n"
        "Output STRICTLY operations between markers, one per line, nothing else:\n"
        "<<<OPS>>>\n"
        "DEL | part <k> | <first 8-12 words of the paragraph to delete, verbatim>\n"
        "REPL | part <k> | <first 8-12 words of the paragraph, verbatim> ||| <full replacement paragraph>\n"
        "<<<END>>>\n"
        f"Hard rules: at most {_REV_MAX_DEL} DEL and {_REV_MAX_REPL} REPL; NEVER touch the opening "
        "hook paragraph, the payoff paragraph inside its window, or the single final outro; when "
        "unsure — KEEP. If nothing needs fixing, output the markers with nothing between them."
    )


def _norm_prefix(s: str) -> str:
    """Нормализация для сопоставления цитаты-якоря с началом абзаца."""
    tr = {"«": '"', "»": '"', "„": '"', "“": '"', "”": '"', "‘": "'", "’": "'",
          "—": "-", "–": "-", "…": "...", " ": " "}
    for a, b in tr.items():
        s = s.replace(a, b)
    return " ".join(s.casefold().split()).strip('"\'' )


def _parse_ops(raw: str) -> List[Dict]:
    """Парсит операции ревизии. Битые строки пропускает."""
    m = _OPS_RE.search(raw or "")
    if not m:
        return []
    ops: List[Dict] = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|", 2)]
        if len(parts) != 3 or parts[0].upper() not in ("DEL", "REPL"):
            continue
        kind = parts[0].upper()
        pm = re.search(r"\d+", parts[1])
        if not pm:
            continue
        part_no = int(pm.group(0))
        rest = parts[2]
        if kind == "REPL":
            if "|||" not in rest:
                continue
            anchor, new_text = rest.split("|||", 1)
            ops.append({"kind": "REPL", "part": part_no,
                        "anchor": anchor.strip(), "new": new_text.strip()})
        else:
            ops.append({"kind": "DEL", "part": part_no, "anchor": rest.strip()})
    return ops


def _apply_ops(chunks: List[str], ops: List[Dict], log_queue) -> Tuple[List[str], Dict]:
    """Применяет DEL/REPL к частям. Якорь = нормализованный префикс абзаца
    (part = подсказка: ищем в указанной части, потом в соседних, потом везде).
    Лимиты: ≤10 DEL, ≤3 REPL, суммарный вырез ≤15% слов, REPL ±60% длины.
    Защита: первый абзац части 1 (хук) и последний абзац последней части (аутро)."""
    paras_by_part = [[p for p in c.split("\n\n") if p.strip()] for c in chunks]
    total_words = sum(len(p.split()) for pp in paras_by_part for p in pp) or 1
    stats = {"del": 0, "repl": 0, "skipped": 0, "words_cut": 0}

    def _find(anchor: str, hint_part: int, prefer_late: bool):
        """→ (part_idx, para_idx) | None. Матч по префиксу нормализованного абзаца.
        AUDIT-фикс: собираем ВСЕ совпадения. Если их несколько (параллельные зачины) —
        сначала пытаемся сузить до hint-части; при неоднозначности для DEL берём ПОЗДНЮЮ
        копию (дубль удаляем, оригинал храним), для REPL — только уникальный матч."""
        an = _norm_prefix(anchor)
        if len(an.split()) < 5:
            return None
        hits = [(pi, qi) for pi, pp in enumerate(paras_by_part)
                for qi, para in enumerate(pp) if _norm_prefix(para).startswith(an)]
        if not hits:
            return None
        if len(hits) == 1:
            return hits[0]
        # неоднозначность: пробуем сузить до подсказанной части
        in_hint = [h for h in hits if h[0] == hint_part - 1]
        if len(in_hint) == 1:
            return in_hint[0]
        if prefer_late:
            return max(hits, key=lambda h: (h[0], h[1]))  # DEL → позднюю копию
        return None  # REPL при неоднозначности — не трогаем (when unsure KEEP)

    # частоты чисел по всему тексту — для orphan-guard (уникальный факт ≠ дубль)
    all_text = "\n\n".join("\n\n".join(pp) for pp in paras_by_part)
    num_freq: Dict[str, int] = {}
    for nm in re.findall(r"\d[\d\s.,/–-]*\d|\d", all_text):
        num_freq[nm] = num_freq.get(nm, 0) + 1

    n_last_part = len(paras_by_part) - 1
    deleted = set()  # (part, para)
    n_del = n_repl = 0
    for op in ops:
        if op["kind"] == "DEL" and n_del >= _REV_MAX_DEL:
            stats["skipped"] += 1
            continue
        if op["kind"] == "REPL" and n_repl >= _REV_MAX_REPL:
            stats["skipped"] += 1
            continue
        hit = _find(op["anchor"], op["part"], prefer_late=(op["kind"] == "DEL"))
        if hit is None:
            stats["skipped"] += 1
            if log_queue:
                log_queue.put(f"🧾 ревизия: якорь не найден/неоднозначен — пропуск ({op['anchor'][:50]}…)")
            continue
        pi, qi = hit
        # защита: первые ДВА абзаца части 1 (титул-карточка + хук) и аутро (последний абзац посл. части)
        if (pi == 0 and qi <= 1) or (pi == n_last_part and qi == len(paras_by_part[pi]) - 1):
            stats["skipped"] += 1
            if log_queue:
                log_queue.put("🧾 ревизия: операция на хук/аутро отклонена")
            continue
        if op["kind"] == "DEL":
            para = paras_by_part[pi][qi]
            # orphan-guard: не удалять абзац, несущий УНИКАЛЬНОЕ во всём тексте число
            # (это не дубль, а единственный носитель факта — panel #5).
            para_nums = set(re.findall(r"\d[\d\s.,/–-]*\d|\d", para))
            if any(num_freq.get(nm, 0) <= 1 for nm in para_nums):
                stats["skipped"] += 1
                if log_queue:
                    log_queue.put("🧾 ревизия: DEL отклонён — абзац несёт уникальное число (не дубль)")
                continue
            w = len(para.split())
            if stats["words_cut"] + w > total_words * _REV_MAX_CUT_FRAC:
                stats["skipped"] += 1
                continue
            deleted.add((pi, qi))
            stats["words_cut"] += w
            n_del += 1
        else:
            old = paras_by_part[pi][qi]
            old_w, new_w = len(old.split()), len(op["new"].split())
            # дефер короче — ок; длиннее +20% — нет (значит ДОБАВЛЯЕТ, а не откладывает)
            if not op["new"] or new_w > old_w * _REV_REPL_MAX_GROW or new_w < 3:
                stats["skipped"] += 1
                if log_queue:
                    log_queue.put(f"🧾 ревизия: REPL вне допуска длины ({old_w}→{new_w} слов) — пропуск")
                continue
            # content-guard: дефер НЕ вводит новых чисел/имён (panel #3 — анти-фабрикация)
            o_nums, o_caps = _tokens_num(old)
            n_nums, n_caps = _tokens_num(op["new"])
            new_facts = (n_nums - o_nums) | (n_caps - o_caps)
            if new_facts:
                stats["skipped"] += 1
                if log_queue:
                    log_queue.put(f"🧾 ревизия: REPL вводит новые факты {list(new_facts)[:3]} — пропуск")
                continue
            paras_by_part[pi][qi] = op["new"]
            n_repl += 1
    stats["del"], stats["repl"] = n_del, n_repl
    out = ["\n\n".join(p for i, p in enumerate(pp) if (pi, i) not in deleted)
           for pi, pp in enumerate(paras_by_part)]
    return out, stats

INTEREST_RULES = (
    "INTEREST RULES — apply to EVERY paragraph unless the CHANNEL PRESET demands otherwise "
    "(the preset wins):\n"
    "1. Every paragraph adds at least one NEW element (fact, name, object, scene turn, sensory "
    "period detail). Never re-state what was already told.\n"
    "2. SHOW, THEN CUT: one physical detail carries the emotion, then cut to the next fact — "
    "zero commentary after a strong scene, no telling the viewer what to feel.\n"
    "3. LOOP DISCIPLINE: plant a loop once and go silent until its payoff; no 'remember this / "
    "we will return to it'. Point forward with fate-prolepsis at first introductions.\n"
    "4. RE-HOOKS are structural (chapter-title promise, irony-forward, trivia spike), not "
    "rhetorical questions; questions to the viewer only inside the first 150 words.\n"
    "5. RHYTHM: default sentence 12-20 words with a turn inside; fragments <=5 words at most "
    "15% and only at peaks; aphorism-enders at most one per 3 paragraphs.\n"
    "6. UNCERTAINTY = INTRIGUE: one eerie line, never a seminar.\n"
    "7. NARRATOR VOICE: impersonal third-person documentary narrator. Never address the "
    "viewer ('you', 'imagine', Q&A games) outside the first 150 words; 'I/you' may live ONLY "
    "inside quoted documents (diaries, letters, testimony). No tour-guide transitions "
    "('let's return', 'let's take a look', 'давайте вернёмся', 'Vraťme se') in ANY language — "
    "a transition is a cut: new time, new place or new person in the first sentence.\n"
    "8. FORWARD ANNOUNCEMENTS ('we will get to that', 'more on that later', 'об этом позже', "
    "'k tomu se dostaneme', 'ale to później') are BANNED — one single use per whole film is "
    "the absolute maximum. Hold the viewer with a NEW concrete question, clue or stake — "
    "never with a promise of future content."
)

OUTPUT_FORMAT = (
    "OUTPUT FORMAT — STRICT: output the part as PLAIN TEXT between two marker lines:\n"
    "<<<SCRIPT>>>\n"
    "...the part text: paragraphs separated by blank lines...\n"
    "<<<END>>>\n"
    "Nothing before <<<SCRIPT>>> and nothing after <<<END>>>. No JSON, no markdown, no headings, "
    "no comments. TTS-ready prose: numbers, dates, currencies and abbreviations written out as "
    "WORDS in the target language; no brackets or stage directions."
)


def _length_line(target_words: int) -> str:
    """Длина через абзацы, без JSON-терминологии (v7 = сырой текст)."""
    paras = max(4, round(target_words / 105))
    return (
        f"LENGTH — write EXACTLY {paras} paragraphs, each 90-120 words (4-6 spoken sentences); a "
        f"paragraph under 60 words is a fragment — merge or grow it. Count PARAGRAPHS, not words — "
        f"{paras} paragraphs land on the target automatically. SELF-CHECK before you answer: count the "
        f"blank-line-separated paragraphs; fewer than {paras} means UNFINISHED — add full new paragraphs "
        f"of real material (a scene, a fact, a turn that belongs here) until exactly {paras}. No filler."
    )


def _extract_script(raw: str) -> str:
    """Текст части из ответа: маркеры → фолбэк на разумную зачистку."""
    if not raw:
        return ""
    m = _SCRIPT_RE.search(raw)
    if m:
        return m.group(1).strip()
    t = raw.strip()
    # фолбэк: маркеры потеряны/побиты — снять одиночные маркеры и очевидную обвязку
    t = re.sub(r"<<<\s*/?\s*(SCRIPT|END)\s*>>>", "", t).strip()
    if t.startswith("{") and '"script"' in t[:200]:  # модель вернула старый JSON — вытащим
        try:
            from rewrite_core import parse_chunk_response
            p = parse_chunk_response(t, expect_funnel=False)
            if p and p.get("script"):
                return p["script"].strip()
        except Exception:
            pass
    return t


# ── STORY-режим (PATCH-2026-07-22-story, гейт GEN_V7_STORY=1) ────────────────
# Диагноз по чтению P_1 и промптов: оба убийцы удержания — артефакты НАШИХ промптов.
# (1) Квоты «ровно N абзацев» + фактор 1.35 заказывают воду: материал части кончился,
#     квота нет → модель пережёвывает («медленность Po-2» ×6 в одном тексте).
# (2) Язык конструктора («Execute part K: its beats… retention map… re-hooks») протекает
#     в текст экскурсоводом: «Přeskočme… Vraťme se…» ×15 — это НАШИ ре-хуки буквально.
# (3) Двойное планирование (семя-план → воронка-план с бюджетами → квоты) не оставляет
#     модели роли рассказчика. Хук всегда 9/10 — там промпт сенсорный. Значит не LLM.
# Решение: заказывать ИСТОРИЮ, не форму. Повтор = главный грех; длина — материалом.

_STORY_STOPLIST = (
    "NEVER open a paragraph by walking the viewer somewhere: no \"Přeskočme\", \"Vraťme se\", "
    "\"Podívejme se\", \"Přesuňme se\", \"Zastavme se\", \"Sledujme\", \"Zůstaňme u\", no "
    "\"давайте посмотрим / вернёмся / а теперь\", no \"let's move on / let's return\" — in ANY "
    "language. A transition is a cut: new time, new place, or new person in the first sentence. "
    "The viewer is never addressed as someone being guided through a lecture."
)

_STORY_RULES = (
    "STORY RULES:\n"
    "1. Every fact, scene, image and explanation is told EXACTLY ONCE in the whole film. "
    "Before writing a paragraph ask: did I already tell this in any earlier words? If yes — "
    "cut it; one half-sentence callback is the maximum echo allowed.\n"
    "2. Concrete beats abstract: a number, a name, a physical detail — then cut to the next "
    "thing. No lecturing, no 'стоит отметить'-style asides.\n"
    "3. If you need more length, pull UNUSED material from the brief (its fact-reserve exists "
    "for that). Running out of material and stopping earlier is honest; repeating is failure.\n"
    "4. Vary sentence length; after a run of long sentences land a short one.\n"
    "5. HOOKS ARE STAKES, NOT ANNOUNCEMENTS. You keep the viewer by raising what is at risk "
    "and dropping NEW clues — never by promising future answers. Phrases like 'k tomu se "
    "dostaneme', 'ale to později', 'ještě ne teď', 'об этом позже', 'we will get to that' are "
    "BANNED (one single use per whole film is the absolute maximum). If you feel the urge to "
    "announce — add a new fact instead.\n"
    "6. A refrain (recurring image) may return at most 3 times in the whole film, each time "
    "carrying NEW information; otherwise it is repetition.\n"
    "7. " + _STORY_STOPLIST
)


def _story_funnel_prompt(preset_content: str, topic: str, language: str,
                         target_words: int, host_name: str, channel_name: str):
    """Воронка story-режима: драматургия без пословных бюджетов. Возвращает
    (system_prompt, turn0_user). JSON-ключи совместимы с parse_funnel_response."""
    system = (
        "You are a master storyteller writing narration scripts for a faceless YouTube "
        "channel. You never pad, never repeat a told fact, never guide the viewer around "
        "like a tour guide — you tell a story that is impossible to stop watching. "
        "Apply the channel preset strictly. When asked for JSON, output ONLY valid JSON, "
        "first character '{'."
    )
    ident = ""
    if channel_name or host_name:
        ident = f"\nChannel: {channel_name or '—'}. Host: {host_name or '—'}."
    user = (
        f"CHANNEL PRESET:\n{preset_content}\n\n"
        f"BRIEF (facts, scenes, quotes, bans):\n{topic}\n{ident}\n\n"
        "How to treat the brief: it is your CHEST OF FACTS and hard limits — verbatim "
        "quotes and every ZAKAZY/ban are sacred. It is NOT the outline of the film. "
        "The storytelling is YOURS: choose the order, drop weak material, keep the best. "
        "The best seventy percent of the material told once beats all of it told twice.\n\n"
        f"The finished narration must be a FULL-LENGTH film of about {target_words} words in "
        f"{language} — this is a floor, not a suggestion. Reach it with MATERIAL and DEPTH "
        "(more scenes, more sensory detail, more of the brief's facts), never with repetition "
        "or padding. A story that stops far short of length has left the brief's material "
        "unused — go back into the chest of facts.\n\n"
        "First, design the story. Output ONLY this JSON:\n"
        "{\"preview\": \"1-5 words for the thumbnail\",\n"
        " \"title\": \"click title, max 60 chars, a promise the film answers only near the end\",\n"
        " \"hook\": \"1-2 sentences: the opening image that makes leaving impossible\",\n"
        " \"plan\": [\"3-5 dramatic turns of the story, one line each — turns, not chapters; "
        "the last one is where the title's promise is finally paid off\"]}"
    )
    return system, user


def run_v7(
    preset_content: str,
    topic: str,
    language: str,
    target_words: int,
    num_chunks: int,
    words_per_chunk: int,
    model: str,
    oauth_token: str,
    log_queue,
    host_name: str = "",
    channel_name: str = "",
    task_info: str = "gen-v7",
) -> Tuple[bool, List[str], str, str, Optional[str]]:
    """Возвращает (ok, chunks, preview, title, err)."""
    from rewrite_core import (
        build_funnel_prompt, parse_funnel_response,
        call_claude_conversation,
    )

    story_on = os.environ.get("GEN_V7_STORY", "0") == "1"
    if story_on:
        system_prompt, _turn0 = _story_funnel_prompt(
            preset_content, topic, language, target_words, host_name, channel_name)
        turns: List[str] = [_turn0]
    else:
        funnel_msgs = build_funnel_prompt(
            preset_content=preset_content, topic=topic, language=language,
            target_words=target_words, num_chunks=num_chunks, words_per_chunk=words_per_chunk,
            host_name=host_name, channel_name=channel_name,
        )
        system_prompt = funnel_msgs[0]["content"]
        turns = [funnel_msgs[-1]["content"]]

    if channel_name or host_name:
        _outro = "ONE short outro that NAMES the host and the channel"
    else:
        _outro = ("ONE short neutral outro in the narrator's voice; do NOT invent, mention or "
                  "imply any channel name, host name, brand or narrator identity")

    # PATCH-2026-07-21-interleave: проверка ПОСЛЕ КАЖДОЙ части (в том же чате).
    # Гипотеза A/B: перепроверка по чанкам ловит дубли/ранний payoff в момент рождения,
    # плотнее внимания, чем один пост-хок ход по 8500 словам.
    interleave_on = os.environ.get("GEN_V7_INTERLEAVE", "0") == "1"
    # PATCH-2026-07-21-polish: «прокачай каждый чанк в том же чате». Полный редрафт
    # вытесняет interleave-заметки (редрафт сильнее заметки).
    polish_on = os.environ.get("GEN_V7_POLISH", "0") == "1"
    if polish_on:
        interleave_on = False
    if story_on:
        # story-режим самодостаточен: без заметок и полировки (простота — вся суть)
        interleave_on = polish_on = False

    if story_on:
        # STORY-части: каждой части ЯВНАЯ РОЛЬ в дуге удержания.
        # 1-я = хук; средние = крючки к развязке (НЕ раскрывать); последняя = развязка.
        # Запрет раскрытия развязки повторён В КАЖДОМ промпте до финала (идея владельца).
        for k in range(num_chunks):
            is_last = (k == num_chunks - 1)
            first = (k == 0)
            p: List[str] = []
            # общий контракт «не пролей развязку» — во ВСЕ части кроме последней
            _no_spoil = (
                "THE PAYOFF — the full answer to the title's promise — belongs ONLY to the "
                "final stretch. In THIS part you may circle it, deepen the question, drop "
                "clues and raise the stakes around it, but you must NOT reveal or explain it "
                "yet. Every part before the end makes the viewer need the answer MORE, never "
                "gives it."
            )
            if first:
                p.append(
                    "PART 1 — THE HOOK. Open with YOUR hook from the design: a person, in a "
                    "place, at a moment — the first concrete scene inside the first 250 words. "
                    "One sentence of title promise at most; no host introduction longer than "
                    "one sentence, no theory before the first scene. Plant the central question "
                    "the whole film will answer, and leave it wide open."
                )
                p.append(_no_spoil)
                p.append(_STORY_RULES)
            elif not is_last:
                p.append(
                    f"PART {k + 1} — KEEP THEM WATCHING. Continue the story exactly from where "
                    "you stopped: next scenes, next facts, forward motion only. Your job here "
                    "is RETENTION — every few paragraphs open a small new loop or raise the "
                    "stakes so leaving feels impossible. Everything already told is TOLD: "
                    "never re-tell or re-explain it (one half-sentence callback maximum)."
                )
                p.append(_no_spoil)
                p.append("STORY RULES from before stay in force: every fact once; transitions "
                         "are cuts, not tour-guide invitations.")
            else:
                p.append(
                    "FINAL PART — THE PAYOFF. NOW deliver the full answer to the title's "
                    "promise — the thing every earlier part has been circling. Land it late in "
                    "this stretch, where it hits hardest, as the emotional peak of the whole "
                    "film. After the payoff: at most 2-3 aftermath paragraphs (fates, the "
                    "today-anchor), then " + _outro + ", and STOP. Nothing after the outro; no "
                    "second endings; no recap."
                )
            if words_per_chunk:
                # СЧЁТЧИК ДЛИНЫ = АБЗАЦЫ (S1-S5 доказали: слова модель считать не умеет —
                # просили строго 1944+, давала 958-1358; абзацная арифметика старого
                # пайплайна стабильно давала 8100-8500). Квота + анти-повтор вместе:
                # каждый абзац обязан быть НОВЫМ материалом — иначе замена из резерва.
                _paras = max(4, round(words_per_chunk / 105))
                p.append(
                    f"LENGTH — count PARAGRAPHS, not words: this stretch is EXACTLY {_paras} "
                    "paragraphs, each 90-120 words (4-6 spoken sentences). SELF-CHECK before "
                    f"answering: count the blank-line-separated paragraphs — fewer than {_paras} "
                    "means UNFINISHED. But every added paragraph must bring a NEW scene, fact "
                    "or sensory moment from the brief (its fact-reserve exists for this). A "
                    "paragraph that re-tells or re-phrases anything already told is a FAILED "
                    "paragraph — replace it with fresh brief material, never count it."
                )
            if not is_last:
                p.append("Do NOT write any closing, outro or sign-off in this stretch.")
            p.append(OUTPUT_FORMAT)
            turns.append("\n\n".join(p))

    for k in ([] if story_on else range(num_chunks)):
        is_last = (k == num_chunks - 1)
        p: List[str] = [
            f"Now write PART {k + 1} of {num_chunks} of the script — following YOUR OWN retention "
            f"map from your first answer. Execute part {k + 1}: its beats, its NEW elements, its "
            f"loop ops (OPEN = plant once then silence; CLOSE = full payoff where mapped; "
            f"keep-open loops stay open past this part). Re-hooks as one-line structural turns."
        ]
        if interleave_on and k > 0:
            p.insert(0,
                "FIRST re-read your COVERAGE CHECK notes above. Do NOT re-tell any fact, scene or "
                "image you already listed as delivered. If a check flagged the title answer as "
                "already leaked, this part must NOT resolve the title either — keep it open.")
        p.append(
            "LIVE MAP: if the story has organically moved ahead of your map, adapt the remaining "
            "beats silently — but every loop's close window and the SINGLE finale are fixed."
        )
        if k == 0:
            p.append(
                "Open with YOUR hook from the map, then one sentence of title-card promise, then "
                "the FIRST concrete scene within the first 250 words — a person, in a place, at a "
                "moment. No host self-introduction longer than one sentence."
            )
            p.append(
                "SPINE DISCIPLINE: your video has ONE central paradox/irony. Voice it in full "
                "exactly ONCE, near the title payoff. Do NOT keep restating the same idea in new "
                "words across the parts — that is padding and kills retention. Every paragraph "
                "ADVANCES the story; a truth stated once is never returned to."
            )
            p.append(INTEREST_RULES)
        else:
            p.append(
                "INTEREST RULES from part 1 stay in force: every paragraph adds NEW information — "
                "never re-tell anything already written (one passing clause maximum); one physical "
                "detail then cut; no 'remember this'; questions to the viewer are over; sentences "
                "12-20 words with the turn inside; uncertainty gets one line, not a seminar."
            )
            p.append(
                "If your map closes the TITLE loop (L1) in THIS part: the payoff redeems the "
                "title literally (same entities), and the SAME part immediately opens the "
                "follow-up arc from the map — a payoff followed by a plateau is a flop pattern."
            )
        if words_per_chunk:
            p.append(_length_line(words_per_chunk))
        if is_last:
            p.append(
                "FINALE DISCIPLINE: close the LAST open arc exactly where your map places it. "
                "If the map put the TITLE payoff in this part, it lands NO LATER than the 70% "
                "mark of the WHOLE video and redeems the title literally — the promised "
                "revenge/secret/number is NAMED with the same entities. After the final close: AT MOST 2-3 "
                "short aftermath paragraphs (fates, consequences, the anchor-today) — then "
                + _outro + ", and STOP. FORBIDDEN after the payoff: a long epilogue, a second or "
                "third 'real ending', a cluster of questions to the viewer, recap, re-explaining, "
                "returning to earlier scenes, restating the SPINE. SELF-CHECK before closing: "
                "every verbatim quote and anchor fact from the brief voiced exactly once — a "
                "missing one goes into the aftermath BEFORE the outro."
            )
        else:
            p.append("Do NOT write any closing, outro or sign-off in this part.")
        p.append(OUTPUT_FORMAT)
        turns.append("\n\n".join(p))

        # проверка-заметка после части k (кроме последней — за ней идёт финальная ревизия)
        if interleave_on and not is_last:
            turns.append(_check_prompt(k + 1, num_chunks))
        # полировочный ход: прокачать часть СРАЗУ, следующая пишется от прокачанной
        if polish_on:
            turns.append(_polish_prompt(k + 1, num_chunks, is_last))

    # PATCH-2026-07-20-review: проверяющий — В ТОМ ЖЕ чате, что план и текст.
    # В interleave-режиме финальный OPS-ход опирается на явные COVERAGE-списки всех частей.
    review_on = os.environ.get("GEN_V7_REVIEW", "0") == "1" or interleave_on
    if review_on:
        turns.append(_review_prompt(num_chunks))

    ok, raws, err = call_claude_conversation(
        turns, system_prompt, model, oauth_token, log_queue,
        task_info=task_info, target_words_per_turn=0,  # v7: без внутреннего дожима
    )
    if not raws:
        return False, [], "", "", err or "v7: пустой ответ разговора"

    fp = parse_funnel_response(raws[0]) or {}
    preview = (fp.get("preview") or "").strip()
    title = (fp.get("title") or "").strip()
    if log_queue:
        log_queue.put(
            f"🧭 v7: воронка-в-чате ok (title={title[:40]!r}, план={len(fp.get('plan') or [])} ч.), "
            f"частей получено {max(0, len(raws) - 1)}/{num_chunks}"
        )

    # AUDIT-фикс #6: НЕ полагаемся на позицию. Ходы-части = те, где есть <<<SCRIPT>>>;
    # ход-ревизия = тот, где <<<OPS>>> без <<<SCRIPT>>>. Если OPS-ход уехал в позицию
    # части (SDK смёржил пустой turn) — он не попадёт в chunks и не даст «DEL |…» в прозе.
    script_raws, ops_raw = [], ""
    for raw in raws[1:]:  # raws[0] = воронка
        has_script = bool(_SCRIPT_RE.search(raw))
        has_ops = bool(_OPS_RE.search(raw))
        if has_ops and not has_script:
            ops_raw = raw            # ход-ревизия
            continue
        if (interleave_on or polish_on) and not has_script:
            continue                 # служебный ход без SCRIPT — не часть
        script_raws.append(raw)

    if polish_on:
        # SCRIPT-ходы идут парами: драфт, полировка. Guard решает, что принять;
        # обрыв на полировке части j → для j остаётся драфт (fail-open).
        part_raws = []
        i = 0
        while i < len(script_raws) and len(part_raws) < num_chunks:
            draft_raw = script_raws[i]
            pol_raw = script_raws[i + 1] if i + 1 < len(script_raws) else ""
            d_txt = _extract_script(draft_raw)
            p_txt = _extract_script(pol_raw) if pol_raw else ""
            part_raws.append(
                "<<<SCRIPT>>>\n"
                + _accept_polish(d_txt, p_txt, topic, log_queue, len(part_raws) + 1)
                + "\n<<<END>>>")
            i += 2
    else:
        part_raws = script_raws[:num_chunks]

    chunks: List[str] = []
    for i, raw in enumerate(part_raws, 1):
        text = _extract_script(raw)
        if not text.strip():
            return False, [], preview, title, f"v7: часть {i} пуста после извлечения"
        chunks.append(text)
        if log_queue:
            log_queue.put(f"✅ v7 part {i}/{num_chunks}: {len(text.split())} слов (raw-текст, без JSON)")

    if len(chunks) < num_chunks:
        return False, chunks, preview, title, f"v7: частей {len(chunks)} < {num_chunks}"

    # PATCH-2026-07-20-review: применяем операции ревизии (если ход дошёл). Fail-open:
    # разговор оборвался на ревизии / маркеры не найдены / все операции битые → текст как есть.
    if review_on:
        if not ops_raw:
            if log_queue:
                log_queue.put("🧾 ревизия: ход не завершился — текст без ревизии")
        else:
            ops = _parse_ops(ops_raw)
            if not ops:
                if log_queue:
                    log_queue.put("🧾 ревизия: правок нет (чисто) или маркеры не распарсились")
            else:
                words_before = sum(len(c.split()) for c in chunks)
                chunks, st = _apply_ops(chunks, ops, log_queue)
                if log_queue:
                    log_queue.put(
                        f"🧾 ревизия-в-чате: DEL={st['del']} (−{st['words_cut']} слов), "
                        f"REPL={st['repl']}, пропущено={st['skipped']}; "
                        f"{words_before}→{sum(len(c.split()) for c in chunks)} слов"
                    )
    return True, chunks, preview, title, None
