# -*- coding: utf-8 -*-
"""gen_output_cleaner — детерминированная чистка выхода генерации (v5, 2026-07-18).

Контракт (server.py FIX-2026-07-10-cleaner уже вызывает именно так, fail-open):
    from gen_output_cleaner import clean_generated_text
    final_script, report_lines = clean_generated_text(raw_script)

Чистит артефакты СБОРКИ (безопасные автоправки) и репортит остальное флагами:
  АВТО: JSON-швы склейки чанков · дословные дубль-абзацы (re-emit recovery/salvage)
        · лишние sign-off (второй/третий финал) · CTA в корпусе (5–85%)
        · разорванные буквицы («Î ntrebarea» → «Întrebarea»)
  ФЛАГ: выдуманные бренды/дикторы · самоисправления модели · утечки меток пресета
        · WARN-метрики интереса (форвард-флаги, объяснитель, стаккато, вопросы)

CLI для локальной проверки:
    python3 gen_output_cleaner.py file.txt            # отчёт, без записи
    python3 gen_output_cleaner.py --fix file.txt      # + file.clean.txt рядом
"""

import difflib
import re
import sys
from typing import Dict, List, Tuple

# англ-мета эхо recovery-промпта, протёкшее в текст (переносим из прод-версии 2026-07-10)
_META_LINE = re.compile(
    r"(Let me\b|I need to\b|I'?ll\b|I will\b|carefully craft|the script left off|"
    r"picking up (exactly )?where|write this chunk|continue the (same )?part)", re.I,
)


def _ascii_ratio(s: str) -> float:
    return sum(1 for ch in s if ord(ch) < 128) / len(s) if s else 0.0

# ── язык ──────────────────────────────────────────────────────────────────────

_LANG_MARKERS = {
    "pl": ("ą", "ę", "ł", "ż", "ź", "ś", " się ", " oraz ", " który "),
    "cs": ("ř", "ě", "ů", " byl ", " jsem ", " které ", " však "),
    "ro": ("ă", "ș", "ț", " și ", " care ", " pentru ", " după "),
    # сервис мультинишевый: русскоязычные пресеты (история/финансы/космос и т.п.)
    "ru": (" и ", " что ", " это ", "ый ", "го ", " не ", " был"),
}


def _detect_lang(text: str) -> str:
    sample = text[:20000].lower()
    scores = {}
    for lang, markers in _LANG_MARKERS.items():
        scores[lang] = sum(sample.count(m) for m in markers)
    best = max(scores, key=scores.get)
    return best if scores[best] > 10 else "other"


# ── per-language словари ─────────────────────────────────────────────────────

# СТРОГИЕ маркеры sign-off: только формулы прощания/подписи диктора,
# не нарративные обороты («to był…» в рассказе — НЕ финал).
_SIGNOFF = {
    "pl": r"(dziękuję (za|że)|dzięki za (obejrzenie|uwagę|oglądanie)|subskryb|do (zobaczenia|usłyszenia)|przy mikrofonie|słuchaliście|oglądaliście)",
    "cs": r"(děkuj[iu] (za|že)|díky za (pozornost|sledování|zhlédnutí)|na (shledanou|slyšenou)|u mikrofonu (byl|je)|díval[ai]? jste se|sledovali jste)",
    "ro": r"(mulțumesc (că|pentru)|mulţumesc (că|pentru)|ne (vedem|auzim) (la|în|data)|pe curând|la microfon a fost|ați (urmărit|ascultat)|acesta a fost .{0,40}(episod|dosar|canal))",
    "ru": r"(спасибо (за просмотр|что (посмотрели|были))|до (встречи|скорого)|увидимся в следующ|с вами был)",
    "other": r"(thank you for watching|thanks for watching|see you (next|in the next))",
}

_CTA = {
    "pl": r"(subskryb|zasubskryb|łapk\w+ w gór|dzwonecz?ek|polub|zostaw komentarz|kciuk)",
    "cs": r"(odběr|odebír|zvoneč|lajk|palec nahor|zanech(te)? koment)",
    "ro": r"(abona(ți|re|t-vă|ti-va)?-?\w*|clopoțel|clopotel|apăsa?ți like|da(ți|ti) like|un like)",
    "ru": r"(подпиш|подписыва|колокольчик|поставь(те)? лайк|жми(те)? лайк|оставь(те)? коммент)",
    "other": r"(subscribe|hit the bell|thumbs up|leave a comment)",
}

_FWD_FLAGS = {
    "pl": r"(zapamiętaj|zapamietaj|wrócimy do|wrocimy do|o tym później|o tym pozniej|ta (informacja|nazwa|liczba|data) (jeszcze|będzie|bedzie)|zapiszcie)",
    "cs": r"(zapamatujte si|vrátíme se|vratime se|o tom bude řeč|o tom později|o tom pozdeji|to si (ještě|jeste) (zapamatujte|připomeneme)|tohle (jméno|číslo|datum) si)",
    "ro": r"(țineți minte|tineti minte|vom reveni|vom vorbi (mai târziu|mai tarziu)|rețineți (acest|această)|retineti (acest|aceasta))",
    "ru": r"(запомните (это|эту)|мы (ещё |еще )?вернёмся|мы (ещё |еще )?вернемся|об этом (позже|чуть позже)|эта деталь ещё)",
    "other": r"(remember this|we will come back|more on that later)",
}

_EXPLAIN = {
    "pl": r"(zatrzymajmy się|zatrzymajmy sie|pomyślmy|pomyslmy|wyobraź(cie|my)? sobie|wyobraz(cie|my)? sobie|zastanówmy się|zastanowmy sie|mówi więcej niż|mowi wiecej niz|nie potrzebuje komentarza)",
    "cs": r"(zastavme se|představte si|predstavte si|uvědomme si|uvedomme si|pomysleme|zamysleme se|říká (víc|vice) než|rika (vic|vice) nez|nepotřebuje komentář)",
    "ro": r"(să ne oprim|sa ne oprim|imaginați-vă|imaginati-va|imaginează-ți|imagineaza-ti|gândiți-vă|ganditi-va|spune mai mult decât|nu (e|este) nevoie de (niciun )?cuv)",
    "ru": r"(остановимся на (этом|минуту)|задумайтесь|вдумайтесь|представьте( себе)?|только вдумайтесь|говорит больше любых)",
    "other": r"(let us pause|imagine this|think about|says more than any)",
}

_META_LEAK = (
    r"(popraw(my|my to)?\b|să corectăm|sa corectam|oprav(me|ím)\b|let me |i'll |i will "
    r"|micro-?bucl\w*|mikro-?smyčk\w*|mikro-?pętl\w*|mikro-?petl\w*"
    r"|fakt-?budž|fakt-?budz|rezerv[aă] fakt|zakazy\b|interdicți|interdicti)"
)

# регистр ЗНАЧИМ (без IGNORECASE): имя бренда/диктора — с заглавной
_BRAND = (
    r"((?:[Kk]anał\w*|[Kk]anál\w*|[Kk]anal\w*|[Cc]analul)\s+[„\"']?[A-ZĄĆĘŁŃÓŚŹŻČĎŇŘŠŤŮŽĂÂÎȘȚ][\w-]+"
    r"|(?:[Uu] mikrofonu|[Pp]rzy mikrofonie|[Ll]a microfon)\s+(?:byl |był |a fost )?[A-ZĄĆĘŁŃÓŚŹŻČĎŇŘŠŤŮŽĂÂÎȘȚ][\w-]+)"
)

# заглавные с диакритикой — почти наверняка оторванная буквица.
# Обычные заглавные (A/V/I/O/U/S/Z/K/W…) НЕ трогаем: это реальные предлоги/союзы
# в PL/CS («A tak…», «V lednu…») — ложные склейки хуже самого дефекта.
_DROPCAP_DIA = "ÎȘȚĂÂŁŚŻŹĆČŘŠŽĚŮŇĎŤ"


# ── шаги чистки ──────────────────────────────────────────────────────────────

def _scrub_json_seams(text: str) -> Tuple[str, int]:
    n = 0
    # склейка объектов: ..."}{"script": "... / ..."]}{"script"... / ..."]{"script"...
    # (шов через фигурную И/ИЛИ квадратную скобку log_update, с пробелами/переносами)
    text, k = re.subn(r'"\s*[\]\}]*\s*,?\s*\{\s*"script"\s*:\s*"', "\n\n", text)
    n += k
    # огрызки ключей в теле: "log_update": [...] и голые '"}' на конце строки/файла
    text, k = re.subn(r',?\s*"log_update"\s*:\s*\[[^\]]*\]\s*\}?', "", text)
    n += k
    text, k = re.subn(r'"\s*\}\s*$', "", text.rstrip())
    n += k
    # хвост утёкшего массива log_update: ...«конец фразы."]» или «."]}"» в самом конце
    text, k = re.subn(r'"\s*\]\s*\}?\s*$', "", text.rstrip())
    n += k
    # литеральные \n и \" пережившие unescape
    if "\\n" in text:
        text, k = re.subn(r"\\n", "\n", text)
        n += k
    text, k = re.subn(r'\\(["\'])', r"\1", text)
    n += k
    return text, n


def _fix_dropcaps(text: str) -> Tuple[str, int]:
    """«Î ntrebarea» в начале абзаца/предложения → «Întrebarea».
    Склеиваем ТОЛЬКО диакритические заглавные — они не бывают самостоятельным словом."""
    n = 0

    def _repl(m):
        nonlocal n
        n += 1
        return m.group(1) + m.group(2)

    # позиция не важна: одиночная диакритическая заглавная перед строчным словом
    # не бывает легитимной в PL/RO/CS — сшиваем везде
    out = re.sub(
        r"\b([" + _DROPCAP_DIA + r"]) ([a-ząćęłńóśźżáäčďéěíňóřšťúůýžăâîșț]{2,})",
        _repl,
        text,
    )
    return out, n


def _strip_meta_lines(text: str) -> Tuple[str, int]:
    """Англ. мета-строки модели («Let me carefully craft…»): строка почти чисто ASCII
    (>95%) со служебным маркером — в PL/RO/CS-прозе это мусор. Перенос из прод-версии 07-10."""
    kept, n = [], 0
    for line in text.split("\n"):
        s = line.strip()
        if s and _META_LINE.search(s) and _ascii_ratio(s) > 0.95:
            n += 1
            continue
        kept.append(line)
    return "\n".join(kept), n


def _dedup_paragraphs(text: str) -> Tuple[str, int, int]:
    """Дубль-абзацы re-emit (recovery/salvage), ≥25 слов:
      • дословные (нормализованный ключ) — по всему тексту;
      • near-dup (SequenceMatcher ≥0.92) — ТОЛЬКО в хвосте >60% и среди соседних 30
        абзацев (ловит переформулированный второй финал).
    Раннюю копию выбрасываем, позднюю оставляем (финал закрывает текст). Без глобального
    отката >20% (из-за него прод-версия оставляла грязным Orzeł с 57% дублей)."""
    paras = text.split("\n\n")

    def _norm(p):
        return " ".join(w.lower().strip(".,!?;:—–\"'«»„…") for w in p.split())

    big = [(i, _norm(p), len(p.split())) for i, p in enumerate(paras) if len(p.split()) >= 25]
    last_of = {}
    for i, key, _w in big:
        last_of[key] = i  # последний индекс дословной копии
    drop = set()
    rw = 0
    for i, key, w in big:
        if last_of.get(key, i) > i:  # есть более поздняя дословная копия — раннюю режем
            drop.add(i)
            rw += w
    # near-dup ПО ВСЕМУ тексту (судья 47cc8c24: 5 почти-дословных дублей на 32-80%,
    # точный ключ их пропускал из-за мелких вариаций). Абзацы ≥40 слов, ratio ≥0.90.
    # Политика: режем ПОЗДНЮЮ копию (первое изложение каноном); если поздняя копия —
    # последний абзац текста (финал) — режем раннюю.
    nd = [(i, key, w) for (i, key, w) in big if w >= 40 and i not in drop]
    last_idx = len(paras) - 1
    for a in range(len(nd)):
        i, ki, wi = nd[a]
        if i in drop:
            continue
        for b in range(a + 1, len(nd)):
            j, kj, wj = nd[b]
            if j in drop or abs(wi - wj) > max(wi, wj) * 0.5:
                continue
            if difflib.SequenceMatcher(None, ki, kj).ratio() >= 0.90:
                if j == last_idx:
                    drop.add(i)
                    rw += wi
                    break
                drop.add(j)
                rw += wj
    kept = [p for idx, p in enumerate(paras) if idx not in drop]
    return "\n\n".join(kept), len(drop), rw


def _dedup_sentence_runs(text: str) -> Tuple[str, int]:
    """Дубли, переупакованные в ДРУГУЮ разбивку абзацев (волна 5, Cień: блок повторён
    с иными границами абзацев — абзацный дедуп слеп). Ловим на уровне предложений:
    предложение ≥12 слов, почти совпадающее (ratio ≥0.85) с более ранним, помечается;
    вырезается только НЕПРЕРЫВНЫЙ ПРОГОН из ≥3 помеченных предложений (поздняя копия) —
    одиночные рефрены и формулы (произносятся 1-2 предложениями) защищены."""
    paras = text.split("\n\n")
    sents: List[Tuple[int, int, str, str]] = []  # (para_idx, sent_idx, raw, norm)
    for pi, p in enumerate(paras):
        for si, s in enumerate(re.split(r"(?<=[.!?…])\s+", p)):
            if s.strip():
                norm = " ".join(w.lower().strip(".,!?;:—–\"'«»„…") for w in s.split())
                sents.append((pi, si, s, norm))
    dup = [False] * len(sents)
    seen: List[str] = []
    for i, (_, _, _, norm) in enumerate(sents):
        w = norm.split()
        if len(w) >= 12:
            for prev in seen:
                if abs(len(prev) - len(norm)) <= max(len(prev), len(norm)) * 0.3:
                    m = difflib.SequenceMatcher(None, prev, norm)
                    if m.real_quick_ratio() >= 0.85 and m.quick_ratio() >= 0.85 and m.ratio() >= 0.85:
                        dup[i] = True
                        break
            if not dup[i]:
                seen.append(norm)
    # непрерывные прогоны ≥3 дублей → под нож
    cut_idx = set()
    i = 0
    while i < len(sents):
        if dup[i]:
            j = i
            while j < len(sents) and dup[j]:
                j += 1
            if j - i >= 3:
                cut_idx.update(range(i, j))
            i = j
        else:
            i += 1
    if not cut_idx:
        return text, 0
    rw = sum(len(sents[i][2].split()) for i in cut_idx)
    out_paras: List[str] = []
    k = 0
    for pi, p in enumerate(paras):
        parts = []
        for si, s in enumerate(re.split(r"(?<=[.!?…])\s+", p)):
            if not s.strip():
                continue
            if k < len(sents) and sents[k][0] == pi and sents[k][1] == si:
                if k not in cut_idx:
                    parts.append(s)
                k += 1
            else:
                parts.append(s)
        if parts:
            out_paras.append(" ".join(parts))
    return "\n\n".join(out_paras), rw


def _collapse_finals(text: str, lang: str) -> Tuple[str, int, List[str]]:
    """Двойной финал: sign-off №1 на ~85%, контент, sign-off №2 в конце.
    Правильный финал — ПОСЛЕДНИЙ (закрывает файл); ранние sign-off-абзацы режем.
    Дословные дубли финала к этому шагу уже сняты _dedup_paragraphs."""
    flags: List[str] = []
    paras = text.split("\n\n")
    total_words = max(1, len(text.split()))
    sig_re = re.compile(_SIGNOFF.get(lang, _SIGNOFF["other"]), re.IGNORECASE)
    # индексы sign-off-абзацев в хвосте (>65% по словам)
    acc = 0
    sig_idx: List[int] = []
    for i, p in enumerate(paras):
        acc += len(p.split())
        if acc > total_words * 0.65 and sig_re.search(p):
            sig_idx.append(i)
    cut = 0
    if len(sig_idx) > 1:
        early = set(sig_idx[:-1])  # все, кроме последнего
        paras = [p for i, p in enumerate(paras) if i not in early]
        cut = len(early)
    if sig_idx:
        # контент ПОСЛЕ последнего sign-off — аномалия (финал обязан закрывать файл)
        last = sig_idx[-1] - cut  # после выреза индексы съехали
        tail_words = sum(len(p.split()) for p in paras[last + 1:])
        if tail_words > 50:
            flags.append(
                f"ФЛАГ: после финального sign-off ещё ~{tail_words} слов — проверить руками"
            )
    return "\n\n".join(paras), cut, flags


def _remove_mid_cta(text: str, lang: str) -> Tuple[str, int]:
    """CTA-предложения в зоне 5–85% текста — под нож (самодостаточная мета)."""
    cta_re = re.compile(_CTA.get(lang, _CTA["other"]), re.IGNORECASE)
    total = max(1, len(text))
    n = 0

    def _clean_para(m):
        nonlocal n
        para = m.group(0)
        pos = m.start() / total
        if pos < 0.05 or pos > 0.85 or not cta_re.search(para):
            return para
        sents = re.split(r"(?<=[.!?…])\s+", para)
        kept = [s for s in sents if not cta_re.search(s)]
        n += len(sents) - len(kept)
        return " ".join(kept)

    out = re.sub(r"[^\n]+", _clean_para, text)
    return out, n


def _collect_flags(text: str, lang: str) -> List[str]:
    flags: List[str] = []
    total = max(1, len(text))
    for m in re.finditer(_BRAND, text):
        flags.append(f"ФЛАГ бренд/диктор @{m.start() * 100 // total}%: «{m.group(0)[:60]}»")
    for m in re.finditer(_META_LEAK, text, re.IGNORECASE):
        ctx = text[max(0, m.start() - 30): m.start() + 50].replace("\n", " ")
        flags.append(f"ФЛАГ мета/утечка @{m.start() * 100 // total}%: «…{ctx}…»")
    return flags[:20]  # не заливать лог


def _interest_warnings(text: str, lang: str) -> List[str]:
    warns: List[str] = []
    words = text.split()
    total_w = max(1, len(words))
    fwd = len(re.findall(_FWD_FLAGS.get(lang, _FWD_FLAGS["other"]), text, re.IGNORECASE))
    if fwd > 2:
        warns.append(f"WARN интерес: форвард-флагов «запомните/вернёмся» {fwd} (норма ≤2)")
    expl = len(re.findall(_EXPLAIN.get(lang, _EXPLAIN["other"]), text, re.IGNORECASE))
    if expl > 2:
        warns.append(f"WARN интерес: объяснительных конструкций {expl} (норма ≤2)")
    sents = [s for s in re.split(r"(?<=[.!?…])\s+", text) if s.strip()]
    if sents:
        short = sum(1 for s in sents if len(s.split()) <= 5)
        share = short / len(sents)
        if share > 0.18:
            warns.append(f"WARN интерес: стаккато {share:.0%} предложений ≤5 слов (норма ≤18%)")
    first150_end = len(" ".join(words[:150]))
    q_after = text[first150_end:].count("?")
    if q_after > 8:
        warns.append(f"WARN интерес: {q_after} вопросов после первых 150 слов — проверить самоответы")
    return warns


# ── публичный контракт ───────────────────────────────────────────────────────

def clean_generated_text(text: str, language: str = "") -> Tuple[str, List[str]]:
    report: List[str] = []
    if not text or not text.strip():
        return text, ["чистка: пустой текст"]
    lang = {"polish": "pl", "czech": "cs", "romanian": "ro"}.get(
        (language or "").lower(), ""
    ) or _detect_lang(text)
    before_w = len(text.split())

    text, n_seams = _scrub_json_seams(text)
    if n_seams:
        report.append(f"вырезано JSON-артефактов склейки: {n_seams}")

    text, n_meta = _strip_meta_lines(text)
    if n_meta:
        report.append(f"удалено англ-мета-строк модели: {n_meta}")

    text, n_caps = _fix_dropcaps(text)
    if n_caps:
        report.append(f"склеено разорванных буквиц: {n_caps}")

    text, n_dup, dup_words = _dedup_paragraphs(text)
    if n_dup:
        report.append(f"удалено дословных дубль-абзацев: {n_dup} (~{dup_words} слов re-emit)")

    text, run_words = _dedup_sentence_runs(text)
    if run_words:
        report.append(f"вырезано переупакованных дубль-прогонов: ~{run_words} слов (предложение-окна)")

    text, n_fin, fin_flags = _collapse_finals(text, lang)
    if n_fin:
        report.append(f"схлопнуто лишних sign-off: {n_fin}")
    report.extend(fin_flags)

    text, n_cta = _remove_mid_cta(text, lang)
    if n_cta:
        report.append(f"вырезано CTA-предложений в корпусе (5–85%): {n_cta}")

    report.extend(_collect_flags(text, lang))
    report.extend(_interest_warnings(text, lang))

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    after_w = len(text.split())
    if after_w != before_w:
        report.insert(0, f"чистка [{lang}]: {before_w} → {after_w} слов (−{before_w - after_w})")
    elif not report:
        report.append(f"чистка [{lang}]: артефактов не найдено")
    return text, report


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--fix"]
    do_fix = "--fix" in sys.argv
    for path in args:
        with open(path, encoding="utf-8") as f:
            src = f.read()
        cleaned, rep = clean_generated_text(src)
        print(f"\n=== {path}")
        for line in rep:
            print("  •", line)
        if do_fix:
            out = path.rsplit(".", 1)[0] + ".clean.txt"
            with open(out, "w", encoding="utf-8") as f:
                f.write(cleaned)
            print(f"  → {out}")
