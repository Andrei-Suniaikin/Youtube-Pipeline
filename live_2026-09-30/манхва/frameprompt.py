# -*- coding: utf-8 -*-
"""Сборка промпта кадра. Здесь решается, что именно увидит генератор.

ЧТО ИЗМЕНИЛОСЬ ПО СРАВНЕНИЮ С ПЕРВЫМ ПРОГОНОМ И ПОЧЕМУ.

1. Ссылки на приложенные картинки — ПО ПОРЯДКУ, а не по имени файла.
   Было: «Fang Shu is the person shown in fang_shu_face.png». Имя файла модель
   игнорирует: девять прогонов из девяти она брала вторую по счёту картинку.
   Тот же вывод независимо получен в боевом main_v40, где роли объявлены как
   «image 1 is the locked identity reference for ...» — то есть номером.

2. Роль каждой картинки объявляется ЯВНО и говорит, чего с неё НЕ брать.
   Проверено живьём: формулировка «планировка точно как на картинке» даёт
   буквальную копию задника, а «отсюда только цвет и материалы» — не даёт.

3. У чертежа помещения отдельный запрет на буквы. Проверено 08.09.2026:
   без него модель переписала подпись с плана прямо на холодильник в кадре
   («DRINKS FRIDGE»). С запретом протечка исчезла на всех трёх кадрах.

4. Порядок блоков перевёрнут. Замер первого прогона: содержание кадра занимало
   11% промпта, стиль и запреты — 55%. Теперь сначала ЧТО ПРОИСХОДИТ, потом
   где кто стоит, потом камера, и только в конце короткая строка стиля.

5. Вместо списка из шестидесяти «no ...» — перевёрнутая формулировка из
   боевого main_v40: перечисляем, чему МОЖНО меняться, и этим закрываем всё
   остальное. Плюс отдельная строка про массовку, потому что именно она
   получала лицо главного героя.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import re as _re_mod
import threading as _threading

import images

_re_traits = _re_mod.compile(
    r"\b(" + "|".join(_c for _c in (
        "black", "dark", "brown", "blonde", "blond", "grey", "gray", "silver",
        "white", "red", "auburn", "ginger", "pale", "tanned", "olive")) +
    r")\s+(?:\w+\s+){0,2}?(" + "|".join((
        "hair", "eyes", "skin", "uniform", "jacket", "coat", "dress", "shirt",
        "blazer", "apron", "hoodie", "sweater", "suit", "scarf", "beard",
        "stubble", "ponytail")) + r")\b", _re_mod.IGNORECASE)

# Крупность: отдельное поле, отдельно от угла. В первом прогоне они были
# смешаны в одном, поэтому нельзя было даже проверить, не идут ли три
# одинаковых плана подряд — а их шла тридцать одна тройка.
SHOT_WORDS = {
    "extreme_close": "extreme close-up: only the eyes and the bridge of the nose, spread "
                     "across the whole panel width",
    "close": "close-up, head and shoulders, the face takes 60-80% of the panel height",
    "medium": "medium shot, waist up",
    "full": "full shot, the whole body in frame",
    "wide": "wide shot, the character small inside the room",
    "insert": "tight insert on the detail described, the faces stay outside the frame",
}
ANGLE_WORDS = {
    "eye": "at eye level",
    "low": "from below, looking up",
    "high": "from above, looking down",
    "over_shoulder": "over the shoulder of the other character",
}

# Стиль НЕ сокращаем. Проверено замером 08.09.2026 на 72 кадрах: сжатие строки
# с 1100 до 147 знаков поднимает захламлённость фона с 4.6% до 8.6%, насыщенность
# уходит с 14.9 до 19.0, худшая пара повторов расходится по цвету с dE 11.3 до 15.6.
# Ключевой кусок — про упрощённый фон: именно он не даёт рисовать исписанную доску.
STYLE = ("Korean webtoon manhwa illustration, vertical composition, dramatic ink lineart with "
         "varied line weight - thicker on the outer silhouette, thinner inside - "
         "cel shading with one hard-edged shadow tone plus fine cross-hatching only in "
         "the darkest folds, high contrast, cinematic directional "
         "lighting, expressive large emotional anime eyes with one bright specular "
         "highlight, hair as solid colour blocks with a glossy highlight band, saturated "
         "clean local colours, one warm key light plus a rim light separating the "
         "character from the background, clean flat colour areas with no dot patterns "
         "and no stray lines, simplified flat-shaded background with reduced detail kept "
         "softly behind the subject, smooth flat digital fills, clean sharp edges, "
         "full-bleed composition filling the entire canvas edge to edge")

# Пресеты манеры рисовки. Опыт 10.09 на 12 кадрах (6 драма + 6 экшен, те же seed):
# лица и герои во всех четырёх одинаковы — стиль можно выбирать под жанр ролика,
# не трогая листы персонажей. S0 — по умолчанию; S2 лучше смотрится на боях,
# S1/S3 — на бытовой драме и романтике.
STYLES = {
    "S0": STYLE,
    "S1": ("Korean webtoon manhwa illustration, vertical composition, clean confident ink lineart with varied line "
           "weight, SOFT RENDERED shading: base colour, an airbrushed gradient shadow and a second deeper shadow blended softly, "
           "fine cross-hatching only in the darkest folds, muted slightly desaturated cinematic palette, gentle warm key light and "
           "a cool rim light, expressive large emotional eyes with one bright highlight, hair in soft colour bands with a glossy "
           "highlight, skin with subtle blush gradients, the whole room fully drawn but with lower contrast than the people, "
           "smooth digital painting, no dot patterns, full-bleed composition filling the entire canvas edge to edge"),
    "S2": ("Korean webtoon manhwa illustration, vertical composition, bold dramatic ink lineart, heavy chiaroscuro: "
           "deep hard-edged shadows covering half of each face, one strong warm key light and a thin cold rim light, high contrast, "
           "dark moody palette with a few saturated accents, expressive intense eyes with a sharp highlight, hair as solid dark colour "
           "blocks with a glossy band, cross-hatching in the darkest folds, background darkened and simplified so the figures glow "
           "against it, cinematic grade like a top action manhwa, no dot patterns, full-bleed composition filling the entire canvas"),
    "S3": ("Korean webtoon manhwa illustration, vertical composition, thin clean even ink lineart, flat cel colours "
           "with very soft airbrushed gradients on skin and hair only, bright clean pastel-leaning palette, minimal shadows (one soft "
           "shadow tone), large clear eyes with one highlight, simple clean backgrounds with reduced detail and light tones, the look "
           "of a popular romance-drama webtoon, smooth flat digital fills, no cross-hatching, no dot patterns, crisp edges, "
           "full-bleed composition filling the entire canvas edge to edge"),
}
# S4 (17.09.2026): манера топ-каналов пересказов (Маньюха Премиум, Asuka Recaps, Mamoru Manhwa — кадры в
# competitors/): гладкая цифровая заливка вебтуна, яркая насыщенная светлая гамма, крупные выразительные
# лица на мягком размытом фоне. Ночь и бой — цветным светом (огонь, магия, луна), а не серой мглой.
STYLES["S4"] = ("FLAT KOREAN WEBTOON COLOURING, NOT PAINTERLY: an official full-colour webtoon panel, bright, clean and "
                "polished. Every surface is flat cel colour plus ONE soft gradient shadow; no painted brush textures, no grit, "
                "no realistic stone, metal or cloth detail, no cinematic film grading. Palette: vivid saturated high-key colours. "
                "Night and battle scenes stay bright and readable: the scene is lit by glowing orange fire, cyan magic and blue "
                "moonlight with a strong colourful rim light on every figure; deep black only in small accents, never a dark "
                "grey, brown or muddy frame. Faces: large clean anime faces with big glossy eyes, bright specular highlights and "
                "crisp medium-weight lineart; emotions large and instantly readable. Backgrounds: reduced to soft blurred colour "
                "gradients, simple flat silhouettes or light bokeh; keep only the one or two background elements the action "
                "needs, everything else dissolves into colour. Action moments: bold speed lines, radial white impact flashes and "
                "glowing particles. Smooth flat digital fills, crisp edges, no dot patterns, full-bleed composition filling the "
                "entire canvas edge to edge")
STYLE_NAMES = {"S0": "текущий: cel + жёсткая тень", "S1": "мягкий: аэрограф, приглушённо",
               "S2": "драматичный: тяжёлые тени, для экшена", "S3": "чистый плоский: светлый, романтика",
               "S4": "вебтун как у топов: ярко, крупно, цветной свет"}


# Стиль ролика помнит и поток, который этот ролик ведёт: при MANGA_PARALLEL_VIDEOS > 1 два ролика
# с разными стилями идут одновременно, и общий STYLE второго перебил бы стиль кадров первого.
_THREAD = _threading.local()


def set_style(name: str) -> str:
    """Выбирает пресет манеры для всех кадров прогона. Возвращает имя."""
    global STYLE
    key = (name or "S0").upper()
    if key not in STYLES:
        raise ValueError(f"неизвестный стиль {name!r}; есть: {', '.join(STYLES)}")
    STYLE = STYLES[key]
    _THREAD.style = STYLE
    return key


def current_style() -> str:
    """Стиль ролика этого потока (set_style в нём же), иначе общий STYLE — как раньше."""
    return getattr(_THREAD, "style", None) or STYLE

# Запреты. Замер: восемь пунктов про текст держат долю кадров с надписями на 25%;
# убрать их — 75%, p=0.039. Заменить утвердительной фразой («clean illustration
# without any lettering») — 50%. То есть народное правило «не называй запрещённое»
# на этой модели работает НАОБОРОТ. Прочие 29 запретов (каймы, letterbox, рамки,
# фотореализм) на 72 кадрах не сработали ни разу — выброшены.
# Буквы вылезают не «вообще», а в трёх конкретных местах: обложка книги,
# упаковка товара и вывеска. Проверено глазами: на кадре 203 модель написала
# CALCULUS на учебнике при действующем общем запрете текста. Общий запрет
# оставляем (замер: без него 75% кадров с надписями, с ним 25%) и добавляем
# прямое указание, ЧТО делать с этими тремя поверхностями.
NEG = ("DO NOT INCLUDE: no text, no letters, no numbers, no captions, no subtitles, "
       "no speech bubbles, no sound-effect lettering, no signage, no readable labels, "
       "no writing on blackboards or screens, no watermark, no signature. "
       "Book covers, notebook covers, packaging, bottles, screens and shop signs are "
       "drawn PLAIN — a solid colour, a simple emblem or a few abstract marks — and "
       "never carry a word, a title or a brand. "
       # Запреты на фотореализм я однажды выбросил: замер на banana-2 показал, что
       # они не срабатывают ни разу на 72 кадрах. На banana-pro срабатывают —
       # кадр 212 вышел настоящей фотографией: кожа с порами, дерево с текстурой,
       # фотографическая глубина резкости. Списки запретов зависят от модели,
       # и переносить вывод с одной на другую нельзя.
       # Кадр 449 (pro): подписи предметов с чертежа расставлены по картинке как
       # выноски на схеме — CEILING LIGHTS, SHARED ARMCHAIR. Чертёж не нарисован,
       # но его манера подписей протекла. Запрет именно на выноски.
       "No callout labels, no floating captions naming objects, no diagram-style "
       "annotations anywhere over the picture. "
       # Жанр «система» (половина ниши): генератор подписывает игровые окна —
       # Awakening, Trade, SYSTEM на трёх кадрах второго текста 09.09.
       "Game-system windows, status screens, skill panels and holographic notices are "
       "drawn as blank glowing rectangles with abstract marks — never with words, "
       "numbers or letters inside. "
       "This is a DRAWING, never a photograph: no photorealism, no photographic skin "
       "texture or pores, no real wood grain or fabric weave, no camera depth of field, "
       "no lens blur, no bokeh, no film grain, no 3D render, no CGI. "
       # Третий текст: лифт 033/035 («brushed silver», «reflective metal floor»)
       # ушёл в фотореальные отражения без контура; 060 — «фото в газете»
       # нарисовано акварельным фото. Поверхности и картинки внутри картинки —
       # той же плоской манерой.
       "No photographic reflections — metal, glass and mirrors are flat tones with one "
       "highlight band. A photograph or newspaper inside the picture is drawn in the same "
       "lineart style.")

# Перевёрнутый замок: что МОЖЕТ меняться. Всё, что не названо, меняться не должно.
LOCK = ("Only the pose, the expression, the action and the camera change between shots. "
        "Faces, hair, clothing, the room and its furniture never get redesigned.")

# Прежняя формулировка просила дать массовке ДРУГОЕ лицо — то есть всё равно
# разрешала лицо, и модель рисовала его по единственному образцу, который у неё
# есть: по эталону героя. Отсюда «на фоне у всех ебало главного». Боевой main_v40
# запрещает лицо как таковое: копировать нечего, если лица нет.
# Первая редакция запрещала фоновым людям лицо как таковое — и получились серые
# призраки с пустыми овалами вместо лиц (кадры 122, 185). Нужна середина: люди
# должны читаться людьми, но не соперничать с героями и не носить их лиц.
EXTRAS = ("Anyone in this frame who is not named above has NO reference picture, so they must "
          "never get a readable face: draw them from behind, turned away, cropped by the edge "
          "or with the head out of frame — a face invented here comes out as a copy of a named "
          "character (кадр 14 ролика 27b42e7c: нянька вышла двойником покойной матери). "
          "Anyone in this frame who is not named above is background: drawn simply and "
          "softly, out of focus or turned away, features suggested with a few strokes — "
          "never a blank empty face, never a grey faceless silhouette, and never the face "
          "of anyone from the attached reference images.")

# Взгляд в объектив ломает ощущение подсмотренной сцены и встречается часто:
# в разборе 493 кадров это одно из самых заметных нарушений мизансцены.
# Правило композиционное, а не стилевое, поэтому стоит отдельной строкой.
NO_CAMERA_LOOK = ("Nobody looks into the camera or acknowledges the viewer. Every gaze "
                  "goes where the EYELINE line says it goes.")

FRAME_RULE = ("One single manhwa drawing that fills the whole canvas edge to edge: no borders, "
              "no multi-panel layout, no split screen, no margins.")

# Список собран из брака, который мы ловили глазами и судьёй: двойник героя, лишний
# человек в глубине, шестой палец, рука из ниоткуда, разный цвет глаз, одежда, у которой
# половины не совпадают. Просим модель пересмотреть СВОЙ рисунок до выдачи: проверка
# внутри одной генерации ничего не стоит, а судья на тех же кадрах обошёлся бы в 11,5
# тыс. токенов на кадр — втрое дороже самого ролика (замер 20.09.2026).
SELF_CHECK = (
    "CHECK YOUR OWN DRAWING BEFORE YOU OUTPUT IT, and silently redraw anything wrong:\n"
    "- HANDS AND LIMBS: five fingers on each visible hand, no fused, missing or extra "
    "fingers, no third arm or leg, no limb growing from the wrong place, elbows and knees "
    "bending the natural way, no blurred mush where a hand should be.\n"
    "- HOW MANY PEOPLE: exactly the people named in CAST and nobody else — no second copy "
    "of the same character, no extra face, hand or body appearing in the background crowd.\n"
    "- FACE: both eyes the same colour, size and shape, pupils looking the same way, ears "
    "level with each other, teeth as a simple shape and never a full row of separate teeth.\n"
    "- THINGS AND BODIES: no object passing through a body, no floating or detached item, "
    "nothing melting into clothing or hair, feet resting on the ground the right way round.\n"
    "- CLOTHING: the same colour, pattern and cut on both halves of the body, one collar, "
    "one set of sleeves, no extra zips, straps or buttons that only exist on one side.\n"
    "- SURFACE: no text, no watermark, no signature, no frame or border, no second panel.")

_SELF_CHECK_WITH_BUBBLE = SELF_CHECK.rsplit("\n- SURFACE:", 1)[0] + (
    "\n- SURFACE: no watermark, no signature, no frame or border, no second panel.")


def self_check(with_bubble: bool = False) -> str:
    """Блок самопроверки. При облаке с репликой запрет текста из него убран: разрешение
    нарисовать облако идёт ниже, и два противоположных указания подряд модель путают."""
    return _SELF_CHECK_WITH_BUBBLE if with_bubble else SELF_CHECK

# Мимика вместо ярлыка эмоции.
EMOTION_FACE = {
    "calm": "relaxed face, brows level, mouth closed and soft, eyes open normally",
    "angry": "brows pulled down and together, deep crease between them, eyes narrowed, "
             "jaw set, mouth a hard line or open with clenched teeth",
    "afraid": "brows raised and pulled together, eyes wide with visible white above the "
              "iris, mouth slightly open, shoulders drawn up",
    "hurt": "brows angled up in the middle, eyes half closed and glassy, mouth pressed "
            "tight at the corners",
    "shocked": "brows shot up, eyes very wide, pupils small, mouth open, head pulled back",
    "sad": "inner brows lifted, eyelids lowered, corners of the mouth pulled down, "
           "gaze angled downward",
    "determined": "brows lowered and level, eyes steady and fixed forward, lips pressed "
                  "together, chin slightly raised",
    "blank": "no expression at all, brows still, eyes unfocused, mouth relaxed and closed",
}


_COLOURS = ("black", "dark", "brown", "blonde", "blond", "grey", "gray", "silver",
            "white", "red", "auburn", "ginger", "pale", "tanned", "olive")
_THINGS = ("hair", "eyes", "skin", "uniform", "jacket", "coat", "dress", "shirt",
           "blazer", "apron", "hoodie", "sweater", "suit", "scarf", "glasses", "beard",
           "stubble", "ponytail", "braid", "build")


_HAIR_COLOURS = _COLOURS + ("chestnut", "platinum-blonde", "platinum", "sandy", "copper",
                            "salt-and-pepper", "ash", "honey", "raven")
_re_hair = _re_mod.compile(
    r"\b(" + "|".join(_HAIR_COLOURS) + r")\s+(?:\w+\s+){0,2}?hair\b", _re_mod.IGNORECASE)

_DECADES = {"twenties": 20, "thirties": 30, "forties": 40, "fifties": 50, "sixties": 60,
            "seventies": 70, "eighties": 80, "nineties": 90}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}


def _age_years(text: str):
    """Возраст из паспорта числом: «32-year-old», «aged 32», «in his late
    seventies» (70+8), «around thirty». None — если возраста нет."""
    t = text or ""
    # «years old» тоже: в run_v все 13 паспортов написаны «around 40-45 years
    # old» — без «s» число не читалось, и доктор шёл просто «man».
    m = _re_mod.search(r"\b(\d{1,2})[-\s]years?[-\s]old\b", t, _re_mod.IGNORECASE)
    if not m:
        m = _re_mod.search(r"\b(?:aged|age)\s+(\d{1,2})\b", t, _re_mod.IGNORECASE)
    if m:
        return int(m.group(1))
    m = _re_mod.search(r"\b(?:in\s+(?:his|her|their)\s+)?(early|mid|late)?[-\s]*(" +
                       "|".join(_DECADES) + r")\b", t, _re_mod.IGNORECASE)
    if m:
        base = _DECADES[m.group(2).lower()]
        shift = {"early": 2, "mid": 5, "late": 8}.get((m.group(1) or "").lower(), 5)
        return base + shift
    m = _re_mod.search(r"\b(?:around|about|roughly|nearly|almost)\s+(" +
                       "|".join(_TENS) + r")\b", t, _re_mod.IGNORECASE)
    if m:
        return _TENS[m.group(1).lower()]
    return None


def visible_traits(appearance_en: str, limit: int = 4) -> str:
    """Две-четыре приметы, которые ВИДНО на приложенном кропе лица.

    Раньше сюда шли первые восемь слов описания — «Young man around 17-18 years
    old, lean build». Телосложение на кропе лица не читается, и привязка не
    работала. Нужны признаки, по которым модель отличит ОДНОГО эталона от
    другого: пол и возраст, цвет волос, характерная деталь. Проверено опытом
    с двумя лицами: работает связка «седой в костюме» против «лохматого в худи»,
    а не «мужчина 17-18 лет».
    """
    text = appearance_en or ""
    out = []
    # Возраст — СНАЧАЛА числом. Просмотр 335 кадров третьего текста: регэксп
    # ниже брал «old man» из «athletic 32-year-old man», и Эдриан ушёл в 164
    # промпта как old man — седой (кадры 20, 235, 272) и состаренный (23, 57,
    # 153, 243). Число в паспорте есть почти всегда; словесный возраст («in his
    # late seventies», «around thirty») — тем же порогом.
    sex = _re_mod.search(r"\b(man|woman|boy|girl)\b", text, _re_mod.IGNORECASE)
    sex_w = sex.group(0).lower() if sex else ""
    age = _age_years(text)
    if age is not None and sex_w:
        band = "young" if age < 35 else ("middle-aged" if age <= 55 else "elderly")
        out.append(f"{band} {sex_w}")
    else:
        # «old» после дефиса («32-year-old») — не возраст, а хвост числа.
        m = _re_mod.search(r"(?<![-\w])(teenage|young|middle-aged|elderly|old)\s+(man|woman|boy|girl)\b",
                           text, _re_mod.IGNORECASE)
        if m:
            out.append(m.group(0).lower())
        elif sex_w:
            out.append(sex_w)
    seen = set()
    # Цвет волос — всегда первым: на кропе лица он читается лучше всего, а в
    # общем цикле его вытесняла одежда («dark jacket» у репортёра — и волосы
    # остались без цвета).
    hm = _re_hair.search(text)
    if hm:
        seen.add("hair")
        out.append(f"{hm.group(1).lower()} hair")
    for col, thing in _re_traits.findall(text):
        key = thing.lower()
        if key in seen or key in ("build",):
            continue
        seen.add(key)
        out.append(f"{col.lower()} {key}")
        if len(out) >= limit:
            break
    # характерные детали без цвета — они и различают похожих героев
    for word in ("ponytail", "braid", "glasses", "beard", "stubble", "scar",
                 "bandage", "apron", "uniform", "suit", "hoodie", "long hair",
                 "short hair", "grey streaks", "bloodshot"):
        if len(out) >= limit:
            break
        if _re_mod.search(r"\b" + word + r"\b", text, _re_mod.IGNORECASE) and word not in seen:
            seen.add(word)
            out.append(word)
    return ", ".join(out[:limit])


def _name_pat(name: str) -> str:
    """Регэксп имени с притяжательными: «Adrians Grandfather» ловит
    «Adrian's grandfather» и «Adrian’s grandfather», «Emma» — «Emma’s»."""
    toks = []
    for t in (name or "").split():
        if len(t) > 3 and t.endswith("s"):
            toks.append(_re_mod.escape(t[:-1]) + "['’]?s")
        else:
            toks.append(_re_mod.escape(t))
    return r"\b" + r"\s+".join(toks) + r"(?:['’]s)?\b"


def mentioned(text: str, names: Dict[str, str]) -> List[str]:
    """Кто из героев НАЗВАН в тексте (по английским именам из names).

    Сначала полное имя без учёта регистра — и его вхождение вычёркивается,
    чтобы «Adrian's grandfather» не засчитался Эдриану; потом имя-первое
    слово с учётом регистра («Emma», «Adrian»), чтобы «the male reporter»
    не ловился на «Male». Порядок — как в names.
    """
    t = text or ""
    if not t:
        return []
    found = []
    for cid, nm in (names or {}).items():
        if not (nm or "").strip():
            continue        # пустое имя дало бы пустой регэксп — совпадение везде
        pat = _re_mod.compile(_name_pat(nm), _re_mod.IGNORECASE)
        if pat.search(t):
            found.append(cid)
            t = pat.sub(" ", t)
    for cid, nm in (names or {}).items():
        if cid in found or not (nm or "").strip():
            continue
        first = nm.split()[0]
        if len(first) < 3 or first.lower() == first:
            continue
        # Здесь первое слово — буквально: «Adrians» (из adrians_grandfather)
        # не должен ловить «Adrian’s», это уже Эдриан.
        if _re_mod.search(r"\b" + _re_mod.escape(first) + r"(?:['’]s)?\b", t):
            found.append(cid)
    return [c for c in (names or {}) if c in found]


def contact_cids(frame, names: Dict[str, str]) -> List[str]:
    """Герои, чья рука входит в кадр (frame.contact), но кого нет в in_frame.
    Просмотр 335 кадров: 101 кадр с contact дал 16 «лишних конечностей»
    против 5 на 234 без него — рука без хозяина рисуется чужой."""
    inf = set(getattr(frame, "in_frame", None) or [])
    con = _txt(getattr(frame, "contact", "") or "")
    cands = [c for c in mentioned(con, names) if c not in inf]
    if len(cands) < 2:
        return cands
    # Названы двое и больше: хозяин руки — тот, чьё имя стоит перед словом-
    # конечностью («Adrian’s hand», «the female reporter’s arm»); адресат
    # («toward Emma outside the frame», «that Fang Shaoning grips») — нет.
    # Кадры 160/168 run_ceo и 407 run_v иначе получали «the hand belongs to»
    # обоим сразу и рукав адресата. Ни у кого не нашлось — оставляем всех.
    limb = (r"\s+(?:\w+\s+){0,2}?(?:hands?|arms?|forearms?|fingers?|fingertips|palm|"
            r"wrist|shoulders?|elbow|torso|waist|foot|legs?|knee|sleeve)\b")
    owners = []
    for c in cands:
        nm = names[c]
        pats = [_name_pat(nm)]
        first = nm.split()[0]
        if first.lower() != first:
            pats.append(r"\b" + _re_mod.escape(first) + r"(?:['’]s)?\b")
        if any(_re_mod.search(p + limb, con, _re_mod.IGNORECASE) for p in pats):
            owners.append(c)
    return owners or cands


def en_name(cid: str, fallback: str = "") -> str:
    """Английское имя героя для промпта.

    Раньше в английский промпт уходило русское имя из скелета («Шэнь Буюй»),
    а описание кадра модель писала латиницей («Shen Buyu») — получались два
    разных человека в одном тексте. Идентификатор латиницей совпадает с тем,
    как персонажа называет само описание.
    """
    return (cid or fallback).replace("_", " ").title()


ROLE_HEAD = ("REFERENCE CONTRACT. {n} image(s) are attached, in this exact order. "
             "Each image has ONE job and is used for nothing else.")

ROLE_FACE = ("IMAGE {i} (attached in position {i}, file {fn}) = FACE SOURCE for {NAME}"
             "{traits}. Copy this face, hairstyle, hair colour and skin tone onto the "
             "character called {NAME}. Take NOTHING else from this image: not its clothes, "
             "not its background, not its pose, not its framing. What {NAME} wears in this "
             "shot is written below and overrides the clothing seen in the reference.")

ROLE_BODY = ("IMAGE {i} (attached in position {i}, file {fn}) = FIGURE SOURCE for {NAME}"
             "{traits}. Take from it the whole silhouette: body build, height proportions, "
             "hair length and how the hair falls from behind, the shape and length of the "
             "clothes. The colours of the clothes come from the scene text below, not from "
             "this image. Take nothing else: not its background, not its pose, not its framing.")

# Рука из-за кадра. Просмотр 335 кадров третьего текста: «interlaces with
# Adrian's hand» без эталона Эдриана в кадре рисовалось рукопожатием с чужим
# (147/152/153/155/163). Лист хозяина руки прикладывался только за рукавом и
# кожей — и с прямым запретом рисовать его самого.
# ВЫКЛЮЧЕНО после круга 2 (r2/review_synthesis.md §4 п.1): приложенная фигура
# `_front` перевешивает текст «его тут нет» — Эмма целиком с папкой (11),
# репортёрша в рост (160), Эдриан в сером костюме (163), 189: четыре EXTRA
# из пяти новых. Рукав и кожа хозяина руки теперь описываются ТОЛЬКО словами
# (см. LIMB ENTERING в build()); константа оставлена, чтобы было видно, что
# вложение отключено сознательно, а не потеряно.
SLEEVE_REF = False
ROLE_SLEEVE = ("IMAGE {i} (attached in position {i}, file {fn}) = SLEEVE AND SKIN SOURCE for "
               "the hand entering the frame: {NAME}{traits}. Take from it only the colour "
               "and cut of the sleeve at the wrist and the skin tone of the hand. {NAME} is "
               "otherwise NOT in this picture: do not draw this person's face, body or "
               "silhouette anywhere.")

# Постоянный эталон манеры. Опыт 09.09 на 12 кадрах сцены 1 (те же промпты,
# эталоны, зёрна; якорь четвёртым слотом): разброс плоскости заливки 0.076→0.047,
# палитры 10.9→8.3, контур сильнее 0.067→0.085. Это НЕ «предыдущий кадр как
# эталон» (та цепочка копит искажение): один и тот же кадр-якорь для всех.
ROLE_STYLE = ("IMAGE {i} (attached in position {i}, file {fn}) = STYLE SOURCE ONLY. Take "
              "from it only the manner of drawing: line weight, flat cel shading, colour "
              "handling, level of background detail. Take NOTHING else: not its people, "
              "not its room, not its composition, not its objects.")

ROLE_PLAN = ("IMAGE {i} (attached in position {i}, file {fn}) = LAYOUT SOURCE ONLY. It is "
             "a flat top-down diagram of the room, it is NOT a picture of the room. Read "
             "from it only where the door, the window and each piece of furniture stand "
             "relative to each other, then build that same room in ordinary eye-level "
             "perspective seen from the camera position described below. NEVER REDRAW THIS "
             "DIAGRAM. The finished picture must contain no plan, no blueprint, no map, no "
             "schematic, no top-down view, no outlines, no arrows, no labels and no text.")

ROLE_MIX = ("Their faces must not be mixed: the hair colour and hairstyle of each person "
            "come from his or her own image.")

# Два эталона в одном промпте делят волосы: кадр 297 круга 2 (дед + Эдриан, оба
# `_front`) вышел «вторым дедом» — седой Эдриан; 14 и 272 (wide, лица мелкие) —
# то же. Общей строки ROLE_MIX в конце легенды мало: привязку «эти волосы —
# только этому» повторяем В КАЖДОЙ строке эталона, когда людей двое и больше.
ROLE_OWN = (" The hair colour and hair length seen in THIS image belong ONLY to {NAME} "
            "and to nobody else in the picture.")


def _roles(refs: List[images.Ref], face_names: List[str], has_plan: bool,
           traits: Optional[Dict[str, str]] = None,
           kinds: Optional[List[str]] = None, has_style: bool = False,
           notes: Optional[Dict[str, str]] = None) -> str:
    """Роль привязана к НОМЕРУ ПОЗИЦИИ, а не к имени файла.

    Проверено 08.09.2026 на 36 живых генерациях: без такой легенды модель не
    выбирает между двумя эталонами, а СПЛАВЛЯЕТ их в одного человека — лицо
    одного, костюм другого. Имя файла адресом не является; работает связка
    «номер позиции + приметы, видимые на самой картинке».
    """
    if not refs:
        return ""
    lines = [ROLE_HEAD.format(n=len(refs))]
    i = 0
    for nm in face_names:
        i += 1
        tr = (traits or {}).get(nm, "")
        kind = (kinds or [])[i - 1] if kinds and len(kinds) >= i else "face"
        tmpl = {"body": ROLE_BODY, "sleeve": ROLE_SLEEVE}.get(kind, ROLE_FACE)
        line = tmpl.format(i=i, fn=refs[i - 1].name, NAME=nm.upper(),
                           traits=(f" ({tr})" if tr else ""))
        if len(face_names) > 1 and kind != "sleeve":
            line += ROLE_OWN.format(NAME=nm.upper())
        # Заметка к эталону: «пиджак на картинке — не считается, он уже снят»
        # (кадры 48/49 круга 2: пиджак и надет, и сложен на руке).
        line += (notes or {}).get(nm, "")
        lines.append(line)
    if has_plan and len(refs) >= i + 1:
        lines.append(ROLE_PLAN.format(i=i + 1, fn=refs[i].name))
        i += 1
    if has_style and len(refs) >= i + 1:
        lines.append(ROLE_STYLE.format(i=i + 1, fn=refs[i].name))
    if len(face_names) > 1:
        lines.append(ROLE_MIX)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Грамматика панели манхвы. Разбор 90 кадров трёх топовых каналов и корейских
# пособий по вебтуну: наша картинка отличалась от панели не крупностью по
# бланку, а исполнением — всё в фокусе, вся комната видна, лица маленькие, ни
# одного знака эмоции, ни одной линии эффекта. У них крупный план — лицо на
# 60–80% высоты панели на абстрактном или упрощённом фоне, на лице знак эмоции
# (капля, вена, линии ужаса), за головой лучи или линии состояния; удар — белая
# звезда в точке контакта и лучи к краям. Класс панели считается из полей,
# которые бланк уже заполняет (panel, shot_size, emotion, sfx), поэтому это
# работает для любого текста, а не для одного ролика.
# ---------------------------------------------------------------------------
def others_in_close(frame, names: Optional[Dict[str, str]]):
    """Крупный план при двух и больше в кадре: кто ещё, и действует ли он.

    Круг 2 (§3 J, §4 п.8): 18 промптов с FRAMING «reaction close-up… The whole
    subject is X's face… NO furnished room» при CAST «exactly 2» — второй
    исчезал (MISSING 16: рукопожатие без Эдриана) или вместо действия рисовалось
    одно лицо (ACTION 315: крупный план деда вместо чашки). «Действует» — назван
    в pose или contact: там глаголы; prompt_en называет всех всегда и признаком
    служить не может. Возвращает (список остальных, действует ли кто-то из них).
    """
    who = [c for c in (getattr(frame, "in_frame", None) or []) if c in (names or {})]
    focus = _txt(getattr(frame, "focus", "") or "")
    rest = [c for c in who if c != focus] if focus in who else who[1:]
    if not rest or not names:
        return rest, False
    # Полный словарь имён, а не только «остальные»: иначе «Adrian's grandfather»
    # засчитается Эдриану по первому слову (см. mentioned).
    # Поле кадра может прийти объектом (21.09: 'dict' has no attribute 'count', строка ниже).
    pose = _txt(getattr(frame, "pose", "") or "")
    acting = [c for c in mentioned(f"{pose} {_txt(getattr(frame, 'contact', '') or '')}", names)
              if c in rest]
    # Бланк пишет позу по одному человеку на клаузу через «;» — «Emma steadies
    # the sheet…; he rests both shoulders against the pillow» (130), «…; his
    # grandfather lowers his free hand» (313), «…; the reporter cradles his
    # camera» (71). Второй назван местоимением или ролью, но он действует.
    if not acting and pose.count(";") >= 1:
        return rest, True
    return rest, bool(acting)


# 27.09.2026 (аудит 2, close при нескольких героях): «рука второго с предметом у края» — только когда
# у второго в позе правда предмет в руке или касание. Признак others_in_close срабатывает и на
# «Barbara keeps her hands together in her lap» (97% людных close) — фраза заказывала бы реквизит.
_В_РУКЕ = _re_mod.compile(
    r"\b(?:hold|grip|clutch|grab|clasp|carr|cradl|pour|tilt|offer|present|pass|giv|tak|lift|rais|keep|"
    r"press|pull|push|touch|squeez|stead|support|wield|brandish|brac|sip|pick|plac|put|set|slid|fill|"
    r"hug|catch|throw|wip|shak|tug|yank|drag|reach|pinch|aim|point|cups|cupp|lower)"
    r"(?:e|s|es|ed|d|ing|y|ies|ied|ping|ped|ting|ted|ging|ged|bing|bed)?\s+(?=(.*))", _re_mod.IGNORECASE)
_ДО_ПРЕДЛОГА = _re_mod.compile(
    r"(?:^|\s+)(?:near|beside|on|at|to|in|into|onto|against|with|toward|towards|by|before|behind|over|"
    r"under|around|across|beneath|below|above|from|off|along|between|while|as|and|or|then|still|"
    r"together|loosely|tightly|lowered|raised|up|down|out|forward|back|level|apart|"
    r"motionless|straight|steady|upright|upward|downward|backward|aside|away)\b|[,;.]",
    _re_mod.IGNORECASE)
_ПРЕДМЕТ = _re_mod.compile(r"\b(?!(?:a|an|the|his|her|their|its|one|both|two|each|this|that|some)\b|\w+ly\b)\w+",
                           _re_mod.IGNORECASE)
_СВОЁ_ТЕЛО = _re_mod.compile(
    r"\b(?:hands?|arms?|forearms?|fingers?|fingertips|palms?|fists?|wrists?|elbows?|shoulders?|heads?|"
    r"chin|face|eyes?|gaze|breath|posture|stance|back|body|knees?|legs?|feet|foot|torso|chest|lips|cheeks?|"
    r"mouth|ears?|neck|forehead|nose|temples?|"
    r"jaw|brows?|hair|position|pose|balance|composure|expression|distance|ground|silence|himself|"
    r"herself|themselves|itself|smile|nod|look|glance|shrug|bow|sigh|weight|claws?|paws?|wings?|tail)\b",
    _re_mod.IGNORECASE)
_ЧУЖОЕ_ТЕЛО = (r"['’]s\s+(?:\w+\s+){0,2}?(?:hands?|arms?|forearms?|fingers?|fingertips|palms?|wrists?|"
               r"shoulders?|elbows?|torso|waist|foot|legs?|knees?|sleeves?|cheek|neck|collar)\b")
_ЕГО_РУКА = r"['’]s\s+(?:\w+\s+){0,2}?(?:hands?|arms?|forearms?|fingers?|fingertips|palms?|wrists?|sleeves?)\b"


def acting_with_object(frame, names: Dict[str, str], rest: List[str]) -> bool:
    """Держит ли кто-то из rest предмет, касается ли другого, держат ли его за руку — по pose и contact.
    Лежащий, кого трогают за щёку или шею, и сидящий «hands in her lap» — не действуют."""
    pose = _txt(getattr(frame, "pose", "") or "")
    if _re_mod.search(r"\bshak\w*\s+hands\b|\bhandshake\b|\bjoined hands\b|\bhand in hand\b",
                      pose, _re_mod.IGNORECASE):
        return True
    части = _re_mod.split(r"[;.]", pose)
    for i, cl in enumerate(части + [_txt(getattr(frame, "contact", "") or "")]):
        who = mentioned(" ".join(cl.split()[:2]), names)        # чья клауза: назван первым
        # безымянная не первая — тоже его: «…; his grandfather tilts the pot»; contact — ничья
        his = i < len(части) and (any(c in rest for c in who) if who else i >= 1)
        objs = [_ДО_ПРЕДЛОГА.split(m.group(1), 1)[0] for m in _В_РУКЕ.finditer(cl)] if his else []
        if (any(_ПРЕДМЕТ.search(o) and not _СВОЁ_ТЕЛО.search(o) for o in objs)
                or his and _re_mod.search(r"\bwith [^,;]*? in (?:one|his|her|each|both|their|the other) "
                                          r"hands?\b", cl, _re_mod.IGNORECASE)):
            return True     # «tilts the pot», «with the folder in one hand»
        for c, nm in names.items():     # он трогает другого или его держат за руку, рукав
            if (nm and c not in who and (his or c in rest)
                    and any(_re_mod.search(r"\b" + _re_mod.escape(x) + (_ЧУЖОЕ_ТЕЛО if his else _ЕГО_РУКА),
                                           cl, _re_mod.IGNORECASE) for x in {nm, nm.split()[0]})):
                return True
    return False


def panel_class(frame, names: Optional[Dict[str, str]] = None) -> str:
    """Класс панели. Крупность (shot_size) главнее ярлыка panel: бланк ставит
    panel="reaction" и на средний план с тремя людьми — а «лицо на 80% высоты»
    противоречит STAGING и DISTANCE такого кадра (кадр 187 первого A/B). Поэтому
    «лицо во всю панель» — только когда крупность close/extreme_close, «деталь» —
    только на insert; ярлык panel решает лишь impact и establishing, которые
    не меняют состав кадра, а добавляют к нему.
    """
    p = (getattr(frame, "panel", "") or "").lower()
    sh = (getattr(frame, "shot_size", "") or "").lower()
    if p == "impact":
        return "impact"
    # Лица нет — класса «лицо на 60–80%» быть не может. Кадр 103 третьего
    # текста: panel=detail, shot=close, face_visible=False → шёл как close с
    # «reaction close-up… face 60–80%», а в кадре только рука на папке.
    if sh in ("extreme_close", "close") and not getattr(frame, "face_visible", True):
        return "detail"
    if sh == "extreme_close":
        return "eyes"
    if sh == "close":
        # 27.09.2026 (аудит 2, close при нескольких героях): крупный план остаётся крупным и при
        # действующем втором — тот виден частью у края (panel_block). Прежний перевод в medium
        # давал заданию «по пояс, оба целиком» при shot_size=close раскадровки.
        return "close"
    if sh in ("insert", "detail"):     # бланк изредка пишет detail вместо insert
        return "detail"
    if p == "establishing" or sh == "wide":
        return "wide"
    return "medium"


# Знаки эмоции (манпу) — рисованные метки, не буквы; и фон-состояние за головой.
EMOTION_FX = {
    "shocked": ("pupils shrunk to pin-points, the highlight gone from the eyes, one bead "
                "of sweat on the temple",
                "straight focus lines converging on the face from every edge of the panel"),
    "afraid": ("a bead of sweat sliding down the temple, short vertical hatching lines on "
               "the upper forehead",
               "thin vertical hatching falling from the top edge over a dark flat tone"),
    "angry": ("a cross-shaped throbbing vein mark on the temple",
              "jagged concentric stir lines radiating from the head"),
    "hurt": ("one bead of sweat, eyes glassy",
             "a flat muted colour field with a soft screentone gradient"),
    "sad": ("eyelids low, a faint shadow band across the eyes",
            "a flat dim tone with thin vertical drip lines from the top edge"),
    "determined": ("eyes narrowed with one sharp highlight",
                   "a flat two-tone field with a few sharp diagonal focus strokes behind "
                   "the head"),
    "calm": ("", "the room only hinted with a few loose strokes, low contrast, no detail"),
    "blank": ("a shadow band across the upper face",
              "an empty flat tone, nothing drawn behind the head"),
}


def panel_block(frame, names: Dict[str, str]) -> str:
    """Что именно делает этот кадр ПАНЕЛЬЮ манхвы, а не иллюстрацией к сцене."""
    k = panel_class(frame, names)
    emo = (getattr(frame, "emotion", "") or "").lower()
    fx, bg = EMOTION_FX.get(emo, ("", "a simplified flat-toned background"))
    nm = names.get(getattr(frame, "focus", "") or "", "")
    who = (nm + "'s") if nm else "the subject's"
    sfx = (getattr(frame, "sfx", "") or "").strip()
    if k == "close":
        t = (f"FRAMING: reaction close-up. The whole subject is {who} face, and it takes "
             f"60-80% of the picture height; hair and shoulders may run off the edges. "
             f"Behind the head there is NO furnished room: {bg}. ")
        # Второй человек есть, но ничего не делает: он может быть у края, а не
        # «исчезнуть» — CAST его требует (круг 2, §7 п.6).
        rest, _acting = others_in_close(frame, names)
        if rest:
            t += (f"The other {'person' if len(rest) == 1 else 'people'} "
                  f"({', '.join(names[c] for c in rest)}) may be partly "
                  f"visible at the edge of the picture — a shoulder, a hand, the side of "
                  f"the head. ")
            if _acting and acting_with_object(frame, names, rest):
                # 27.09.2026 (аудит 2): у второго предмет в руке или касание — рука видна у края,
                # иначе теряется действие (круг 2: 315 — чашка, 16 — рукопожатие).
                t += ("Their acting hand and whatever it holds stay clearly visible at that edge, "
                      "so the action still reads; the rest of them is cropped by the frame. ")
        if fx:
            t += f"Manhwa emotion marks drawn on the face: {fx}. "
        # Абстрактный фон тянет модель в «страницу комикса»: кадр 193 A/B вышел
        # с белым полем вокруг рисунка. Запрет полей повторяем прямо здесь.
        t += ("Every line of the face is sharp; the background carries almost no detail, "
              "yet the drawing still runs to all four edges of the canvas — no white "
              "margin, no border around the picture.")
    elif k == "eyes":
        t = (f"FRAMING: extreme close-up strip. Only {who} eyes, brows and the bridge of the "
             f"nose fill the whole picture; pupils sharp with one highlight. Behind: {bg}. ")
        rest_e, _ = others_in_close(frame, names)
        if rest_e:
            t += (f"The other named {'person is' if len(rest_e) == 1 else 'people are'} "
                  f"outside this strip: only the eyes above are drawn. ")
        if fx:
            t += f"Emotion marks: {fx}."
    elif k == "detail":
        t = ("FRAMING: insert detail. The named object or hands fill most of the picture, drawn "
             "large and sharp. The named people are present only as these hands or as the part "
             "of the body that touches the object; their faces stay outside the frame. The "
             "background is reduced to a flat tone or a few loose strokes of the room.")
    elif k == "impact":
        t = ("FRAMING: impact moment. A white star-shaped flash at the exact point of contact, "
             "radial impact lines shooting to the edges of the picture, small debris and splinters in "
             "the air, the struck body or object thrown along a diagonal; the room is still "
             "recognisable but its detail gives way to the effect lines.")
    elif k == "wide":
        t = ("FRAMING: establishing view. The place is the subject: full architecture, "
             "furniture, light and weather readable at a glance; people are small in it.")
    else:
        _sz = (getattr(frame, "shot_size", "") or "").lower()
        if _sz == "full":
            t = ("FRAMING: full-figure shot. The whole body is inside the picture, from the top "
                 "of the head to the shoes, with the floor under the feet visible and a hand's "
                 "width of empty space above the head; the room is present but simplified — "
                 "fewer lines and flatter tones than the people, so the figures pop out of it. ")
        else:
            t = (f"FRAMING: dialogue medium shot. Waist up; faces take about a third of the picture "
                 f"height, hands visible; the room is present but simplified — fewer lines and "
                 f"flatter tones than the people, so the figures pop out of it. ")
        if fx and emo not in ("calm", "blank"):
            t += f"Emotion marks on {who} face: {fx}."
    if sfx and k not in ("impact", "wide"):
        t += (" Something moves suddenly here: speed lines parallel to the movement and a "
              "faint after-image trailing the moving limb or object.")
    return t.strip()


# Сколько первых кадров ролика рисуются во всю ширину. Замер 26.09: вертикальная панель
# занимает 42% ширины экрана, остальное — размытые поля. В хуке таких кадров было 9–12 из 14,
# то есть первые сорок секунд зритель смотрит на узкую полосу посреди размытой каши.
HOOK_WIDE_FRAMES = 14


def aspect_for(frame) -> str:
    """Пропорция панели по её типу — замер конкурентов: почти всё вертикальное,
    во всю ширину только установочные и полосы «одни глаза» (~13% кадров).
    Исключение — хук: первые кадры идут во всю ширину, чтобы картинка занимала весь экран."""
    try:
        if int(getattr(frame, "idx", 0) or 0) <= HOOK_WIDE_FRAMES:
            return "16:9"
    except (TypeError, ValueError):
        pass
    k = panel_class(frame)
    if k == "eyes":
        return "16:9"
    if k == "wide" and (getattr(frame, "panel", "") or "").lower() == "establishing":
        return "16:9"
    return "3:4"


# Облако реплики внутри рисунка (MANGA_QUOTE_MODE=panel). Проба 17.09.2026
# (tasks/gpt_battle/bubble_probe): GPT Image 2.5 рисует облако с хвостиком к
# говорящему, кириллица верна 4 из 4 — поверх общего запрета текста, поэтому
# блок идёт ПОСЛЕДНИМ, а запрет NEG остаётся.
# Говорящий за кадром (quote_by нет в in_frame): хвостик к краю панели, иначе генератор
# направит его на того, кто нарисован, и реплика достанется другому герою.
_QUOTE_PAIRS = {"«": "»", "„": "“”", "“": "”", '"': '"', "‚": "‘’", "‘": "’", "»": "«"}


def _unquote(quote: str) -> str:
    """Снять с краёв реплики ПАРНЫЕ кавычки («…», „…”, “…”, "…"). Непарная кавычка и апостроф
    остаются: «Aspetta un po'» — не «Aspetta un po», «Powiedział: „nie”» — целиком."""
    q = (quote or "").strip()
    while len(q) >= 2 and q[0] in _QUOTE_PAIRS and q[-1] in _QUOTE_PAIRS[q[0]]:
        q = q[1:-1].strip()
    return q


def bubble_block(quote: str, speaker_offscreen: bool = False, lang: str = "Russian",
                 speaker_name: str = "") -> str:
    """Язык и азбука облака — язык ролика (languages.script): для польского — «Polish text …
    uppercase Latin», а не кириллица. Русская строка НЕ побайтно как на VPS (672ec4c): в 3a8042a
    добавлено «without quotation marks». С краёв реплики снимаются только парные кавычки (_unquote)."""
    import languages as LG
    кто = (speaker_name or "").strip()
    if speaker_offscreen:
        tail = ("a short tail pointing to the nearest edge of the panel — the speaker is outside this panel, "
                "so the tail points at no character in the picture")
    elif кто:
        # Имя обязательно: без него генератор ведёт хвостик к главному герою кадра, а не
        # к говорящему (кадр 33: в кадре трое, говорит экономка, хвостик ушёл к госпоже Ли).
        tail = (f"a short tail whose tip touches {кто}'s mouth and nobody else's — "
                f"{кто} is the one speaking these words, even if another character is the "
                f"main subject of this frame")
    else:
        tail = "a short tail pointing to the mouth of the character who is speaking"
    quote = _unquote(quote)
    return ("SPEECH BUBBLE: inside this panel draw exactly ONE white manhwa speech bubble with a black "
            f"outline and {tail}. The bubble "
            f"contains exactly this {lang} text, spelled letter for letter in clean bold uppercase {LG.script(lang)} "
            f"comic lettering, nothing added or changed, without quotation marks: «{quote}». The bubble covers at most 20% of the "
            "panel and never covers a face. No other text anywhere.")


def _txt(value) -> str:
    """Поле кадра строкой. Модель иногда отдаёт объект вместо строки, и промпт падал.

    20.09 ночью итальянский ролик семь раз встал на POSE, пришедшем словарём
    (`'dict' object has no attribute 'strip'`, frameprompt.py:906).
    """
    from sheets import plain
    return plain(value)


_TEXT_FIELDS = ("prompt_en", "staging", "pose", "contact", "focus", "distance", "gaze",
                "camera_pos", "changed", "shot_size", "panel", "layer")


def _normalise_frame(frame) -> None:
    """Текстовые поля кадра — строками, один раз на входе в сборку промпта.

    Модель иногда отдаёт поле объектом или списком. 21.09 ночью это роняло ролики трижды
    подряд в разных местах (POSE на .strip, поза на .count, фокус как ключ словаря) —
    латать каждое место по отдельности бесполезно, их больше десятка.
    """
    for name in _TEXT_FIELDS:
        value = getattr(frame, name, None)
        if value is not None and not isinstance(value, str):
            try:
                setattr(frame, name, _txt(value))
            except Exception:       # замороженный или чужой объект — оставляем как есть
                pass


def build(frame, scene, names: Dict[str, str], refs: List[images.Ref],
          who_with_face: List[str], has_plan: bool,
          heights: Optional[Dict[str, str]] = None,
          traits: Optional[Dict[str, str]] = None,
          locks: Optional[Dict[str, str]] = None,
          kinds: Optional[List[str]] = None,
          memory=None, has_style: bool = False, quote_bubble: str = "",
          speaker_offscreen: bool = False, lang: str = "Russian") -> str:
    """frame — Frame, scene — Scene, names — cid → имя героя.
    quote_bubble — текст реплики, который генератор рисует облаком внутри кадра.
    lang — язык ролика (ключ languages.LANGS): на нём генератор пишет облако и слово-звук."""
    _normalise_frame(frame)
    who = [c for c in (frame.in_frame or []) if c in names]
    parts: List[str] = []

    # Что видно из точки съёмки — считаем кодом по плану, а не оставляем модели.
    # Для воспоминания — нет: план, поверхности и «что видно» принадлежат комнате
    # НАСТОЯЩЕГО, а воспоминание происходит в другом месте (кадр 105: детская
    # комната в семье получала чертёж больничной палаты).
    frame._view_line = ""
    if (memory is None and scene is not None and getattr(scene, "room", None)
            and getattr(frame, "camera_pos", "")):
        try:
            from floorplan import from_json, view_from
            _plan = from_json(f"scene{getattr(scene, 'num', 0)}", scene.room)
            frame._view_line = view_from(_plan, frame.camera_pos)
        except Exception:
            frame._view_line = ""

    # Приметы в легенде эталона — из паспорта ВОСПОМИНАНИЯ, если кадр в нём:
    # иначе рядом с детским листом стоит «young man, dark hair», и модель рисует
    # взрослого (кадр 100: мальчик семи лет вышел мужчиной в костюме).
    def _trait(c):
        if memory is not None and c in (getattr(memory, "who", None) or {}):
            w = memory.who[c] or {}
            return ", ".join(x for x in (w.get("age", ""), w.get("look", "")) if x)[:80]
        return (traits or {}).get(c, "")
    # Что герой уже снял по ходу сцены (costume_state «jacket off») — на эталоне
    # это ещё надето. Кадры 48/49 круга 2: пиджак и надет (с листа), и сложен на
    # руке (по BY NOW). Эталону — прямая оговорка, строке одежды — вычерк.
    off_by = {c: items_off((getattr(frame, "costume_state", None) or {}).get(c, ""))
              for c in names}
    notes = {names[c]: (f" Ignore the {' and the '.join(off_by[c])} seen in this "
                        f"reference — it is off in this shot.")
             for c in who_with_face if off_by.get(c)}
    roles = _roles(refs, [names[c] for c in who_with_face], has_plan,
                   {names[c]: _trait(c) for c in who_with_face}, kinds, has_style,
                   notes=notes)
    if roles:
        parts.append(roles)

    # 1. Что происходит — главный блок, он же самый длинный.
    parts.append(f"WHAT HAPPENS: {_txt(frame.prompt_en).strip()}")
    parts = [x for x in parts if x]

    # Закрытый состав кадра. Без него модель дорисовывает людей «для массовки»,
    # и они получают лицо героя. Плюс явное назначение главного самым крупным:
    # иначе на общем плане лицо героя занимает двадцать пикселей и не читается.
    # Чьё плечо на переднем плане при over_shoulder — решаем ДО строки CAST,
    # потому что в CAST об этом надо сказать. При focus='' раньше в «other»
    # попадал сам герой: «over the shoulder of Adrian» при единственном
    # Эдриане — и генератор рисовал второго Эдриана (кадры 103, 308).
    os_owner = ""
    if getattr(frame, "angle", "") == "over_shoulder" and who:
        lead_cid = frame.focus if getattr(frame, "focus", "") in who else who[0]
        rest = [c for c in who if c != lead_cid]

        # плечо не отдаём лежащему: если герой по осанке или позе лежит, его плечо
        # на переднем плане означает, что он нарисован дважды — спереди и там, где лежит
        # (замер 26.09: кадры 12 и 13 ролика 27b42e7c — героиня и спереди, и на кровати)
        def _лежит(cid_):
            п = getattr(frame, "posture", None)
            п = (п.get(cid_, "") if isinstance(п, dict) else (п or ""))
            текст = f"{п} {getattr(frame, 'pose', '') or ''} {getattr(frame, 'staging', '') or ''}".lower()
            имя = (names.get(cid_) or "").lower()
            if имя and имя in текст:
                рядом = текст[max(0, текст.find(имя) - 60):текст.find(имя) + 120]
            else:
                рядом = str(п).lower()
            return any(w in рядом for w in ("lying", "lies", "lay ", "reclin", "on the bed",
                                            "in the bed", "unconscious", "collaps"))

        rest = [c for c in rest if not _лежит(c)] or []
        os_owner = names[rest[0]] if rest else ""
    # Кто назван в тексте кадра, но не в кадре: рука из-за кадра (contact) —
    # своя история (LIMB ниже); остальным — прямой запрет. Иначе «toward
    # someone beyond the edge» рисуется силуэтом (EXTRA 25/101/128/193/209).
    limb_cids = contact_cids(frame, names)
    absent_mem = set((getattr(memory, "absent", None) or []) if memory is not None else [])
    off_cids = [c for c in mentioned(f"{frame.prompt_en or ''} {getattr(frame, 'staging', '') or ''}",
                                     names)
                if c not in who and c not in limb_cids and c not in absent_mem]
    off_line = " ".join(f"{names[c]} is NOT in this picture — not a shoulder, not an arm, "
                        f"not a silhouette; that side of the frame stays empty."
                        for c in off_cids)
    if who:
        focus_id = _txt(getattr(frame, "focus", "") or "").strip()
        lead = names.get(focus_id) if focus_id in names else names[who[0]]
        n = len(who)
        # Точный счёт обязателен. Без него модель дорисовывает «для массовки»
        # ещё одного человека и делает его КОПИЕЙ героя: в кадре 26 первого
        # прогона были главный герой и его двойник с тем же синяком на той же
        # скуле. Формулировка «рисуй только этих» этого не остановила —
        # останавливает только число плюс прямой запрет копии.
        line = (f"CAST: exactly {n} named "
                f"{'person' if n == 1 else 'people'} in this frame — "
                f"{', '.join(names[c] for c in who)}. "
                f"Each of them appears ONCE and only once. Never draw a second copy of "
                f"the same person anywhere in the frame, not in the foreground, not in "
                f"the background, not reflected.")
        if n > 1 and lead and not getattr(frame, "distance", ""):
            # Строка про «самого крупного» нужна только там, где дистанция не
            # задана: иначе она играет буквально и делает героя гигантом рядом
            # с фигуркой, хотя оба сидят за одной партой.
            line += (f" {lead} is the main subject: face clearly readable, "
                     f"placed so the eye goes to them first.")
        elif n > 1 and lead:
            line += f" The eye should go to {lead} first."
        if os_owner:
            line += (f" {os_owner} appears ONLY as a back and shoulder in the near "
                     f"foreground and nowhere else in the picture.")
        if n > 1:
            # Чем люди отличаются — одной строкой: 297 круга 2 (дед + Эдриан)
            # вышел двумя седыми, 14/272 — то же на wide. Цвет волос из примет
            # листа, запасной путь — первая фраза паспорта внешности.
            hc = [(names[c], hair_colour((traits or {}).get(c, ""), (locks or {}).get(c, "")))
                  for c in who]
            hc = [(nm_, h) for nm_, h in hc if h]
            # Только когда цвета РАЗНЫЕ: «tells them apart: X — dark hair; Y — dark
            # hair» — бессмыслица (run_v: 362 из 392 таких строк, все герои темноволосые).
            if len(hc) > 1 and len({h for _nm, h in hc}) > 1:
                line += (" Hair tells them apart: " +
                         "; ".join(f"{nm_} — {h} hair" for nm_, h in hc) +
                         ". Never swap these hair colours between people.")
        if off_line:
            line += " " + off_line
        parts.append(line)
    elif off_line:
        parts.append("CAST: no named character is in this frame. " + off_line)

    # 2. Где кто стоит. Без якоря к мебели персонаж телепортируется по комнате
    #    между соседними кадрами, даже когда фон нарисован верно.
    #    Три разные вещи — три отдельных строки: слитые в одну через точку с
    #    запятой, они читаются как один длинный список и теряют вес.
    stage = []
    if frame.staging:
        stage.append(_txt(frame.staging).strip().rstrip("."))
    # Экранная сторона добавляется ТОЛЬКО когда камера не переезжала с прошлого
    # кадра. Иначе бланк требует физически невозможного: если камера ушла к
    # противоположной стене, стороны героев ОБЯЗАНЫ поменяться местами, а
    # требование «он всегда слева» с этим спорит — 201 пара соседних кадров из
    # 479 в прошлом прогоне попадала ровно в этот конфликт, и побеждало то одно,
    # то другое случайным образом. Кто где стоит относительно МЕБЕЛИ — сказано
    # в staging, и это работает с любой точки съёмки.
    if getattr(frame, "_same_camera", False):
        for cid in who:
            side = scene.side_of(cid) if scene else ""
            if side and side != "centre":
                stage.append(f"{names[cid]} stays in the {side} part of the frame, "
                             f"exactly as in the previous shot")
    if stage:
        parts.append("STAGING: " + "; ".join(stage) + ".")
    post = getattr(frame, "posture", None)
    if isinstance(post, dict) and post:
        # Осанка ПО КАЖДОМУ герою: в одном кадре отец стоит, а сын сидит, и одно
        # значение на весь кадр ставило сидящего на ноги.
        # Только про тех, кто в ЭТОМ кадре или чья рука входит из-за края. Прежний фильтр
        # «если герой известен ролику» описывал осанку людей, которых в кадре нет: художнику
        # говорили «в кадре один человек» и тут же описывали троих (221 кадр из 3646).
        pl = [f"{names.get(c, c)} is {v}" for c, v in post.items()
              if c in names and (c in who or c in limb_cids)]
        if pl:
            parts.append("POSTURE: " + "; ".join(pl) +
                         ". The pose below refines this and never replaces it.")
    elif isinstance(post, str) and post:
        parts.append(f"POSTURE: {post}. The pose below refines this and never replaces it.")
    if frame.pose:
        parts.append(f"POSE: {_txt(frame.pose).strip().rstrip('.')}.")
    if getattr(frame, "contact", ""):
        limb = (f"LIMB ENTERING THE FRAME: {_txt(frame.contact).strip().rstrip('.')}. "
                f"Draw it attached to that person and to nobody else; no stray hand.")
        # Хозяин руки за кадром: рукав из паспорта сцены и прямое «это не
        # рукопожатие с чужим» — 147/152/153/155/163 рисовались именно так.
        # Лист хозяина больше НЕ прикладывается (SLEEVE_REF, §4 п.1 круга 2):
        # пол и кожа хозяина — словами из примет и паспорта внешности, рукав —
        # из одежды на ЭТОТ кадр (снятый пиджак в рукаве не участвует: 48).
        for c in limb_cids:
            sleeve = (_sleeve_bit(costume_now(scene, c, frame)[0])
                      if memory is None else "")
            # Пол — из примет листа И паспорта внешности (в воспоминании
            # приметы свои, без «man/woman»: кадр 318); кожа — из паспорта листа.
            hand = hand_of(f"{_trait(c)} {(traits or {}).get(c, '')}", (locks or {}).get(c, ""))
            if hand and sleeve:
                limb += (f" The hand {hand}, coming out of the sleeve of {sleeve} — "
                         f"{names[c]}'s sleeve.")
            elif sleeve:
                limb += f" The hand comes out of the sleeve of {sleeve} — {names[c]}'s sleeve."
            elif hand:
                limb += f" The hand {hand}."
            limb += (f" This is NOT a handshake with a stranger — the hand belongs to "
                     f"{names[c]}, who is otherwise outside the picture.")
        parts.append(limb)
    if getattr(frame, "distance", ""):
        parts.append(f"DISTANCE: {_txt(frame.distance).strip().rstrip('.')}. Keep these distances "
                     f"true — do not push one person into the foreground to make them bigger.")
    if frame.gaze:
        parts.append(f"EYELINE: {_txt(frame.gaze).strip().rstrip('.')}.")

    # Неизменные приметы каждого героя — дословно одной и той же строкой во все
    # кадры, где он есть. Картинка эту строку не заменяет: между кадрами она
    # интерпретируется заново, и тон волос дрейфует (замерено глазами на кадрах
    # 203-206: у отца седина то появляется, то исчезает).
    if locks:
        ll = [f"{names[c]}: {locks[c]}" for c in who if c in locks]
        if ll:
            parts.append("NEVER CHANGES — " + "; ".join(ll) + ".")

    # Относительный рост — только когда в кадре двое и больше: иначе сравнивать
    # не с кем, а лишняя строка отбирает вес у остальных.
    if heights and len(who) > 1:
        # heights[cid] — место героя в общем ряду по росту (0 = самый высокий).
        # Строим строку только из тех, кто в ЭТОМ кадре: свободная заметка
        # раньше ссылалась на людей, которых в кадре нет.
        rank = [(int(heights[c]), c) for c in who if str(heights.get(c, "")).isdigit()]
        # Строку роста выводим ТОЛЬКО когда в ряду есть ВСЕ, кто в кадре.
        # Частичный ряд хуже отсутствия: он утверждает порядок между двумя,
        # умалчивая о третьем, и модель достраивает его как придётся.
        if len(rank) > 1 and len(rank) == len(who):
            rank.sort()
            parts.append("HEIGHT: tallest to shortest in this frame — " +
                         ", then ".join(names[c] for _, c in rank) +
                         ". Keep that order visible when they stand together.")

    # Эмоция ярлыком («angry») даёт «недоволен» вместо «в ярости» — проверено на
    # парах генераций. Работает описание мимики: что делают брови, глаза и рот.
    if frame.emotion and frame.face_visible:
        nm = names.get(frame.focus or (who[0] if who else ""), "")
        face = EMOTION_FACE.get((frame.emotion or "").lower(), frame.emotion)
        parts.append(f"EXPRESSION: {nm + ' — ' if nm else ''}{face}.")

    # 3. Камера: крупность и угол разными полями, плюс точка съёмки по плану.
    ang = ANGLE_WORDS.get(frame.angle, "at eye level")
    if frame.angle == "over_shoulder":
        # Без указания, ЧЬЁ плечо, модель дорисовывает плечо третьего человека —
        # и рисует его копией героя. Плечо принадлежит тому, кто НЕ в фокусе;
        # если такого нет (герой один) — плеча нет вовсе, только край мебели.
        ang = (f"over the shoulder of {os_owner}, whose back and shoulder are in the "
               f"near foreground and whose face is not visible"
               if os_owner else
               "at eye level; the near foreground holds only the edge of a desk or a "
               "doorframe — no second person")
    # 27.09.2026 (аудит 2): detail — то же, что insert (см. panel_class); раньше CAMERA
    # получала запасное «medium shot, waist up» рядом с FRAMING insert detail.
    крупность = "insert" if frame.shot_size == "detail" else frame.shot_size
    shot = SHOT_WORDS.get(крупность, "medium shot, waist up")
    if frame.shot_size in ("close", "extreme_close") and not getattr(frame, "face_visible", True):
        # Крупно, но лица нет (кадр 103: рука на папке) — «лицо на 60–80%»
        # из SHOT_WORDS спорило бы с содержанием.
        shot = "close-up on the hands or the object described, the face stays out of frame"
    pk = panel_class(frame, names)
    if крупность == "insert":
        # Врезки на руки и предметы — самое слабое место манеры: там нет лица,
        # за которое модель держится, и она сваливается в фотографию.
        # Замер: оба худших по манере кадра ролика — insert без лица.
        parts.append("Even in this tight detail shot the drawing style does not change: "
                     "same flat cel shading, same clean ink outline on every object, "
                     "same simplified background as in the wider shots of this scene.")
        # Деталь без указания высоты камеры вырождается в вид сверху: в кадре 10
        # получился план стола, из-под которого растут руки без тела.
        if (frame.angle or "") == "high":
            # Бланк сам просит «сверху»: анти-«сверху» хвост давал в одной фразе
            # «never straight down from above… from above, looking down» — 25
            # вставок круга 2, среди дефектных 36 61 230 285. Один ракурс, без спора.
            shot += (", from a high angle, tilted down at the detail described, but "
                     "not straight down — never a flat bird's-eye view")
            ang = ""
        else:
            shot += (", seen from the standing person's own eye level and slightly to "
                     "the side — never straight down from above, never a bird's-eye view")
    cam = [x for x in (shot, ang) if x]
    if frame.camera_pos:
        cam.append(_txt(frame.camera_pos).strip().rstrip("."))
    # Точка съёмки задаётся ТОЛЬКО текстом и сторонами света. Проверено:
    # нарисованная на плане стрелка камеры игнорируется, а без текстового
    # описания камера залипает в один ракурс во всех трёх попытках из трёх.
    if pk in ("wide", "medium", "impact"):
        # «at eye level, about 1.6 m» — только при ракурсе с уровня глаз: с
        # «from above, looking down» в одном предложении это спорило в 47
        # кадрах третьего текста (145, 209 среди DUP/EXTRA).
        if (getattr(frame, "angle", "") or "") in ("eye", ""):
            tail = (". The camera stands inside the room at eye level, about 1.6 m from "
                    "the floor. Draw the room exactly as a person standing at that spot "
                    "would see it.")
        else:
            tail = (". The camera stands inside the room at the spot described. Draw the "
                    "room exactly as it looks from there at that angle.")
        parts.append("CAMERA: " + ", ".join(cam) + tail)
    else:
        # Крупный план и деталь: комната за спиной лишь угадывается, иначе
        # модель рисует «иллюстрацию к сцене» — маленькое лицо в полной комнате.
        # Не «soft»: генератор читает это как боке — врезки 225/230/285 и
        # крупные 247/061/078 ушли в объективное размытие.
        parts.append("CAMERA: " + ", ".join(cam) +
                     ". The camera is close to the subject; only a fragment of the room "
                     "can be behind it, and that fragment is drawn with fewer lines and "
                     "flat tones, edges stay sharp, no blur.")
    parts.append(panel_block(frame, names))

    # 4. Комната — один раз, а не трижды. У воспоминания своя комната (блок THE PLACE
    #    OF THIS MEMORY ниже); стены, план и вид из точки съёмки настоящей сцены
    #    туда не идут — это другой мир.
    if scene and memory is None and (scene.where or scene.plan_line):
        # своё место кадра сильнее места сцены: сцена — глава сюжета и ходит по комнатам.
        # Чертёж, поверхности и вид с точки съёмки описывают КОМНАТУ СЦЕНЫ, поэтому там,
        # где кадр ушёл в другое место, они не прикладываются: иначе сад поместья рисуется
        # больничным коридором (кадры 30–47 ролика e05262b5, замер 25.09).
        своё = _txt(getattr(frame, "location", "") or "").strip()
        чужая_комната = bool(своё) and bool(scene.where) and своё.lower() != str(scene.where).lower()
        room = f"THE ROOM: {своё if чужая_комната else scene.where}."
        if чужая_комната:
            room += (" This shot happens away from the room of the rest of the scene: draw this "
                     "place, not that one, and keep the same light and time of day.")
        view = "" if чужая_комната else getattr(frame, "_view_line", "")
        if view:
            room += " " + view
        sf = "" if чужая_комната else (scene.surfaces_block() if hasattr(scene, "surfaces_block") else "")
        if sf:
            room += " " + sf
        if scene.plan_line and not чужая_комната and pk in ("wide", "medium", "impact"):
            room += (f" {scene.plan_line}. Seen from this new camera position the room "
                     f"keeps the same architecture: the door and the window stay where "
                     f"the plan puts them, the furniture keeps the same distances from "
                     f"the walls and from each other, the ceiling height does not change.")
        parts.append(room)

    # 5. Постоянные сцены — дословно одинаковые во всех её кадрах.
    #    У воспоминания — СВОИ постоянные: другое место, свет, возраст, состав.
    if memory is not None:
        parts.append(f"THE PLACE OF THIS MEMORY: {memory.where}." if memory.where else "")
        c = memory.constants_block()
        if c:
            parts.append(c)
        w = memory.wear_block(who, names)
        if w:
            parts.append(w)
        # Сдвиг возраста отдельной строкой по каждому: эталон сегодняшний, и без
        # прямого запрета седина и морщины переезжают в прошлое (кадр 105).
        y = memory.younger_block(who, names) if hasattr(memory, "younger_block") else ""
        if y:
            parts.append(y)
        if memory.absent:
            gone = [names[c] for c in memory.absent if c in names]
            if gone:
                parts.append("NOT IN THIS MEMORY, do not draw them: " + ", ".join(gone) + ".")
    elif scene:
        c = scene.constants_block()
        if c:
            parts.append(c)
        pr = (scene.props_block(names, getattr(frame, "props_seen", None))
              if hasattr(scene, "props_block") else "")
        if pr:
            parts.append(pr)
        w = scene.wear_block(who, names) if memory is None else ""
        if w:
            # Строка одежды на ЭТОТ кадр: снятое по costume_state вычеркнуто
            # (48/49: «jacket ON» + «jacket off» вместе), галстук назван явно —
            # без слова он сам появлялся в 39 43 105 126 131 160 185 (круг 2 §3 G).
            for c in who:
                c0 = _costume_start(scene, c)
                c1, off = costume_now(scene, c, frame)
                c1 = c1 + tie_tail(c1, off)
                if c0 and c1 != c0:
                    w = w.replace(f"{names[c]} wears {c0}", f"{names[c]} wears {c1}", 1)
            parts.append(w)
        # Что изменилось в одежде по ходу сцены — поверх паспорта. Паспорт
        # говорит, во что герой одет в начале; состояние говорит, что с этим
        # стало. Без второго паспорт спорит с действием кадра.
        st = getattr(frame, "costume_state", None) or {}
        # Только про тех, кто в этом кадре, и про хозяина руки из-за края. Раньше сюда
        # шли ВСЕ герои ролика: кадр с одним героем получал реквизит трёх посторонних,
        # и строка объявлена старшей над паспортом одежды (замер 25.09: 24–72% кадров).
        cl = [f"{names[c]}: {v}" for c, v in st.items() if c in who or c in limb_cids]
        if cl:
            parts.append("BY NOW IN THIS SCENE — " + "; ".join(cl) +
                         ". This overrides the NEVER CHANGES and SAME THROUGHOUT lines "
                         "wherever they disagree: it is what happened since. Where two notes "
                         "about the same person disagree, the last one is true; and when "
                         "WHAT HAPPENS shows the hands doing something else, the hands "
                         "follow WHAT HAPPENS.")

    # Слой времени. Без него детское воспоминание рисовалось настоящим временем:
    # ребёнка изображали взрослым, а в чужом воспоминании стояли посторонние.
    if getattr(frame, "layer", "now") and frame.layer != "now":
        parts.append("THIS SHOT IS A MEMORY of an earlier time, not the present. "
                     "Only the people named above exist in it. If the text says a "
                     "character was a child then, draw them as a child of that age — "
                     "same face, younger. Slightly softer, warmer rendering than the "
                     "present-day shots.")

    # 6. Что изменилось — абсолютной фразой, не «повернулся».
    if frame.changed:
        parts.append(f"CONTINUITY: {_txt(frame.changed).strip().rstrip('.')}.")

    if who_with_face:
        parts.append("Physical state — snow, rain, dirt, blood, tears, bandages — is added "
                     "ON TOP of the person and never changes their hair colour, eye colour "
                     "or face. Snow on the hair stays white flecks on hair of its own colour.")
        # РАЗДЕЛЕНИЕ ВЛАСТИ, и оно важнее, чем кажется. Раньше здесь стояло
        # «во всём споре побеждает картинка, включая цвет одежды» — а на листе
        # персонажа он в школьной форме, тогда как сцена требует зимней куртки.
        # Модель металась между двумя источниками и переодевала героя от кадра
        # к кадру: разбор 493 кадров дал 357 разрывов, и переодевание — самый
        # частый. Лицо решает картинка, одежду решает сцена. Каждый отвечает
        # ровно за своё.
        parts.append("WHO DECIDES WHAT. Face, hairstyle, hair colour, eye colour and skin "
                     "come from the attached reference image and from the NEVER CHANGES "
                     "line — the picture wins there. Clothing comes from the SAME "
                     "THROUGHOUT THIS SCENE line — the words win there, even when the "
                     "reference image shows different clothes.")
    # Слово о композиции — по пропорции кадра. «vertical composition» в 16:9
    # давало вертикальную панель внутри широкого холста с белыми полями
    # (кадры 9, 145, 198, 205, 321). STYLE — константа, строку собираем здесь.
    comp = ("vertical composition" if aspect_for(frame) == "3:4"
            else "wide horizontal composition filling the whole canvas")
    # Форма холста — ОТДЕЛЬНЫМ предложением. Прежде она подставлялась заменой подстроки
    # «vertical composition» внутри строки стиля, и на S4 (93 ролика студии из 96) замена
    # молча не срабатывала: художнику вообще не говорили, высокий холст или широкий.
    tall = aspect_for(frame) == "3:4"
    canvas = ("CANVAS: a TALL portrait canvas, three units wide by four high. Compose for it: "
              "the subject fills the height, no empty floor at the bottom and no empty ceiling "
              "at the top, nothing important closer than a finger-width to any edge."
              if tall else
              "CANVAS: a WIDE landscape canvas, sixteen units by nine. Compose for it: the "
              "drawing reaches both side edges, no vertical panel in the middle, no empty "
              "margins left or right, nothing important closer than a finger-width to any edge.")
    parts.append(f"STYLE: {current_style().replace('vertical composition', comp)}.")
    parts.append(canvas)
    parts.append(f"{LOCK} {EXTRAS} {NO_CAMERA_LOOK}")
    parts.append(self_check(bool(quote_bubble)))
    sfx = sfx_word(frame)
    if sfx:
        # Замер (20 генераций): pro пишет русское слово-звук без ошибок 6/6, и
        # выходит кистевая цветная надпись, как у конкурентов, — лучше любого
        # шрифта кодом. Слово без кавычек (кавычки копирует буквально).
        # Язык слова — язык ролика (польское «ŁUP», а не «Russian word ŁUP»);
        # на латинских языках с диакритикой замера ещё нет — см. tasks/pipeline_link/langs.md.
        parts.append(f"SOUND WORD: draw the {lang} word {sfx} as big hand-brushed manhwa "
                     f"sound lettering — bold brush strokes, a thick white outline, tilted "
                     f"8-12 degrees, placed in the empty background right next to the source "
                     f"of the sound, never over a face, may run off the edge. No other text "
                     f"anywhere.")
        neg = NEG.replace("no sound-effect lettering, ", "")
        neg += (" The ONLY exception to this ban is the single sound word named in "
                "SOUND WORD above; nothing else readable.")
        parts.append(f"{FRAME_RULE}\n{neg}")
    else:
        parts.append(f"{FRAME_RULE}\n{NEG}")
    if quote_bubble:
        parts.append(bubble_block(quote_bubble, speaker_offscreen, lang,
                                  speaker_name=names.get(_txt(getattr(frame, "quote_by", "")).strip(), "")))
    return "\n\n".join(parts)


def _sleeve_bit(cost: str) -> str:
    """Кусок строки костюма, по которому узнаётся рукав: та часть списка, где
    есть sleeve/blouse/shirt/jacket…; иначе первая часть. Части «NO jacket…»
    (снятое, см. costume_now) рукавом быть не могут."""
    cost = (cost or "").strip()
    if not cost:
        return ""
    bits = [b.strip() for b in cost.split(",") if b.strip()
            and not _re_mod.match(r"no\b", b.strip(), _re_mod.IGNORECASE)]
    if not bits:
        return ""
    for b in bits:
        if _re_mod.search(r"\bsleeve", b, _re_mod.IGNORECASE):
            return b
    for b in bits:
        if _re_mod.search(r"\b(blouse|shirt|jacket|cardigan|coat|sweater|dress|blazer|gown)\b",
                          b, _re_mod.IGNORECASE):
            return b
    return bits[0]


def _sleeve_of(scene, cid: str) -> str:
    """Рукав по паспорту сцены на старт (без учёта снятого). Оставлено для
    совместимости; build() берёт costume_now."""
    return _sleeve_bit(((getattr(scene, "costume", None) or {}).get(cid) or ""))


# --- одежда на конкретный кадр ----------------------------------------------
# Паспорт сцены говорит, во что герой одет на ПЕРВЫЙ кадр; costume_state —
# что с этим стало. Круг 2 (§3 G, §4 п.5): «SAME THROUGHOUT: … suit jacket ON»
# + «BY NOW: jacket off, folded over his forearm» + лист в пиджаке → пиджак
# и надет, и сложен (48, 49). Снятое вычёркивается из строки одежды кодом.
_OFF_ITEMS = ("jacket", "blazer", "overcoat", "coat", "cardigan", "necktie", "tie", "cap",
              "hat", "gloves", "glove", "scarf", "glasses", "sweater", "hoodie", "apron",
              "ring", "wristwatch", "watch", "badge", "handbag", "bag", "shoes", "heels")
_OFF_AFTER = r"\b[^;,.]{0,25}\b(?:off|removed|taken off|gone|discarded|set aside)\b"
_OFF_BEFORE = (r"\b(?:removed|takes? off|taken off|took off|without|no longer wear(?:s|ing)?|"
               r"has taken off|pulls? off)\b[^;,.]{0,25}\b")


def items_off(state: str) -> List[str]:
    """Что снято по строке состояния: «jacket off, folded over his forearm»,
    «coat removed», «takes off his cap», «ring off, resting on the desk»."""
    t = (state or "").lower()
    if not t:
        return []
    out = []
    for it in _OFF_ITEMS:
        if (_re_mod.search(r"\b" + it + _OFF_AFTER, t)
                or _re_mod.search(_OFF_BEFORE + it + r"\b", t)):
            out.append(it)
    return out


def _costume_start(scene, cid: str) -> str:
    """Ровно та строка, что scene.wear_block ставит после «NAME wears»."""
    from scene import start_state
    return start_state((getattr(scene, "costume", None) or {}).get(cid, ""))


def costume_now(scene, cid: str, frame):
    """(одежда героя на ЭТОТ кадр, [что снято]). Части списка со снятым
    предметом: «jacket ON over a white shirt…» → остаётся «a white shirt…»,
    «grandmother's ring visible» → уходит; в конец — «NO jacket (taken off)»."""
    cost = _costume_start(scene, cid)
    off = items_off(((getattr(frame, "costume_state", None) or {}).get(cid) or ""))
    if not cost or not off:
        return cost, off
    kept = []
    for b in [x.strip() for x in cost.split(",") if x.strip()]:
        hit = [it for it in off if _re_mod.search(r"\b" + it + r"\b", b, _re_mod.IGNORECASE)]
        if not hit:
            kept.append(b)
            continue
        m = _re_mod.search(r"\bover\s+(.+)$", b, _re_mod.IGNORECASE)
        if m and not any(_re_mod.search(r"\b" + it + r"\b", m.group(1), _re_mod.IGNORECASE)
                         for it in off):
            kept.append(m.group(1).strip())     # то, что было ПОД снятым, остаётся
    kept.append("NO " + " and no ".join(off) + " (taken off by now)")
    return ", ".join(kept), off


def tie_tail(cost: str, off: List[str]) -> str:
    """Галстук — явно. Без слова он сам появлялся в 7 кадрах круга 2 (39 43 105
    126 131 160 185) там, где по тексту ворот расстёгнут. Только для одежды с
    рубашкой/костюмом: к платью или блузке галстук не пририсовывают."""
    c = cost or ""
    # Ворот по паспорту застёгнут («collar closed», «top button fastened»,
    # «high-collared shirt») — «collar open» спорил бы с ним в одной строке:
    # проверка скептика на run_v/run_isagi/r2 дала 54/83/46 таких строк.
    # Тогда только «no tie», про ворот молчим.
    closed = bool(_re_mod.search(
        r"\bcollar (?:closed|fastened|buttoned|up|straightened)\b|\bhigh[- ]collar|"
        r"\bmandarin collar\b|\btop button (?:fastened|buttoned|done up|closed)\b|"
        r"\bbuttoned (?:up )?to the (?:top|neck|collar|throat)\b", c, _re_mod.IGNORECASE))
    no_tie = ", no tie" if closed else ", no tie, collar open"
    if "tie" in off or "necktie" in off:
        return no_tie
    if _re_mod.search(r"\bno tie\b|\bopen collar\b|\bcollar open\b", c, _re_mod.IGNORECASE):
        return ""
    if _re_mod.search(r"\b(?:neck)?tie\b", c, _re_mod.IGNORECASE):
        return ", tie on"
    # Футболка (T-shirt) — не рубашка: у неё нет ворота, галстук к ней не пририсовывают
    # (run_v, 24 промпта с «T-shirt … collar open»).
    c_no_tee = _re_mod.sub(r"\b(?:t|tee)[- ]?shirts?\b|\bsweatshirts?\b", "", c,
                           flags=_re_mod.IGNORECASE)
    if _re_mod.search(r"\b(shirt|suit|blazer)\b", c_no_tee, _re_mod.IGNORECASE):
        return no_tie
    return ""


def hand_of(trait: str, lock: str) -> str:
    """Чья рука — словами вместо листа хозяина (SLEEVE_REF выключен): пол из
    примет («young woman» → a woman's slim hand), тон кожи из паспорта листа
    («fair skin», «medium-brown skin»)."""
    sex = _re_mod.search(r"\b(man|woman|boy|girl)\b", trait or "", _re_mod.IGNORECASE)
    sx = sex.group(1).lower() if sex else ""
    hand = {"woman": "is a woman's slim hand", "man": "is a man's hand",
            "girl": "is a girl's small hand", "boy": "is a boy's small hand"}.get(sx, "")
    skin = _re_mod.search(r"\b((?:[\w-]+\s){0,2}?[\w-]+)\s+skin\b", f"{lock or ''} {trait or ''}",
                          _re_mod.IGNORECASE)
    if skin:
        s = skin.group(1).lower()
        # «no scars, fair skin» → только сам тон: срезаем всё до последней запятой
        s = s.split(",")[-1].strip()
        # Без пола (паспорт сотрудницы 204-207 не содержит «woman») — только кожа:
        # «the hand has fair freckled skin», а не «the hand is a hand».
        hand = f"{hand} with {s} skin" if hand else f"has {s} skin"
    return hand


_re_hair_lead = _re_mod.compile(
    r"\b((?:" + "|".join(_HAIR_COLOURS) + r")(?:-(?:" + "|".join(_HAIR_COLOURS) + r"))?"
    r"(?:\s(?:blond|blonde|gray|grey|brown))?)\b", _re_mod.IGNORECASE)


def hair_colour(trait: str, lock: str) -> str:
    """Цвет волос героя для строки различия в CAST: из примет («silver hair»),
    иначе первая фраза паспорта листа («Silver-gray, chin-length…»)."""
    m = _re_hair.search(trait or "")
    if m:
        return m.group(1).lower()
    head = _re_mod.split(r"[,;]", (lock or "").strip(), maxsplit=1)[0][:60]
    m = _re_hair_lead.search(head)
    return m.group(1).lower() if m else ""


def sfx_word(frame) -> str:
    """Одно слово-звук из бланка, чистое: без кавычек и ёлочек, заглавными."""
    w = (getattr(frame, "sfx", "") or "").strip().strip('«»"\'„“”‚‘’').strip()
    if not w or len(w) > 10 or " " in w.replace("-", ""):
        return ""
    return w.upper()


def refs_for(frame, sheets, scene, budget: int = 4, style_path: str = ""):
    """Какие картинки прикладываем и в каком порядке. Максимум четыре.

    Порядок жёсткий и совпадает с объявлением ролей.

    ЧТО ИМЕННО ПРИКЛАДЫВАТЬ, зависит от кадра, и это не мелочь. Замер по 156
    кадрам подряд: одежда плывёт в 30% кадров, причёска в 17% — и почти все
    сбои сидят там, где ЛИЦА В КАДРЕ НЕТ (со спины, сверху, врезка на руки) или
    где фигура видна целиком. Причина простая: эталон у нас был один — портрет
    лица. Пока лицо в кадре, модель цепляется за него и вытягивает остальное;
    как только лица нет, зацепки не остаётся, и длина волос с одеждой
    достраиваются заново — один кадр со спины отрастил волосы до середины спины.

    Поэтому: лицо крупно — берём портрет; фигура целиком или лица не видно —
    берём фигуру в рост. Число слотов не растёт, меняется их содержимое.
    """
    who = [c for c in (frame.in_frame or []) if c in sheets]
    if getattr(frame, "focus", "") in who:
        who = [frame.focus] + [c for c in who if c != frame.focus]

    face_shown = bool(getattr(frame, "face_visible", True))
    shot = getattr(frame, "shot_size", "") or "medium"
    body_shot = shot in ("wide", "full") or not face_shown

    refs: List[images.Ref] = []
    used: List[str] = []
    kinds: List[str] = []
    # Воспоминание — другой мир: чертёж комнаты настоящего к нему не прикладываем,
    # иначе детская комната строится по плану больничной палаты (кадр 105).
    in_memory = (getattr(frame, "layer", "now") or "now") != "now"
    has_plan_file = bool(scene and getattr(scene, "plan_path", "")) and not in_memory
    room_for_people = budget - (1 if has_plan_file else 0)
    for cid in who[:max(0, room_for_people)]:
        sh = sheets[cid]
        want = "front" if body_shot else "face"
        r = _one_ref(sh, want) or _one_ref(sh, "face") or _one_ref(sh, "front")
        if r:
            refs.append(r)
            used.append(cid)
            kinds.append("body" if (body_shot and r.name.endswith("_front.png")) else "face")

    # Рука из-за кадра: лист её хозяина (_front — там рукав) в оставшийся слот,
    # ролью «рукав и кожа». Без него 16 LIMBS на 101 кадр с contact против 5 на
    # 234 без. Лица и план важнее: не влезает — не прикладываем.
    # Выключено (SLEEVE_REF=False): в круге 2 фигура с листа тянула хозяина
    # руки в кадр целиком — EXTRA 11, 160, 163, 189. Рукав и кожа — текстом.
    for cid in (contact_cids(frame, {c: en_name(c) for c in sheets}) if SLEEVE_REF else []):
        if cid in used or len(refs) >= room_for_people:
            continue
        r = _one_ref(sheets[cid], "front") or _one_ref(sheets[cid], "face")
        if r:
            refs.append(r)
            used.append(cid)
            kinds.append("sleeve")

    plan_ref = None
    if has_plan_file:
        import os
        if os.path.exists(scene.plan_path):
            plan_ref = images.Ref(name="plan.png",
                                  data_uri=images.data_uri(scene.plan_path))
    out = refs + ([plan_ref] if plan_ref else [])
    style_ref = None
    import os as _os
    if style_path and _os.path.exists(style_path) and len(out) < budget:
        # Лица и план важнее якоря: при трёх названных людях он не влезает,
        # и это правильно — узнаваемость дороже ровной манеры.
        style_ref = images.Ref(name="style_anchor.png", data_uri=images.data_uri(style_path))
        out.append(style_ref)
    return out[:budget], used, bool(plan_ref), kinds, bool(style_ref)


def _one_ref(sheet, view: str):
    import os
    p = (sheet.files or {}).get(view)
    if p and os.path.exists(p) and os.path.getsize(p) > 4096:
        return images.Ref(name=f"{sheet.cid}_{view}.png", data_uri=images.data_uri(p))
    return None


# --- лестница упрощений при отказе генератора --------------------------------
# Раньше при отказе один и тот же промпт уходил повторно до шести раз. Если
# генератор режет кадр по содержанию (а манхва — это драки, кровь и угрозы),
# все шесть попыток умрут одинаково и кадр приедет дырой в готовый ролик.
# Приём из боевого main_v40: четыре версии нарастающей мягкости, замены —
# таблицей регулярных выражений, без единого обращения к модели.
import re as _re

_SOFT_1 = [
    (r"\bblood(y|ied)?\b", "marked"),
    (r"\bbleed(ing|s)?\b", "hurt"),
    (r"\bstab(bed|bing|s)?\b", "strike"),
    (r"\bbeat(en|ing|s)?\b", "push"),
    (r"\bpunch(es|ed|ing)?\b", "shove"),
    (r"\bkick(s|ed|ing)?\b", "push"),
    (r"\bwound(s|ed)?\b", "mark"),
    (r"\bbruis(e|ed|es)\b", "mark"),
    (r"\bstrangl\w*", "grip"),
    (r"\bchok(e|ed|ing)\b", "grip"),
    (r"\bcorpse\b", "still figure"),
    (r"\bkill(s|ed|ing)?\b", "stop"),
    (r"\bknife\b", "small tool"),
    (r"\bweapon\b", "object"),
    (r"\bnaked\b", "unclothed shoulders"),
]
_SOFT_2 = _SOFT_1 + [
    (r"\bfight(s|ing)?\b", "confrontation"),
    (r"\bviolen\w*", "tense"),
    (r"\battack(s|ed|ing)?\b", "approach"),
    (r"\bscream(s|ed|ing)?\b", "calls out"),
    (r"\bcry(ing)?\b", "upset"),
    (r"\bthreat\w*", "tense moment"),
]

_KEEP = {
    # Названия блоков обязаны совпадать с теми, что собирает build(): при отказе
    # генератора по содержанию лестница оставляет только перечисленное. Однажды
    # тут остались старые имена («ATTACHED IMAGES»), и на первом же смягчении
    # выпадала легенда эталонов — лица переставали привязываться.
    1: ("CANVAS", "LIMB ENTERING", "THE PLACE OF THIS MEMORY", "NOT IN THIS MEMORY",
        "REFERENCE CONTRACT", "WHAT HAPPENS", "CAST", "STAGING", "POSTURE", "POSE",
        "EYELINE", "DISTANCE", "HEIGHT", "NEVER CHANGES", "EXPRESSION", "CAMERA",
        "FRAMING", "SOUND WORD", "CORRECTION", "THE ROOM", "TIME", "OBJECTS OF", "SAME THROUGHOUT", "BY NOW", "CONTINUITY",
        "THIS SHOT IS A MEMORY", "AGE IN THIS MEMORY", "WHO DECIDES", "Physical state", "STYLE",
        "CHECK YOUR OWN DRAWING",
        "Only the pose", "One single manhwa drawing", "SPEECH BUBBLE"),
    2: ("CANVAS", "LIMB ENTERING", "THE PLACE OF THIS MEMORY", "NOT IN THIS MEMORY",
        "THIS SHOT IS A MEMORY",
        "REFERENCE CONTRACT", "WHAT HAPPENS", "CAST", "STAGING", "POSTURE", "NEVER CHANGES",
        "AGE IN THIS MEMORY",
        "CAMERA", "FRAMING", "SOUND WORD", "THE ROOM", "TIME", "SAME THROUGHOUT", "BY NOW", "WHO DECIDES", "STYLE",
        "CHECK YOUR OWN DRAWING",
        "One single manhwa drawing", "SPEECH BUBBLE"),
    3: ("CANVAS", "REFERENCE CONTRACT", "WHAT HAPPENS", "CAST", "NEVER CHANGES", "CAMERA", "STYLE",
        "THE PLACE OF THIS MEMORY",     # 27.09.2026 V2-05: место воспоминания — на всех уровнях
        "One single manhwa drawing", "SPEECH BUBBLE"),
}


def _apply(table, text):
    for pat, rep in table:
        text = _re.sub(pat, rep, text, flags=_re.IGNORECASE)
    return text


def _одежда_воспоминания(block: str, prompt: str) -> str:
    """27.09.2026 V2-05: из IN THIS MEMORY при смягчении остаётся «кто во что одет». Возраст и облик
    («is 9 years old here, slim young boy») лестница убирает, как и раньше: их фильтр генератора
    режет у детей (pipeline: листы девочек 7 и 14 лет в школьной форме). Имена — из строки CAST."""
    cast = block.startswith("IN THIS MEMORY: ") and _re.search(
        r"^CAST: exactly \d+ named (?:person|people) in this frame — (.+?)\. Each of them", prompt, _re.M)
    if not cast:
        return ""
    nm = "|".join(_re.escape(n) for n in sorted(cast.group(1).split(", "), key=len, reverse=True))
    body = block[len("IN THIS MEMORY: "):]
    out = []
    for m in _re.finditer(rf"(?:^|; )({nm}) (.*?)(?=; (?:{nm}) |\.?$)", body, _re.S):
        c = m.group(2)[6:] if m.group(2).startswith("wears ") else m.group(2).partition(", wears ")[2]
        # «bloodstained trousers» таблица _SOFT не ловит, а хвост уровня 3 просит «no blood»
        c = _re.sub(r"\bblood[- ]?(?=stain|soak|spatter|splatter|smear|drench|streak)", "", c, flags=_re.I)
        if c:
            out.append(f"{m.group(1)} wears {c}")
    return ("IN THIS MEMORY: " + "; ".join(out) + ".") if out else ""


def soften(prompt: str, level: int) -> str:
    """level 0 — как есть; 1 — мягче слова; 2 — короче блоки; 3 — только суть."""
    if level <= 0:
        return prompt
    blocks = [b for b in prompt.split("\n\n") if b.strip()]
    keep = _KEEP.get(min(level, 3), _KEEP[3])
    # 27.09.2026 V2-05: одежда воспоминания остаётся на всех уровнях (без возраста и облика)
    kept = [b if any(b.startswith(k) or k in b[:40] for k in keep) else _одежда_воспоминания(b, prompt)
            for b in blocks]
    kept = [b for b in kept if b]
    text = "\n\n".join(kept or blocks[:2])
    text = _apply(_SOFT_1 if level == 1 else _SOFT_2, text)
    if level >= 3:
        text += ("\n\nKeep it calm and non-graphic: no injury detail, no blood, "
                 "no violence on screen.")
    return text
