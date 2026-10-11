#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chapters — главы ролика по транскрипту рендера + блок таймкодов для описания.

Зачем главы: плотная сетка кликабельных меток = плотная сетка мест под рекламу.
Зритель прыгает по таймлайну, каждый прыжок пересекает метку; когда у зрителя
истекает frequency capping, ближайшая метка находится сразу, а не через 10 минут.

Требования владельца (04.08.2026):
  • шаг 2–4 минуты, оптимум ≈2.5–3 (на роликах любой длины, включая 1–2 часа);
  • заголовки кликбейтные И информативные;
  • глава кончается крючком — чтобы паузу на рекламе не закрывали;
  • метка ставится с отступом +1 с от начала главы (попадает на смену кадра/паузу,
    а не на середину слова) — это даёт High Quality Ad Break.

КАК ЭТО УСТРОЕНО (главное отличие от «попросить модель расставить таймкоды»):
сетку строит КОД. Модель никогда не возвращает секунды — только id точки реза из
шортлиста, который мы ей дали, плюс заголовок. Поэтому на двухчасовом ролике
арифметика интервалов не может разъехаться: она вообще не в руках модели.

Вход  — {project_dir}/chapters/transcript.json (пишет render_worker в момент
        whisper-прогона), либо любой .srt/.vtt/.json с таймкодами.
Выход — chapters/chapters.json, chapters/description.txt, chapters/midrolls.txt,
        chapters/chapters.meta.json.

LLM берётся тем же каналом, что пишет промпты сцен (prompt_llm: подписка ChatGPT /
Claude / OpenAI API) — отдельных ключей не нужно.

CLI:
  python3 chapters.py --project_dir ~/video_projects/Имя
  python3 chapters.py --project_dir ... --dry_run     # сетка без вызова LLM
"""
import os
import re
import sys
import json
import time
import math
import hashlib
import logging
import argparse
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import requests
except ImportError:                                   # noqa
    requests = None

try:
    import prompt_llm
except Exception:                                     # модуль не собран в exe
    prompt_llm = None

logger = logging.getLogger("chapters")

ALGO_VERSION = 2

# ── Сетка ─────────────────────────────────────────────────────────────────────
MIN_GAP_SEC = 120          # ближе — YouTube начинает считать это Ad Spam
MAX_GAP_SEC = 240          # дальше — сетка слотов рвётся, появляется холостой ход
TARGET_GAP_SEC = 165       # 2:45 — середина рекомендованного окна 2.5–3 мин
TAIL_GUARD_SEC = 60        # в последнюю минуту метку не ставим
MIN_DURATION_SEC = 480     # короче 8 мин трёх глав по 2 мин не набрать
MARK_OFFSET_SEC = 1        # +1 с от начала главы

# ── Заголовки ─────────────────────────────────────────────────────────────────
TITLE_MIN_CHARS = 24
TITLE_MAX_CHARS = 70
BATCH_WINDOWS = 6          # окон в одном запросе к LLM
MAX_CANDIDATES = 4         # точек реза на окно
CTX_BEFORE_CHARS = 170     # хвост предыдущего куска (нужен, чтобы судить о крючке)
CTX_AFTER_CHARS = 210      # начало следующего куска — для ВЫБОРА точки реза
CHAPTER_TEXT_CHARS = 1900  # текст главы целиком — для НАПИСАНИЯ заголовка.
# 1900 ≈ 166 c речи (замер: PL 782 симв/мин, RU 760, EN 942). По одному только началу
# главы заголовок описывал первую фразу, а не главу: «113 kandydatów, 28 miejsc» —
# про вступление, тогда как две трети куска были уже про другое.
MAX_FALLBACK_SHARE = 0.30  # больше — считаем прогон провалившимся

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
CHAPTERS_MODEL = os.environ.get("VS_CHAPTERS_MODEL", "gpt-4.1-mini")
LLM_TIMEOUT = (5, 120)

_NUMBERING_RE = re.compile(
    r"^\s*(?:глава|часть|chapter|part|kapitola|kapitel|rozdzia[lł]|capitolul|bölüm)\s*"
    r"\d+\s*[.:)\-–—]*\s*", re.I)
_TIMECODE_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿️⬀-⯿]")
_NUM_RE = re.compile(r"\d+")


class ChaptersError(Exception):
    """Фатально: делать главы не из чего или нечем. Молча не деградируем."""


# ---------------------------------------------------------------------------
# Транскрипт
# ---------------------------------------------------------------------------
def _ts_to_sec(ts: str) -> float:
    ts = ts.strip().replace(",", ".")
    parts = [float(p) for p in ts.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    h, m, s = parts[-3], parts[-2], parts[-1]
    return h * 3600 + m * 60 + s


def load_segments(path):
    """Транскрипт → [{'start','end','text'}]. Поддержка transcript.json, .srt, .vtt.

    Соседние куски одного предложения НЕ склеиваются: точки реза берутся именно
    из границ сегментов whisper, а он режет по паузам — то, что нам и нужно.
    """
    if not path or not os.path.exists(path):
        return []
    raw = open(path, "r", encoding="utf-8", errors="replace").read()
    out = []

    if path.lower().endswith(".json"):
        try:
            data = json.loads(raw)
        except Exception as e:
            raise ChaptersError(f"транскрипт не разобран как JSON: {e}")
        segs = data.get("segments") if isinstance(data, dict) else data
        for s in segs or []:
            if not isinstance(s, dict):
                continue
            txt = (s.get("text") or "").strip()
            if not txt:
                continue
            try:
                st = float(s.get("start"))
                en = float(s.get("end", st))
            except (TypeError, ValueError):
                continue
            out.append({"start": st, "end": max(en, st), "text": txt})
        return out

    # srt / vtt
    for block in re.split(r"\n\s*\n", raw):
        m = re.search(r"(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}|\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*"
                      r"(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}|\d{1,2}:\d{2}[.,]\d{1,3})", block)
        if not m:
            continue
        text = " ".join(ln.strip() for ln in block.splitlines()
                        if "-->" not in ln and not ln.strip().isdigit()
                        and not ln.strip().upper().startswith("WEBVTT")).strip()
        if text:
            out.append({"start": _ts_to_sec(m.group(1)),
                        "end": _ts_to_sec(m.group(2)), "text": text})
    return out


def parse_transcript(path):
    """Совместимость: [(секунда, текст)]. Новый код использует load_segments()."""
    return [(s["start"], s["text"]) for s in load_segments(path)]


def _as_segments(items):
    """Принимает и [{'start','end','text'}], и [(sec, text)] — приводит к dict-форме."""
    segs = []
    for it in items or []:
        if isinstance(it, dict):
            st = float(it.get("start", 0.0))
            segs.append({"start": st, "end": float(it.get("end", st)),
                         "text": (it.get("text") or "").strip()})
        elif isinstance(it, (list, tuple)) and len(it) >= 2:
            segs.append({"start": float(it[0]), "end": float(it[0]),
                         "text": str(it[1]).strip()})
    segs = [s for s in segs if s["text"]]
    segs.sort(key=lambda s: s["start"])
    for i, s in enumerate(segs):                       # чиним отсутствующие end
        if s["end"] <= s["start"]:
            s["end"] = segs[i + 1]["start"] if i + 1 < len(segs) else s["start"] + 2.0
    return segs


def transcript_duration(segments):
    segs = _as_segments(segments)
    return max((s["end"] for s in segs), default=0.0)


# ---------------------------------------------------------------------------
# Сетка окон и шортлист точек реза
# ---------------------------------------------------------------------------
def plan_grid(duration, target=TARGET_GAP_SEC, min_gap=MIN_GAP_SEC, max_gap=MAX_GAP_SEC):
    """Целевые секунды меток (без нулевой) + шаг. Шаг всегда внутри [min_gap, max_gap]."""
    usable = max(0.0, duration - TAIL_GUARD_SEC)
    if usable <= 0:
        return [], 0.0
    n = max(1, int(round(duration / float(target))))
    while duration / n > max_gap:
        n += 1
    while n > 1 and duration / n < min_gap:
        n -= 1
    step = duration / n
    targets = [k * step for k in range(1, n) if k * step <= usable]
    return targets, step


def _window_half(step, min_gap=MIN_GAP_SEC, max_gap=MAX_GAP_SEC):
    """Полуширина окна, при которой ЛЮБОЙ выбор внутри него держит коридор.

    Соседние метки сдвигаются максимум на ±half каждая, значит интервал гуляет
    в [step-2·half, step+2·half] — оно и должно остаться внутри [min_gap, max_gap].
    """
    return max(0.0, min(0.20 * step, (step - min_gap) / 2.0, (max_gap - step) / 2.0))


def _ctx_before(segs, idx, limit=CTX_BEFORE_CHARS):
    parts = []
    for s in reversed(segs[max(0, idx - 4):idx]):
        parts.insert(0, s["text"])
        if sum(len(p) for p in parts) >= limit:
            break
    return ("… " + " ".join(parts))[-limit:].strip()


def _ctx_after(segs, idx, limit=CTX_AFTER_CHARS):
    parts = []
    for s in segs[idx:idx + 5]:
        parts.append(s["text"])
        if sum(len(p) for p in parts) >= limit:
            break
    return " ".join(parts)[:limit].strip()


def build_windows(segs, duration, target=TARGET_GAP_SEC,
                  min_gap=MIN_GAP_SEC, max_gap=MAX_GAP_SEC):
    """Окна выбора: [{'window', 'target', 'candidates':[{id, sec, pause, before, after}]}].

    Кандидаты — только НАЧАЛА сегментов whisper (то есть места, где диктор уже
    сделал паузу). Приоритет — длине паузы перед сегментом.
    """
    targets, step = plan_grid(duration, target, min_gap, max_gap)
    half = _window_half(step, min_gap, max_gap)
    windows = []
    for wi, tgt in enumerate(targets, start=1):
        cands = []
        for idx, s in enumerate(segs):
            if idx == 0:
                continue
            if abs(s["start"] - tgt) > half:
                continue
            pause = max(0.0, s["start"] - segs[idx - 1]["end"])
            cands.append({"idx": idx, "sec": s["start"], "pause": round(pause, 2)})
        if not cands:
            # окно пустое (длинный монолог без пауз) — берём ближайшую границу сегмента
            near = min(range(1, len(segs)), key=lambda i: abs(segs[i]["start"] - tgt),
                       default=None)
            if near is None:
                continue
            cands = [{"idx": near, "sec": segs[near]["start"],
                      "pause": round(max(0.0, segs[near]["start"] - segs[near - 1]["end"]), 2)}]
        # в шортлист — самые «паузистые», но ближайший к цели остаётся всегда
        nearest = min(cands, key=lambda c: abs(c["sec"] - tgt))
        rest = sorted([c for c in cands if c is not nearest],
                      key=lambda c: (-c["pause"], abs(c["sec"] - tgt)))
        chosen = ([nearest] + rest)[:MAX_CANDIDATES]
        chosen.sort(key=lambda c: c["sec"])
        out = []
        for ci, c in enumerate(chosen, start=1):
            out.append({
                "id": f"w{wi}c{ci}",
                "idx": c["idx"],
                "sec": c["sec"],
                "pause": c["pause"],
                "default": c is nearest,
                "before": _ctx_before(segs, c["idx"]),
                "after": _ctx_after(segs, c["idx"]),
            })
        windows.append({"window": wi, "target": round(tgt, 1), "candidates": out,
                        "default_idx": nearest["idx"]})
    # Текст главы целиком: от точки реза этого окна до точки реза следующего. Считается
    # по кандидатам-умолчаниям — реальный выбор модели сдвинет границу максимум на ±half,
    # то есть на пару предложений, что для написания заголовка несущественно.
    for i, w in enumerate(windows):
        nxt = windows[i + 1]["default_idx"] if i + 1 < len(windows) else len(segs)
        w["chapter"] = _chapter_text(segs, w["default_idx"], nxt, CHAPTER_TEXT_CHARS)
    return windows, step


# ---------------------------------------------------------------------------
# Запрос к LLM
# ---------------------------------------------------------------------------
_SYSTEM = (
    "Ты режешь длинные YouTube-ролики на главы и придумываешь им заголовки. "
    "Отвечаешь ТОЛЬКО JSON-объектом, без пояснений и без markdown."
)

_RULES = """ЗАДАЧА: для каждого окна выбрать точку реза и написать заголовок главы.

ТОЧКА РЕЗА
• Выбираешь ТОЛЬКО id из списка кандидатов этого окна. Свои секунды не придумываешь.
• Хороший рез — там, где в поле "after" начинается новый поворот темы, а в поле
  "before" предыдущий кусок обрывается на крючке (вопрос, недосказанность, обещание).
• Если разницы нет — бери кандидата с "default": true.

ЗАГОЛОВОК
• Пишешь его по блоку «ТЕКСТ ГЛАВЫ» — это глава ЦЕЛИКОМ, все ~3 минуты.
  Заголовок должен покрывать главу, а не только её первую фразу.
• Язык — строго язык транскрипта. {lang_hint}
• Длина {tmin}–{tmax} символов, одной строкой.
• Одновременно КЛИКБЕЙТНЫЙ (интрига, конфликт, конкретная деталь или цифра) и
  ИНФОРМАТИВНЫЙ (по нему понятно, о чём кусок).
• Ничего не выдумывай: имена, числа и факты — только те, что есть в тексте главы.

ЗАПРЕЩЕНО в заголовке: нумерация («Глава 1», «Часть 2»), таймкоды, кавычки, эмодзи,
многоточие в конце, слова-пустышки «важно отметить», «интересный факт», «вы не поверите»,
«шокирующая правда», «это изменит всё».

❌ ПЛОХО (пустой кликбейт): Вы не поверите, что случилось дальше
❌ ПЛОХО (скучно): Продолжение рассказа о событиях
❌ ПЛОХО (спойлер финала): Он погибает в конце от рук собственного адъютанта
✅ ХОРОШО: Приказ, который отменили через восемь минут
✅ ХОРОШО: Почему санитары обходили этот барак стороной

ФОРМАТ ОТВЕТА:
{{"picks": [{{"window": <номер>, "cut": "<id кандидата>", "title": "<заголовок>"}}]}}
Ровно по одному объекту на каждое окно из запроса, ничего лишнего.

ОКНА:
{body}
"""

_OPENING_RULES = """ЗАДАЧА: написать заголовок ПЕРВОЙ главы ролика (она начинается с 0:00).

• Язык — строго язык транскрипта. {lang_hint}
• Длина {tmin}–{tmax} символов, одной строкой, без нумерации, кавычек, эмодзи и таймкодов.
• Кликбейтно и информативно: это первое, что видит зритель в списке глав.
• Опирайся только на текст ниже, ничего не выдумывай.

ФОРМАТ ОТВЕТА: {{"picks": [{{"window": 0, "cut": "start", "title": "<заголовок>"}}]}}

ТЕКСТ ПЕРВОЙ ГЛАВЫ (целиком):
{body}
"""


def _openai_key():
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if key:
        return key
    try:
        cfg = json.load(open(os.path.join(os.path.expanduser("~"),
                                          "video_studio_api_keys.json"), encoding="utf-8"))
        return str(cfg.get("openai") or "").strip()
    except Exception:
        return ""


def _provider():
    if prompt_llm is None:
        return "openai"
    try:
        return prompt_llm.provider()
    except Exception:
        return "openai"


def _llm_chat(payload, timeout=LLM_TIMEOUT):
    """Один чат-вызов тем же каналом, что пишет промпты сцен.

    OpenAI-звено цепочки работает только если передать замыкание openai_call —
    без него prompt_llm считает канал недоступным (см. prompt_llm._channel_ready).
    """
    key = _openai_key()

    def _openai_call():
        if requests is None:
            raise ChaptersError("requests недоступен — нечем сходить в OpenAI")
        if not key:
            raise ChaptersError("нет OpenAI-ключа (~/video_studio_api_keys.json['openai'])")
        return requests.post(OPENAI_API_URL,
                             headers={"Authorization": f"Bearer {key}",
                                      "Content-Type": "application/json"},
                             json=payload, timeout=timeout)

    if prompt_llm is None:
        return _openai_call()
    try:
        if _provider() == "openai" and not prompt_llm.fallback_enabled():
            return _openai_call()
    except Exception:
        pass
    return prompt_llm.post(payload, openai_call=(_openai_call if key else None))


def _extract_json(text):
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _ask_llm(user_prompt, model=None):
    """→ dict ответа модели. Кидает ChaptersError на нерабочем канале."""
    payload = {
        "model": model or CHAPTERS_MODEL,
        "messages": [{"role": "system", "content": _SYSTEM},
                     {"role": "user", "content": user_prompt}],
        "temperature": 0.8,
        "response_format": {"type": "json_object"},
    }
    resp = _llm_chat(payload)
    code = getattr(resp, "status_code", 0)
    if code == 401:
        raise ChaptersError("канал промптов не авторизован (401): проверь ключ/токен "
                            "в «Ключи → Чем писать промпты»")
    if code == 429:
        raise ChaptersError("канал промптов упёрся в лимит/исчерпан счёт (429)")
    if code != 200:
        body = ""
        try:
            body = str(resp.json())[:200]
        except Exception:
            body = str(getattr(resp, "text", ""))[:200]
        raise ChaptersError(f"канал промптов вернул {code}: {body}")
    data = resp.json()
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return _extract_json(content)


# ---------------------------------------------------------------------------
# Чистка и проверка заголовков
# ---------------------------------------------------------------------------
def _norm(s):
    return unicodedata.normalize("NFC", (s or "").strip().lower())


def clean_title(title):
    """Приводит заголовок к формату строки описания. Возвращает '' если чинить нечего."""
    t = unicodedata.normalize("NFC", str(title or ""))
    t = t.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    t = _EMOJI_RE.sub("", t)
    t = _TIMECODE_RE.sub("", t)
    t = _NUMBERING_RE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = t.strip("«»\"'“”‘’ ")
    t = re.sub(r"[.…]+$", "", t).strip()
    if len(t) > TITLE_MAX_CHARS:                        # режем по границе слова
        cut = t[:TITLE_MAX_CHARS + 1]
        sp = cut.rfind(" ")
        t = (cut[:sp] if sp > TITLE_MIN_CHARS else cut[:TITLE_MAX_CHARS]).strip(" ,;:—-")
    return t


def fallback_title(text):
    """Заголовок из самого текста главы — когда модель не дала годного.

    Whisper режет по паузам, поэтому кусок часто начинается с СЕРЕДИНЫ фразы
    («a nawet broni atomowej, której…») — такой заголовок читается как обрывок.
    Поэтому первое неполное предложение пропускаем.
    """
    parts = [p for p in re.split(r"(?<=[.!?…])\s+", (text or "").strip()) if p]
    if len(parts) > 1 and parts[0][:1].islower():
        parts = parts[1:]
    t = clean_title(parts[0] if parts else text)
    if len(t) < TITLE_MIN_CHARS and len(parts) > 1:
        t = clean_title(" ".join(parts[:2]))
    if t:
        t = t[:1].upper() + t[1:]
    return t or "Продолжение"


# Язык расшифровки → язык num2words. Хорватского и греческого в библиотеке нет; числа
# хорватского до тысячи пишутся как сербские, а для греческого проверка остаётся
# прежней — только цифрами.
_NUM_WORDS_LANG = {"cs": "cs", "es": "es", "hu": "hu", "pl": "pl", "pt": "pt",
                   "ro": "ro", "uk": "uk", "ru": "ru", "en": "en", "de": "de",
                   "fr": "fr", "it": "it", "sk": "sk", "hr": "sr", "sr": "sr"}
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.U)


def _spelled(n, lang):
    """Число словами на языке главы или "" (нет языка, нет библиотеки, слишком большое)."""
    code = _NUM_WORDS_LANG.get((lang or "").lower()[:2])
    if not code or len(n) > 4:
        return ""
    try:
        from num2words import num2words
        return num2words(int(n), lang=code)
    except Exception:
        return ""


def _said_in_words(n, words, joined, lang):
    """Число из заголовка произнесено в главе словами — с любым падежным окончанием.

    Текст для озвучки пишет числа словами («тринадцять кулеметів»), и расшифровка
    повторяет их словами же, а модель в заголовке ставит цифры (11.10.2026: 480 таких
    отказов, верные заголовки менялись на обрывки фраз). Каждое слово числа должно
    найтись в главе. Короткое (до 5 букв) — целиком, с окончанием до двух букв: иначе
    «tři» засчиталось бы по «třicet». Длинное — по основе: без двух последних букв, а у
    слов длиннее семи — без четырёх («trzynaście» → «trzynastu» меняет «ś» на «s»).
    Найденное слово не длиннее числа больше чем на три буквы.
    """
    spelled = _spelled(n, lang)
    if not spelled:
        return False
    for w in _WORD_RE.findall(spelled.lower()):
        if len(w) <= 5:
            stem, room = w, len(w) + 2
        else:
            stem, room = (w[:-2] if len(w) <= 7 else w[:-4]), len(w) + 3
        if any(t.startswith(stem) and len(t) <= room for t in words):
            continue
        # Библиотека пишет «devětset» слитно, текст — «devět set»: длинное слово
        # ищем и в склеенной главе.
        if len(stem) > 5 and stem in joined:
            continue
        return False
    return True


def _numbers_ok(title, chapter_text, lang=""):
    """Числа в заголовке должны быть в тексте главы — защита от выдуманных дат.

    В тексте число может стоять цифрами или словами (см. `_said_in_words`)."""
    body = _NUM_RE.findall(chapter_text or "")
    missing = [n for n in _NUM_RE.findall(title or "") if n not in body]
    if not missing:
        return True
    found = _WORD_RE.findall((chapter_text or "").lower())
    words, joined = set(found), "".join(found)
    return all(_said_in_words(n, words, joined, lang) for n in missing)


def _detect_lang(text):
    try:
        from langdetect import detect
        return detect(text)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Сборка глав
# ---------------------------------------------------------------------------
def _chapter_text(segs, start_idx, end_idx, limit=1200):
    parts = [s["text"] for s in segs[start_idx:end_idx]]
    return " ".join(parts)[:limit]


def _windows_body(windows, segs):
    blocks = []
    for w in windows:
        lines = [f'ОКНО {w["window"]} (ориентир {int(w["target"])} c):']
        lines.append('  ТЕКСТ ГЛАВЫ (по нему пишешь заголовок):')
        lines.append(f'    {w.get("chapter", "")}')
        lines.append('  КАНДИДАТЫ НА ТОЧКУ РЕЗА (по ним выбираешь cut):')
        for c in w["candidates"]:
            mark = " ← default" if c["default"] else ""
            lines.append(f'  id={c["id"]} пауза={c["pause"]}с{mark}')
            lines.append(f'    before: {c["before"]}')
            lines.append(f'    after:  {c["after"]}')
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _lang_hint(lang):
    names = {"ru": "русский", "en": "английский", "uk": "украинский", "pl": "польский",
             "cs": "чешский", "ro": "румынский", "de": "немецкий", "fr": "французский",
             "es": "испанский", "it": "итальянский", "pt": "португальский",
             "tr": "турецкий", "nl": "нидерландский"}
    n = names.get((lang or "").lower())
    return f"Язык транскрипта: {n}." if n else ""


def build_chapters(segs, duration=None, target=TARGET_GAP_SEC, min_gap=MIN_GAP_SEC,
                   max_gap=MAX_GAP_SEC, offset=MARK_OFFSET_SEC, language="",
                   model=None, dry_run=False, log=print):
    """Полный проход: сетка → LLM → валидатор → главы.

    Возвращает dict-результат (он же ложится в chapters.json).
    """
    segs = _as_segments(segs)
    if not segs:
        raise ChaptersError("транскрипт пуст")
    duration = float(duration or transcript_duration(segs))
    if duration < MIN_DURATION_SEC:
        raise ChaptersError(
            f"ролик короче {MIN_DURATION_SEC // 60} мин ({duration / 60:.1f} мин) — "
            f"трёх глав с шагом {min_gap // 60}+ мин не набрать, YouTube их не покажет")

    warnings = []
    windows, step = build_windows(segs, duration, target, min_gap, max_gap)
    if not windows:
        raise ChaptersError("не удалось построить сетку окон")
    log(f"   Сетка: {len(windows) + 1} глав, шаг ≈{step:.0f} c "
        f"(окно выбора ±{_window_half(step, min_gap, max_gap):.0f} c)")

    lang = language or _detect_lang(" ".join(s["text"] for s in segs[:40]))
    hint = _lang_hint(lang)

    picks = {}
    if not dry_run:
        # первая глава: только заголовок
        try:
            head = _ask_llm(_OPENING_RULES.format(lang_hint=hint, tmin=TITLE_MIN_CHARS,
                                                  tmax=TITLE_MAX_CHARS,
                                                  body=_chapter_text(segs, 0, windows[0]["default_idx"],
                                                                     CHAPTER_TEXT_CHARS)),
                            model)
            for p in (head or {}).get("picks") or []:
                picks[0] = p
        except ChaptersError:
            raise
        except Exception as e:
            warnings.append(f"заголовок первой главы: {e}")

        batches = [windows[i:i + BATCH_WINDOWS] for i in range(0, len(windows), BATCH_WINDOWS)]
        for bi, batch in enumerate(batches, start=1):
            prompt = _RULES.format(lang_hint=hint, tmin=TITLE_MIN_CHARS,
                                   tmax=TITLE_MAX_CHARS, body=_windows_body(batch, segs))
            got = None
            for attempt in (1, 2):
                try:
                    got = _ask_llm(prompt, model)
                    if got and got.get("picks"):
                        break
                except ChaptersError:
                    raise
                except Exception as e:
                    warnings.append(f"батч {bi}, попытка {attempt}: {e}")
                    time.sleep(1.5)
            for p in (got or {}).get("picks") or []:
                try:
                    picks[int(p.get("window"))] = p
                except (TypeError, ValueError):
                    warnings.append(f"батч {bi}: ответ без номера окна")
            log(f"   Батч {bi}/{len(batches)}: окон {len(batch)}, ответов {len(picks)}")

    # ── сборка: точки реза ────────────────────────────────────────────────────
    cuts = [{"idx": 0, "sec": 0.0}]
    for w in windows:
        by_id = {c["id"]: c for c in w["candidates"]}
        default = next((c for c in w["candidates"] if c["default"]), w["candidates"][0])
        p = picks.get(w["window"])
        chosen = default
        if p is not None:
            cid = str(p.get("cut") or "")
            if cid in by_id:
                chosen = by_id[cid]
            else:
                warnings.append(f"окно {w['window']}: модель вернула id «{cid[:20]}», "
                                f"которого нет в шортлисте — беру default")
        elif not dry_run:
            warnings.append(f"окно {w['window']}: ответа не было — беру default")
        cuts.append({"idx": chosen["idx"], "sec": chosen["sec"], "window": w["window"]})

    # ── сборка: заголовки ─────────────────────────────────────────────────────
    chapters = []
    used = set()
    rejected = 0          # модель ответила, но заголовок не прошёл нашу проверку
    for i, cut in enumerate(cuts):
        end_idx = cuts[i + 1]["idx"] if i + 1 < len(cuts) else len(segs)
        body = _chapter_text(segs, cut["idx"], end_idx)
        # Проверка чисел идёт по ПОЛНОМУ тексту главы, а не по обрезанному куску:
        # на 166-секундной главе 1200 символов — это её первая половина, и живое число
        # из второй половины считалось «выдуманным» (ложная замена на запасной заголовок).
        full = _chapter_text(segs, cut["idx"], end_idx, limit=100000)
        p = picks.get(cut.get("window", 0) if i else 0)
        title = clean_title((p or {}).get("title"))
        source = "llm"
        if not title or len(title) < TITLE_MIN_CHARS:
            if title:
                warnings.append(f"глава {i + 1}: заголовок короче {TITLE_MIN_CHARS} символов")
            title, source = fallback_title(body), "fallback"
        elif not _numbers_ok(title, full, lang):
            warnings.append(f"глава {i + 1}: числа в заголовке не найдены в тексте главы")
            title, source = fallback_title(body), "fallback"
            rejected += 1
        elif _norm(title) in used:
            warnings.append(f"глава {i + 1}: заголовок повторяется")
            title, source = fallback_title(body), "fallback"
            rejected += 1
        used.add(_norm(title))
        chapters.append({"index": i + 1, "start": round(cut["sec"], 2),
                         "title": title, "source": source})

    # ── метки: 0:00 + (start + offset) ────────────────────────────────────────
    prev = -1
    for i, ch in enumerate(chapters):
        mark = 0 if i == 0 else int(math.floor(ch["start"])) + int(offset)
        if mark <= prev:
            mark = prev + 1
        ch["mark"] = mark
        prev = mark

    # ── валидатор сетки ───────────────────────────────────────────────────────
    marks = [c["mark"] for c in chapters]
    for a, b in zip(marks, marks[1:]):
        gap = b - a
        if gap < min_gap or gap > max_gap:
            warnings.append(f"интервал {a}→{b} = {gap} c вне коридора "
                            f"[{min_gap}, {max_gap}] — проверь вручную")
    if marks[-1] > duration - TAIL_GUARD_SEC:
        dropped = chapters.pop()
        warnings.append(f"последняя глава «{dropped['title'][:40]}» на {dropped['mark']} c "
                        f"попадала в хвост ролика — убрал")

    fb = sum(1 for c in chapters if c["source"] == "fallback")
    status = "ok"
    if len(chapters) < 3:
        status = "failed"
        warnings.append(f"глав получилось {len(chapters)} — YouTube показывает от трёх")
    elif not dry_run and fb > MAX_FALLBACK_SHARE * len(chapters):
        status = "failed"
        # Причину называем по факту: «канал почти не отвечал» писалось и тогда, когда
        # модель ответила на все окна, а заголовки отбраковала проверка (11.10.2026).
        why = ("канал промптов почти не отвечал" if rejected * 2 < fb else
               "модель ответила, но заголовки не прошли проверку — числа или повторы")
        warnings.append(f"{fb} из {len(chapters)} заголовков — запасные "
                        f"({why}); описание не пишу")

    if not dry_run and lang and status == "ok":
        got_lang = _detect_lang(" ".join(c["title"] for c in chapters
                                         if c["source"] == "llm"))
        if got_lang and lang and got_lang != lang:
            warnings.append(f"язык заголовков ({got_lang}) не совпал с языком "
                            f"транскрипта ({lang}) — проверь глазами")

    return {
        "version": ALGO_VERSION,
        "status": status,
        "duration": round(duration, 2),
        "language": lang,
        "step_sec": round(step, 1),
        "target_gap": target, "min_gap": min_gap, "max_gap": max_gap,
        "offset_sec": int(offset),
        "provider": _provider(),
        "model": model or CHAPTERS_MODEL,
        "dry_run": bool(dry_run),
        "created_at": int(time.time()),
        "chapters": chapters,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Совместимость со старым API
# ---------------------------------------------------------------------------
def generate_chapters(segments, duration_sec=None, target_gap=TARGET_GAP_SEC, timeout=180):
    """[(секунда, заголовок)] — как в первой версии модуля."""
    res = build_chapters(segments, duration=duration_sec, target=target_gap)
    return [(c["mark"], c["title"]) for c in res["chapters"]]


def normalize_chapters(chapters, duration_sec):
    """Сортировка, дедуп, первая метка 0:00, выброс хвостовых. Без обращения к LLM."""
    if not chapters:
        return []
    uniq = {}
    for t, title in chapters:
        if title:
            uniq[int(round(float(t)))] = title
    out = []
    for t, title in sorted(uniq.items()):
        if t > duration_sec - TAIL_GUARD_SEC:
            continue
        if out and t - out[-1][0] < MIN_GAP_SEC:
            continue
        out.append((t, title))
    if out and out[0][0] != 0:
        out[0] = (0, out[0][1])
    return out


def fmt_stamp(sec, long_form=False):
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if (h or long_form) else f"{m}:{s:02d}"


def chapters_to_description(chapters, duration=0):
    """Блок для описания. Первая строка ВСЕГДА '0:00 …' — иначе YouTube главы не включит.

    Формат метки — по её собственному значению (M:SS до часа, H:MM:SS после), а не по
    длительности ролика: на двухчасовом ролике первая метка должна остаться «0:00»,
    а не «0:00:00».
    """
    lines = []
    for ch in chapters:
        if isinstance(ch, dict):
            sec, title = ch.get("mark", ch.get("start", 0)), ch.get("title", "")
        else:
            sec, title = ch
        lines.append(f"{fmt_stamp(sec)} {title}")
    return "\n".join(lines)


def chapter_starts(chapters):
    """Секунды меток без нулевой — вход для расстановки рекламных слотов."""
    out = []
    for ch in chapters:
        sec = ch.get("mark", ch.get("start", 0)) if isinstance(ch, dict) else ch[0]
        if sec:
            out.append(int(sec))
    return out


# ---------------------------------------------------------------------------
# Проект: файлы и паспорт
# ---------------------------------------------------------------------------
def transcript_path(project_dir):
    return os.path.join(project_dir, "chapters", "transcript.json")


def description_path(project_dir):
    return os.path.join(project_dir, "chapters", "description.txt")


def _sha1_file(path):
    try:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def _write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def load_description(project_dir, video_duration=None, tolerance=2.0):
    """Готовый блок глав для описания — или ''.

    Гейт актуальности: если длительность готового mp4 разошлась с той, по которой
    считались главы, метки уедут в пустоту — такой блок не отдаём.
    """
    path = description_path(project_dir)
    if not os.path.exists(path):
        return ""
    try:
        meta = json.load(open(os.path.join(project_dir, "chapters", "chapters.json"),
                              encoding="utf-8"))
        if meta.get("status") != "ok":
            return ""
        if video_duration and abs(float(meta.get("duration", 0)) - float(video_duration)) > tolerance:
            return ""
    except Exception:
        return ""
    try:
        return open(path, encoding="utf-8").read().strip()
    except Exception:
        return ""


def run_project(project_dir, transcript=None, target=TARGET_GAP_SEC, min_gap=MIN_GAP_SEC,
                max_gap=MAX_GAP_SEC, offset=MARK_OFFSET_SEC, model=None,
                dry_run=False, log=print):
    """Главы для проекта: читает транскрипт, пишет артефакты в chapters/."""
    tpath = transcript or transcript_path(project_dir)
    if not os.path.exists(tpath):
        raise ChaptersError(
            f"нет транскрипта ({os.path.basename(tpath)}). Он появляется при рендере с "
            f"включённым «🎯 Синхрон с озвучкой» — отрендерь ролик и повтори")
    segs = load_segments(tpath)
    if not segs:
        raise ChaptersError(f"транскрипт пуст: {tpath}")

    meta_dur = 0.0
    try:
        meta_dur = float(json.load(open(tpath, encoding="utf-8")).get("duration") or 0)
    except Exception:
        pass

    log(f"📑 Главы: сегментов {len(segs)}, длительность "
        f"{(meta_dur or transcript_duration(segs)) / 60:.1f} мин, канал «{_provider()}»")
    res = build_chapters(segs, duration=meta_dur or None, target=target, min_gap=min_gap,
                         max_gap=max_gap, offset=offset, model=model, dry_run=dry_run, log=log)

    out_dir = os.path.join(project_dir, "chapters")
    desc = chapters_to_description(res["chapters"], res["duration"])
    _write_atomic(os.path.join(out_dir, "chapters.json"),
                  json.dumps(res, ensure_ascii=False, indent=2))
    if dry_run:
        # Прогон «посмотреть сетку» не должен оставлять описание: иначе заливка подхватит
        # запасные заголовки как готовый результат.
        log("   (dry_run: описание не записано, только chapters.json)")
    elif res["status"] == "ok":
        _write_atomic(description_path(project_dir), desc + "\n")
        _write_atomic(os.path.join(out_dir, "midrolls.txt"),
                      "\n".join(str(s) for s in chapter_starts(res["chapters"])) + "\n")
        _write_atomic(os.path.join(out_dir, "chapters.meta.json"), json.dumps({
            "algo_version": ALGO_VERSION,
            "transcript_sha1": _sha1_file(tpath),
            "duration": res["duration"], "target_gap": target,
            "min_gap": min_gap, "max_gap": max_gap, "offset_sec": int(offset),
            "created_at": res["created_at"],
        }, ensure_ascii=False, indent=2))

    for w in res["warnings"]:
        log(f"   ⚠️ {w}")
    log(f"   Готово: {len(res['chapters'])} глав, запасных заголовков "
        f"{sum(1 for c in res['chapters'] if c['source'] == 'fallback')}")
    for c in res["chapters"]:
        log(f"   {fmt_stamp(c['mark'])}  {c['title']}")
    return res


def main():
    ap = argparse.ArgumentParser(description="Главы ролика по транскрипту")
    ap.add_argument("--project_dir", required=True)
    ap.add_argument("--transcript", default="", help="свой файл транскрипта (.json/.srt/.vtt)")
    ap.add_argument("--target", type=int, default=TARGET_GAP_SEC)
    ap.add_argument("--min_gap", type=int, default=MIN_GAP_SEC)
    ap.add_argument("--max_gap", type=int, default=MAX_GAP_SEC)
    ap.add_argument("--offset", type=int, default=MARK_OFFSET_SEC)
    ap.add_argument("--model", default="")
    ap.add_argument("--dry_run", action="store_true", help="сетка без вызова LLM")
    args = ap.parse_args()

    try:
        res = run_project(args.project_dir, transcript=(args.transcript or None),
                          target=args.target, min_gap=args.min_gap, max_gap=args.max_gap,
                          offset=args.offset, model=(args.model or None),
                          dry_run=args.dry_run)
    except ChaptersError as e:
        print(f"__CHAPTERS_ERROR__|{e}")
        return 1
    except Exception as e:
        print(f"__CHAPTERS_ERROR__|неожиданная ошибка: {e}")
        return 1
    if res["status"] != "ok":
        print(f"__CHAPTERS_ERROR__|{'; '.join(res['warnings'][-2:]) or 'главы не собрались'}")
        return 1
    print(f"__CHAPTERS_SUCCESS__|{len(res['chapters'])}|{description_path(args.project_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
