# -*- coding: utf-8 -*-
"""Состояние сцены: кто в кадре и где мы.

Зачем: в живых текстах пересказа имя названо примерно в 8% битов, место —
тоже в 8%. Значит по одному биту картинку не собрать. Этот модуль идёт по
битам сверху вниз и достраивает то, чего в тексте нет, — так что промпт
картинки собирается из «бит + состояние», а не из одного бита.

Русская специфика: китайские имена не склоняются, поэтому подлежащее в
предложении с двумя именами определяется РОДОМ СКАЗУЕМОГО, а не падежом.
Отсюда пол в глоссарии — обязательное поле, а не украшение.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import languages as LG



@dataclass
class Character:
    cid: str
    canonical: str
    gender: str                       # 'm' | 'f'
    variants: List[str] = field(default_factory=list)

    def all_forms(self) -> List[str]:
        return [self.canonical] + list(self.variants)


@dataclass
class SceneState:
    in_frame: List[str] = field(default_factory=list)   # cid, первый — главный
    location: Optional[str] = None
    resolved: bool = True
    reason: str = ""


@dataclass
class Glossary:
    chars: Dict[str, Character] = field(default_factory=dict)
    locations: List[str] = field(default_factory=list)
    place_variants: Dict[str, List[str]] = field(default_factory=dict)   # место -> падежные формы

    def find_in(self, text: str, lang: Optional[str] = None) -> List[str]:
        """Какие персонажи прямо названы в тексте. Порядок — как в тексте.

        Русский (и старый вызов без lang) — text.find, побайтно как было.
        Остальные языки — целым словом, буква в букву по имени и формам из variants
        (_name_spans): «Leon» не находится в «Leona», «Mina» — в «Minato», «Min» — в «Min-ho».
        Польский, чешский, румынский склоняют имена: падежи выписывает модель глоссария
        (LG.variants_rule). Основу слова не угадываем: падеж не из variants не найдётся."""
        if not lang or not LG.word_search(lang):
            hits = []
            for cid, ch in self.chars.items():
                for form in ch.all_forms():
                    pos = text.find(form)
                    if pos >= 0:
                        hits.append((pos, cid))
                        break
            return [cid for _p, cid in sorted(hits)]
        first: Dict[str, int] = {}
        for start, _end, cid in _name_spans(self, _norm(text)):
            first.setdefault(cid, start)
        return [cid for cid, _p in sorted(first.items(), key=lambda kv: kv[1])]

    def place_forms(self, loc: str) -> List[str]:
        return [loc] + list(self.place_variants.get(loc) or [])


# Румынские ş/ţ с седилью и ș/ț с запятой — разные символы; неразрывный и прочие
# дефисы — обычный. Замена один символ на один: позиции в тексте не сдвигаются.
_NORM = str.maketrans({"ş": "ș", "ţ": "ț", "Ş": "Ș", "Ţ": "Ț",
                       "\u2010": "-", "\u2011": "-", "\u2012": "-"})


_EN_DASH_IN_WORD = re.compile(r"(?<=\w)\u2013(?=\w)")


def _norm(text: str) -> str:
    """NFC + замены _NORM. Короткое тире «Min–ho» между буквами — дефис; тире между
    словами («Leon – Mina») не трогается. Длина текста не меняется."""
    return _EN_DASH_IN_WORD.sub("-", unicodedata.normalize("NFC", text or "").translate(_NORM))


def _name_spans(glossary: "Glossary", text: str) -> List[tuple]:
    """Все упоминания героев в (нормализованном) тексте: [(start, end, cid)] по порядку.

    Один кусок текста принадлежит одному герою. Если на нём совпали формы разных героев,
    побеждает: 1) более длинное совпадение; 2) каноническое имя над падежной формой
    («Leona weszła» — это Leona, а не родительный падеж от Leon; «Widziała Leona» тоже
    уйдёт Leona — текст здесь неоднозначен). Если на куске совпали только падежные формы
    разных героев («Leonie» — дательный Leona и предложный Leon), засчитываются оба.
    Регистр: первая буква — как в форме (строчная «mina» — не имя), остальные — любые
    («MIN-HO!», «Min-Ho»)."""
    cands = []
    for order, (cid, ch) in enumerate(glossary.chars.items()):
        for k, form in enumerate(ch.all_forms()):
            form = _norm(form)
            rx = _word_rx(form, ignore_case=True)
            if not rx:
                continue
            for m in rx.finditer(text):
                if m.group(0)[:1] != form[:1] and m.group(0)[:1] != form[:1].upper():
                    continue
                cands.append((m.start(), -(m.end() - m.start()), k > 0,
                              order, m.end(), cid))
    taken: List[tuple] = []
    for start, _neg, variant, _order, end, cid in sorted(cands):
        same = [t for t in taken if t[0] == start and t[1] == end]
        if same:   # тот же кусок: вторая падежная форма другого героя («Leonie» — Leon и Leona)
            if variant and all(t[3] for t in same) and all(t[2] != cid for t in same):
                taken.append((start, end, cid, variant))
            continue
        if any(start < t[1] and t[0] < end for t in taken):
            continue
        taken.append((start, end, cid, variant))
    return sorted((s, e, c) for s, e, c, _v in taken)


# Фамилии, совпадающие с частым названием места: «Park Min-ho» — фамилия, «Park» — место.
_PLACE_SURNAMES = {"park"}
# Реплика в кавычках не описывает перемещение: «„Nech mě na pokoji!“» — не комната.
_QUOTED = re.compile(r'„[^„“”"\n]*[“”"]|«[^«»\n]*»|“[^“”\n]*”|"[^"\n]*"')


def _find_place(glossary: "Glossary", text: str, lang: str) -> Optional[str]:
    """Названное в бите место (не русский). Целым словом, без учёта регистра, по формам из
    place_variants. Не считаются:
    - совпадение внутри имени героя или пересекающее его частично («Park» в «Park Min-ho»);
      если форма места целиком включает имя («gabinetu Adriana», «mieszkania Min-ho») —
      это место;
    - фамилия: место с заглавной вплотную перед именем, если это слово — известная фамилия
      (_PLACE_SURNAMES) или первое слово формы героя («Park Min-ho» → «Park»).
      «Pokój Ji-woo był pełen książek» — комната;
    - слово внутри реплики в кавычках («Nech mě na pokoji!»)."""
    text = _norm(text)
    names = _name_spans(glossary, text)
    surnames = set(_PLACE_SURNAMES)
    for ch in glossary.chars.values():
        for form in ch.all_forms():
            words = _norm(form).split()
            if len(words) > 1:
                surnames.add(words[0].lower())
    quoted = [(m.start(), m.end()) for m in _QUOTED.finditer(text)]
    for loc in glossary.locations:
        for f in glossary.place_forms(loc):
            rx = _word_rx(_norm(f), ignore_case=True)
            if not rx:
                continue
            for m in rx.finditer(text):
                a, b = m.start(), m.end()
                if any(qa <= a and b <= qb for qa, qb in quoted):
                    continue
                if any(a < e and s < b and not (a <= s and e <= b) for s, e, _c in names):
                    continue
                after = len(text) - len(text[b:].lstrip())
                if (m.group(0)[:1].isupper() and after > b and m.group(0).lower() in surnames
                        and any(s == after for s, _e, _c in names)):
                    continue
                return loc
    return None


def _word_rx(form: str, ignore_case: bool = False):
    """Форма имени/места → регулярка «буква в букву, целым словом».
    Пробелы внутри формы — любой пробел текста; дефис — часть слова: «Min-ho» не
    находится внутри «Min-hoa», а «Min» — внутри «Min-ho»."""
    parts = (form or "").split()
    if not parts:
        return None
    body = r"\s+".join(re.escape(w) for w in parts)
    return re.compile(r"(?<![\w-])" + body + r"(?!-?\w)", re.IGNORECASE if ignore_case else 0)


def _verb_gender(doc_span) -> Optional[str]:
    """Род сказуемого в прошедшем времени — главный ключ для русского."""
    for t in doc_span:
        if t.morph.get("VerbForm") == ["Fin"] or t.pos_ == "VERB":
            g = t.morph.get("Gender")
            if g:
                return "m" if g[0] == "Masc" else ("f" if g[0] == "Fem" else None)
    return None


def _noun_gender(doc_span) -> Optional[str]:
    """Род одушевлённого существительного в бите — берётся из МОРФОЛОГИИ.

    Раньше здесь был рукописный словарь ролевых слов; он был лишним и вдобавок
    подогнан под один конкретный текст. Морфология знает род сама, для любого
    существительного языка, включая те, которых в словаре не было бы никогда.
    """
    for t in doc_span:
        if t.pos_ != "NOUN":
            continue
        if t.morph.get("Animacy") != ["Anim"]:
            continue
        g = t.morph.get("Gender")
        if g:
            return "m" if g[0] == "Masc" else ("f" if g[0] == "Fem" else None)
    return None



# Зацепки для правила «звук за кадром не переносит героя».
# Списки короткие намеренно: чем длиннее список, тем чаще правило срабатывает
# там, где герой действительно перешёл в другое место.
OFFSCREEN_CUES = (
    "за дверью", "из коридора", "в коридоре", "за стеной", "снаружи", "с улицы",
    "послышал", "донёсся", "донеслись", "раздался", "раздались", "слышн",
    "звук", "шаги", "стук", "голоса за", "где-то",
)
STAY_CUES = (
    "сидел", "сидела", "сидит", "сидя", "за столом", "на стуле", "на кровати",
    "лежал", "лежала", "лежит", "не двигал", "не шевел", "остался", "осталась",
    "замер", "замерла", "стоял на месте", "продолжал", "продолжала",
)
MOVE_CUES = (
    "вошёл", "вошла", "вошли", "входит", "вышел", "вышла", "вышли", "выходит",
    "пошёл", "пошла", "пошли", "подошёл", "подошла", "перешёл", "перешла",
    "направил", "шагнул", "шагнула", "побежал", "побежала", "поднялся",
    "поднялась", "спустил", "приехал", "приехала", "добрал",
)


def resolve(src: str, beats, glossary: Glossary, lookback: int = 8,
            lang: str = "Russian") -> List[SceneState]:
    """Проставляет состояние каждому биту. beats — из beats.segment().

    Род глагола как ключ к подлежащему — только для русского (LG.gender_morph):
    модель spaCy на сервере русская, её «род» у польского слова — догадка, и
    неверный род отдал бы кадр другому герою. Без него — последний названный.
    Зацепки «звук за кадром» — OFFSCREEN/STAY/MOVE_CUES для русского, для польского и
    чешского — LG.scene_cues; у остальных языков их нет, и место меняется при каждом упоминании.
    Все языки, кроме русского, ищут имена и места целым словом буква в букву по формам из
    глоссария (_name_spans, _find_place), без угадывания основы; русский — подстрока, как было."""
    use_gender = LG.gender_morph(lang)
    words = LG.word_search(lang)
    cues = LG.scene_cues(lang) or (OFFSCREEN_CUES, STAY_CUES, MOVE_CUES)
    import spacy
    nlp = spacy.load("ru_core_news_sm", disable=["ner", "lemmatizer"])
    doc = nlp(src)

    states: List[SceneState] = []
    # стек последних упомянутых персонажей: cid -> номер бита
    last_seen: Dict[str, int] = {}
    location: Optional[str] = None
    in_frame: List[str] = []

    for i, bt in enumerate(beats):
        text = bt.text(src)
        span = doc.char_span(bt.start, bt.end, alignment_mode="expand")

        named = glossary.find_in(text, lang)
        for cid in named:
            last_seen[cid] = i

        # Место: если названо явно — обновляем и держим дальше.
        # НО: упоминание места не всегда означает, что герой туда переехал.
        # «За дверью в коридоре послышались шаги» — герой по-прежнему за столом,
        # а наивный поиск подстроки переставит кадр в коридор, и следующая
        # картинка нарисует другую комнату. Приём взят из боевого main_v40
        # (fix_offscreen_lid_for_visible_frames): смена места засчитывается,
        # только если рядом нет признаков «звук за кадром при неподвижном герое».
        named_loc = None
        if words:      # «gabinet» → «w gabinecie», только если «gabinecie» есть в variants
            named_loc = _find_place(glossary, text, lang)
        else:
            for loc in glossary.locations:
                if loc.lower() in text.lower():
                    named_loc = loc
                    break
        if named_loc and named_loc != location:
            low = text.lower()
            if words:      # pl/cs: зацепка с начала слова — «leży» не в «należy»
                def has(lst):
                    return any(re.search(r"(?<!\w)" + re.escape(c), low) for c in lst)
            else:          # русский — подстрока, как было
                def has(lst):
                    return any(c in low for c in lst)
            offscreen = has(cues[0])
            staying = has(cues[1])
            moving = has(cues[2])
            if not (offscreen and staying and not moving):
                location = named_loc
        elif named_loc:
            location = named_loc

        st = SceneState(location=location)

        if named:
            st.in_frame = named + [c for c in in_frame if c not in named][:2]
        else:
            # имени нет — разрешаем по роду сказуемого и по ролевому слову
            want = _verb_gender(span) if (use_gender and span is not None) else None
            if want is None and use_gender:
                want = _noun_gender(span) if span is not None else None
            cands = [cid for cid, n in sorted(last_seen.items(), key=lambda kv: -kv[1])
                     if i - n <= lookback]
            if want:
                cands = [c for c in cands if glossary.chars[c].gender == want] or cands
            if cands:
                st.in_frame = cands[:2]
            elif in_frame:
                st.in_frame = in_frame[:2]
            else:
                st.resolved = False
                st.reason = "не с кем связать: имён не было в последних битах"

        if not st.location:
            st.resolved = False
            st.reason = (st.reason + "; место неизвестно").strip("; ")

        in_frame = st.in_frame or in_frame
        states.append(st)

    return states


def autoglossary(src: str, min_hits: int = 3) -> Glossary:
    """Черновой глоссарий прямо из текста — чтобы прогнать до первого прохода модели.

    В боевом конвейере глоссарий приходит из прохода 1 (там же и пол),
    здесь — грубая заготовка по частоте имён собственных.
    """
    import spacy
    nlp = spacy.load("ru_core_news_sm", disable=["lemmatizer"])
    doc = nlp(src)
    counts: Dict[str, int] = {}
    for t in doc:
        if t.pos_ == "PROPN":
            counts[t.text] = counts.get(t.text, 0) + 1

    # склеиваем соседние PROPN в составные имена («Фан Шу»)
    compounds: Dict[str, int] = {}
    for sent in doc.sents:
        toks = [t for t in sent]
        i = 0
        while i < len(toks):
            if toks[i].pos_ == "PROPN":
                j = i
                while j + 1 < len(toks) and toks[j + 1].pos_ == "PROPN":
                    j += 1
                name = " ".join(t.text for t in toks[i:j + 1])
                compounds[name] = compounds.get(name, 0) + 1
                i = j + 1
            else:
                i += 1

    g = Glossary()
    for name, n in sorted(compounds.items(), key=lambda kv: -kv[1]):
        if n < min_hits or len(name) < 3:
            continue
        if any(name in c.canonical for c in g.chars.values()):
            continue
        cid = re.sub(r"\W+", "_", name.lower())
        g.chars[cid] = Character(cid=cid, canonical=name, gender="?", variants=[])
    return g
