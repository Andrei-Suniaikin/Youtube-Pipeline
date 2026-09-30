# -*- coding: utf-8 -*-
"""Паспорт воспоминания — отдельное состояние мира внутри сцены.

Замер: 39–45 кадров ролика помечены как воспоминание, но получали паспорт
сцены настоящего времени — место, свет, состав и эталон ВЗРОСЛОГО героя.
Итог: ребёнок нарисован взрослым, а в чужом воспоминании стояли посторонние.
Ярлык «flashback» в промпте этого не лечит: нужен свой паспорт — где, когда,
кто и в каком возрасте — и свой лист героя-ребёнка.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

from llm import call_json, LLMError
import claude_pool as CP
# общий предел одновременных вызовов модели на службу (MANGA_CLAUDE_CONCURRENCY); без него — прямой вызов
call_json = CP.limited(call_json)

MEMORY_PROMPT = """Below is one scene of a story and the shots inside it that are MEMORIES —
things remembered from an earlier time, not the present. Describe the world of that memory.

Answer in JSON only:
{{"where": "<place of the memory, in English — ONE real place where the remembered event happened; never a blend, dissolve or transition with the place of the present scene>",
  "years_ago": <how many years before the present scene the remembered event happened — a whole number; 0 only if it happened within the last year>,
  "time_of_day": "<...>", "weather": "<... or 'indoor - no weather'>",
  "light": "<one sentence, softer than the present>",
  "palette": "<three or four colours>",
  "who": {{"<character id>": {{"age": "<EXPLICIT for every person: a number ('7 years old',
                                         '35 years old') or a stage with the shift ('newborn',
                                         'infant', 'child about 7', 'teenager about 16',
                                         'adult about 12 years younger than now').
                                         'as now' is allowed ONLY when years_ago is 0>",
                                "look": "<how they look in the memory, 8-15 words>",
                                "costume": "<head to toe>"}}}},
  "absent": ["<character ids who exist in the present scene but are NOT in the memory>"]}}

RULES
- Only people who are actually in the memory go into "who". Everyone else from the
  present goes into "absent" — the illustrator must not draw them.
- If a character is a child in the memory, say so in "age" — this decides which
  reference sheet is used.
- AGE IS NEVER IMPLIED. Every person in "who" gets an explicit age: a number or a stage
  (newborn / infant / child about N / teenager about N / adult about N years younger than
  now). "as now" is allowed only when years_ago is 0. A memory of someone's childhood lies
  ten or twenty years back for EVERYONE in it, the parents included: an adult written
  "as now" is drawn from the present-day reference with its grey hair and wrinkles —
  measured: the mother in a childhood memory came out grey-haired (frame 105).
- A previous life that the hero lived AFTER the present (he died and was reborn into his
  younger self) is not in the past: write "years_ago": 0 and every age as
  "adult about N years older than now" — never "younger", never a number below today's age.
- Resolve every ambiguity; never write "or".

CHARACTERS — use these ids as keys, never names:
{cast}

SCENE {n} TEXT:
{text}

THE MEMORY SHOTS (what happens in each):
{shots}
"""

# Добавка к MEMORY_PROMPT, только когда в сцене несколько отрезков воспоминания.
# Сцена с одним отрезком получает прежний промпт побайтно.
PART_NOTE = """
THIS SCENE HOLDS {total} SEPARATE MEMORIES, split by shots of the present. The shots above
are memory {part} of {total} ONLY. Describe the world of THIS memory: its own place, its own
time and its own people — they may differ from the other memories of the scene. Read the
narration quoted in each shot to see what is remembered; the drawn description after it
was written before the memory was known and may name the wrong place or the wrong person.
"""


@dataclass
class Memory:
    scene: int
    where: str = ""
    time_of_day: str = ""
    weather: str = ""
    light: str = ""
    palette: str = ""
    # Сколько лет назад было воспоминание. Нужно, чтобы для КАЖДОГО взрослого в нём
    # вывести сдвиг возраста: мать во флешбэке детства сына вышла седой (кадр 105),
    # потому что её возраст был «as now», а эталон — сегодняшний.
    years_ago: str = ""
    who: Dict[str, dict] = field(default_factory=dict)
    absent: List[str] = field(default_factory=list)
    # Отрезок воспоминания внутри сцены. Замер 18.09 (часовой ромком, сцена 1):
    # в сцене два воспоминания в разных местах — чек рвут в банкетном зале (кадры
    # 8–9), героя убивают ножом в тёмном переулке (кадры 12–15). Паспорт был один
    # на сцену — «банкетный зал (и мраморный коридор)», и смерть нарисована в зале.
    # part 0 и пустой frames — один паспорт на всю сцену (сцена с одним отрезком,
    # старые memory.json). part 1..N — свой паспорт на каждый непрерывный отрезок.
    part: int = 0
    frames: List[int] = field(default_factory=list)
    # Сегодняшний возраст героя (из appearance_en глоссария: «in his early 20s» → 22).
    # Нужен, чтобы «32 years old» при years_ago=10 у 22-летнего не стало «на 10 лет
    # моложе»: это «прошлая жизнь» перерождённого, она в его будущем (замер 19.09,
    # живой паспорт сцены 1, кадры 12–15). Пусто у старых memory.json — как было.
    now_age: Dict[str, int] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Ключ листа героя в этом воспоминании: mem<сцена> (как было) или mem<сцена>_<отрезок>."""
        return f"mem{self.scene}" if self.part <= 1 else f"mem{self.scene}_{self.part}"

    def constants_block(self) -> str:
        bits = []
        when = " ".join(x for x in (self.time_of_day, self.weather) if x and "no weather" not in x)
        if when:
            bits.append(f"TIME: {when}.")
        if self.light:
            bits.append(f"LIGHT: {self.light}.")
        if self.palette:
            bits.append(f"PALETTE: {self.palette}.")
        return " ".join(bits)

    def wear_block(self, who: List[str], names: Dict[str, str]) -> str:
        out = []
        for cid in who:
            w = self.who.get(cid) or {}
            bits = []
            if w.get("age") and w["age"] != "as now":
                # «is a 35 years old here» — артикль только перед стадией (a child, a teenager)
                age = str(w["age"]).strip()
                art = "" if (age[:1].isdigit() or "younger" in age.lower()
                            or "older" in age.lower()) else "a "
                bits.append(f"is {art}{age} here")
            if w.get("look"):
                bits.append(w["look"])
            if w.get("costume"):
                bits.append(f"wears {modest_costume(w.get('age', ''), w['costume'])}")
            if bits:
                out.append(f"{names.get(cid, cid)} " + ", ".join(bits))
        return ("IN THIS MEMORY: " + "; ".join(out) + ".") if out else ""

    def younger_than_now(self, cid: str) -> bool:
        """False — герой в воспоминании не моложе эталона: «older», или возраст числом
        больше сегодняшнего. Сегодняшний неизвестен — считаем моложе, как было.
        «Сегодняшний» — оценка по «early/mid/late 20s», поэтому сравнение строгое:
        25 лет у героини «late 20s» (27) остаётся «на 2 года моложе» (07d, сцена 7)."""
        w = (self.who or {}).get(cid) or {}
        age = str(w.get("age") or "").strip()
        if re.search(r"\bolder\b", age, re.IGNORECASE):
            return False
        m = re.search(r"(\d+)[\s-]*years?[\s-]+old", age, re.IGNORECASE)
        now = (self.now_age or {}).get(cid)
        if m and now and int(m.group(1)) > now:
            return False
        return True

    def years_younger(self, cid: str) -> Optional[int]:
        """На сколько лет герой моложе эталона в этом воспоминании. None — «как сейчас»."""
        w = (self.who or {}).get(cid) or {}
        age = str(w.get("age") or "").strip()
        if not age or age.lower() == "as now":
            return None
        # «Прошлая жизнь» перерождённого — это его будущее: смерть через 10 лет,
        # возраст «adult about 10 years older than now» (замер 18.09, сцена 1).
        # Сдвига в молодость нет — иначе из years_ago=10 выходило «на 10 лет моложе».
        if not self.younger_than_now(cid):
            return None
        m = re.search(r"(\d+)\s*years?\s+younger", age, re.IGNORECASE)
        if m:
            return int(m.group(1))
        ya = str(self.years_ago or "").strip()
        if ya.isdigit() and int(ya) > 0:
            return int(ya)
        return 0        # сдвиг есть, число неизвестно

    def younger_block(self, who: List[str], names: Dict[str, str]) -> str:
        """Сдвиг возраста — отдельной строкой по каждому, кто в воспоминании моложе.

        Эталон героя — сегодняшний, и без прямого запрета модель переносит с него
        седину и морщины в прошлое: мать в детском воспоминании сына — седая
        (кадр 105). Строка «is a teenager here» этого не лечит: модель меняет
        рост и одежду, но лицо и волосы копирует с картинки как есть.
        """
        out = []
        for cid in who:
            n = self.years_younger(cid)
            if n is None:
                continue
            shift = (f"about {n} years younger than in the reference image" if n
                     else "much younger than in the reference image")
            out.append(f"{names.get(cid, cid)} is {shift}: no grey hair, "
                       f"no wrinkles, the hair colour of youth, a younger face")
        return ("AGE IN THIS MEMORY — " + "; ".join(out) + ".") if out else ""


def _to_id(key: str, by_name: Dict[str, str], ids: set) -> Optional[str]:
    """Имя или id → id. Модель отвечала именами («Shen Buyu», «Fang Shu's mother»),
    и ни один ключ не совпадал с shen_buyu — паспорт молча не работал."""
    k = (key or "").strip()
    if k in ids:
        return k
    low = k.lower().replace("'s", "").replace("’s", "")
    if low in by_name:
        return by_name[low]
    # мягкое совпадение: все слова имени входят в ключ
    for nm, cid in by_name.items():
        if nm and all(w in low for w in nm.split()):
            return cid
    return None


def build(frames: List[dict], scene_texts: Dict[int, str], model: str,
          characters: Optional[List[dict]] = None, log=print,
          workers: int = 1) -> Dict[int, Memory]:
    """frames — кадры раскадровки (словари). Возвращает {сцена или «сцена.отрезок»: Memory}.
    workers > 1 — отрезки воспоминаний спрашиваются одновременно (они друг от друга
    не зависят); 1 — по одной, как было."""
    characters = characters or []
    ids = {str(c.get("id")) for c in characters if c.get("id")}
    by_name = {}
    for c in characters:
        cid = str(c.get("id") or "")
        for nm in (c.get("name", ""), cid.replace("_", " ")):
            if nm:
                by_name[str(nm).lower()] = cid
    cast = "\n".join(f"  - {c.get('id')}: {c.get('name','')} — {(c.get('appearance_en') or '')[:80]}"
                     for c in characters) or "  (unknown)"
    now_age = {str(c.get("id")): a for c in characters
               if c.get("id") and (a := _now_age(c.get("appearance_en") or ""))}
    # Отрезки воспоминаний друг от друга не зависят: при workers > 1 паспорта
    # всех отрезков всех сцен спрашиваются одновременно, порядок ключей прежний.
    items = []
    for n, runs in sorted(_runs(frames).items()):
        for k, shots in enumerate(runs, 1):
            items.append((n, k, len(runs), shots))

    def one(item):
        n, k, total, shots = item
        m = _build_one(n, k if total > 1 else 0, total, shots, cast,
                       scene_texts, model, by_name, ids, log)
        m.now_age = {cid: a for cid, a in now_age.items() if cid in m.who}
        return item, m

    if workers > 1 and len(items) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as ex:
            done = list(ex.map(CP.carry(one), items))
    else:
        done = [one(it) for it in items]
    out: Dict[object, Memory] = {}
    for (n, k, _t, _s), m in done:
        out[n if k == 1 else f"{n}.{k}"] = m
    return out


def _now_age(appearance: str) -> Optional[int]:
    """«Man in his early 20s» → 22, «mid-20s» → 25, «late 50s» → 57, «40s» → 45,
    «35 years old» → 35. Не нашли — None."""
    a = (appearance or "").lower()
    m = re.search(r"(\d+)[\s-]*years?[\s-]+old", a)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(early|mid|late)?[\s-]*(\d)0s\b", a)
    if m:
        return int(m.group(2)) * 10 + {"early": 2, "mid": 5, "late": 7}.get(m.group(1) or "", 5)
    return None


def _runs(frames: List[dict]) -> Dict[int, List[List[dict]]]:
    """{сцена: [отрезок, ...]} — непрерывные куски кадров-воспоминаний сцены.
    Кадр настоящего между ними разрывает отрезок."""
    runs: Dict[int, List[List[dict]]] = {}
    prev: Dict[int, bool] = {}
    for f in frames:
        n = int(f.get("scene_num") or 0)
        is_mem = (f.get("layer") or "now") != "now"
        if is_mem:
            if not prev.get(n):
                runs.setdefault(n, []).append([])
            runs[n][-1].append(f)
        prev[n] = is_mem
    return runs


def _build_one(n: int, part: int, total: int, shots: List[dict], cast: str,
               scene_texts: Dict[int, str], model: str, by_name: Dict[str, str],
               ids: set, log) -> Memory:
    if not part:
        lines = "\n".join(f"  - shot {s['idx']}: {(s.get('prompt_en') or '')[:200]}"
                          for s in shots)
    else:
        lines = "\n".join(f"  - shot {s['idx']}: «{(s.get('text') or '').strip()[:300]}» — "
                          f"{(s.get('prompt_en') or '')[:200]}" for s in shots)
    prompt = MEMORY_PROMPT.format(n=n, cast=cast, text=(scene_texts.get(n) or "")[:6000],
                                  shots=lines)
    if part:
        prompt += PART_NOTE.format(part=part, total=total)
    m = Memory(scene=n, part=part, frames=[int(s["idx"]) for s in shots] if part else [])
    where_log = (f"воспоминание в сцене {n}, отрезок {part} из {total} "
                 f"(кадры {shots[0]['idx']}–{shots[-1]['idx']})" if part
                 else f"воспоминание в сцене {n}")
    try:
        data, _ = call_json(prompt, model=model, timeout=300, retries=1)
        for k in ("where", "time_of_day", "weather", "light", "palette", "years_ago"):
            setattr(m, k, str(data.get(k, "") or "").strip())
        raw_who = data.get("who") if isinstance(data.get("who"), dict) else {}
        m.who, unknown = {}, []
        for k, v in raw_who.items():
            cid = _to_id(k, by_name, ids)
            (m.who.__setitem__(cid, v) if cid else unknown.append(k))
        m.absent = []
        for k in (data.get("absent") or []):
            cid = _to_id(str(k), by_name, ids)
            (m.absent.append(cid) if cid else unknown.append(str(k)))
        if unknown:
            log(f"    ! {where_log}: не опознаны: {unknown} — пропущены")
        log(f"  {where_log}: {len(shots)} кадров, где «{m.where}», "
            f"{m.years_ago or '?'} лет назад, в нём {list(m.who)}, отсутствуют {m.absent}")
        # Возраст обязан быть явным у каждого; «as now» при years_ago > 0 — это
        # и есть седая мать в детском воспоминании (кадр 105).
        for cid, w in m.who.items():
            age = str((w or {}).get("age") or "").strip()
            if not age or (age.lower() == "as now" and str(m.years_ago).strip() not in ("", "0")):
                log(f"    ! {cid}: возраст «{age or 'пусто'}» при {m.years_ago or '?'} "
                    f"лет назад — сдвиг возраста в кадр не уйдёт")
    except LLMError as e:
        log(f"  {where_log}: паспорт не собрался — {e}")
    return m


def save(mem: Dict[int, Memory], path: str) -> None:
    tmp = path + ".tmp"
    json.dump({str(k): asdict(v) for k, v in mem.items()},
              open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load(path: str) -> Dict[object, Memory]:
    """Ключ — номер сцены (int) или «сцена.отрезок» (str) для второго и дальше отрезка."""
    if not os.path.exists(path):
        return {}
    raw = json.load(open(path, encoding="utf-8"))
    return {(int(k) if str(k).isdigit() else str(k)): Memory(**v) for k, v in raw.items()}


def pick(mem: Dict[object, Memory], scene: int, idx: Optional[int] = None) -> Optional[Memory]:
    """Паспорт для кадра-воспоминания: отрезок, в который входит кадр; если кадр не
    входит ни в один (ярлык слоя поменялся позже) — ближайший по номеру кадра отрезок
    его сцены; у старых memory.json — паспорт сцены, как было."""
    scene = int(scene or 0)
    parts = [m for m in mem.values() if m.scene == scene and m.frames]
    if idx is not None and parts:
        idx = int(idx)
        for m in parts:
            if idx in m.frames:
                return m
        return min(parts, key=lambda m: min(abs(idx - i) for i in m.frames))
    return mem.get(scene)


# Несовершеннолетний в воспоминании: генератор картинок блокирует запрос, если его одежда
# описана как раздетость («рубашка наполовину расстёгнута», пижама, босиком в спальне) —
# 19.09.2026 так не собрались 10 листов воспоминаний ролика «Реванш». Такое описание
# заменяется скромной одеждой: возраст и облик остаются, раздетости у ребёнка нет.
_MINOR_WORDS = ("child", "kid", "boy", "girl", "infant", "newborn", "toddler", "baby", "teen", "school")
_REVEALING = re.compile(
    r"unbutton|half[- ]open|open (?:shirt|blouse)|shirtless|topless|undress|naked|nude|"
    r"bare[- ]?(?:chest|skin|shoulders?|back|legs?|feet|foot|midriff|belly)|barefoot|"
    r"underwear|lingerie|\bbra\b|panties|boxers|towel|sleepwear|pajama|pyjama|nightgown|"
    r"nightwear|nightdress|negligee|swimsuit|bikini|see-through|sheer|soaked|"
    r"wet (?:clothes|shirt|blouse|uniform)|clinging|crop top|short shorts|mini ?skirt|low-cut|cleavage", re.I)
MODEST_COSTUME = "modest, fully buttoned everyday clothes that cover the body"


def is_minor(age: str) -> bool:
    low = str(age or "").lower()
    if "adult" in low:
        return False                        # «adult about 10 years younger than now»
    m = re.search(r"(\d+)\s*(?:-\s*)?(?:years?[- ]old|y/?o)\b", low)
    if m:
        return int(m.group(1)) < 18
    return any(w in low for w in _MINOR_WORDS)


_AGE_WORDS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
              "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20}
_SCHOOL_MINOR = re.compile(r"\b(?:junior|middle|primary|elementary|grade|high)[- ]?school\b|\bschoolgirl\b|\bschoolboy\b", re.I)
# Фигура несовершеннолетнего: генератор блокирует лист целиком («Seventeen-year-old girl, slim
# curvy build…» — 19.09 «Реванш», shizuka/base и два её наряда). Описание фигуры у ребёнка не
# нужно: убираем слова, оставляя рост, лицо, волосы, одежду.
_BODY_RISKY = re.compile(
    r"\b(?:slim[- ]?curvy|curvy|curvaceous|voluptuous|busty|buxom|shapely|hourglass|ample|"
    r"full[- ]figured|well[- ]endowed|large[- ](?:breasts|bust|chest)|breasts?|bust|bosom|"
    r"cleavage|thighs|hips)(?:\s+(?:build|figure|frame|body|physique))?\b", re.I)
SAFE_BUILD = "slender build"


def looks_minor(text: str) -> bool:
    """Возраст младше 18 в свободном описании: «Seventeen-year-old», «14 years old», «junior-school girl»."""
    low = str(text or "").lower()
    if "adult" in low:
        return False
    m = re.search(r"(\d+)[- ]?(?:years?[- ]old|yo\b)", low)
    if m:
        return int(m.group(1)) < 18
    for w, n in _AGE_WORDS.items():
        if re.search(rf"\b{w}[- ]?year[- ]?old\b", low):
            return n < 18
    return bool(_SCHOOL_MINOR.search(low))


# Предметы В РУКАХ не место в паспорте внешности: паспорт идёт в КАЖДЫЙ кадр героя как
# «эти приметы никогда не меняются», и телефон из описания оказывается у неё в руке весь
# ролик. 21.09 «Реванш»: смартфон попал в паспорт трёх героев — и приехал в 370 кадров из
# 864, включая сцены, где герой сидит за партой или идёт по улице.
# Одежду, очки, серьги и сумку через плечо не трогаем: это часть образа, она и должна
# повторяться. Убираем именно «с предметом в руках».
_PROPS_IN_HANDS = re.compile(
    r"(?:,\s*)?\b(?:with|holding|carrying|clutching|gripping)\s+"
    r"(?:a|an|the|their|his|her|its)?\s*"
    r"[^,.;]*?\b(?:smartphones?|phones?|mobiles?|tablets?|laptops?|cameras?|books?|"
    r"notebooks?|papers?|documents?|folders?|cups?|mugs?|bottles?|cans?|umbrellas?|"
    r"knives|knifes?|sticks?|bats?|weapons?|guns?|swords?|flowers?|bouquets?|letters?|"
    r"envelopes?|magazines?|newspapers?)\b[^,.;]*",
    re.IGNORECASE)


def no_props(text: str) -> str:
    """Паспорт внешности без предметов в руках: что герой держит — дело кадра, а не примет."""
    if not text:
        return text
    out = _PROPS_IN_HANDS.sub("", str(text))
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\s+([,;.])", r"\1", out)
    out = re.sub(r",\s*,", ",", out)
    return out.strip().strip(",").strip()


# Прямой детский возраст в паспорте внешности («junior-school girl around twelve», «16-year-old»)
# отбивается фильтром генератора: 21.09 польский ролик 24d3cdca часами не мог нарисовать лист chisa,
# а русская версия того же сюжета без возраста числом нарисовалась с первого раза.
_AGE_PHRASE = re.compile(r",?\s*\b(?:aged?|around|about|approximately)\s+"
                         r"(?:\d{1,2}|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
                         r"sixteen|seventeen|eighteen|nineteen)\b(?:\s*years?\s*old)?", re.I)
_AGE_HYPHEN = re.compile(r"\b(?:\d{1,2}|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
                         r"sixteen|seventeen|eighteen|nineteen)[-\s]year[s]?[-\s]old\b", re.I)


def no_age(text: str) -> str:
    """Паспорт внешности без возраста числом: «young» вместо «16-year-old», «около двенадцати» — долой.
    Возраст героя виден по остальным приметам, а генератор на цифре спотыкается."""
    if not text:
        return text
    out = _AGE_HYPHEN.sub("young", _AGE_PHRASE.sub("", str(text)))
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\s+([,;.])", r"\1", out)
    return out.strip(" ,;")


def youth_safe(text: str) -> str:
    """Описание внешности несовершеннолетнего без слов про фигуру; у взрослых — как есть."""
    if not text or not looks_minor(text):
        return text
    return youth_safe_text(text)


def youth_safe_text(text: str) -> str:
    """Убрать слова про фигуру, не спрашивая про возраст: для паспорта внешности героя,
    про которого уже известно, что он несовершеннолетний (в самом паспорте возраста нет)."""
    if not text:
        return text
    out = _BODY_RISKY.sub(SAFE_BUILD, str(text))
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\s+([,;.])", r"\1", out)
    out = re.sub(r"([,;])\s*(?=[,;])", "", out)
    out = re.sub(r"(?:,\s*)+(?=(?:and\b|$))", ", ", out)
    return out.strip(" ,;")


def modest_costume(age: str, costume: str) -> str:
    """Одежда несовершеннолетнего без раздетости; у взрослых и у скромной одежды — как есть."""
    if costume and is_minor(age) and _REVEALING.search(str(costume)):
        return MODEST_COSTUME
    return costume


def child_costume_text(m: Memory, cid: str) -> Optional[str]:
    """Что писать в лист героя-ребёнка. None — если в воспоминании он «как сейчас»."""
    w = (m.who or {}).get(cid) or {}
    age = (w.get("age") or "as now").strip()
    if age == "as now" or not m.younger_than_now(cid):
        return None     # не моложе, чем сейчас: лист «моложе» неверен, рисуем по сегодняшнему
    # Ребёнок — по слову-стадии или по числу лет до 13. Раньше признаком было
    # «year» в строке — а возраст теперь пишется и как «adult about 10 years
    # younger than now», и такой взрослый получал бы детское тело.
    low = age.lower()
    m_old = re.search(r"(\d+)\s*years?\s+old", low)
    child = (any(t in low for t in ("child", "kid", "boy", "girl", "infant", "newborn",
                                    "toddler", "baby"))
             or bool(m_old and int(m_old.group(1)) < 13))
    body = ("a CHILD with a child's body and proportions: small, short, big head relative "
            "to the body, no adult build — NOT a teenager, NOT an adult. " if child else "")
    # Взрослый в прошлом: тот же человек, только моложе — без седины и морщин
    # (мать во флешбэке детства сына вышла седой, кадр 105). Сдвиг берём так же,
    # как для строки кадра (years_younger): из «N years younger» в возрасте, а
    # если возраст записан числом («35 years old») или стадией («teenager about
    # 16») — из years_ago. Иначе лист-эталон для воспоминания рисуется с
    # сегодняшнего лица без единого слова про молодость — та же седая мать.
    n_younger = None if child else m.years_younger(cid)
    if n_younger:
        body = (f"The same person about {n_younger} years younger than today: no grey hair, "
                f"no wrinkles, the hair colour of youth, a younger face. ")
    return (f"THIS IS A {age.upper()}. {body}{w.get('look', '')}. The same person as the "
            f"adult, only younger: same eye colour, same hair colour. "
            f"Wearing {modest_costume(age, w.get('costume')) or 'simple plain clothes'}").strip(". ")
