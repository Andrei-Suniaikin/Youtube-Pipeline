# -*- coding: utf-8 -*-
"""Рерайт в формат рекапа: три прохода.

1. Разбор и скелет — один вызов. Глоссарий (герои с ПОЛОМ, места) и разбивка
   на сцены с кусками источника и бюджетом слов. До шлюза не пишется ни слова
   готового текста: переспросить скелет дёшево, написать семь тысяч слов не о том — дорого.
2. Разворачивание — по вызову на сцену, ПАРАЛЛЕЛЬНО. В каждый подкладывается
   глоссарий, поэтому сцены не знают друг о друге и всё равно связны.
3. Проверка — машинная, бесплатная. Модель зовётся только на помеченные места.

Полноту, объём и форму считает КОД. Модели доверяем вкус, но не полноту:
измерено, что модель-судья ловит выдуманное хорошо, а пропущенное — на 57% хуже.
"""
from __future__ import annotations

import json
import os
import re
import statistics as st
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

from llm import call, call_json, LLMError
import claude_pool as CP
# общий предел одновременных вызовов модели на службу (MANGA_CLAUDE_CONCURRENCY); без него — прямой вызов
call, call_json = CP.limited(call), CP.limited(call_json)
import languages as LG

HERE = os.path.dirname(os.path.abspath(__file__))
STYLE = open(os.path.join(HERE, "style_prompt.txt"), encoding="utf-8").read() \
    if os.path.exists(os.path.join(HERE, "style_prompt.txt")) else ""


@dataclass
class Scene:
    num: int
    title: str
    src_from: int
    src_to: int
    budget_words: int
    who: List[str] = field(default_factory=list)
    where: str = ""
    what: str = ""
    role: str = ""
    text: str = ""
    words: int = 0
    status: str = "ждёт"
    error: str = ""
    seconds: float = 0.0
    model: str = ""
    budget_share: float = 0.0


@dataclass
class Skeleton:
    characters: List[dict] = field(default_factory=list)
    locations: List[dict] = field(default_factory=list)
    scenes: List[Scene] = field(default_factory=list)


# ---------------------------------------------------------------- проход 1

GLOSSARY_PROMPT = """You analyse a source text for a manhwa retelling video.
The source is auto-generated subtitles of a competitor's video: it contains
recognition errors — swapped genders and pronouns, mangled names. See through them.

Return ONE JSON object and nothing else:
{{
  "characters": [
    {{"id": "latin_snake_case", "name": "<name in {lang}>", "gender": "m"|"f",
      "variants": ["<other spellings seen in the source>"],
      "appearance_en": "<25-45 words, English, for an image generator: age, build,
                        hair colour and style, eye colour, skin, clothing, one distinctive mark>"}}
  ],
  "locations": [
    {{"id": "latin_snake_case", "name": "<name in {lang}>",{loc_variants}
      "look_en": "<25-45 words, English, for an image generator>"}}
  ]
}}

RULES:
- gender is REQUIRED for every character. {gender_why}
{variants_rule}- Include every character who acts anywhere, and every distinct place.
- appearance_en and look_en must be concrete and visual. No personality, no backstory.
- appearance_en describes the character AS THEY LOOK AT THE VERY START of the story,
  before anything in it has happened to them. Never write a state the story gives them
  later: not "restored arm", not "healed scar", not "reborn", not "now stronger",
  not "later wears". If the character starts crippled, blind, burned, ragged or missing
  a limb, that IS the appearance and you must write it plainly — the reference picture is
  drawn from these words and is used for the whole video, including the opening minutes.

SOURCE:
{source}
"""

SCENES_PROMPT = """You split a source text into scenes for a manhwa retelling video.

Return ONE JSON object and nothing else:
{{
  "scenes": [
    {{"num": 1, "title": "<short, in {lang}>",
      "starts_with": "<COPY the first 6-10 words of this scene from the SOURCE, EXACTLY,
                      character for character, so a program can find them>",
      "share": <what fraction of the finished video this scene is worth, 0.0-1.0>,
      "who": ["character id"], "where": "location id",
      "what": "<one sentence, {lang}>",
      "role": "cold_open" | "middle" | "cliffhanger"}}
  ]
}}

RULES:
- Between {min_scenes} and {max_scenes} scenes.
- Scenes follow the source in order and cover it completely.
- "starts_with" must be copied VERBATIM from the source. Do not paraphrase it,
  do not translate it, do not fix its errors. A program searches for this string.
- Scene 1 starts at the very beginning of the source, so its "starts_with" is the
  first words of the source.
- shares must add up to about 1.0.
- First scene role is "cold_open", last is "cliffhanger", the rest "middle".

KNOWN CHARACTERS: {chars}
KNOWN PLACES: {places}

SOURCE:
{source}
"""


def _find_anchor(source: str, anchor: str, search_from: int) -> int:
    """Ищем якорь сцены в источнике. Модель могла слегка исказить — тогда
    ищем по первым словам, потом нечётко. Смещения считает КОД, не модель."""
    a = (anchor or "").strip()
    if not a:
        return -1
    pos = source.find(a, search_from)
    if pos >= 0:
        return pos
    words = a.split()
    for n in (8, 6, 5, 4, 3):
        if len(words) >= n:
            probe = " ".join(words[:n])
            pos = source.find(probe, search_from)
            if pos >= 0:
                return pos
    import difflib
    tail = source[search_from:search_from + 20000]
    probe = " ".join(words[:5]).lower()
    best, best_at = 0.0, -1
    step = max(1, len(probe) // 2)
    for k in range(0, max(1, len(tail) - len(probe)), step):
        chunk = tail[k:k + len(probe)].lower()
        r = difflib.SequenceMatcher(None, probe, chunk).ratio()
        if r > best:
            best, best_at = r, k
    return search_from + best_at if best >= 0.72 else -1


def scene_count_range(source: str, target_words: int) -> tuple[int, int]:
    """Keep normal 8–14 scenes; short narration/source cannot sustain that many."""
    capacity = max(1, min((max(1, target_words) + 119) // 120,
                          max(1, len(source) // 200)))
    return min(8, capacity, max(1, target_words // 120)), min(14, capacity)


def allocate_words(shares: List[float], target_words: int) -> List[int]:
    """Positive scene budgets whose total equals the requested narration budget."""
    if not shares:
        return []
    if target_words < len(shares):
        raise ValueError("Число сцен превышает бюджет слов")
    weights = [max(0.01, float(x)) for x in shares]
    remaining = target_words - len(weights)
    exact = [remaining * x / sum(weights) for x in weights]
    budgets = [1 + int(x) for x in exact]
    for idx in sorted(range(len(exact)), key=lambda i: exact[i] - int(exact[i]), reverse=True)[:target_words - sum(budgets)]:
        budgets[idx] += 1
    return budgets


def build_skeleton(source: str, target_words: int, lang: str = "Russian",
                   model: str = "claude:sonnet", min_scenes: Optional[int] = None,
                   max_scenes: Optional[int] = None, retries: int = 2, log=print) -> Skeleton:
    """Два коротких вызова вместо одного длинного: глоссарий и разбивка на сцены.
    Границы сцен модель даёт ЯКОРЯМИ, смещения считает код."""
    default_min, default_max = scene_count_range(source, target_words)
    min_scenes = default_min if min_scenes is None else min_scenes
    max_scenes = max(default_max, min_scenes) if max_scenes is None else max_scenes
    g, ans = call_json(GLOSSARY_PROMPT.format(lang=lang, source=source,
                                             gender_why=LG.gender_why(lang),
                                             variants_rule=LG.variants_rule(lang),
                                             loc_variants=LG.loc_variants(lang)),
                       model=model, timeout=420, retries=1)
    chars = g.get("characters") or []
    locs = g.get("locations") or []
    log(f"  глоссарий: {len(chars)} героев, {len(locs)} мест, {ans.seconds:.0f}s")
    bad = [c.get("id") or c.get("name") for c in chars if c.get("gender") not in ("m", "f")]
    if bad:
        raise LLMError(f"без пола остались герои: {bad}")

    chars_line = ", ".join(f"{c['id']}={c['name']}" for c in chars) or "—"
    places_line = ", ".join(f"{l['id']}={l['name']}" for l in locs) or "—"

    last = ""
    for attempt in range(1, retries + 2):
        d, ans2 = call_json(SCENES_PROMPT.format(
            lang=lang, min_scenes=min_scenes, max_scenes=max_scenes,
            chars=chars_line, places=places_line, source=source),
            model=model, timeout=420, retries=0)
        raw = d.get("scenes") or []
        log(f"  сцены, попытка {attempt}: модель дала {len(raw)}, {ans2.seconds:.0f}s")

        scenes: List[Scene] = []
        cursor = 0
        lost = []
        for i, sc in enumerate(raw):
            pos = 0 if i == 0 else _find_anchor(source, sc.get("starts_with", ""), cursor)
            if pos < 0:
                lost.append(sc.get("num", i + 1))
                continue
            cursor = max(cursor, pos)
            scenes.append(Scene(num=len(scenes) + 1, title=sc.get("title", ""),
                                src_from=pos, src_to=0,
                                budget_words=0, who=sc.get("who") or [],
                                where=sc.get("where", ""), what=sc.get("what", ""),
                                role=sc.get("role", "middle"),
                                budget_share=float(sc.get("share") or 0.0)))
        # смещения и бюджеты считает КОД: границы встык, покрытие полное
        for i, sc in enumerate(scenes):
            sc.src_to = scenes[i + 1].src_from if i + 1 < len(scenes) else len(source)
        if scenes:
            scenes[0].src_from = 0
            scenes[0].role = "cold_open"
            scenes[-1].role = "cliffhanger"
        shares = [max(0.01, s.budget_share) for s in scenes]
        for s_, budget in zip(scenes, allocate_words(shares, target_words)):
            s_.budget_words = budget

        sk = Skeleton(characters=chars, locations=locs, scenes=scenes)
        ok, why = check_skeleton(sk, source, target_words, min_scenes, max_scenes)
        if lost:
            log(f"  ! якорь не найден у сцен {lost} — они выброшены, соседние расширены")
        if ok:
            log(f"  скелет принят: {len(scenes)} сцен, покрытие источника полное")
            return sk
        last = why
        log(f"  скелет отклонён: {why}")
    raise LLMError(f"скелет не прошёл шлюз после {retries+1} попыток: {last}")


def check_skeleton(sk: Skeleton, source: str, target_words: int,
                   min_scenes: int, max_scenes: int) -> tuple:
    """Шлюз — арифметика, а не мнение."""
    if not sk.scenes:
        return False, "сцен нет"
    if not (min_scenes <= len(sk.scenes) <= max_scenes):
        return False, f"сцен {len(sk.scenes)}, надо {min_scenes}-{max_scenes}"
    for c in sk.characters:
        if c.get("gender") not in ("m", "f"):
            return False, f"у героя {c.get('id') or c.get('name')} не проставлен пол"
    ss = sorted(sk.scenes, key=lambda s: s.num)
    invalid = [s.num for s in ss if not 0 <= s.src_from < s.src_to <= len(source)]
    if invalid:
        return False, f"неверные или нулевые границы сцен: {invalid}"
    # смещения и бюджеты теперь считает код, поэтому здесь только страховка
    assert ss[0].src_from == 0 and ss[-1].src_to == len(source), "код посчитал границы неверно"
    for i in range(1, len(ss)):
        assert ss[i].src_from == ss[i - 1].src_to, "код оставил дыру между сценами"
    empty = [s.num for s in ss if not source[s.src_from:s.src_to].strip()]
    if empty:
        return False, f"пустые сцены: {empty}"
    # Short sources need proportionally shorter scenes. Keep the original 200-char
    # guard for long sources, while allowing one brief opening or closing fragment.
    minimum_span = min(200, len(source) // (3 * len(ss)))
    short = [s.num for s in ss if s.src_to - s.src_from < minimum_span]
    if len(short) > max(1, len(ss) // 3):
        return False, f"слишком много непропорционально коротких сцен: {short}"
    if len(ss) > 1 and ss[0].role != "cold_open":
        return False, "первая сцена не помечена как холодный старт"
    if ss[-1].role != "cliffhanger":
        return False, "последняя сцена не помечена как обрыв"
    return True, ""


# ---------------------------------------------------------------- проход 2

SCENE_PROMPT = """{style}

===========================================================
GLOSSARY — use these exact spellings, never invent variants
===========================================================
CHARACTERS:
{chars}
PLACES:
{places}

===========================================================
YOUR SCENE
===========================================================
Scene {num} of {total}. Role: {role}.
{role_note}
What happens: {what}
On screen: {who}
Place: {where}

Previous scene ended with: {prev}
Next scene will begin with: {nxt}

WORD TARGET: {budget} words. Reach it — {ratio}

LANGUAGE AND RETELLING. The source may be in another language, for example English
subtitles of someone else's video. Write the narration in {lang} only, whatever language
the source or the style guide above mentions. Retell the events in your own words as a
new narration: do not translate the source sentence by sentence and do not keep its phrasing.

SOURCE FOR THIS SCENE:
{src}

Output ONLY the {lang} narration for this scene. No title, no notes, no markdown.

DIRECT SPEECH. Recap videos in this niche are built on the characters' own lines: the
narrator quotes them. Where the source has a character speaking, keep ONE short line of
direct speech in {lang} (3–12 words) in quotation marks {quotes}, attributed with a verb of
speech ({said}). Not every beat needs it — two or three lines per scene,
at the moments that matter. Never invent lines the source does not imply.
"""

_WORDS_EN = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen']


def hook_corridor(wpm=None):
    """Коридор длины фраз хука (от, до) под скорость голоса: фраза звучит около 2,8 секунды.

    Один на двоих: его зовут и записка первой сцены (cold_open_note), и приёмка хука
    (hookqa). 27.09 они разошлись — записка просила русскому 4–7 слов, а приёмка считала
    хорошими 5–9 и браковала фразы, которые студия сама заказала.
    """
    try:
        темп = float(wpm or 0)
    except (TypeError, ValueError):
        темп = 0.0
    if темп <= 0:
        темп = 135.0
    цель = 2.8 * темп / 60.0
    lo = max(4, int(round(цель * 0.7)))
    hi = max(lo + 2, int(round(цель * 1.3)))
    return lo, hi


def cold_open_note(wpm=None) -> str:
    """Записка первой сцены с коридором длины фраз под скорость голоса.

    Кадр — это одна фраза, поэтому темп смены картинок задаёт длина фразы, а не бюджет
    кадра. Русский голос читает 119 слов в минуту, испанский 152: одна и та же фраза
    в девять слов звучит 4,5 и 3,6 секунды. Замер 26.09 по волне 4: у испанского эталона
    11 картинок за первые 30 секунд, у трёх русских — восемь.

    Целимся в фразу длиной около 2,8 секунды звучания.
    """
    lo, hi = hook_corridor(wpm)
    словом = lambda n: _WORDS_EN[n] if 0 <= n < len(_WORDS_EN) else str(n)
    return ROLE_NOTES["cold_open"].format(lo=словом(lo), hi=словом(hi))


# 27.09.2026 tekst-huka (аудит 2, «Хук и первые 2–5 минут»): донор звал героиню «старшей
# сестрой», хук — «женщиной», и три набора картинок отдали роль трём неверным героиням;
# будущее отравление вышло действием в гостиной. Люди — ролью; будущее — замыслом
# («собиралась», «должна была»), не выходя из прошедшего времени правила 6.
# 28.09.2026 geroi (проба 9f8b8a20): «остальных — ролью» и «одно слово-роль на весь хук» подтолкнули
# модель дать слово-роль и самому герою — «Герой» 6 и 9 раз в 2 прогонах из 4. Сам герой роли и
# метки не получает: в фразах 1–10 местоимение, дальше местоимение или имя; имя — только у героя.
# 27.09.2026 povtor-huka (effort_ab, 693da668): хук показал событие сцены целиком, а со второго
# абзаца то же событие рассказано заново; проба 180b590d — заново в конце сцены, добирая объём.
# Хук — один раз, дальше вперёд; объём — тем, чего хук не показал; срок отступа — из источника.
# 28.09 verify_2 (91afade8, 180b590d): хук взят из конца сцены — модель доходила до него и
# пересказывала его в конце, принимая за показ обещания фразы 10. Теперь момент хука дальше —
# одной фразой, а если им сцена кончается — этой фразой сцена и заканчивается.
# 28.09 verify3_1 (460f2b0b, claude-sonnet-5): фразы 11–30 пересказали весь ужин по порядку, а
# после 30-й ужин рассказан заново — «CONDENSE: keep every event» и цель по словам перевесили.
# Теперь 11–30 — ОДИН момент, не сводка сцены; показанное в хуке считается сохранённым (и в
# строке WORD TARGET); сцена-одиночка кончается этой одной фразой, и это же её открытый конец.
# 28.09.2026 hookv2 (hookcomp, слепые судьи): хук по ячейкам («вопрос / цитата / число / взгляд вперёд»
# на фиксированных местах, все 30 фраз по 4–9 слов) судьи узнавали как шаблон: 2–3 из 10 против 6–8 у
# конкурентов. Записка v2: открытие — как у источника; к 12-й фразе рассказчик сам говорит, про что
# история (у конкурентов суть звучит в первые ~45 с у 17 из 23); отношение рассказчика внутри фраз;
# длина фраз разная, как в речи (длинные — из двух половин: beats.py режет фразу длиннее 14 слов на
# две картинки). Проба v2 (5 прогонов): картинок за 30 с 10–11, суть к 45 с у всех; судьи — 4–6 из 10.
# v2.1 — правки по судьям v2 без новой пробы: ставка донора с числами в первых двух фразах, событие
# хука один раз и по порядку, срок шага назад только из источника, примеры реакций убраны (их
# переводили дословно: «Pobre Gabriella», «Vaya bienvenida»), кальки, справочные карточки, имя не
# больше трёх раз и без фразы-«разоблачения». HOOK_KEPT — все тридцать фраз хука, не только 11–30.
HOOK_KEPT = ("In this first scene the events the hook shows in its thirty sentences are "
             "already kept: never tell them again to keep them or to reach the target.")
ONE_SCENE_END = "If the hook's moment is where this scene ends, its one sentence is that unresolved end."
ROLE_NOTES = {
    "cold_open": (
        'This is the FIRST scene of the video — the HOOK. For this scene only, style rules 1, 3, 4, 7 and 8 do not apply; rules 2, 5, 6, 9, 10 still do. The first thirty sentences of this scene are the hook. It must sound like a narrator retelling a story he loves to a friend — with his own attitude, in everyday spoken words, in sentences of different lengths — not like a trailer, not like a report, not like a row of captions.\n'
        '\n'
        'HOW TO OPEN. Keep the device the source itself opens with, in your own words; skip any greeting, channel talk or review in it. One of three:\n'
        '- The source opens on a later moment — a fight, a death, a victory, a humiliation — and then goes back: open inside that ONE moment and give only its sharpest beats, six to eight sentences at most, never a summary of the scene.\n'
        '- The source opens by presenting the hero — his strangest deed, his secret, the twist of his fate: give that deed in one or two sentences, then what makes it strange.\n'
        '- Anything else: go straight in. Sentence one is the first striking thing that happens in this scene. If the source begins with the rules of its world, keep at most two sentences of them, each showing what the rule does to people, and bring the hero in by sentence three.\n'
        "Whichever it is, when the source's first lines set the stake — who is thrown in, how many, how many can come out alive, what the loser loses — the first two sentences keep that stake with its numbers, as what is happening to these people: a stake is not a rule in general words. Inside the opening moment keep the order in which things happened: once someone has arrived, fallen or won, the hook never shows him again still on the way, still fighting or still standing.\n"
        '\n'
        "WHAT THIS STORY IS ABOUT. By sentence twelve at the latest the narrator steps out of the moment and says plainly, in his own words and in two to four sentences, what this story is about: what is unusual about the hero's situation, who the hero really is, what stands against him — the count's daughter who knows how the novel ends, the rankless porter who remembers his first life, the man reborn into his killer's family. If the source's narrator names what changes everything — a meeting, a gift, a discovery — say that too, in one sentence. Say it once in the whole scene: after that neither the hook nor the rest of the scene says it again, even in other words. When a step back follows, say here only what is strange about the hero now; who he was and how he lived before belong to the step back and are told there, once. Take it only from this scene's source and the line 'Next scene will begin with', never guessing beyond them; what only that line gives, name in one sentence and leave how it came about to the scene that tells it. Then, if this scene's source goes back in time, step back once, in one sentence, measured only as the source measures it ('a few hours earlier', 'ten years before') — if the source gives no measure, give none, only this language's plain word for 'before that' — never by an event the viewer has not been told of, and move forward from there without saying again anything the narrator has already said. When the step back catches up with the moment the hook opened inside, that moment gets one bare sentence at most — none of its images, lights, voices or lines again — and the story goes on past it. If the source does not go back, go back into the moment and carry it on to what happened next in it, never showing again what the opening showed. End the hook on what sets the story moving, as the source gives it — what the hero now wants, fears or is about to face — never on a bare arrival somewhere or a routine act.\n"
        '\n'
        'EVERY HOOK.\n'
        '- Sentence one is {lo} to {hi} words: something happening or something the hero did. Never a place phrase — no preposition of place followed by the name of a room, house, city or building, in any language; a place may be named inside a sentence, never instead of one. Never a question, never a bare line of speech, never a rule in general words — a rule of the world is shown by what it does to someone.\n'
        '- Inside the moment and after the step back, tell cause and effect: each sentence follows from the one before, and something new happens at least every three sentences. Every sentence adds something: never say again, in other words, what an earlier sentence already said. Each event happens once in the hook: an arrival, a fall, a death, a defeat, a screen, a light or a voice already shown is never shown again, not from another side and not as a reaction. No sentence may describe furniture, clothing, or a pose held still.\n'
        '- Spend sentences where the source is most striking: a fight, a kill, a humiliation gets several sentences, beat by beat; everyday routine — walking in, sitting down, a machine switching on, hands on keys — gets one sentence at most.\n'
        "- Nothing the source does not give. Every object, wound, word and event that matters in the story comes from the source — never a letter, contract, ring, scar or promise it does not mention. Stage detail — rain, light, a hand, a step — only shows what the source says; it never adds a fact. Name the weather, the light or the place once and move on. An object or deed the source gives is never dropped in bare: say in the same sentence what it means to someone, or leave it out. Never stop the story to list a person's titles, posts or full name: one role word is enough.\n"
        '- Sound like telling. Join sentences with the small words of telling in this language — but, then, so, when, until, because, even, only, still: about one sentence in three leans on the one before, and many sentences start with something other than the one who acted.\n'
        "- Say it as a native speaker of this language says it aloud. Never carry an English idiom, game term or tech term over word for word: say what it means in the everyday words of this language — not 'the screen stayed without patches' but that the makers never fixed the game. If a phrase would puzzle a listener, say it simpler. A glossary entry that is a description rather than a proper name is written as ordinary words fitted to the sentence, lowercase, never pasted in as a title. No descriptive word — 'strange', 'unfamiliar', 'wet' and their like — comes back more than twice in the hook.\n"
        '\n'
        "THE NARRATOR'S VOICE. The narrator has an attitude to what he tells — sympathy, irony, surprise — and it lives inside the sentences that show things: a wry word, an unfair comparison, an understatement that still names what happened. Two to four times in the hook. At most one of them may stand alone, as an aside of two or three words right after the event it answers, riding on its picture; the others sit inside sentences that also show something. A reaction comes only after the viewer has seen what it answers; it never retells that event in other words and never adds a fact — only how the narrator sees it. Find its words in what just happened: never two reactions built alike or starting with the same word, and never one that opens like a stock exclamation of this language — 'Вот', 'Vaya', 'Menudo', 'Qué', 'Pobre', 'What a', 'Poor' and their like. Reactions keep rules 6 and 9: no 'I', 'we', 'let me' or 'you', no question to the viewer, no 'imagine' or 'interestingly', no word about the story as something told or drawn (chapter, episode, author, 'the story begins'), and the past tense wherever there is a verb.\n"
        '\n'
        'RHYTHM. Vary the length the way speech does. Length also sets the pace of the pictures: a sentence of up to fourteen words is one picture; a longer one is cut into two pictures at a comma near its middle.\n'
        '- Most sentences are {lo} to {hi} words — one picture each.\n'
        '- A few are two or three words: a punch, or the one aside, riding on the picture before it.\n'
        "- Four to eight are long, fifteen to twenty words, where the narrator explains or something turns. Build each long sentence as two halves of about equal length with a comma at the joint — ', but', ', when', ', because', ', until', ', so', ', while' — so that each half is a picture; a long sentence with no comma near its middle stays one picture far too long. Spread them through the hook: after sentence ten, never more than six sentences in a row without a long one.\n"
        '- Keep out of the middle: a sentence of more than {hi} but fewer than fifteen words is one picture held too long. At most three such sentences in the hook.\n'
        '- In the first ten sentences every sentence is {lo} to {hi} words, except at most one long one and one punch: this is where the pictures must change fastest.\n'
        '- Never four sentences in a row of one length; never three in a row starting with the same word or name.\n'
        '- Before you answer, count the long sentences of the hook. Fewer than four reads as a row of captions: join two neighbouring sentences that belong together into one long sentence with a comma at the joint, until there are four.\n'
        '\n'
        "ONLY IF THE SOURCE HAS IT — each at most once, where it happens, never at a fixed place, never invented to fill a slot: a number that matters to the hero (an age, a count, years, a deadline); one line someone says at that moment, with its speaker — the hero need not speak; one question, only a real puzzle the source raises, answered soon after — never 'will he manage', 'will he survive', 'what will become of'; one look ahead, only where the source's narrator looks ahead, tied to what just happened. Numbers are words; a rank or grade written in Latin letters — an SS rank, an F grade — is spelled out as the narrator says it in this language, never left as capital letters.\n"
        '\n'
        "PEOPLE. No proper names in the first ten sentences. Bring the hero in by what he is in this story, as the source gives it — the count's youngest son, the rankless porter, the scrawny newcomer — once or twice, then by a pronoun of this language. His name may come from sentence eleven on; the first time, put it in a sentence that also moves the story — his name beside what the viewer already knows of him and something new he does or is — never a sentence whose only job is to reveal who that was, and never a bare name the viewer cannot place. After that a pronoun or his name, the name wherever a pronoun could mean someone else, and at most three times in the hook. The hero never gets a label: never 'the hero', 'the heroine', 'the protagonist', 'our hero', 'our girl', 'our boy', 'the main character' or 'the MC', nor any of them in this language. No one but the hero is named in the hook, however often the source names the others — not even the one closest to him: every other person keeps one role or relation the source gives them — his elder sister, the maid, her husband, the rival mage — with a pronoun wherever it is clear; never coin a nickname the source does not use. Never call a person of the story — the hero included, even in the narrator's asides — by a bare word for a woman, a man, a girl, a boy, a guy, a young one, a child, an old woman or a person, nor 'a woman's hand' or 'a male voice'.\n"
        '\n'
        "TIME. Keep the past tense of rule 6. What has not happened yet is told as a plan, a threat or what someone knows is coming — 'was going to', 'would', 'was to' — naming who plans it, never as an action happening in the room right now; what happened before this scene is told as already done. The hook jumps in time at most once: the step back above.\n"
        '\n'
        "AFTER THE HOOK. From sentence thirty-one to the end of the scene, return to ten-to-fourteen word sentences. The hook is told once. A later moment you opened inside is spent and counts as kept, in the middle of the scene and at its end alike: when the story reaches it again, give it exactly one sentence — the bare event, with none of the images, gestures, wounds, lights or lines the hook used, never 'as already shown' — and go on to what came after it; if that moment is where this scene ends, that sentence is the LAST sentence of the scene. What the narrator said about what this story is about is spent too: never say it again. A deed you opened with was only named: when the scene reaches it, show it once. Reach the word target with what the hook did not show — what led up to its moment and what followed — never by telling it again. Before you answer, hold every sentence after thirty against the hook: cut every beat it already showed, and wherever several sentences show its moment again, replace them with that one sentence."
    ),
    "cliffhanger": "This is the LAST scene. End mid-tension, on an unresolved question. "
                   "Do not wrap the story up, do not summarise, do not thank anyone.",
    "middle": "Continue the story. Do not recap what came before.",
}


def _fmt_chars(sk: Skeleton, lang: str = "Russian") -> str:
    lb = LG.labels(lang)
    out = []
    for c in sk.characters:
        v = ", ".join(c.get("variants") or [])
        out.append(f"  - {c.get('name')} ({lb['m'] if c.get('gender')=='m' else lb['f']})"
                   + (f"; {lb['variants']}: {v}" if v else ""))
    return "\n".join(out) or f"  {lb['none']}"


def _fmt_places(sk: Skeleton, lang: str = "Russian") -> str:
    return "\n".join(f"  - {l.get('name')}" for l in sk.locations) or f"  {LG.labels(lang)['none']}"


def length_note(budget_words: int, source_words: int) -> str:
    """Во сколько раз текст сцены длиннее или короче её куска источника.
    Субтитры трёхчасового ролика под сорокаминутный рекап — это сжатие, а не разворачивание."""
    ratio = budget_words / max(1, source_words)
    if ratio >= 1:
        return (f"you are expanding this compressed source roughly {round(ratio, 1)} times. "
                "Expand with concrete stage detail only.")
    return (f"this source is roughly {round(1 / ratio, 1)} times LONGER than your target, so CONDENSE it: "
            "keep every event that moves the story, drop repetition, filler and side remarks. "
            "This overrides the expansion note in the style guide.")


def scene_prompt(sk: Skeleton, i: int, source: str, lang: str = "Russian",
                 budget: Optional[int] = None, wpm: Optional[float] = None) -> str:
    """Промпт сцены i: глоссарий, соседние сцены, бюджет слов (или свой budget), кусок источника."""
    chars, places = _fmt_chars(sk, lang), _fmt_places(sk, lang)
    lb = LG.labels(lang)
    # style_prompt.txt написан для русского (три упоминания «Russian»): для другого
    # языка подставляем его имя, иначе промпт требует сразу два языка ответа.
    style = STYLE if lang == "Russian" else STYLE.replace("Russian", lang)
    total = len(sk.scenes)
    by_id = {c.get("id"): c.get("name") for c in sk.characters}
    loc_id = {l.get("id"): l.get("name") for l in sk.locations}
    s = sk.scenes[i]
    src = source[s.src_from:s.src_to]
    budget = s.budget_words if budget is None else budget
    ratio = length_note(budget, len(src.split()))
    if total == 1 or s.role == "cold_open":
        ratio += " " + HOOK_KEPT        # povtor-huka: «keep every event» не про события хука
    return SCENE_PROMPT.format(
        style=style, chars=chars, places=places, num=s.num, total=total,
        role=s.role, role_note=(cold_open_note(wpm) + " " + ROLE_NOTES["cliffhanger"] + " " + ONE_SCENE_END
                                if total == 1
                                else (cold_open_note(wpm) if s.role == "cold_open"
                                      else ROLE_NOTES.get(s.role, ROLE_NOTES["middle"]))),
        what=s.what, who=", ".join(by_id.get(w, w) for w in s.who) or "—",
        where=loc_id.get(s.where, s.where) or "—",
        prev=(sk.scenes[i - 1].what if i > 0 else lb["start"]),
        nxt=(sk.scenes[i + 1].what if i + 1 < total else lb["end"]),
        budget=budget, ratio=ratio, src=src, lang=lang,
        quotes=LG.quotes(lang), said=LG.said(lang))


# 28.09.2026: первую сцену (хук) пишет Codex. Записка cold_open длинная и строгая: claude-sonnet-5
# на ней думал 6–14 мин и в 3 прогонах из 4 упёрся в предел вывода 32000 — хук терялся; Codex
# пишет ту же сцену за 3–4 мин без ошибок. Остальные сцены — модель ролика, как раньше.
# MANGA_HOOK_MODEL задаёт модель хука явно; пустое значение — выключить (хук пишет модель ролика).
_hm = os.environ.get("MANGA_HOOK_MODEL")
HOOK_MODEL = _hm.strip() if _hm is not None else next(
    (m.strip() for m in os.environ.get("MANGA_TEXT_MODELS", "").split(",") if m.strip().startswith("codex:")), "")


def _scene_model(sk: "Skeleton", i: int, model: str) -> str:
    """Модель для сцены i: хук (первая сцена или единственная) — HOOK_MODEL, прочие — модель ролика."""
    if HOOK_MODEL and (sk.scenes[i].role == "cold_open" or len(sk.scenes) == 1):
        return HOOK_MODEL
    return model


def _hook_call(prompt: str, m: str, model: str) -> "object":
    """Вызов модели сцены. Если это модель хука вместо модели ролика — без ожидания сброса недельного
    окна Codex (иначе сцена висела бы до сброса, до суток и больше) и с откатом на модель ролика."""
    if m == model:
        return call(prompt, model=model, timeout=600, retries=1)
    try:
        return call(prompt, model=m, timeout=600, retries=1, wait_reset=False)
    except CP.JobGone:
        raise
    except Exception as e:
        _log_hook(f"  хук: {m} не ответил ({type(e).__name__}: {str(e)[:120]}) — пишу моделью ролика {model}")
        return call(prompt, model=model, timeout=600, retries=1)


def _log_hook(text: str) -> None:
    try:
        CP.log.warning(text)
    except Exception:  # noqa: BLE001 — журнал не должен ронять сцену
        print(text, flush=True)


# 29.09.2026 meta (эталон 54706a7d, испанский, claude-sonnet-5): сцены 2 и 13 начались служебной строкой
# модели («Good, 858 words — within range of the target. Here is the final narration:»), она ушла в
# final.txt и в кадры — голос прочитал бы её; strip_fence её не снимает. Строка убирается, только если
# служебная КАЖДАЯ её фраза: английская пометка про объём, цель, проверки, «Here is the … narration:»,
# «Draft:», «Word count» при языке ролика не английском (_META_EN, строка только латиницей); на любом
# языке — явная «N words» + цель/narration (_META_ANY). Реплики и строки длиннее 40 слов не трогаются;
# «the narration reveals…», «he uses maybe 10 words», «within range» у лучника — не пометка (корпус source.txt).
_META_ANY = re.compile(r"\b\d[\d,.]*\s+words?\b.*\b(?:target|narration|within range|word[- ]?count|ceiling)\b"
                       r"|\b(?:target|narration)\b.*\b\d[\d,.]*\s+words?\b|^[~≈]?\d[\d,.]*\s+words?\W*$"
                       r"|^word[- ]?count\b\W*[~≈]?\d|\bsentence[- ]length checks?\b|\bwithin the [\w-]+-word ceiling\b"
                       r"|\bhere(?:'s|’s| is)\b[^.!?]*\bnarration\b[^.!?]*:$", re.I)
_META_EN = re.compile(r"\b\d[\d,.]*\s+words?\b.*\b(?:range|limit|budget|total|goal|within|matching|requested)\b"
                      r"|\b(?:goal|total|budget)\b.*\b\d[\d,.]*\s+words?\b"
                      r"|\bword[- ]?count\b|\b(?:length|style|sentence|all) checks?\b"
                      r"|\bwithin (?:the )?(?:target|budget|word (?:range|limit))\b"
                      r"|\b(?:matching|meets) (?:the )?target\b|\b(?:here|below)(?:'s|’s| is| are)\b[^.!?]*\b(?:text|"
                      r"version|draft|scene|rewrite|revision)\b[^.!?]*:$|\bhere(?:'s|’s| is) (?:the|my) (?:final|"
                      r"revised|rewritten|expanded|condensed|updated|new|full)\b[^.!?]*\b(?:text|version|draft|scene)\b"
                      r"|^(?:(?:final|revised|new|full) )?(?:draft|narration|text|version)\s*:$", re.I)


def meta_line(line: str, lang: str = "Russian") -> bool:
    """Строка — служебная пометка модели целиком, а не текст ролика (см. _META_ANY/_META_EN)."""
    t = line.strip().strip("*_#>`[]()- ").strip()
    if not t or t[0] in "«»\"„“”—–" or len(t.split()) > 40:
        return False
    en = (lang or "").strip() != "English" and not re.search(r"[^\x00-\x7f—–…’‘“”]", t)
    if en:   # «Good, 858 words …», «Okay. Here is …»: вступительное слово само по себе не пометка
        t = re.sub(r"^(?:ok(?:ay)?|good|great|done|sure|alright|perfect)\b\W*", "", t, flags=re.I)
    return bool(t) and all(_META_ANY.search(ф) or (en and _META_EN.search(ф))
                           for ф in re.split(r"(?<=[.!?])\s+|(?<=:)\s+(?=\D)", t) if ф)


def strip_meta(text: str, lang: str = "Russian"):
    """(текст без служебных строк модели, [убранные строки]); нет таких строк — текст как был."""
    lines = text.split("\n")
    убрано = [l.strip() for l in lines if meta_line(l, lang)]
    if not убрано:
        return text, []
    left = "\n".join(l for l in lines if not meta_line(l, lang)).strip()
    return re.sub(r"\A(?:[-*_=~]{3,}\s*)+|(?:\s*[-*_=~]{3,})+\Z", "", left).strip(), убрано   # «---» у края


def _volume_status(s: Scene) -> str:
    lo, hi = s.budget_words * 0.65, s.budget_words * 1.45
    return "готово" if lo <= s.words <= hi else "объём мимо"


def expand_scenes(sk: Skeleton, source: str, lang: str = "Russian",
                  model: str = "claude:sonnet", workers: int = 4, log=print,
                  on_scene=None, wpm: Optional[float] = None) -> Skeleton:
    """Сцена с текстом (не «ошибка») уже написана прошлым прогоном — её не заказываем снова.
    on_scene(s) зовётся после каждой новой сцены: шаг сохраняет написанное сразу."""
    total = len(sk.scenes)

    def one(i: int):
        s = sk.scenes[i]
        if (s.text or "").strip() and s.status != "ошибка":
            return
        prompt = scene_prompt(sk, i, source, lang, wpm=wpm)
        try:
            a = _hook_call(prompt, _scene_model(sk, i, model), model)   # 28.09.2026: хук — Codex
            from llm import strip_fence
            s.text, убрано = strip_meta(strip_fence(a.text), lang)   # 29.09.2026 meta
            if убрано:
                log(f"  сцена {s.num}: убрана служебная строка модели: {' | '.join(убрано)[:300]}")
            s.words = len(s.text.split())
            s.seconds = round(a.seconds, 1)
            s.model = a.model
            s.status = _volume_status(s)
        except CP.JobGone:
            raise  # задача ушла: сцены не трогаем, шаг остановится
        except Exception as e:
            s.status = "ошибка"
            s.error = f"{type(e).__name__}: {e}"[:200]
        log(f"  сцена {s.num}/{total}: {s.status}, {s.words} слов "
            f"(цель {s.budget_words}), {s.seconds}s")
        if on_scene is not None:
            on_scene(s)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(CP.carry(one), range(total)))
    return sk


# ---------------------------------------------------------------- длина по темпу голоса

FIT_NOTE = """
===========================================================
YOUR DRAFT OF THIS SCENE ({words} words)
===========================================================
{draft}

LENGTH FIX. The whole video came out too {direction} for the ordered length. Revise the
draft above to {goal} words. {how}
Keep its events, names, places and quoted lines; change only the length.
Output ONLY the revised {lang} narration for this scene. No title, no notes, no markdown.
"""
FIT_LONGER = ("Add concrete stage detail and moments from the source that the draft skipped. "
              "Do not invent events.")
FIT_SHORTER = "Condense: drop repetition, filler and side remarks; keep every event that moves the story."


def length_tolerance() -> Optional[float]:
    """MANGA_LENGTH_FIX — допуск длины ролика в процентах (например 10 → ±10%).
    Пусто, 0 или off — правки длины нет, как раньше. Иное — понятная ошибка шага."""
    raw = (os.environ.get("MANGA_LENGTH_FIX") or "").strip().lower()
    if raw in ("", "0", "off"):
        return None
    try:
        value = float(raw.rstrip("%"))
    except ValueError:
        raise ValueError(f"MANGA_LENGTH_FIX: нужен допуск в процентах от 1 до 50 или off, а не {raw!r}") from None
    if not 1 <= value <= 50:
        raise ValueError(f"MANGA_LENGTH_FIX: допуск от 1 до 50 процентов, а не {raw!r}")
    return value / 100


def fit_length(sk: Skeleton, source: str, target_words: int, lang: str = "Russian",
               model: str = "claude:sonnet", tolerance: float = 0.10, workers: int = 4,
               log=print) -> dict:
    """Один круг правки длины: если текст всего ролика вне ±tolerance от бюджета слов
    (бюджет = минуты × настоящий темп голоса), переписать САМЫЕ ДАЛЁКИЕ от своего бюджета
    сцены в нужную сторону — ровно столько сцен, чтобы их поправка закрыла разницу.
    Новый вариант сцены принимается, только если он ближе к цели, чем старый; иначе
    остаётся старый (и это видно в отчёте). Ошибка вызова — старый текст, не падение."""
    total = sum(s.words for s in sk.scenes)
    lo, hi = target_words * (1 - tolerance), target_words * (1 + tolerance)
    # within — уложился ли текст в допуск (после правки); calls — сколько вызовов модели ушло на правку.
    rep = {"target_words": target_words, "tolerance": tolerance, "before": total, "after": total,
           "within": lo <= total <= hi, "calls": 0, "revised": [], "kept": []}
    if rep["within"] or not sk.scenes:
        return rep
    longer = total < lo
    need = abs(target_words - total)
    # Бюджеты сцен приводятся к цели: скелет мог быть посчитан при другом темпе (сохранённый
    # skeleton.json), тогда сумма бюджетов ≠ цели и «дойти до бюджета» не даёт нужной длины.
    k = target_words / max(1, sum(s.budget_words for s in sk.scenes))
    aim = {s.num: s.budget_words * k for s in sk.scenes}
    cands = sorted((s for s in sk.scenes if (s.words < aim[s.num] if longer else s.words > aim[s.num])),
                   key=lambda s: abs(aim[s.num] - s.words), reverse=True)
    picked, gain = [], 0.0
    for s in cands:
        if gain >= need:
            break
        picked.append(s)
        gain += abs(aim[s.num] - s.words)
    if not picked:
        return rep
    # Каждая выбранная сцена сдвигается к своему бюджету на долю need/gain: вместе ровно цель.
    # Предел — 65–145% бюджета сцены (граница «объём мимо»).
    share = min(1.0, need / gain)
    index = {id(s): i for i, s in enumerate(sk.scenes)}
    goals = {}
    for s in picked:
        goal = s.words + (aim[s.num] - s.words) * share
        goals[s.num] = int(round(min(max(goal, aim[s.num] * 0.65), aim[s.num] * 1.45)))
    log(f"  длина: {total} слов при цели {target_words} (±{tolerance * 100:.0f}%) — "
        f"правлю {len(picked)} сцен: {', '.join(str(s.num) for s in picked)}")

    def one(s: Scene):
        goal = goals[s.num]
        prompt = scene_prompt(sk, index[id(s)], source, lang, budget=goal) + FIT_NOTE.format(
            words=s.words, draft=s.text.strip(), direction="short" if longer else "long",
            goal=goal, how=FIT_LONGER if longer else FIT_SHORTER, lang=lang)
        # 28.09.2026: правит та модель, что сцену написала (хук — Codex, если он и писал; после отката — модель ролика)
        fm = _scene_model(sk, index[id(s)], model)
        fm = fm if (not s.model or s.model == fm) else model
        try:
            a = call(prompt, model=fm, timeout=600, retries=1, **({"wait_reset": False} if fm != model else {}))
            from llm import strip_fence
            text, убрано = strip_meta(strip_fence(a.text), lang)   # 29.09.2026 meta
            text = text.strip()
            if убрано:
                log(f"  сцена {s.num}: из правки длины убрана служебная строка модели: "
                    f"{' | '.join(убрано)[:300]}")
        except CP.JobGone:
            raise  # задача ушла (часть B): правку не записываем, шаг остановится
        except Exception as e:
            rep["kept"].append({"scene": s.num, "words": s.words, "goal": goal,
                                "why": f"{type(e).__name__}: {e}"[:200]})
            log(f"  сцена {s.num}: правка длины не удалась ({type(e).__name__}) — оставлен прежний текст")
            return
        words = len(text.split())
        if text and abs(words - goal) < abs(s.words - goal):
            rep["revised"].append({"scene": s.num, "before": s.words, "after": words, "goal": goal})
            log(f"  сцена {s.num}: {s.words} → {words} слов (цель {goal})")
            s.text, s.words = text, words
            s.status = _volume_status(s)
        else:
            rep["kept"].append({"scene": s.num, "words": s.words, "goal": goal, "offered": words,
                                "why": "новый вариант не ближе к цели"})
            log(f"  сцена {s.num}: новый вариант {words} слов не ближе к цели {goal} — оставлен прежний")

    rep["calls"] = len(picked)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(CP.carry(one), picked))   # вызовы правки длины — в журнал Claude этого ролика и шага
    rep["after"] = sum(s.words for s in sk.scenes)
    rep["within"] = lo <= rep["after"] <= hi
    return rep


# ---------------------------------------------------------------- проход 3

import profiles
STOPS = profiles.stops("ru")


def stops_for(lang: str = "Russian") -> Optional[List[str]]:
    """Стоп-лист мета-речи языка из profiles.py (ключ — iso). None — профиля нет:
    проверка мета-речи для этого языка не делается, и отчёт об этом говорит,
    а не прогоняет польский текст через русские слова."""
    code = LG.iso(lang)
    return profiles.stops(code) if code in profiles.PROFILES else None


def assemble_text(sk: Skeleton) -> str:
    parts = [s.text.strip() for s in sorted(sk.scenes, key=lambda x: x.num) if s.text.strip()]
    return "\n\n".join(parts)


def quality(text: str, target_words: int, sk: Optional[Skeleton] = None,
            lang: str = "Russian") -> dict:
    # Конец предложения перед закрывающей кавычкой («…крикнула.” Потом») — тоже конец:
    # прямая речь в польском/чешском идёт в „…”, и без этого две фразы считались одной
    # «длиннее 24 слов». Для русского разбиение прежнее.
    split_at = (r"(?<=[.!?…])\s+" if lang == "Russian"
                else r"(?<=[.!?…])\s+|(?<=[.!?…][»”“\"’])\s+")
    ss = [s.strip() for s in re.split(split_at, text) if s and s.strip()]
    L = sorted(len(s.split()) for s in ss)
    words = len(text.split())
    q = lambda p: L[min(len(L) - 1, int(len(L) * p))] if L else 0
    stop_list = STOPS if lang == "Russian" else stops_for(lang)
    stops = {p: len(re.findall(p, text, flags=re.I)) for p in (stop_list or [])}
    digits = len(re.findall(r"\d", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    cyrillic = len(re.findall(r"[А-Яа-яЁё]", text))
    probs = []
    if words < target_words * 0.85:
        probs.append(f"объём {words} слов, цель {target_words} — недобор больше 15%")
    if words > target_words * 1.30:
        probs.append(f"объём {words} слов, цель {target_words} — перебор больше 30%")
    over = sum(1 for x in L if x > 24)
    if L and over / len(L) > 0.05:
        probs.append(f"предложений длиннее 24 слов: {over} ({over/len(L)*100:.0f}%)")
    if sum(stops.values()) > 3:
        probs.append(f"мета-речь: {sum(stops.values())} попаданий стоп-листа")
    if digits > 5:
        probs.append(f"цифр в тексте: {digits} — для озвучки нужны слова")
    # Чужая азбука: для русского — латиница, для латинских языков — кириллица
    # (русские слова, просочившиеся в польский текст). Латиница в польском — норма.
    if LG.script(lang) == "Cyrillic":
        if latin > words * 0.02:
            probs.append(f"латиницы много: {latin} букв")
    elif cyrillic > words * 0.02:
        probs.append(f"кириллицы много: {cyrillic} букв — в тексте на языке {lang} русские слова")
    return {
        "words": words, "sentences": len(ss),
        "med_sent": st.median(L) if L else 0, "p90_sent": q(0.9),
        "max_sent": L[-1] if L else 0, "over24": over,
        "stops": sum(stops.values()),
        "stops_detail": {k: v for k, v in stops.items() if v},
        "digits": digits, "latin": latin,
        **({} if lang == "Russian" else {"cyrillic": cyrillic, "stops_profile": stop_list is not None}),
        "ok": not probs, "problems": probs,
    }
