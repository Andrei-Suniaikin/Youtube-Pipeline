# -*- coding: utf-8 -*-
"""Шаг «Обложка»: превью ролика-донора -> НОВАЯ обложка нашего ролика на его языке.

Решение владельца 19.09.2026: «по обложкам делает хорошо GPT думающий, только давай
добавим стадию, что делается 3 варианта и модель 5.6 sol выбирает лучшую обложку».
Раньше (18.09): обложка — новая картинка по идее конкурента (пересъёмка без зеркала),
крупность героев как у донора, наши герои с листов ролика, текст — локализация для
подростка-носителя без английских слов.

Коротко, что происходит:
  1. «Карточка кадра» — модель СМОТРИТ превью донора: идея, каждый ключевой человек и
     объект с рамкой (из рамок код считает план крупности), надписи, новая камера.
  2. Наши герои — листами из проекта ролика по карте «человек обложки -> наш герой».
  3. Надписи — локализация и вычитка носителем; английское слово ловит код.
  4. ТРИ варианта одного промпта ОДНОВРЕМЕННО у openai-image в режиме thinking (6 единиц
     за вариант). Зерна у генератора нет: различие даёт сама модель и строка «вариант A/B/C»
     о ракурсе; идея, план крупности и надписи у всех трёх одинаковые.
  5. Румынские Ș/Ț рисует КОД поверх каждого варианта (генератор их не умеет).
  6. Выбирает gpt-5.6-sol: видит донора и три варианта, чек-лист владельца, возвращает
     оценки 0–10, лучший и одну фразу «почему». Буквы он переписывает, а сверяет их с
     заказанными КОД. «Не копия» (SSIM/dHash с донором) код считает и даёт выбирающему
     подсказкой. Блокирующий брак — неверные буквы, школьная форма, чужой логотип,
     человек моложе 20. Если брак у всех трёх — ещё один круг из трёх (не больше одного),
     потом лучший с пометкой «проверьте глазами».
  7. Выбранная обложка чистится от зерна (Real-ESRGAN аниме на машине с видеокартой, если
     она настроена, иначе сглаживание h=3); вариант без чистки лежит рядом.
  8. Оператор на странице видит все варианты и может «взять другой» без новой покупки.

Порядок в очереди. Шаг стоит ПОСЛЕДНИМ, после «Сборки»: ролик — продукт, обложка —
довесок, и отказ обложки закрывает шаг ошибкой, а не валит задачу (см. web_worker).
"""
from __future__ import annotations

import contextvars
import itertools
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional

import images as IM
import languages as LG
import llm as LLM
import source as SRC

# ---------------------------------------------------------------- настройки
# Генератор — openai-image (GPT Image 2.5) в режиме thinking: живой прогон 19.09 на пяти
# донорах, владелец: «по обложкам делает хорошо GPT думающий». 6 единиц за вариант.
MODEL = "openai-image"
OPTIONS = {"quality": "thinking"}
ASPECT = "16:9"
TRIES = 2                 # покупок на один отказ генератора за прогон; каждая — новым запросом (retry_prompt)
COVER_SIZE = (1672, 941)  # что отдаёт openai-image на 16:9
PREVIEW_SIZE = (246, 138)  # размер картинки ролика в ленте YouTube
UNITS_PER_PASS = 6        # openai-image thinking, 16:9: столько списывает один вариант
VARIANTS = 3              # вариантов в круге (решение владельца 19.09)
REDO_ROUNDS = 1           # «переделать» даёт не больше одного нового круга из трёх
# Строка о ракурсе в конце промпта варианта. Идея, план крупности и надписи — общие.
VARIANT_HINTS = (
    "VARIANT A of 3: follow the NEW CAMERA exactly as written.",
    "VARIANT B of 3: the same NEW CAMERA a little lower and closer, other hand gestures for the same action; "
    "the sizes stay those of the SIZE PLAN.",
    "VARIANT C of 3: the same NEW CAMERA turned a little more to the side, another spot of the NEW BACKGROUND; "
    "the sizes stay those of the SIZE PLAN, the left/right layout is not mirrored.",
)

# «Не копия» грубо — подсказка выбирающему, а не приговор. Замер циклов 5-7 (256×144, серое):
# правка оригинала (владелец: «1 в 1 копирка») — SSIM 0.22-0.80; пересъёмка — 0.04-0.18.
# dHash 16×16: у копий отличалось 17-46 битов из 256, у пересъёмки 73-134.
COPY_SSIM_STOP = 0.30
COPY_DHASH_STOP = 48
KEY_MIN_AREA = 0.05       # не наш человек меньше 5% кадра — массовка, не ключевой
# Массовка по роли: живьём (HZcvF8TvNww) тела поверженных хулиганов, 3-4% кадра.
MOB_ROLE = re.compile(r"background|\bmob\b|crowd|\bextras?\b|bystander|passer|onlooker|minion|henchm|audience|"
                      r"spectator|defeated|knocked|unconscious|sprawled", re.I)
KEY_ROLE = re.compile(r"\b(?:lead|protagonist|hero|heroine|villain|antagonist|boss|main|rival)\b", re.I)
# Читаемость надписи кода — РАЗНИЦА ЯРКОСТИ букв и фона под ними (цвет букв выбирается по ней).
INK_DELTA_MIN = 60.0      # уровней яркости из 255 между буквами и тем, что под ними
SHIFT_X = 0.10            # центр человека по горизонтали вправе сдвинуться ради новой камеры

CARD_NAME = "cover_card.json"
HERO_DIR = "cover_heroes"   # листы наших героев, собранные для генератора (лицо; фигура спереди + 3/4)
CARD_VERSION = "L1-grid"    # карточка без сетки донора (рамки «на глаз») разбирается заново
METHOD = "3var-sol"
PROOF_VERSION = "proof-2"   # вычитка перевода с проверкой каждого знака; другая отметка — перевод заново

# Шрифты для надписи, которую рисует код. Порядок: сначала Mac (там же снимались
# замеры цикла 5), потом то, что реально стоит на VPS. Берётся первый шрифт, в котором
# есть ВСЕ буквы надписи — иначе румынская Ș молча превратится в квадрат.
FONTS = (
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
BOLD = 0.05               # «раскормка» глифа в долях кегля
BOLD_COMMA = 0.02         # для слов с Ș/Ț: при 0.05 запятая прилипает к букве и читается как седиль
COMMA_BELOW = "ȘșȚț"
# Место надписи — это место донорской надписи, и ещё 6% на воздух.
FILL_H, WIDEN = 0.94, 0.94
INK_TOL = 100
# Цвет букв берётся у донора замером (sample_colors). Сюда доходит только случай, когда
# замерить не удалось: белые буквы с чёрной обводкой читаются на чём угодно.
DEFAULT_FILL = [255, 255, 255]

RO_COMMA = ("In Romanian the letters Ș ș Ț ț carry a COMMA hanging BELOW the letter and detached from it. "
            "A cedilla hook joined to the letter (Ş ş Ţ ţ, the Turkish shape) is WRONG and counts as a spelling mistake.")


# Не беда обложки, а сигнал исполнителю: служба останавливается, пока шаг ждёт лимита Claude,
# задачу забрали, или ожидание места FastGen прервано. Широкие «except Exception» шага их не глотают.
_STOP = (LLM.CP.JobGone, IM.FP_POOL.Cancelled)


class CoverError(RuntimeError):
    """Причина по-русски: её видно владельцу на странице ролика как есть."""


def enabled(env=None) -> bool:
    """MANGA_COVER=on|off, по умолчанию on."""
    value = (os.environ if env is None else env).get("MANGA_COVER", "on").strip().lower()
    if value not in ("on", "off"):
        raise ValueError("MANGA_COVER: on или off")
    return value == "on"


def defer_enabled(env=None) -> bool:
    """MANGA_COVER_DEFER=on|off, по умолчанию off: при on, если в очереди ждут другие ролики,
    обложка делается отдельной задачей после них.

    Придумано, когда исполнитель у очереди был один. С параллельными роликами (MANGA_PARALLEL_VIDEOS)
    откладывание только вредит (разбор 21.09.2026): отложенная задача часами держит готовый ролик
    «в работе» — ложное «Ролик упал» в Telegram, закрытые кадры и пересдача, а при сбое «Названия»
    ещё и спрятанное видео. Обложка сразу после «Сборки» занимает место ролика на ~4 минуты."""
    value = (os.environ if env is None else env).get("MANGA_COVER_DEFER", "off").strip().lower()
    if value not in ("on", "off"):
        raise ValueError("MANGA_COVER_DEFER: on или off")
    return value == "on"


_MODEL_RE = r"(codex|claude):[A-Za-z0-9._-]+"


def vision_model(env=None) -> str:
    """Модель, которая разбирает превью донора и переводит надписи. По умолчанию — та же,
    что у текстовых шагов."""
    env = os.environ if env is None else env
    value = (env.get("MANGA_COVER_VISION", "").strip()
             or env.get("MANGA_TEXT_MODEL", "").strip() or "claude:claude-sonnet-5")
    if not re.fullmatch(_MODEL_RE, value):
        raise ValueError("MANGA_COVER_VISION: «claude:<модель>» или «codex:<модель>»")
    return value


def judge_model(env=None) -> str:
    """Кто выбирает лучший из трёх: MANGA_COVER_JUDGE, по умолчанию codex:gpt-5.6-sol
    (разведка 19.09: три картинки одним вызовом, 37 с, видит различия и лишнюю обложку)."""
    env = os.environ if env is None else env
    value = env.get("MANGA_COVER_JUDGE", "").strip() or "codex:gpt-5.6-sol"
    if not re.fullmatch(_MODEL_RE, value):
        raise ValueError("MANGA_COVER_JUDGE: «codex:<модель>» или «claude:<модель>»")
    return value


def judge_fallback(env=None) -> str:
    """Запасной выбирающий: MANGA_COVER_JUDGE_FALLBACK, по умолчанию claude:claude-sonnet-5;
    off — запасного нет (тогда берётся первый годный вариант с пометкой)."""
    env = os.environ if env is None else env
    value = env.get("MANGA_COVER_JUDGE_FALLBACK", "").strip() or "claude:claude-sonnet-5"
    if value.lower() == "off":
        return ""
    if not re.fullmatch(_MODEL_RE, value):
        raise ValueError("MANGA_COVER_JUDGE_FALLBACK: «claude:<модель>», «codex:<модель>» или off")
    return value


def judge_effort(env=None) -> str:
    """Усилие рассуждения выбирающего (codex), по умолчанию medium. Надписи выбирающий читает
    вслепую (заказанный текст ему не показывается), буквы сверяет код; на живых c8 с medium
    слепое чтение поймало «БРОСЬ Ё!» вместо «БРОСЬ ЕЁ!». Ошибку самого перевода (VAD вместо
    VĂD) ловит не чтение, а вычитка и поле «fixes» выбирающего."""
    env = os.environ if env is None else env
    value = env.get("MANGA_COVER_JUDGE_EFFORT", "").strip().lower() or "medium"
    if value not in ("low", "medium", "high"):
        raise ValueError("MANGA_COVER_JUDGE_EFFORT: low, medium или high")
    return value


def clean_mode(env=None) -> str:
    """Чистка выбранной обложки: MANGA_COVER_CLEAN=auto|esrgan|nlmeans|off, по умолчанию auto —
    Real-ESRGAN, если заданы MANGA_CLEAN_HOST и MANGA_CLEAN_BIN, иначе сглаживание h=3."""
    env = os.environ if env is None else env
    value = env.get("MANGA_COVER_CLEAN", "").strip().lower() or "auto"
    if value not in ("auto", "esrgan", "nlmeans", "off"):
        raise ValueError("MANGA_COVER_CLEAN: auto, esrgan, nlmeans или off")
    if value == "auto":
        return "esrgan" if env.get("MANGA_CLEAN_HOST", "").strip() and env.get("MANGA_CLEAN_BIN", "").strip() \
            else "nlmeans"
    return value


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] обложка: {msg}", flush=True)


# ---------------------------------------------------------------- мелкое
_NORM = str.maketrans({"ş": "ș", "ţ": "ț", "Ş": "Ș", "Ţ": "Ț"})


def norm(text: str) -> str:
    """NFC + румынская седиль в запятую: сравнивать надписи можно только так."""
    return unicodedata.normalize("NFC", str(text or "")).translate(_NORM)


def pct(a, b) -> str:
    return f"{round(float(a) * 100)}-{round(float(b) * 100)}%"


def accents(text: str) -> List[str]:
    """Буквы со знаками, которые генератор должен нарисовать.

    На кириллице знак несут только Ё и Й; перечислять там каждую букву нельзя —
    модель начинает «дорисовывать» знаки ко всем (замер цикла 4)."""
    text = norm(text)
    if any("А" <= ch <= "я" or ch in "Ёё" for ch in text):
        return [ch for i, ch in enumerate(text) if ch in "ЁёЙй" and ch not in text[:i]]
    return [ch for i, ch in enumerate(text) if ch.isalpha() and ord(ch) > 127 and ch not in text[:i]]


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _rgb(value):
    """#RRGGBB -> [r,g,b]. Всё остальное — None.

    Раньше здесь молча подставлялся жёлтый: модель называла цвет словом («black»), и
    надпись, которую рисует код, выходила жёлтой поверх белой рамки. Теперь непонятный
    цвет — это отсутствие цвета, и его замеряют с самого донора (sample_colors).
    Уже разобранный цвет [r, g, b] проходит как есть: карточка на диске лежит разобранной."""
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            nums = [int(v) for v in value]
        except (TypeError, ValueError):
            return None
        return nums if all(0 <= n <= 255 for n in nums) else None
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(value or "").strip())
    if not m:
        return None
    h = m.group(1)
    return [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)]


def _box(value):
    """[x0,y0,x1,y1] в долях кадра или None."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (max(0.0, min(1.0, float(v))) for v in value)
    except (TypeError, ValueError):
        return None
    return [x0, y0, x1, y1] if x1 > x0 and y1 > y0 else None


def _slug(value, default="p"):
    s = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return s[:24] or default


# ---------------------------------------------------------------- наши герои: паспорт и листы
SHEET_VIEWS = ("face", "front", "three_quarter")


def heroes_of(root) -> List[dict]:
    """Наши герои: скелет (кто это) + text/locks.json (приметы словами) + листы из проекта.

    Листы прикладываются генератору картинками: так решено после цикла 6 — без листа
    на обложке выходит чужое лицо конкурента, а не наш герой."""
    root = Path(root)
    skeleton = read_json(root / "text" / "skeleton.json")
    if not isinstance(skeleton, dict) or not skeleton.get("characters"):
        raise CoverError("Нет героев ролика (text/skeleton.json): сначала должны пройти «Скелет» и «Листы героев».")
    locks = read_json(root / "text" / "locks.json", {}) or {}
    out = []
    for c in skeleton["characters"]:
        cid = str(c.get("id") or "")
        if not cid:
            continue
        lock = str(locks.get(cid) or "")
        parts = [p.strip().rstrip(".") for p in lock.split(";") if p.strip()]
        # паспорт листа: волосы; затылок; лицо и глаза; фигура. Затылок на обложке не виден.
        traits = "; ".join([parts[0]] + parts[2:4]) if len(parts) >= 3 else ""
        if not traits:
            traits = str(c.get("appearance_en") or "").strip()
        out.append({"id": cid, "name": str(c.get("name") or cid), "gender": str(c.get("gender") or ""),
                    "traits": traits or f"{c.get('name') or cid}, an adult",
                    "sheets": hero_sheets(root, cid)})
    return out


def hero_sheets(root, hero_id) -> Dict[str, str]:
    """Листы героя В ПРОЕКТЕ ролика: sheets/<id>__base_<вид>.png, иначе первый вариант образа."""
    folder = Path(root) / "sheets"
    found = {}
    for view in SHEET_VIEWS:
        base = folder / f"{hero_id}__base_{view}.png"
        if base.is_file():
            found[view] = str(base)
            continue
        other = sorted(folder.glob(f"{hero_id}__*_{view}.png")) if folder.is_dir() else []
        other = [p for p in other if p.name.count("__") == 1]
        if other:
            found[view] = str(other[0])
    return found


def hero_refs(heroes, out_dir, limit) -> List[tuple]:
    """Опорные картинки наших героев: на героя лист лица и одна «фигура» (спереди и 3/4
    рядом на одном холсте, как в цикле 7). Не больше limit картинок: у провайдера их
    всего 10, первая — превью донора. Кто не влез — остаётся в промпте словами."""
    from PIL import Image
    out_dir = Path(out_dir)
    refs = []
    for h in heroes:
        sheets = h.get("sheets") or {}
        pieces = []
        if sheets.get("face"):
            pieces.append((f"{h['id']}_face.png", sheets["face"]))
        body = [sheets[v] for v in ("front", "three_quarter") if sheets.get(v)]
        if body:
            dst = out_dir / f"{h['id']}_body.png"
            if not dst.is_file() or any(os.path.getmtime(b) > os.path.getmtime(dst) for b in body):
                out_dir.mkdir(parents=True, exist_ok=True)
                ims = [Image.open(b).convert("RGB") for b in body]
                height = max(im.height for im in ims)
                ims = [im.resize((max(1, round(im.width * height / im.height)), height), Image.LANCZOS) for im in ims]
                canvas = Image.new("RGB", (sum(im.width for im in ims) + 24 * (len(ims) - 1), height), (255, 255, 255))
                x = 0
                for im in ims:
                    canvas.paste(im, (x, 0))
                    x += im.width + 24
                canvas.save(str(dst) + ".tmp.png")
                os.replace(str(dst) + ".tmp.png", dst)
            pieces.append((f"{h['id']}_body.png", str(dst)))
        if len(refs) + len(pieces) > limit:
            pieces = pieces[:max(0, limit - len(refs))]
        for name, path in pieces:
            refs.append((name, path, h["id"]))
        if len(refs) >= limit:
            break
    return refs


# ---------------------------------------------------------------- карточка кадра
CARD_PROMPT = """You are an art director. Our channel makes a NEW YouTube thumbnail with the same idea as an existing one.

Open and LOOK AT the picture file: {path}
and the same picture with a 10×10 grid (lines every 10% of the frame, labelled): {grid}

It is the thumbnail of this video: "{title}".

Describe its IDEA and MEASURE where every key person and object is and how big it is. Then plan a NEW shot of the
same idea: our characters, a different camera, a new background, new body poses with the same action and emotion.

Answer with ONE JSON object and nothing else. Use these exact keys:

{{
 "style": "the art style in one sentence (glossy colour webtoon / black-and-white manga page / 3D render), including how it is split into panels if it is",
 "situation": "what is happening, one sentence — what a viewer reads in one second",
 "contradiction": "the hook: what clashes inside this picture and makes people click",
 "palette": "the main colours and the light",
 "setting": "the background of THIS picture, one sentence",
 "camera": "where the camera of THIS picture stands: height (eye level / from above / from below), angle (frontal / three-quarter / side), distance",
 "new_camera": "a DIFFERENT camera for our version that still shows every key person at the same size, and the change must be VISIBLE at those sizes: tilt the camera (dutch angle) or raise / lower it clearly, turn frontal faces to three-quarter, cut the frame at another edge (another crop of the bodies). Name exactly what changes",
 "new_setting": "a DIFFERENT background for our version: the same kind of place and mood, but another room / street / hall, with other details",
 "fanservice": "how revealing the clothing is, person by person, in words — this is a ceiling our version must not raise",
 "adult": "one sentence: how every person is clearly an adult 20+ in our version; a school uniform (blazer + tie + pleated or checked skirt, sailor collar, bow) becomes office / club / university clothes without any uniform",
 "remove": ["every logo, channel name, watermark, episode or part number, foreign lettering — each with where it is"],
 "keep_graphics": "arrows, speech balloons, plates, panel borders, frame splits of THIS picture that carry the idea",
 "chars": [
   {{"box": [x0, y0, x1, y1],
     "face": [x0, y0, x1, y1],
     "framing": "how the frame cuts this figure: full body / knee-up / waist-up / chest-up / head only, cut by which edge",
     "lock": "one short latin word naming this person, for a file name",
     "kind": "human, or creature for a monster, beast, pet, summoned being, skeleton or golem",
     "human_body": "true if this person or creature has a human body (a monster girl, beastkin, elf, demon woman count), false for a beast, a skeleton, a dragon",
     "same_as": "the number (1, 2 ...) of an earlier entry of this list if this is THE SAME person shown again (before/after panels, past and present), else null",
     "hero": "id of OUR character this person becomes, or null",
     "role": "who this person is in the story",
     "was": "how this person looks in the picture now",
     "emotion": "the emotion shown by ANATOMY: eyes, pupils, eyebrows, mouth, sweat, blush, shading lines",
     "action": "what this person DOES — this action must stay",
     "new_pose": "a NEW body pose for our version with the same action and emotion: other turn of the body, other arms or hands, other lean",
     "outfit": "the clothes in our version (adult clothes, the same coverage); for a person who is not our hero ANOTHER garment in ANOTHER colour than now",
     "coverage": "how fastened the clothes are, how deep the neckline is — as they are now",
     "new_look": "only if hero is null: a NEW person design — ANOTHER hair colour AND another haircut than now, another face, age 20+ — so this person is not a copy; keep only the role and the emotion, never write the word same"}}
 ],
 "key_objects": [{{"what": "a key object of the idea (chest, weapon, system window, credit card, sign) — only if it matters", "box": [x0, y0, x1, y1]}}],
 "texts": [
   {{"orig": "the words exactly as written, letter for letter",
     "rows": ["the first printed line", "the second printed line"],
     "box": [x0, y0, x1, y1],
     "words": 2,
     "look": "colour, outline, weight, how many lines, where",
     "fill": "#RRGGBB of the letters",
     "stroke": "#RRGGBB of the outline, or null"}}
 ]
}}

RULES.
- Coordinates x0,y0,x1,y1 are shares of the frame from the top-left corner, between 0 and 1. Read them off the grid
  lines of the grid file, not by eye. They are the SIZE PLAN of our version, so a wrong box makes our heroes too big
  or too small. In a close-up the face alone can fill most of the frame: measure it, do not shrink it.
- "box" is tight around the VISIBLE figure of that person: from the top of the head to its lowest visible point
  (where the body disappears behind another person, a table or the frame edge).
- "face" is tight around the face from the hairline to the chin and from cheek to cheek (without the hair); if a
  hand or an object covers the chin, give where the chin is under it. null if the face is not visible.
- List EVERY person who is visible enough to be recognised, left to right. If there are none, "chars" is [].
- THE PEOPLE OF THIS PICTURE BECOME OUR CHARACTERS. The one or two people whose figures are biggest MUST get
  a "hero" id from the list below, matched by role and gender (the man of the pair to our male lead, the woman
  to our female lead). EVERY other person whose role matches one of our characters gets that id too (a father to
  our father, a rich daughter to our rich heiress, a rival to our rival): read the video title and the names.
  "hero": null is only for people who match none of our characters and for the crowd.
- The same "hero" id is never given to two DIFFERENT people. When the picture shows ONE person twice (a before/after
  split, past and present, weak and strong), both entries are that one person: give the second one "same_as" = the
  number of the first and the same "hero" id.
- A creature (monster, beast, pet, summoned skeleton, golem) has "kind": "creature" and "hero": null; it never
  becomes one of our characters, even when it is the biggest figure. Its "new_look" is a new design of the creature.
- Quote every text EXACTLY as printed, keeping capitals and punctuation. Do not translate it here.
- "rows" is that same text split the way it is really printed: one entry per printed line, never empty.
  Words that stand side by side on ONE line are ONE entry.
- Do not invent anything that is not in the picture, except the fields that plan OUR version (new_camera,
  new_setting, new_pose, outfit, new_look, adult).

OUR CHARACTERS (choose "hero" only from these ids, or null):
{heroes}
"""

CARD_KEYS = ("style", "situation", "setting", "fanservice", "adult", "new_camera", "new_setting")
CHAR_KEYS = ("was", "emotion", "action", "new_pose", "outfit")


def card_problems(card) -> List[str]:
    """Чего в карточке не хватает — словами и с числами, чтобы модель дозаполнила именно это.

    Полноту проверяет КОД: ответ модели на веру не берём. Рамки обязательны: из них код
    считает план крупности, без них промпт не может сказать генератору «той же величины»."""
    bad: List[str] = []
    if not isinstance(card, dict):
        return ["ответ не JSON-объект"]
    for key in CARD_KEYS:
        if not str(card.get(key) or "").strip():
            bad.append(f"пустое поле {key}")
    if not isinstance(card.get("remove"), list):
        bad.append("remove должен быть списком (что убрать с обложки)")
    chars = card.get("chars")
    if not isinstance(chars, list) or not chars:
        bad.append("chars пуст: на обложке должен быть хотя бы один человек")
        chars = []
    for i, c in enumerate(chars, start=1):
        if not isinstance(c, dict):
            bad.append(f"человек {i}: не объект")
            continue
        for key in CHAR_KEYS:
            if not str(c.get(key) or "").strip():
                bad.append(f"человек {i}: пустое поле {key}")
        if _box(c.get("box") or c.get("crop")) is None:
            bad.append(f"человек {i}: box должен быть [x0,y0,x1,y1] в долях кадра, 0..1, x1>x0 и y1>y0")
        if c.get("face") not in (None, "", []) and _box(c.get("face")) is None:
            bad.append(f"человек {i}: face должен быть [x0,y0,x1,y1] в долях кадра или null")
    for i, o in enumerate(card.get("key_objects") or [], start=1):
        if not isinstance(o, dict) or _box(o.get("box")) is None:
            bad.append(f"объект {i}: box должен быть [x0,y0,x1,y1] в долях кадра")
    texts = card.get("texts")
    if not isinstance(texts, list):
        bad.append("texts должен быть списком (надписи обложки; если их нет — пустой список)")
        texts = []
    for i, t in enumerate(texts, start=1):
        if not isinstance(t, dict):
            bad.append(f"надпись {i}: не объект")
            continue
        if not str(t.get("orig") or "").strip():
            bad.append(f"надпись {i}: пустое поле orig (что написано)")
        if _box(t.get("box")) is None:
            bad.append(f"надпись {i}: box должен быть [x0,y0,x1,y1] в долях кадра, 0..1")
        if not str(t.get("look") or "").strip():
            bad.append(f"надпись {i}: пустое поле look (цвет, обводка, сколько строк)")
        # rows — разбивка надписи на печатные строки: по ней считаются предел длины
        # перевода и высота строки.
        if not [r for r in (t.get("rows") or []) if str(r).strip()]:
            bad.append(f"надпись {i}: пустое поле rows (как надпись разбита на печатные строки)")
    # Цвет букв (fill, stroke) в обязательных НЕ проверяется: он замеряется на доноре.
    return bad


def _area(c) -> float:
    box = _box(c.get("box") or c.get("crop"))
    return (box[2] - box[0]) * (box[3] - box[1]) if box else 0.0


def hero_problems(card, heroes, strict=True) -> List[str]:
    """Стали ли главные люди кадра НАШИМИ героями.

    Обложка, где всем людям проставлено hero=null, — это перерисованный кадр конкурента с
    нашей надписью. strict — ещё и «двое самых крупных», когда наших героев двое и больше.
    Без strict — только «хоть один наш»: ниже этого обложку не покупаем вовсе."""
    if not heroes:
        return []
    # Монстр, питомец, призванный скелет нашим героем не становится (живьём: карточка
    # назначала человеческого героя «сильнейшему скелету некроманта»). Повтор того же
    # человека (панели «до/после») отдельным человеком не считается.
    chars = [c for c in (card.get("chars") or []) if isinstance(c, dict) and not is_creature(c) and not c.get("same_as")]
    if not chars:
        return []
    ids = {h["id"] for h in heroes}
    ours = [c for c in chars if str(c.get("hero") or "") in ids]
    if not ours:
        return ["ни один человек кадра не назначен нашим героем: двое самых крупных людей кадра ДОЛЖНЫ "
                "получить hero из списка наших (мужчина — нашему мужчине, женщина — нашей женщине), "
                "null оставляют только массовке"]
    if not strict:
        return []
    need = min(2, len(ids), len(chars))
    biggest = sorted(chars, key=_area, reverse=True)[:need]
    left = [c for c in biggest if str(c.get("hero") or "") not in ids]
    if left:
        return ["крупный человек кадра остался чужим: " + "; ".join(str(c.get("role") or c.get("lock") or "?") for c in left)
                + f" — {need} самых крупных людей кадра ДОЛЖНЫ получить hero из списка наших"]
    return []


def is_creature(c) -> bool:
    return str((c or {}).get("kind") or "").strip().lower() in CREATURE_KINDS


# Существо с телом человека: монстродевушка, звероженщина, эльфийка, демоница. Нашим героем
# оно не становится, но одежду и возраст у него судим как у человека: живьём (W_Ot8gylpU8)
# волчица в латах вышла с открытым вырезом, гидра — в бикини, и ни одна проверка не смотрела.
HUMAN_BODY = re.compile(r"girl|woman|women|female|lady|maiden|queen|princess|wife|bride|beastkin|humanoid|\belf|"
                        r"succub|demoness|mermaid|harpy|lamia|centaur|nymph|cleavage|\bbust|breast|\bbra\b|bikini|"
                        r"skirt|dress|neckline|midriff|thigh|\bhips?\b|torso", re.I)


def human_body(c) -> bool:
    """Есть ли у этого человека или существа фигура человека (тогда его одежду сверяем)."""
    if not is_creature(c):
        return True
    if (c or {}).get("human_body") is True:
        return True
    if (c or {}).get("human_body") is False:
        return False
    text = " ".join(str((c or {}).get(k) or "") for k in ("role", "was", "coverage", "outfit", "framing"))
    return bool(HUMAN_BODY.search(text))


# Номер части, серии, главы донора: «1-2», «(3~4)», «Part 6», «Ch:1-32», «EP 12», «S2».
# Надпись, похожая на номер, не переводится и не рисуется: номера частей владелец велел
# убирать. Живьём «1-2» уходило в перевод и рисовалось, проверка ловила его как чужой
# знак, покупала ремонт, ремонт откатывался — итог «отдавать нельзя».
EPISODE = re.compile(r"^[\(\[]?\s*(?:(?:part|pt|ep|eps|episode|ch|chap|chapter|season|vol|s)\s*[.:#]?\s*\d+"
                     r"(?:\s*[-–—~/]\s*\d+)?|\d+\s*[-–—~/]\s*\d+)\s*[\)\]]?$", re.I)
# Что по смыслу входит в «убрать»: логотипы, имена каналов, водяные знаки, номера частей,
# иностранные буквы. Остальное (стрелки, сами надписи, которые мы переводим) — не «убрать»:
# промпт одновременно велел бы убрать надпись и нарисовать на её месте перевод.
REMOVE_KINDS = re.compile(r"logo|watermark|channel|signature|subscribe|episode|\bpart\b|chapter|season|"
                          r"number|korean|chinese|japanese|hangul|kanji|foreign|@|\.com|copyright|©", re.I)
CREATURE_KINDS = {"creature", "monster", "beast", "animal", "pet", "summon", "skeleton", "golem", "undead"}
NEW_LOOK = "a NEW adult design (20+): another hair colour and cut, another face shape, so this person is not a copy"
NEW_CREATURE = "a NEW design of this creature: another silhouette details, colours and armour, the same size and menace"


# Облик не нашего человека, который карточка велела сохранить. Живьём (S3 PL): «same long
# straight red hair … same red eyes», одежда «pink zip-up hoodie» — та же рыжая девушка в той
# же розовой толстовке, что у донора, то есть копия чужого персонажа.
SAME_WORD = re.compile(r"\b(?:same|identical|unchanged|as before|as now|as in the (?:picture|original|image))\b", re.I)
COLOUR_WORD = re.compile(r"\b(?:red|ginger|orange|pink|purple|violet|blue|navy|teal|green|yellow|blond|blonde|golden|"
                         r"brown|chestnut|black|white|silver|grey|gray|platinum|beige|cream)\b", re.I)


def new_look_of_stranger(c) -> None:
    """Не наш человек обязан стать другим: другие волосы и другая одежда, роль и эмоция те же.
    Код не верит карточке на слово: слово «same» или донорский цвет — облик заменяется."""
    was = str(c.get("was") or "")
    was_colours = {w.lower() for w in COLOUR_WORD.findall(was)}
    look = str(c.get("new_look") or "")
    if look.startswith(NEW_LOOK):
        pass
    elif SAME_WORD.search(look) or (was_colours & {w.lower() for w in COLOUR_WORD.findall(look)}):
        c["new_look"] = NEW_LOOK + (f" — NOT like IMAGE 1 ({was.strip()[:120]}): another hair colour and cut" if was else "")
        c["look_forced"] = True
    outfit = str(c.get("outfit") or "")
    same_colour = was_colours & {w.lower() for w in COLOUR_WORD.findall(outfit)}
    if (SAME_WORD.search(outfit) or same_colour) and "ANOTHER colour and cut" not in outfit:
        c["outfit"] = (outfit.strip().rstrip(".") + " — in ANOTHER colour and cut than on IMAGE 1"
                       + (f" (not {', '.join(sorted(same_colour))})" if same_colour else ""))
        c["look_forced"] = True


def _quoted(text: str) -> List[str]:
    return [q.strip() for q in re.findall(r'["«“](.+?)["»”]', str(text or "")) if q.strip()]


def tidy_texts_and_remove(card) -> None:
    """Надписи-номера частей — в «убрать»; из «убрать» — то, что мы переводим, и не-знаки."""
    remove = [str(x).strip() for x in (card.get("remove") or []) if str(x).strip()]
    texts, dropped = [], []
    for t in card.get("texts") or []:
        orig = str((t or {}).get("orig") or "").strip()
        said = [r for r in remove if orig and orig.lower() in [q.lower() for q in _quoted(r)]]
        if orig and (EPISODE.match(orig) or any(re.search(r"episode|\bpart\b|chapter|season|logo|watermark|channel", r, re.I)
                                                for r in said)):
            dropped.append(orig)
            continue
        texts.append(t)
    keep_origs = {str(t.get("orig") or "").strip().lower() for t in texts}
    kept = []
    for r in remove:
        if any(q.lower() in keep_origs for q in _quoted(r)):
            continue             # это наша надпись: её не убирают, а переводят
        if not REMOVE_KINDS.search(r) and not any(q.lower() in [d.lower() for d in dropped] for q in _quoted(r)):
            continue             # стрелка, плашка — это keep_graphics, а не «убрать»
        kept.append(r)
    for d in dropped:
        if not any(d.lower() in [q.lower() for q in _quoted(r)] for r in kept):
            kept.append(f'"{d}" part or episode number')
    card["texts"] = texts
    card["remove"] = kept
    card["dropped_texts"] = list(dict.fromkeys(list(card.get("dropped_texts") or []) + dropped))


def tidy_card(card, heroes) -> dict:
    """Приводит ответ модели к виду, на который рассчитан сборщик промпта."""
    hero_ids = {h["id"] for h in heroes}
    card = dict(card)
    tidy_texts_and_remove(card)
    chars, used = [], {}
    raw_chars = [c for c in (card.get("chars") or []) if isinstance(c, dict)]
    for i, c in enumerate(raw_chars, start=1):
        c = dict(c)
        # «crop» — имя рамки у старой карточки и у листа для глаз: держим оба
        c["box"] = _box(c.get("box") or c.get("crop"))
        c["crop"] = c["box"]
        c["face"] = _box(c.get("face"))
        c["lock"] = _slug(c.get("lock"), f"p{i}")
        hb = c.get("human_body")
        if isinstance(hb, str):
            hb = {"true": True, "yes": True, "false": False, "no": False}.get(hb.strip().lower())
        c["human_body"] = hb if isinstance(hb, bool) else None
        try:
            same = int(c.get("same_as") or 0)
        except (TypeError, ValueError):
            same = 0
        c["same_as"] = same if 1 <= same < i else None
        hero = str(c.get("hero") or "").strip()
        if not hero and c["same_as"]:
            hero = str(chars[c["same_as"] - 1].get("hero") or "")   # тот же человек — тот же герой
        if is_creature(c):
            hero = ""
        # Один герой — двум людям только если это ОДИН человек дважды (панели «до/после»).
        if hero in used and not (c["same_as"] and used[hero] == c["same_as"]):
            hero = ""
        c["hero"] = hero if hero in hero_ids else None
        if c["hero"] and c["hero"] not in used:
            used[c["hero"]] = i
        if not c["hero"] and not str(c.get("new_look") or "").strip():
            # Модель оставила пустым облик не нашего человека. Шаг из-за этого не падает:
            # «новый взрослый облик» подставляет код (живьём: донор «до/после» падал 2 из 2).
            c["new_look"] = NEW_CREATURE if is_creature(c) else NEW_LOOK
        if not c["hero"] and not is_creature(c):
            new_look_of_stranger(c)
        c.setdefault("slot", f"person {i}")
        chars.append(c)
    seen = {}
    for c in chars:      # имена уникальны: по ним различаются люди в листе и в ремонте
        seen[c["lock"]] = seen.get(c["lock"], 0) + 1
        if seen[c["lock"]] > 1:
            c["lock"] = f"{c['lock']}{seen[c['lock']]}"
    card["chars"] = chars
    card["key_objects"] = [{"what": str(o.get("what") or "object").strip(), "box": _box(o.get("box"))}
                           for o in (card.get("key_objects") or []) if isinstance(o, dict) and _box(o.get("box"))]
    texts = []
    for t in card.get("texts") or []:
        t = dict(t)
        t["orig"] = str(t.get("orig") or "").strip()
        t["rows"] = [str(r).strip() for r in (t.get("rows") or []) if str(r).strip()]
        t["box"] = _box(t.get("box"))
        t["fill"] = _rgb(t.get("fill"))
        t["stroke"] = _rgb(t.get("stroke")) if t.get("stroke") else None
        t["sw"] = 0.10 if t["stroke"] else 0.0
        t["lines"] = t.get("lines") if isinstance(t.get("lines"), dict) else {}
        try:
            t["words"] = int(t.get("words") or len(t["orig"].split()))
        except (TypeError, ValueError):
            t["words"] = len(t["orig"].split())
        texts.append(t)
    card["texts"] = texts
    card["version"] = CARD_VERSION
    return card


# Сколько раз шаг действительно запускал claude CLI: повторы внутри одного вопроса (ответ не
# разобрался как JSON) — такие же вызовы пула Claude, и в заметке их не было видно.
CLI_TRIES = [0]
# Свой счётчик у каждой обложки: три обложки в одном процессе (4 ролика идут параллельно) иначе
# смешивали вызовы друг друга в заметке. run_step ставит [0], потоки шага получают его через CP.carry.
_TRIES = contextvars.ContextVar("cover_cli_tries", default=None)
# Общий предел одновременных вызовов Claude (MANGA_CLAUDE_CONCURRENCY): обложка стоит в той же
# очереди, что текстовые шаги. Через lambda — чтобы подмена LLM.call_json в тестах работала.
_call_json = LLM.CP.limited(lambda *a, **k: LLM.call_json(*a, **k))


def ask_json(prompt: str, model: str, retries: int = 2, **kw):
    """LLM.call_json, но каждая попытка считается в CLI_TRIES и в счётчике этой обложки."""
    last = None
    for attempt in range(retries + 1):
        CLI_TRIES[0] += 1
        box = _TRIES.get()
        if box is not None:
            box[0] += 1
        try:
            return _call_json(prompt, model=model, retries=0, **kw)
        except LLM.LLMError as exc:
            last = exc
            if "JSON" not in str(exc):
                raise
            prompt = prompt + "\n\nПРЕДЫДУЩИЙ ОТВЕТ НЕ РАЗОБРАЛСЯ КАК JSON. Верни ТОЛЬКО валидный JSON."
    raise last


def with_files(prompt: str, images: List[str]) -> str:
    """claude CLI получает картинки не вложением, а путём: без списка файлов в промпте он их
    не открывает и отвечает выдумкой (critic р3: запасной чтец вернул «No image provided» и
    «Marvot's Guesthouse», и код счёл это браком букв хорошей обложки)."""
    if not images:
        return prompt
    listing = "\n".join(f"  IMAGE {i}: {os.path.abspath(str(p))}" for i, p in enumerate(images, start=1))
    return (prompt + "\n\nThe images are files on disk. Open EVERY one with the Read tool before answering, "
            "in this order:\n" + listing + "\nIf you cannot open a file, answer {\"error\": \"no image\"} and nothing else.")


def vision_json(prompt: str, images: List[str], model: str, timeout: int = 300):
    """Один вопрос модели, которая СМОТРИТ картинки. Возвращает разобранный JSON.

    claude — путь по подписке владельца: CLI сам читает файл инструментом Read, поэтому
    картинка никуда не грузится (она уже лежит на той же машине). codex — запасной путь:
    тот же вызов с вложениями, что у судьи кадров."""
    kind = model.partition(":")[0]
    if kind == "claude":
        # Read — только в папке ролика: надпись на чужой обложке не должна суметь
        # заставить модель прочитать файл сервера (web.env) и вернуть его в карточку,
        # откуда текст идёт в промпт и на страницу. Папка ролика — ещё и рабочая папка
        # CLI: под рабочей папкой Read разрешён всегда, и она не должна быть папкой службы.
        folder = os.path.commonpath([os.path.dirname(os.path.abspath(str(p))) for p in images]) if images else None
        tools = [f"Read(/{folder}/**)"] if folder else ["Read"]
        data, _answer = ask_json(with_files(prompt, images), model, timeout=timeout, tools=tools, cwd=folder)
        return data
    if kind == "codex":
        import judge as JU
        raw = JU._codex(prompt, list(images), model.partition(":")[2] or JU.MODEL, timeout=timeout)
        text = LLM.strip_fence(raw)
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j <= i:
            raise CoverError(f"Модель не вернула JSON про картинку: {text[:160]}")
        return json.loads(text[i:j + 1])
    raise CoverError(f"Неизвестная модель для разбора картинки: {model}")


def build_card(thumb_path, donor_title, heroes, model=None, vision=None) -> dict:
    """Карточка кадра по превью донора. Неполноту дозапрашиваем, пустоту не принимаем."""
    model = model or vision_model()
    vision = vision or vision_json
    heroes_text = "\n".join(f"- {h['id']}: {h['name']} ({h['gender'] or '?'}) — {h['traits']}" for h in heroes) or "- (нет)"
    # Рамки донора — по НАСТОЯЩЕЙ сетке, а не «представь сетку»: живьём (TkZusvlzUoU, крупный
    # план) лицо вышло 0.42 кадра при настоящих ~0.70, и генератор честно нарисовал героиню мельче.
    grid = make_grid(thumb_path, Path(thumb_path).with_name("cover_donor_grid.jpg"))
    prompt = CARD_PROMPT.format(path=str(thumb_path), grid=grid, title=str(donor_title or "").replace('"', "'"),
                                heroes=heroes_text)
    card, bad = None, ["разбор кадра не выполнялся"]
    for attempt in (1, 2):
        try:
            raw = vision(prompt if attempt == 1 else prompt + _reask(bad), [str(thumb_path), grid], model)
        except CoverError:
            raise
        except _STOP:
            raise
        except Exception as exc:
            raise CoverError(
                f"Не удалось разобрать обложку донора ({type(exc).__name__}: {str(exc)[:200]}). "
                "Шаг «Обложка» смотрит превью моделью по подписке: проверьте, что на сервере есть "
                "CLAUDE_CODE_OAUTH_TOKEN и модель MANGA_COVER_VISION отвечает, и повторите незавершённое."
            ) from None
        card = tidy_card(raw if isinstance(raw, dict) else {}, heroes)
        bad = card_problems(card) + hero_problems(card, heroes)
        if not bad:
            return card
        if attempt == 2 and not card_problems(card) and not hero_problems(card, heroes, strict=False):
            # Второй крупный человек так и остался чужим (например, двое самых крупных —
            # две женщины при одной нашей героине). Нижняя граница выполнена: наш есть.
            log("второй крупный человек кадра остался чужим и со второй попытки — беру карточку как есть")
            return card
        log(f"в карточке кадра не хватает: {'; '.join(bad[:6])} — перезапрашиваю")
    if hero_problems(card or {}, heroes, strict=False):
        # Дальше идти незачем: проход стоит 4 единицы, а отдать он может только
        # перекрашенную чужую обложку. Отказ виден владельцу и повторяется кнопкой.
        raise CoverError("На превью донора не нашлось ни одного человека, которым может стать наш герой: "
                         "обложка без наших героев — это перекрашенная обложка конкурента, и покупать её "
                         "шаг не стал. Повторите незавершённое или задайте другого донора.")
    raise CoverError("Разбор обложки донора неполный даже со второй попытки: "
                     + "; ".join(bad[:8])
                     + ". Обложка не собрана; повторите незавершённое или задайте другого донора.")


def _reask(bad) -> str:
    return ("\n\nYOUR PREVIOUS ANSWER WAS INCOMPLETE. Send the WHOLE JSON again, filled in, "
            "fixing exactly these points:\n- " + "\n- ".join(bad[:10]))


# ---------------------------------------------------------------- надписи: локализация
TEXT_PROMPT = """You are a native {lang} copywriter of a manhwa recap YouTube channel. Your viewers are {lang}-speaking
teenagers (13-19). Write the {lang} words for OUR new thumbnail of this video: "{title}".

What the picture shows: {situation} The hook: {hook}

Here are the texts of the ORIGINAL English thumbnail and where they stand. Write OUR {lang} version of each one:

{items}

RULES (from the study of this niche and the owner's decisions).
- This is LOCALISATION, not translation: write what a {lang} teenager would really write on such a picture —
  living spoken {lang}, natural word order, correct cases, articles and verb forms. Never word by word.
- NO ENGLISH WORDS AT ALL. English fan jargon is replaced by a native word with the same meaning:
  MC / FMC (the hero) -> "I" or the native word ("YO", "EU", "JA", "Я"); GF / WAIFU -> "my girl" in native slang;
  TOXIC-EX -> the native phrase ("MI EX LOCA", "БЫВШАЯ-ИСТЕРИЧКА"); LEVEL / LVL -> the native word
  (Polish POZIOM, Russian УРОВЕНЬ); SYSTEM, SKILL, CLASS, BULLY, HELP, KING, SIMP, RIZZ, BRO -> native words.
  Allowed as they are: numbers, money amounts, currency signs, ranks written as letters (A, B, S, SS, SSS),
  and "M" of millions written the native way ("10 МЛН", "10 MLN").
- Keep the HOOK of the original: the same promise, the same tease, the same surprise, the same speaker.
- Drop episode, part, chapter and season numbers ("Ch:1-32", "Part 6") and any channel name completely.
- Keep the same number of lines, split where the original splits them.
- NO LINE MAY BE LONGER THAN ITS LIMIT IN CHARACTERS (given after each text): the words go into the same zone
  and at the same letter height as the original. If a word does not fit, choose a SHORTER native word.
- CAPITALS stay capitals. Use the correct {lang} letters with all their accent marks.

Good examples from our owner-approved thumbnails: "MC | TOXIC-EX" -> Spanish "YO | MI EX LOCA", Russian "Я |
БЫВШАЯ-ИСТЕРИЧКА"; "BEFORE | AFTER | WAIFU?" -> Czech "PŘED | PO | TA PRAVÁ?", Portuguese "ANTES | DEPOIS |
MOZÃO?"; "NOW WE'RE MARRIED." -> Polish "NO TO PO ŚLUBIE.", Italian "ECCO. SIAMO SPOSATI."; "BULLY" -> Polish
"ŁOBUZ"; "Ok, Thanks! | Leave her! | 10M $$" -> Russian "СПАСИБО, БЕРУ! | БРОСЬ ЕЁ! | 10 МЛН $$"; "LEVEL 500" ->
Polish "POZIOM 500".

Answer with ONE JSON object and nothing else:
{{"texts": [{{"orig": "<the English text, copied exactly>", "lines": ["LINE 1", "LINE 2"]}}]}}
"""

PROOF_PROMPT = """You are a native {lang} proofreader of YouTube thumbnails for teenagers.

Each English caption below was localised into {lang}. Check every one as a native speaker: word order, cases,
articles, prepositions, verb forms, accent marks, and does it sound like living teen speech and not like a
translation. Check EVERY accent mark letter by letter: a missing one is a spelling mistake (Romanian "TE VAD" is
wrong, "TE VĂD" is right; Polish "DZIEKI" is wrong, "DZIĘKI" is right). Word-by-word versions are wrong (Romanian "AM LUAT-O MAMA EI?!" is wrong, "AM LUAT-O PE MAMA EI?!" is
right; Polish "KOMANDOS KRÓL" is wrong, "KRÓL KOMANDOSÓW" is right). ANY English word (MC, GF, WAIFU, TOXIC,
LEVEL, SYSTEM, HELP, BULLY, KING ...) is wrong: replace it with the native word. Numbers, money and rank letters
(A, B, S, SSS) stay.

{items}

If a caption is already natural and correct, return it unchanged. If not, return the corrected one with the SAME
hook, the same number of lines, CAPITALS kept, and no line longer than its limit.

Answer with ONE JSON object and nothing else:
{{"texts": [{{"orig": "<the English text, copied exactly>", "lines": ["LINE 1", "LINE 2"]}}]}}
"""

# Английские слова, которых на нашей обложке быть не должно (решение владельца 18.09:
# «текст не адаптирован под язык ЦА»). Ранги буквами и числа разрешены.
ENGLISH_WORDS = frozenset("""
MC FMC ML GF BF WAIFU HUSBANDO TOXIC EX LEVEL LEVELS LVL SYSTEM SKILL SKILLS CLASS BULLY HELP KING QUEEN
SIMP RIZZ BRO NOOB BOSS HERO VILLAIN THANKS THANK YOU MY HER HIM SHE THE AND NOT YES NOW WE WERE
BEFORE AFTER LEAVE LOVE WIFE HUSBAND GIRL BOY GIRLFRIEND BOYFRIEND MONEY RICH POOR POWER OP STRONGEST
WEAKEST GOD SWORD MASTER COMMANDO FAKE CHEAT WHAT WHO WHY HOW OMG LOL WOW OK OKAY RANK
""".split())
# Слова, совпадающие по буквам с английскими, но свои в языке ролика (исп. «MI EX LOCA»
# одобрено владельцем в цикле 6; пол. «OK» — обычное польское слово).
NATIVE_OK = {
    "es": {"EX", "BOSS", "OK"}, "it": {"EX", "BOSS", "OK"}, "pt": {"EX", "BOSS", "OK"},
    # пол. «MY» — «мы», «WE» — «в» («WE ŚNIE»), «EX» — «бывший»; чеш. «MY» — «мы».
    "ro": {"EX", "BOSS", "OK"}, "pl": {"BOSS", "OK", "MY", "WE", "EX"}, "cs": {"EX", "BOSS", "OK", "MY"},
    "ru": set(), "en": set(ENGLISH_WORDS),
}
RANK = re.compile(r"^(?:[A-F]|S{1,3}|[A-F][+-]{1,2}|S{1,3}[+-]{1,2})$")


def english_words(lines, iso) -> List[str]:
    """Английские слова в надписи — проверка КОДОМ, а не на слово модели.

    Ранги буквами (A, S, SSS) и числа разрешены; слова, совпадающие по буквам со своими
    словами языка (исп. «EX», пол. «NO»), разрешены списком NATIVE_OK."""
    allowed = NATIVE_OK.get(iso, set())
    found = []
    for word in re.findall(r"[^\W\d_]+", norm(" ".join(lines or [])).upper()):
        if RANK.match(word) or word in allowed:
            continue
        if word in ENGLISH_WORDS and word not in found:
            found.append(word)
    return found


LINE_GROW = 1.3   # локализация вправе быть длиннее английского на 30%: «СПАСИБО, БЕРУ!» против «Ok, Thanks!»


def _cap(rows) -> int:
    return max(4, round(max(len(r) for r in rows) * LINE_GROW))


def line_limit(text) -> str:
    """Предел длины строки для переводчика — по самой длинной строке оригинала.

    Замер на доноре 4uwdsJjLCqs: в узкой рамке одно слово из 11 букв сжимает весь блок
    втрое, и надпись в ленте не читается. Годный короткий вариант почти всегда есть."""
    rows = [r for r in (text.get("rows") or []) if r]
    if not rows:
        return ""
    return (f'; lines of the original: {" / ".join(rows)}; HARD LIMIT: no line longer than {_cap(rows)} characters')


def too_long(items, data) -> List[str]:
    """Строки перевода, которые длиннее предела своей надписи."""
    caps = {}
    for t in items:
        rows = [r for r in (t.get("rows") or []) if r]
        if rows:
            caps[t["orig"]] = _cap(rows)
    bad = []
    for row in (data or {}).get("texts") or []:
        cap = caps.get(str((row or {}).get("orig") or "").strip())
        if not cap:
            continue
        for line in (row or {}).get("lines") or []:
            if len(str(line)) > cap:
                bad.append(f'«{line}» — {len(str(line))} букв при пределе {cap}')
    return bad


def english_left(data, iso) -> List[str]:
    """Строки ответа, где осталось английское слово: ««MC» в «MC I BYŁA»»."""
    bad = []
    for orig, lines in _lines_of(data).items():
        words = english_words(lines, iso)
        if words:
            bad.append(f'{", ".join(words)} in "{" / ".join(lines)}" (for "{orig}")')
    return bad


def translate_texts(card, lang, model=None, call=None, title="") -> Dict[str, List[str]]:
    """Надписи на языке ролика: {английский оригинал: [строки]}."""
    iso = LG.iso(lang)
    items = [t for t in card.get("texts") or [] if t.get("orig")]
    if not items:
        return {}
    call = call or (lambda prompt: ask_json(prompt, model or vision_model())[0])
    listing = "\n".join(
        f'{i}. "{t["orig"]}" — {t.get("words") or len(t["orig"].split())} word(s), {t.get("look") or ""}'
        + (f', zone x {pct(t["box"][0], t["box"][2])}, y {pct(t["box"][1], t["box"][3])}' if t.get("box") else "")
        + line_limit(t) for i, t in enumerate(items, start=1))
    prompt = TEXT_PROMPT.format(lang=_lang_name(lang), items=listing, title=str(title or "").replace('"', "'"),
                                situation=str(card.get("situation") or "").strip(),
                                hook=str(card.get("contradiction") or "").strip())
    data = call(prompt)
    # Предел строки и английские слова проверяет КОД: модель нарушает их молча.
    bad = too_long(items, data) + [f"English word left: {x}" for x in english_left(data, iso)]
    if bad:
        log("перевод надо поправить: " + "; ".join(bad[:3]) + " — прошу ещё раз")
        data = call(prompt + "\n\nYOUR PREVIOUS ANSWER BROKE THE RULES. Send the WHOLE JSON again with SHORTER native "
                             "words that keep the same hook and NO English words, fixing exactly these lines:\n- "
                    + "\n- ".join(bad[:6])) or data
    got = _lines_of(data)
    # Вторым запросом — вычитка носителем. Живьём перевод выходил дословным («AM LUAT-O
    # MAMA EI?!», «KOMANDOS KRÓL»), а сверка «буква в букву» проверяла только совпадение
    # с этим неверным текстом. Считается один раз на ролик.
    if got:
        listing_proof = "\n".join(
            f'{i}. "{t["orig"]}" -> ' + " / ".join(got[t["orig"]]) + line_limit(t)
            for i, t in enumerate(items, start=1) if t["orig"] in got)
        try:
            proof = call(PROOF_PROMPT.format(lang=_lang_name(lang), items=listing_proof))
        except _STOP:
            raise
        except Exception as exc:    # вычитка — довесок: без неё остаётся перевод первого запроса
            log(f"вычитка перевода не ответила ({type(exc).__name__}) — беру перевод как есть")
            proof = None
        fixed = _lines_of(proof)
        if fixed and not too_long(items, proof):
            for orig, lines in fixed.items():
                if orig in got and lines != got[orig] and len(english_words(lines, iso)) <= len(english_words(got[orig], iso)):
                    log(f"вычитка поправила перевод: {' / '.join(got[orig])} -> {' / '.join(lines)}")
                    got[orig] = lines
        elif fixed:
            log("вычитка вышла длиннее предела строки — беру перевод первого запроса")
    missing = [t["orig"] for t in items if t["orig"] not in got]
    if missing:
        raise CoverError("Не получены надписи обложки на языке ролика: "
                         + "; ".join(f'«{m}»' for m in missing[:5])
                         + ". Обложка без своих надписей не собирается.")
    return got


def _lines_of(data) -> Dict[str, List[str]]:
    got = {}
    rows = data.get("texts") if isinstance(data, dict) else None
    for row in rows if isinstance(rows, list) else []:
        orig = str((row or {}).get("orig") or "").strip()
        lines = [str(x).strip() for x in ((row or {}).get("lines") or []) if str(x).strip()]
        if orig and lines:
            got[orig] = lines
    return got


# ---------------------------------------------------------------- план крупности
def _third(y) -> int:
    return 0 if y < 1 / 3 else (1 if y < 2 / 3 else 2)


def _hpos(x) -> str:
    return ("at the far left" if x < 0.15 else "in the left part" if x < 0.35 else "left of centre" if x < 0.45
            else "in the centre" if x <= 0.55 else "right of centre" if x <= 0.65 else "in the right part"
            if x <= 0.85 else "at the far right")


def size_plan(card, donor_path=None) -> dict:
    """План крупности донора ЧИСЛАМИ — из рамок карточки, а не на слово модели.

    Для каждого человека: высота видимой фигуры, высота лица, центр, доля площади, как
    обрезан. Для объекта — рамка и доля площади. Для надписи — зона и высота строки
    (замер по чернилам донора, если он есть)."""
    people = []
    for n, c in enumerate(card.get("chars") or [], start=1):
        box = _box(c.get("box") or c.get("crop"))
        if not box:
            continue
        face = _box(c.get("face"))
        people.append({"n": n, "hero": c.get("hero"), "lock": c.get("lock"),
                       "height": round(box[3] - box[1], 3), "face": round(face[3] - face[1], 3) if face else None,
                       "face_w": round(face[2] - face[0], 3) if face else None,
                       "cx": round((box[0] + box[2]) / 2, 3), "cy": round((box[1] + box[3]) / 2, 3),
                       "top": round(box[1], 3),
                       "area": round((box[2] - box[0]) * (box[3] - box[1]), 3),
                       "framing": str(c.get("framing") or "").strip(), "box": box})
    chars = card.get("chars") or []
    for p in people:
        p["key"] = is_key(chars[p["n"] - 1] if p["n"] - 1 < len(chars) else {}, p)
    if people and not any(p["key"] for p in people):
        # Ключевых по роли и площади нет — ключевой хотя бы самый крупный: без него крупность
        # не проверялась бы вовсе.
        max(people, key=lambda p: p["area"])["key"] = True
    objects = [{"k": k, "what": o["what"], "box": o["box"], "height": round(o["box"][3] - o["box"][1], 3),
                "cy": round((o["box"][1] + o["box"][3]) / 2, 3),
                "area": round((o["box"][2] - o["box"][0]) * (o["box"][3] - o["box"][1]), 3)}
               for k, o in enumerate(card.get("key_objects") or [], start=1)]
    texts = []
    for t in card.get("texts") or []:
        box = _box(t.get("box"))
        if not box:
            continue
        rows = max(1, len([r for r in (t.get("rows") or []) if r]))
        line_h = None
        if donor_path:
            try:
                spread = ink_spread(donor_path, box)
            except (OSError, ValueError):
                spread = None
            if spread:
                line_h = round(spread["h"] / rows / spread["frame"][1], 3)
        texts.append({"orig": t["orig"], "box": box,
                      "line_h": line_h or round((box[3] - box[1]) / rows * 0.8, 3)})
    return {"people": people, "objects": objects, "texts": texts}


def is_key(c, p) -> bool:
    """Ключевой ли человек для крупности: наш герой, ключевая роль (злодей, главный), или
    не массовка крупнее KEY_MIN_AREA кадра. Массовку (фон, толпа, поверженные статисты) не судим."""
    if (c or {}).get("hero") or (c or {}).get("key") is True:
        return True
    role = " ".join(str((c or {}).get(k) or "") for k in ("role", "was"))
    if KEY_ROLE.search(role):
        return True
    if MOB_ROLE.search(role):
        return False
    return (p.get("area") or 0) >= KEY_MIN_AREA


def size_lines(card, plan, heroes_by_id) -> List[str]:
    """План крупности словами и числами для генератора (как в цикле 7)."""
    chars = card.get("chars") or []
    out = ["SIZE PLAN — the most important rule. Every key person and object is EXACTLY as big as in IMAGE 1 and at the "
           "same height in the frame. You change the camera height and angle, the poses and the background, but NOT the "
           "scale. Do not zoom in, do not enlarge the heroes, do not shrink them, do not mirror. If you are unsure, draw a "
           "person slightly SMALLER, never bigger. The horizontal centre of a person may move by up to "
           f"{round(SHIFT_X * 100)}% of the frame width if the new camera needs it; the height of the figure, the height of "
           "the face and the vertical place stay. Measured on IMAGE 1 (percentages of the frame; x from the left edge, "
           "y from the top edge):"]
    for p in plan.get("people") or []:
        c = chars[p["n"] - 1]
        hero = heroes_by_id.get(c.get("hero")) if c.get("hero") else None
        name = hero["name"].upper() if hero else (str(c.get("role") or c.get("was") or f"person {p['n']}").strip())
        s = (f"  - {name}: {_hpos(p['cx'])} of the frame, centre at x≈{round(p['cx'] * 100)}%, y≈{round(p['cy'] * 100)}%; "
             f"the visible figure (top of the head to its lowest visible point) is ≈{round(p['height'] * 100)}% of the frame "
             f"height, the top of the head at y≈{round(p['top'] * 100)}%")
        if p.get("face"):
            s += f"; the face (hairline to chin) is ≈{round(p['face'] * 100)}% of the frame height"
        s += f"; ≈{round(p['area'] * 100)}% of the frame area"
        if p.get("framing"):
            s += f"; framing: {p['framing']}"
        out.append(s + ".")
    for o in plan.get("objects") or []:
        b = o["box"]
        out.append(f"  - {o['what']}: occupies x {pct(b[0], b[2])}, y {pct(b[1], b[3])} "
                   f"(about {round(o['area'] * 100)}% of the frame area).")
    return out


# ---------------------------------------------------------------- промпт генератора
def code_texts(card, iso, forced=()) -> List[dict]:
    """Надписи, которые рисует КОД: румынские с Ș/Ț и те, где генератор дважды ошибся в букве."""
    out = []
    for t in card.get("texts") or []:
        lines = (t.get("lines") or {}).get(iso)
        if not lines:
            continue
        if (iso == "ro" and any(ch in COMMA_BELOW for ch in norm(" ".join(lines)))) or t.get("orig") in set(forced):
            out.append(t)
    return out


MONO_STYLE = re.compile(r"black[- ]and[- ]white|monochrome|gr[ae]yscale|sepia|b/w|screentone|ink[- ]only", re.I)
COVER_COLOUR = ("FULL VIVID COLOUR clickbait YouTube thumbnail: saturated high-key colours, strong colourful light and rim light "
                "on every figure, high contrast, bright and eye-catching even at 246x138 in the feed. NEVER black-and-white, "
                "greyscale, sepia, monochrome or desaturated — not even the background, even when IMAGE 1 is a black-and-white "
                "manga page.")


def cover_style(video_style=None) -> str:
    """Стиль обложки — стиль НАШЕГО ролика (frameprompt.STYLES) плюс требование полного яркого цвета."""
    import frameprompt as FP
    ours = FP.STYLES.get(str(video_style or "")) or FP.STYLES.get("S4") or ""
    return f"{COVER_COLOUR} Drawing manner of our video: {ours}."


def build_prompt(card, lang, heroes_by_id, skip_code=(), plan=None, hero_images=(), feedback=(),
                 video_style=None) -> str:
    """Промпт пересъёмки (L1 цикла 7 без зеркала): донор — образец ИДЕИ и КОМПОЗИЦИИ,
    наши герои — листами, крупность — планом донора, камера, позы и фон — новые.

    hero_images — [(имя файла, путь, id героя)] в том порядке, в каком они приложены
    после IMAGE 1. feedback — поправки после проверки прошлого прохода (пересъёмка)."""
    iso = LG.iso(lang)
    L = _lang_name(lang)
    plan = plan or size_plan(card)
    skip = {id(t) for t in skip_code}
    out = []
    out.append("Create a NEW YouTube thumbnail illustration for a manhwa recap video. IMAGE 1 (competitor_reference.jpg) is a "
               "competitor's thumbnail: use it as the reference for the IDEA and the COMPOSITION — the situation, the emotions, "
               "the contrast, the fan-service level, and above all WHERE each key character, object and "
               "caption sits and HOW BIG it is. Keep the same left/right layout (do NOT mirror). Change the camera height and "
               "angle, every pose (same action, same emotion), every face (our heroes) and the whole background, so a viewer "
               "who has seen IMAGE 1 feels it is a different drawing. Do not reproduce its text. Do NOT take its colours "
               "if IMAGE 1 is black-and-white, greyscale or dull: our video is in FULL COLOUR and so is this thumbnail.")
    out.append("")
    by_hero: Dict[str, List[int]] = {}
    for i, (name, _path, hid) in enumerate(hero_images, start=2):
        by_hero.setdefault(hid, []).append(i)
    ours = [c for c in card.get("chars") or [] if c.get("hero") and heroes_by_id.get(c["hero"]) and not c.get("same_as")]
    if ours:
        out.append("OUR HEROES: the attached character sheets define WHO the heroes are. Copy their faces, hairstyles, hair "
                   "colours, eye colours and marks exactly; ignore the white studio background and the neutral sheet pose — "
                   "they act and emote in the scene.")
        for c in ours:
            hero = heroes_by_id[c["hero"]]
            nums = by_hero.get(hero["id"]) or []
            names = [n for n, _p, hid in hero_images if hid == hero["id"]]
            if nums:
                label = f"IMAGE {nums[0]}" if len(nums) == 1 else f"IMAGES {nums[0]}-{nums[-1]}"
                out.append(f"  {label} ({', '.join(names)}): {hero['name'].upper()} character sheet "
                           f"(face close-up; full body front and three-quarter view) — {hero['traits']}.")
            else:
                out.append(f"  {hero['name'].upper()} (no sheet attached, described in words): {hero['traits']}.")
        out.append("")
    out.append(f"STYLE: {cover_style(video_style)}")
    out.append("")
    brief = f"SITUATION: {card['situation']}."
    if card.get("contradiction"):
        brief += f" CONTRAST (the hook): {card['contradiction']}."
    if card.get("palette") and not MONO_STYLE.search(f"{card.get('style', '')} {card.get('palette', '')}"):
        brief += f" PALETTE of the competitor (a hint, keep it vivid): {card['palette']}."
    brief += f" FAN-SERVICE (a ceiling, never higher): {card['fanservice']}."
    out.append("BRIEF: " + brief)
    out.append("")
    out.append("CAST:")
    for n, c in enumerate(card.get("chars") or [], start=1):
        hero = heroes_by_id.get(c.get("hero")) if c.get("hero") else None
        who = str(c.get("role") or c.get("was") or f"person {n}").strip()
        if hero and c.get("same_as"):
            line = (f"  - {who} = {hero['name'].upper()} AGAIN — the same person as person {c['same_as']}, shown a second "
                    "time (the other panel); same face and hair from the sheet, the look of this panel")
        elif hero:
            line = (f"  - {who} = {hero['name'].upper()} (face, hair, eye colour, marks, build and posture from the "
                    f"sheet: {hero['traits']})")
        elif is_creature(c):
            line = (f"  - {who}: a creature, NOT a person and NOT one of our heroes — a NEW design, not a copy of IMAGE 1: "
                    f"{str(c.get('new_look') or NEW_CREATURE).strip()}")
        else:
            line = (f"  - {who}: NOT one of our heroes — a NEW person, not a copy of IMAGE 1: "
                    f"{str(c.get('new_look') or NEW_LOOK).strip()}")
        line += f"; EMOTION: {c['emotion']}; ACTION: {c['action']}; NEW POSE: {c['new_pose']}; OUTFIT: {c['outfit']}"
        if c.get("coverage"):
            # Потолок фан-сервиса поимённо: живьём у героини ромкома вырез выходил глубже
            # донорского в обоих проходах.
            line += (f" (a CEILING, never more revealing: {c['coverage']} — the neckline no deeper, the bust no bigger "
                     "and no more skin than this person on IMAGE 1)")
        # Монстродевушка — тоже взрослая 20+: живьём у гидры и волчицы строки о возрасте не было вовсе.
        out.append(line + ("." if not human_body(c) else "; clearly an adult 20+ in face and body."
                           if is_creature(c) else "; clearly an adult 20+."))
    out.append("")
    out.extend(size_lines(card, plan, heroes_by_id))
    out.append("")
    out.append(f"NEW CAMERA: {card['new_camera']}. IMAGE 1 was shot like this: {card.get('camera') or 'see IMAGE 1'} — "
               "yours must be visibly different, with the same sizes as in the SIZE PLAN.")
    out.append(f"NEW BACKGROUND: {card['new_setting']}.")
    if card.get("keep_graphics"):
        out.append(f"KEEP THESE GRAPHIC ELEMENTS in the same place and size: {card['keep_graphics']}.")
    if feedback:
        out.append("")
        out.append("CORRECTIONS AFTER THE CHECK OF THE PREVIOUS ATTEMPT (they override everything above except the size plan):")
        for item in feedback:
            out.append(f"  - {item}")
    out.append("")
    out.append("Same action, same emotions, same fan-service level and the same sizes as IMAGE 1 — new camera angle, "
               "new poses, new background.")
    out.append("")
    zones = {t["orig"]: t for t in plan.get("texts") or []}
    drawn = [t for t in card["texts"] if (t.get("lines") or {}).get(iso) and id(t) not in skip]
    empty = [t for t in card["texts"] if (t.get("lines") or {}).get(iso) and id(t) in skip]
    if drawn:
        out.append(f"TEXT — draw EXACTLY these words, letter for letter, with every accent mark, and NO other text anywhere. "
                   f"They are the final {L} captions written by native speakers; do not translate them, do not add English "
                   "words. Each caption has the SAME letter height and sits in the SAME zone as the caption of IMAGE 1. "
                   "Draw the lines one under another, centred — no slashes, no bullets, no quotation marks, no line numbers.")
        for i, t in enumerate(drawn, start=1):
            lines = t["lines"][iso]
            acc = accents(" ".join(lines))
            acc_line = (f"Letters with accent marks in this text: {', '.join(acc)} — draw these marks exactly; every other "
                        "letter is a plain letter with no hook, tail, cedilla or dot added." if acc else
                        "This text has no accent marks at all: every letter is plain, with no hooks, tails or dots added.")
            if iso == "ro" and any(ch in COMMA_BELOW for ch in norm(" ".join(lines))):
                acc_line += " " + RO_COMMA
            box = t["box"] or [0.0, 0.8, 1.0, 1.0]
            zone = zones.get(t["orig"]) or {}
            size = (f"; letters about {round(zone['line_h'] * 100)}% of the frame height per line"
                    if zone.get("line_h") else "")
            out.append(f"TEXT {i} (replaces \"{t['orig']}\") — zone x {pct(box[0], box[2])}, y {pct(box[1], box[3])} of the "
                       f"frame{size}; {t.get('look') or 'bold letters with an outline'}; {len(lines)} line(s):")
            for k, line in enumerate(lines, start=1):
                out.append(f"    LINE {k}: {line}")
            out.append("   " + acc_line)
        out.append("Captions are crisp and readable in a tiny 246x138 preview — not bigger and not smaller than in IMAGE 1. "
                   "A longer word keeps the SAME letter height and takes more width, it never shrinks. Arrows and balloon "
                   "tails point at the same characters as in IMAGE 1 and never cross a letter.")
    else:
        out.append("TEXT: NONE anywhere in the picture: no letters, words, numbers or signs.")
    for t in empty:
        box = t["box"] or [0.0, 0.8, 1.0, 1.0]
        # Живьём (S2 RO): у донора жёлтые буквы лежали прямо на картинке, а генератор по
        # «нарисуй ту же плашку» придумал жёлтую плашку — и донорские жёлтые буквы на ней пропали.
        out.append(f"EMPTY SPOT — at x {pct(box[0], box[2])}, y {pct(box[1], box[3])} a caption will be printed later. "
                   "If the caption of IMAGE 1 sits in a speech balloon or on a plate, draw the same balloon / plate there "
                   "(same shape, place and colour, and AT LEAST as big as in IMAGE 1 — the whole caption goes inside); if it is printed straight over the picture, draw NO plate and NO "
                   "balloon — just a calm, plain part of the background. Either way NO LETTERS AND NO SIGNS in this spot: "
                   "no letters, no numbers, no punctuation, no scribbles imitating letters, no remains of the old caption.")
    out.append("")
    rm = "; ".join(card.get("remove") or [])
    out.append("RULES: every person is a clear ADULT (20+) with an adult face and adult proportions. "
               + str(card.get("adult") or "")
               + " No school uniforms (no blazer + tie + pleated skirt combos, no sailor collars, no bows, no plaid skirts) — "
                 "office, club or university clothes instead. No nudity, no underwear, no sexual act; fan-service no higher "
                 "than in IMAGE 1, and a person who is covered in IMAGE 1 stays covered. "
               + f"REMOVE: {rm + '; ' if rm else ''}no logos, no watermarks, no channel names, no part or episode numbers, "
                 "no Korean, Chinese or Japanese lettering, no borders or frames around the picture. "
                 "OUTPUT: one 16:9 full-bleed YouTube thumbnail, brightness and contrast not lower than IMAGE 1.")
    return "\n".join(out)


def _lang_name(lang) -> str:
    """Имя языка для генератора, как в цикле 5: испанский — латиноамериканский, португальский — бразильский."""
    return {"Spanish": "Spanish (Latin America, es-MX)",
            "Portuguese": "Portuguese (Brazil, pt-BR)"}.get(lang, lang)


# ---------------------------------------------------------------- надпись кодом
def font_for(text: str) -> str:
    """Первый шрифт, в котором есть ВСЕ буквы надписи. Нет такого — понятная ошибка."""
    tried = []
    for path in FONTS:
        if not os.path.exists(path):
            continue
        tried.append(path)
        if has_glyphs(path, text):
            return path
    if not tried:
        raise CoverError("На сервере не найден ни один жирный шрифт для надписи обложки. "
                         "Поставьте fonts-liberation или fonts-dejavu (ищутся: " + ", ".join(FONTS) + ").")
    raise CoverError(f"Ни в одном шрифте сервера нет всех букв надписи «{text}»: проверены "
                     + ", ".join(os.path.basename(p) for p in tried)
                     + ". Поставьте шрифт с нужными буквами или смените язык надписи.")


def has_glyphs(font_path: str, text: str) -> bool:
    """Есть ли в шрифте каждая буква. Сравниваем с отрисовкой заведомо отсутствующего
    символа: «квадратик» вместо Ș даёт ровно ту же картинку, и так он и ловится."""
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype(font_path, 48)
    except OSError:
        return False

    def shot(ch):
        im = Image.new("L", (96, 96), 0)
        ImageDraw.Draw(im).text((10, 10), ch, font=font, fill=255)
        return im.tobytes()

    missing = shot("￾")
    return all(shot(ch) != missing for ch in set(text) if not ch.isspace())


def _layer(lines, font_path, fill, stroke, sw, gap=0.16, bold=BOLD, big=260):
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(font_path, big)
    fat = int(big * bold)
    stroke_px = int(big * sw)
    pad = stroke_px + fat + 40
    sizes = [font.getbbox(ln, stroke_width=stroke_px + fat) for ln in lines]
    sizes = [(b[2] - b[0], b[3] - b[1], b[0], b[1]) for b in sizes]
    step = int(big * (1 + gap))
    W = max(s[0] for s in sizes) + 2 * pad
    H = step * len(lines) + 2 * pad
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for i, ln in enumerate(lines):
        x = (W - sizes[i][0]) // 2 - sizes[i][2]
        y = pad + i * step - sizes[i][3]
        if stroke and stroke_px:
            d.text((x, y), ln, font=font, fill=tuple(stroke) + (255,),
                   stroke_width=stroke_px + fat, stroke_fill=tuple(stroke) + (255,))
        d.text((x, y), ln, font=font, fill=tuple(fill) + (255,),
               stroke_width=fat, stroke_fill=tuple(fill) + (255,))
    return im.crop(im.getbbox())


def _box_px(size, box):
    """Рамка в долях кадра -> пиксели этой картинки."""
    W, H = size
    x0, y0, x1, y1 = box or [0.0, 0.8, 1.0, 1.0]
    return int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H)


def ink_spread(path, box, inset=0.06):
    """Где в рамке box лежат чернила надписи и насколько они видны.

    Одна мерка на ВСЕ надписи — и на те, что рисует код, и на те, что рисует генератор.
    Рамкой модель отдаёт всю панель целиком, буквы занимают её треть; отступ в 6% убирает
    линию самой панели, чтобы она не считалась строкой.

    Возвращает пиксельную рамку букв, высоту всех строк, разницу яркости букв и фона —
    или None, если чернил в рамке нет."""
    from PIL import Image
    import numpy as np
    with Image.open(path) as im:
        W, H = im.size
        x0, y0, x1, y1 = _box_px(im.size, box)
        crop = im.convert("L").crop((x0, y0, x1, y1))
    ix, iy = round(crop.width * inset), round(crop.height * inset)
    if crop.width - 2 * ix < 8 or crop.height - 2 * iy < 8:
        return None
    arr = np.asarray(crop.crop((ix, iy, crop.width - ix, crop.height - iy)), dtype=np.float32)
    mask = np.abs(arr - float(np.median(arr))) > 60
    # Строкой считаем ряд, где чернил не меньше 3%: одиночный столбик рамки, оставшийся
    # после отступа, даёт 1% в КАЖДОМ ряду и растягивает надпись на всю панель.
    # Ряд, закрашенный почти целиком (85%), — это ЛИНИЯ РАМКИ, а не строка букв: между
    # буквами всегда есть просветы. Без этого высотой строки выходила вся панель.
    row_ink, col_ink = mask.mean(axis=1), mask.mean(axis=0)

    def keep(ink):
        """Край рамки — это сплошная линия у самого края куска, а не строка букв."""
        edge = np.zeros(ink.shape, bool)
        band = max(1, int(round(ink.size * 0.15)))
        edge[:band] = edge[-band:] = True
        return (ink >= 0.03) & ~((ink >= 0.85) & edge)

    rows = np.where(keep(row_ink))[0]
    cols = np.where(keep(col_ink))[0]
    if rows.size == 0 or cols.size == 0:
        return None
    delta = (abs(float(np.median(arr[mask])) - float(np.median(arr[~mask])))
             if mask.any() and (~mask).any() else 0.0)
    return {"frame": (W, H),
            "px": [x0 + ix + int(cols.min()), y0 + iy + int(rows.min()),
                   x0 + ix + int(cols.max()) + 1, y0 + iy + int(rows.max()) + 1],
            "h": float(rows.max() - rows.min() + 1), "delta": round(float(delta), 1)}


def line_px(path, box, rows=1) -> Optional[float]:
    """Высота ОДНОЙ строки надписи в пикселях этой картинки."""
    spread = ink_spread(path, box)
    return spread["h"] / max(1, int(rows)) if spread else None


# Старое имя: норму читаемости задаёт донор, но мерка у нашей обложки та же самая.
donor_line_px = line_px


def ink_box(donor_path, box, pad=0.06) -> List[float]:
    """Рамка САМИХ БУКВ донора внутри box.

    Модель отдаёт рамкой всю панель вместе с её чёрной линией и полями, и надпись,
    положенная по этой рамке, вылезала за линию наружу (замер по пикселям: буквы
    1304..1611 при нарисованной панели 1305..1606). Кладём буквы туда, где они у самого
    донора, — тогда они заведомо внутри панели и ровно того же размера."""
    box = _box(box) or [0.0, 0.8, 1.0, 1.0]
    try:
        spread = ink_spread(donor_path, box)
    except (OSError, ValueError):
        return box
    if not spread:
        return box
    W, H = spread["frame"]
    x0, y0, x1, y1 = spread["px"]
    px, py = (x1 - x0) * pad, (y1 - y0) * pad
    out = [max(box[0], (x0 - px) / W), max(box[1], (y0 - py) / H),
           min(box[2], (x1 + px) / W), min(box[3], (y1 + py) / H)]
    return out if out[2] - out[0] > 0.01 and out[3] - out[1] > 0.005 else box


def glyph_delta(im, layer, pos, fill) -> Optional[float]:
    """Насколько буквы отличаются по яркости от того, что лежит ВПЛОТНУЮ к ним.

    Считаем по маске самих букв, а не по цвету пикселей в рамке: чёрная надпись на чёрном
    пиджаке давала «краски в 4 раза больше донорской» и считалась отличной, хотя её не
    видно вовсе. Рядом с буквой лежит либо её обводка, либо фон — и читаемость надписи
    это и есть."""
    from PIL import Image, ImageFilter
    import numpy as np
    arr = np.asarray(layer)
    if arr.ndim != 3 or arr.shape[2] != 4:
        return None
    core = (arr[:, :, 3] > 200) & (np.abs(arr[:, :, :3].astype(np.int16)
                                          - np.array(fill, dtype=np.int16)).sum(axis=2) <= 60)
    if not core.any():
        return None
    k = max(3, int(layer.height * 0.03))
    k = k if k % 2 else k + 1
    grown = np.asarray(Image.fromarray((core * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(min(k, 15)))) > 200
    ring = grown & ~core
    if not ring.any():
        return None
    px, py = pos
    sub = np.asarray(im.crop((px, py, px + layer.width, py + layer.height)).convert("L"), dtype=np.float32)
    return round(float(abs(float(np.median(sub[core])) - float(np.median(sub[ring])))), 1)


def sample_colors(donor_path, box) -> dict:
    """Каким цветом надпись НАПИСАНА У ДОНОРА: замер по пикселям, а не со слов модели.

    В рамке надписи почти всегда три цвета: фон (его больше всего), буквы (дальше всего
    от фона) и обводка (дальше всего от букв). Если «обводка» оказалась почти фоном —
    обводки нет. Так чёрная надпись в белой рамке и остаётся чёрной."""
    from PIL import Image
    import numpy as np
    x0, y0, x1, y1 = box or [0.0, 0.8, 1.0, 1.0]
    with Image.open(donor_path) as im:
        im = im.convert("RGB")
        W, H = im.size
        crop = im.crop((int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H)))
    # Запасной вариант, если в рамке один сплошной цвет: читаемая пара по фону.
    # Жёлтый по умолчанию не берём никогда — на белой рамке он не читается.
    def contrast_pair(source):
        import numpy as np
        luma = float(np.asarray(source.convert("L"), dtype=np.float32).mean())
        return ({"fill": [0, 0, 0], "stroke": [255, 255, 255]} if luma >= 128
                else {"fill": [255, 255, 255], "stroke": [0, 0, 0]})

    if crop.width < 2 or crop.height < 2:
        return {"fill": None, "stroke": None}
    small = crop.quantize(colors=6, method=Image.Quantize.MEDIANCUT).convert("P")
    palette = small.getpalette() or []
    counts = sorted(small.getcolors() or [], key=lambda pair: -pair[0])
    area = max(1, crop.width * crop.height)
    colors = [(n, [palette[3 * i], palette[3 * i + 1], palette[3 * i + 2]]) for n, i in counts]
    if not colors:
        return contrast_pair(crop)

    def far(a, b):
        return float(np.abs(np.array(a, float) - np.array(b, float)).sum())

    bg = colors[0][1]
    rest = [(n, c) for n, c in colors[1:] if n >= 0.02 * area and far(c, bg) >= 60]
    if not rest:
        return contrast_pair(crop)
    fill = max(rest, key=lambda pair: far(pair[1], bg))[1]
    others = [(n, c) for n, c in rest if c != fill]
    stroke = max(others, key=lambda pair: far(pair[1], fill))[1] if others else None
    if stroke is not None and (far(stroke, bg) < 90 or far(stroke, fill) < 90):
        stroke = None      # это тот же фон или тот же цвет букв: обводки у донора нет
    return {"fill": fill, "stroke": stroke}


def paint_from_donor(texts, donor_path) -> List[str]:
    """Дозаполняет цвета надписей замером с донора. Возвращает строки для журнала."""
    notes = []
    for t in texts:
        if t.get("fill"):
            continue
        got = sample_colors(donor_path, t.get("box"))
        if not got.get("fill"):
            continue
        t["fill"] = got["fill"]
        t["stroke"] = got["stroke"]
        t["sw"] = 0.10 if got["stroke"] else 0.0
        notes.append(f"«{t.get('orig') or ''}»: буквы {got['fill']}, обводка "
                     + (str(got["stroke"]) if got["stroke"] else "нет"))
    return notes


def place_box(donor_path, box, rows_donor, lines_ours) -> List[float]:
    """Куда лечь надписи: место БУКВ донора, а не вся панель, которую он отдаёт рамкой.

    Если наш перевод разбит на большее число строк, место растёт — но ровно настолько,
    чтобы СТРОКА осталась такой же высокой, как у донора, и не вышло за саму панель."""
    panel = _box(box) or [0.0, 0.8, 1.0, 1.0]
    place = ink_box(donor_path, panel) if donor_path else panel
    grow = max(1.0, float(lines_ours) / max(1, int(rows_donor or lines_ours)))
    w = min((place[2] - place[0]) * 1.10, (panel[2] - panel[0]) * 0.94)
    h = min((place[3] - place[1]) * grow, (panel[3] - panel[1]) * FILL_H)
    cx, cy = (place[0] + place[2]) / 2, (place[1] + place[3]) / 2
    cx = min(max(cx, panel[0] + w / 2), panel[2] - w / 2)
    cy = min(max(cy, panel[1] + h / 2), panel[3] - h / 2)
    return [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]


def _readable_stroke(base, place_px, fill):
    """Обводка, если буквы сливаются с тем, что под ними.

    Живьём: чёрная надпись легла поверх чёрного пиджака и не читалась, а проверка
    «краски» этого не видела. Цвет букв — донорский, менять его нельзя; читаемость даёт
    обводка."""
    import numpy as np
    sub = np.asarray(base.crop(tuple(place_px)).convert("L"), dtype=np.float32)
    if sub.size == 0:
        return None
    bg = float(np.median(sub))
    luma = 0.299 * fill[0] + 0.587 * fill[1] + 0.114 * fill[2]
    if abs(luma - bg) >= INK_DELTA_MIN:
        return None
    return [255, 255, 255] if luma < 128 else [0, 0, 0]


def _plain(text: str) -> str:
    """Те же буквы без диакритики: Ș -> S, Ă -> A. По ним меряется высота строки."""
    return "".join(ch for ch in unicodedata.normalize("NFD", norm(text)) if not unicodedata.combining(ch))


def _accent_pad(lines, font_path, stroke_w, big=260):
    """Сколько диакритика (шапочка Ă, запятая Ș) добавляет над первой строкой и под последней,
    в пикселях при кегле big. Живьём (S2 RO): блок «BĂTĂUȘ» вписывался целиком, и заглавные
    выходили на четверть мельче донорского «BULLY», хотя мерка блока писала «норма»."""
    from PIL import ImageFont
    font = ImageFont.truetype(font_path, big)
    top = font.getbbox(_plain(lines[0]), stroke_width=stroke_w)[1] - font.getbbox(lines[0], stroke_width=stroke_w)[1]
    bot = font.getbbox(lines[-1], stroke_width=stroke_w)[3] - font.getbbox(_plain(lines[-1]), stroke_width=stroke_w)[3]
    return max(0, top), max(0, bot)


def _rgb_under(im, layer, pos):
    """Средний (медианный) цвет картинки ПОД буквами вместе с обводкой — до того, как они легли."""
    import numpy as np
    alpha = np.asarray(layer)[:, :, 3] > 0
    px, py = pos
    sub = np.asarray(im.crop((px, py, px + layer.width, py + layer.height)).convert("RGB"), dtype=np.float32)
    if not alpha.any() or sub.shape[:2] != alpha.shape:
        return None
    return [float(np.median(sub[:, :, c][alpha])) for c in range(3)]


def _luma_of(rgb) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def readable_fill(fill, stroke, under):
    """Донорский цвет букв, если он виден на новом фоне; иначе — контрастный.

    Живьём (S2 RO): генератор нарисовал в зоне надписи жёлтую плашку, код положил на неё
    донорские жёлтые буквы, и они держались только на обводке. Цвет под буквами меряется
    ПОСЛЕ генерации; сливается — берём из обводки донора, чёрного и белого самый далёкий."""
    if not under:
        return fill
    close = (abs(_luma_of(fill) - _luma_of(under)) < INK_DELTA_MIN
             and sum(abs(a - b) for a, b in zip(fill, under)) < 180)
    if not close:
        return fill
    options = ([list(stroke)] if stroke else []) + [[0, 0, 0], [255, 255, 255]]
    return max(options, key=lambda c: abs(_luma_of(c) - _luma_of(under)))


PLATE_WORDS = re.compile(r"balloon|bubble|plate|\bbox\b|banner|panel|\blabel|\bsign\b|cloud|\boval|bar\b|ribbon|tag\b",
                         re.I)
PLATE_TOL = 70            # сумма |ΔR|+|ΔG|+|ΔB|: пиксель того же цвета, что облако или плашка
PLATE_PAD = 0.035         # воздух между буквами и краем облака, в долях высоты облака
PLATE_MISS = 0.004        # столько пикселей букв вправе лечь на край (сглаживание)


def plate_color(donor_path, box) -> Optional[list]:
    """Цвет облака или плашки донора под надписью — самый частый цвет в её рамке."""
    from PIL import Image
    with Image.open(donor_path) as im:
        crop = im.convert("RGB").crop(_box_px(im.size, box))
    if crop.width < 4 or crop.height < 4:
        return None
    small = crop.quantize(colors=6, method=Image.Quantize.MEDIANCUT)
    palette = small.getpalette() or []
    counts = sorted(small.getcolors() or [], key=lambda pair: -pair[0])
    if not counts:
        return None
    rgb = lambda i: [palette[3 * i], palette[3 * i + 1], palette[3 * i + 2]]
    top = rgb(counts[0][1])
    # оттенки того же цвета (шум JPEG делит белое на три «белых») считаются вместе
    share = sum(n for n, i in counts if sum(abs(a - b) for a, b in zip(rgb(i), top)) <= PLATE_TOL)
    if share < 0.40 * crop.width * crop.height:
        return None               # одного цвета в рамке меньше 40% — надпись лежит прямо на картинке
    return top


def find_plate(im, box, color, holes=False) -> Optional[dict]:
    """Облако или плашка, которую НАРИСОВАЛ ГЕНЕРАТОР рядом с донорским местом надписи.

    Живьём (S2 RO): облако на нашей картинке сдвинуто влево от донорского, а код клал текст в
    донорскую рамку — буква V легла на тёмный пиджак, в ленте читалось «Ă MULȚUMESC».
    Ищем кусок того же цвета, что облако донора, который больше всех перекрывает донорскую
    рамку; окно поиска — рамка и ещё её половина с каждой стороны. holes — залить дырки
    (буквы донора внутри его же облака): облако считается по внешнему контуру."""
    import cv2
    import numpy as np
    W, H = im.size
    x0, y0, x1, y1 = _box_px((W, H), box)
    bw, bh = x1 - x0, y1 - y0
    wx0, wy0 = max(0, x0 - bw // 2), max(0, y0 - bh // 2)
    wx1, wy1 = min(W, x1 + bw // 2), min(H, y1 + bh // 2)
    arr = np.asarray(im.convert("RGB").crop((wx0, wy0, wx1, wy1)), dtype=np.int16)
    mask = (np.abs(arr - np.array(color, np.int16)).sum(axis=2) <= PLATE_TOL).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, labels, stats, _c = cv2.connectedComponentsWithStats(mask, connectivity=4)
    if n <= 1:
        return None
    inner = np.zeros(mask.shape, bool)
    inner[y0 - wy0:y1 - wy0, x0 - wx0:x1 - wx0] = True
    best, best_hit = None, 0
    for lab in range(1, n):
        hit = int(((labels == lab) & inner).sum())
        if hit > best_hit:
            best, best_hit = lab, hit
    if best is None or best_hit < 0.30 * bw * bh:
        return None
    sx, sy, sw_, sh_ = (int(v) for v in stats[best][:4])
    # Облако замкнуто: край окна поиска, который не край кадра, оно не трогает. Иначе это
    # небо или стена того же цвета, а не облако.
    if (sx == 0 and wx0 > 0) or (sy == 0 and wy0 > 0) or (sx + sw_ >= wx1 - wx0 and wx1 < W) \
            or (sy + sh_ >= wy1 - wy0 and wy1 < H):
        return None
    comp = (labels == best).astype(np.uint8)
    if holes:
        # буквы донора внутри облака — дырки; облако считается целиком, без них
        cnts, _h = cv2.findContours(comp, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        comp = np.zeros_like(comp)
        cv2.drawContours(comp, cnts, -1, 1, thickness=-1)
    full = np.zeros((H, W), bool)
    full[wy0:wy1, wx0:wx1] = comp.astype(bool)
    dist = cv2.distanceTransform(comp, cv2.DIST_L2, 5)
    cy, cx = np.unravel_index(int(np.argmax(dist)), dist.shape)
    return {"mask": full, "bbox": [wx0 + sx, wy0 + sy, wx0 + sx + sw_, wy0 + sy + sh_],
            "inner": [wx0 + int(cx), wy0 + int(cy)], "color": list(color)}


def fit_on_plate(layer, k0, plate, core_ratio=1.0):
    """Самый крупный масштаб (не больше k0 — донорской высоты строки) и место, при которых
    КАЖДЫЙ пиксель букв с обводкой и воздухом лежит на облаке. Проверка кодом, а не глазами.
    Возвращает (слой, x, y, k, доля букв вне облака)."""
    import cv2
    import numpy as np
    from PIL import Image
    mask = plate["mask"]
    H, W = mask.shape
    bx0, by0, bx1, by1 = plate["bbox"]
    pad = max(2, int((by1 - by0) * PLATE_PAD))
    grown = cv2.erode(mask.astype(np.uint8), np.ones((2 * pad + 1, 2 * pad + 1), np.uint8)).astype(bool)
    centres = [tuple(plate["inner"]), ((bx0 + bx1) // 2, (by0 + by1) // 2)]
    alpha_big = np.asarray(layer)[:, :, 3] > 40
    best = None
    k = k0
    while k >= k0 * 0.45:
        w, h = max(1, int(layer.width * k)), max(1, int(layer.height * k))
        a = np.asarray(Image.fromarray(alpha_big.astype(np.uint8) * 255).resize((w, h), Image.NEAREST)) > 0
        total = max(1, int(a.sum()))
        for cx, cy in centres:
            for dx, dy in ((0, 0), (-pad, 0), (pad, 0), (0, -pad), (0, pad)):
                px, py = int(cx - w / 2 + dx), int(cy - h / 2 + dy)
                if px < 0 or py < 0 or px + w > W or py + h > H:
                    continue
                miss = float((a & ~grown[py:py + h, px:px + w]).sum()) / total
                if best is None or miss < best[4] - 1e-9:
                    best = (w, h, px, py, miss, k)
                if miss <= PLATE_MISS:
                    return layer.resize((w, h), Image.LANCZOS), px, py, k, round(miss, 4)
        k *= 0.95
    if best is None:
        return None
    w, h, px, py, miss, k = best
    return layer.resize((w, h), Image.LANCZOS), px, py, k, round(miss, 4)


FIT_SHARE = 0.80          # строка кодовой надписи в облаке донора мельче этой доли донорской — просим короче


def small_captions(report) -> List[str]:
    """Надписи кодом, которые на готовом варианте легли мельче FIT_SHARE донорской строки.

    27.09 (аудит F08): прогноз до покупки просит короче один раз и по облаку ДОНОРА, а строку
    в облаке, которое нарисовал генератор, никто не сверял: RO d4cd8cf9 — 8.2 px против
    донорских 11.7 в ленте 246 (0.70), и verdict=ok. Замер уже делает draw_texts, report
    просто терялся."""
    out = []
    for it in (report or {}).get("items") or []:
        ours, donor = it.get("line_h_in_preview_246"), it.get("donor_line_h_in_preview_246")
        if it.get("by_code") and ours and donor and ours < donor * FIT_SHARE:
            out.append(f"надпись «{it.get('text')}» мельче донорской: строка {ours} px против {donor} в ленте")
    return out


def forecast_line(t, lines, donor_path) -> Optional[tuple]:
    """Какой высоты выйдет строка кодовой надписи, если положить её в облако САМОГО ДОНОРА, —
    ДО покупки прохода. (наша строка, донорская) в px ленты 246 или None, если облака нет.

    Живьём (S2 RO): «VĂ MULȚUMESC / TUTUROR, CĂ / VREȚI SĂ-MI / FIȚI PRIETENI.» в том же облаке
    выходила 6 px против донорских 9.7 — в ленте не читалась, как ни клади."""
    from PIL import Image
    if not PLATE_WORDS.search(str(t.get("look") or "")):
        return None
    color = plate_color(donor_path, t["box"])
    if not color:
        return None
    with Image.open(donor_path) as im:
        im = im.convert("RGB")
        W, H = im.size
        donor_line = line_px(donor_path, t["box"], len(t.get("rows") or []) or len(lines))
        plate = find_plate(im, t["box"], color, holes=True)
    if not plate or not donor_line:
        return None
    path = t.get("font") or font_for(" ".join(lines))
    bold = BOLD_COMMA if any(ch in COMMA_BELOW for ch in norm(" ".join(lines))) else BOLD
    stroke, sw = t.get("stroke"), t.get("sw", 0.0)
    big = _layer(lines, path, [0, 0, 0], stroke, sw, bold=bold)
    top, bot = _accent_pad(lines, path, (int(260 * sw) if stroke else 0) + int(260 * bold), 260)
    core_big = max(1, big.height - top - bot) / max(1, len(lines))
    fitted = fit_on_plate(big, donor_line / core_big, plate)
    if not fitted:
        return None
    k = fitted[3]
    to_feed = PREVIEW_SIZE[0] / W
    return round(core_big * k * to_feed, 1), round(donor_line * to_feed, 1)


SHORTEN_PROMPT = """You are a native {lang} copywriter of a manhwa recap YouTube channel for {lang}-speaking teenagers.
This caption of our thumbnail does not fit its speech balloon: the letters come out too small to read in the feed.

Original English: "{orig}"
Our {lang} caption now: {now}

Write a SHORTER {lang} caption with the SAME meaning and hook: EVERY line at most {chars} characters (spaces
count), at most {rows} lines, the fewest and shortest words a {lang} teenager would really use. No English words. CAPITALS stay
capitals; correct {lang} letters with all their accent marks.

Answer with ONE JSON object and nothing else:
{{"lines": ["LINE 1", "LINE 2"]}}
"""


def shorten_for_plate(t, iso, lang, donor_path, call) -> Optional[str]:
    """Надпись, которую рисует код, не влезает в облако донора крупно — один раз просим короче.
    Меняет t["lines"][iso] и возвращает строку для журнала, если стало лучше."""
    lines = (t.get("lines") or {}).get(iso) or []
    got = forecast_line(t, lines, donor_path) if lines else None
    if not got or got[0] >= got[1] * FIT_SHARE:
        return None
    # ширина строки при той же высоте букв растёт с числом букв: столько букв влезет крупно
    longest = max(len(x) for x in lines)
    chars = max(5, int(longest * got[0] / (got[1] * (FIT_SHARE + 0.05))))
    prompt = SHORTEN_PROMPT.format(lang=_lang_name(lang), orig=t["orig"], now=" / ".join(lines),
                                   chars=chars, rows=max(1, len(t.get("rows") or []) or len(lines)))
    try:
        data = call(prompt)
    except CoverError:
        raise
    except _STOP:
        raise
    except Exception as exc:
        return f"«{' / '.join(lines)}» мельче донорской в облаке ({got[0]} px против {got[1]}), короче не вышло ({type(exc).__name__})"
    new = [str(x).strip() for x in ((data or {}).get("lines") or []) if str(x).strip()] if isinstance(data, dict) else []
    if not new or english_words(new, iso):
        return f"«{' / '.join(lines)}» мельче донорской в облаке ({got[0]} px против {got[1]}), короче не вышло"
    try:
        font_for(" ".join(new))
    except CoverError:
        return None
    after = forecast_line(t, new, donor_path)
    if not after or after[0] <= got[0]:
        return f"«{' / '.join(lines)}» мельче донорской в облаке ({got[0]} px против {got[1]}), короче не вышло"
    t["lines"][iso] = new
    return (f"надпись для облака сокращена: «{' / '.join(lines)}» -> «{' / '.join(new)}» "
            f"(строка в ленте {got[0]} -> {after[0]} px, у донора {got[1]})")


def draw_texts(base_path, out_path, texts, iso, donor_path=None) -> dict:
    """Кладёт надписи кодом на base_path и пишет out_path. Возвращает замеры."""
    from PIL import Image
    im = Image.open(base_path).convert("RGB")
    W, H = im.size
    donor_w = donor_h = None
    if donor_path:
        with Image.open(donor_path) as d_im:
            donor_w, donor_h = d_im.size
    report = {"base": str(base_path), "items": []}
    for t in texts:
        lines = t["lines"][iso]
        joined = " ".join(lines)
        path = t.get("font") or font_for(joined)
        # Место надписи — это место БУКВ донора, а не вся панель, которую модель отдаёт
        # рамкой: иначе буквы вылезают за нарисованную линию панели наружу.
        place = place_box(donor_path, t["box"], len([r for r in (t.get("rows") or []) if r]), len(lines))
        x0, y0, x1, y1 = place
        bw, bh = (x1 - x0) * W, (y1 - y0) * H
        bold = BOLD_COMMA if any(ch in COMMA_BELOW for ch in norm(joined)) else BOLD

        def render(fill, stroke, sw):
            """Слой букв той же высоты ЗАГЛАВНЫХ, что у донора: диакритика торчит сверх неё."""
            layer = _layer(lines, path, fill, stroke, sw, bold=bold)
            big = 260
            top, bot = _accent_pad(lines, path, (int(big * sw) if stroke else 0) + int(big * bold), big)
            core = max(1, layer.height - top - bot)
            max_w = min(bw * WIDEN, 2 * min(x0 * W + bw / 2, W - (x0 * W + bw / 2)) - 4)
            k = min(FILL_H * bh / core, max_w / layer.width, (H - 4) / layer.height)
            layer = layer.resize((max(1, int(layer.width * k)), max(1, int(layer.height * k))), Image.LANCZOS)
            px = int(x0 * W + (bw - layer.width) / 2)
            py = int(y0 * H + (bh - core * k) / 2 - top * k)
            py = min(max(py, 0), H - layer.height)     # шапочка Ă у верхнего края не уходит за кадр
            return layer, px, py, core * k

        fill = t.get("fill") or DEFAULT_FILL
        stroke, sw = t.get("stroke"), t.get("sw", 0.0)
        # Облако или плашка, которую нарисовал генератор: надпись ложится В НЕЁ, а не в
        # донорскую рамку (генератор вправе сдвинуть облако вместе с новой камерой).
        plate, plate_miss = None, None
        color = plate_color(donor_path, t["box"]) if donor_path and PLATE_WORDS.search(str(t.get("look") or "")) else None
        if color:
            plate = find_plate(im, t["box"], color)
            if not plate:
                log(f"надпись «{joined}»: облака цвета {color} рядом с донорским местом нет — кладу по донорской рамке")
        layer, px, py, core_h = render(fill, stroke, sw)
        recolored = None
        under = plate["color"] if plate else _rgb_under(im, layer, (px, py))
        better = readable_fill(fill, stroke, under)
        if better != list(fill):
            recolored = list(fill)
            fill, stroke, sw = better, None, 0.0
            log(f"надпись «{joined}»: донорский цвет {recolored} сливается с тем, что нарисовал генератор, "
                f"— буквы {better}")
        if not stroke and not plate:
            auto = _readable_stroke(im, _box_px((W, H), place), fill)
            if auto:
                stroke, sw = auto, 0.10
        if recolored or stroke != t.get("stroke"):
            # Карточку не меняем: следующий проход — другой фон, и донорский цвет там может подойти.
            layer, px, py, core_h = render(fill, stroke, sw)
        if plate:
            big_layer = _layer(lines, path, fill, stroke, sw, bold=bold)
            k0 = layer.width / max(1, big_layer.width)
            # Потолок — высота ЗАГЛАВНЫХ донора, а не ширина донорской рамки: облако генератора
            # бывает шире, и буквы вправе дорасти до донорских (живьём 6.0 px в ленте против 9.7).
            donor_line0 = line_px(donor_path, t["box"], len(t.get("rows") or []) or len(lines))
            if donor_line0 and donor_h:
                top, bot = _accent_pad(lines, path, (int(260 * sw) if stroke else 0) + int(260 * bold), 260)
                core_big = max(1, big_layer.height - top - bot) / max(1, len(lines))
                k_cap = donor_line0 * H / donor_h / core_big
            else:
                k_cap = k0
            fitted = fit_on_plate(big_layer, max(k0, k_cap), plate)
            if fitted:
                layer, px, py, k, plate_miss = fitted
                core_h = core_h * k / k0
                log(f"надпись «{joined}» положена в облако, которое нарисовал генератор "
                    f"(масштаб {k / k0:.2f} от донорского, вне облака {plate_miss:.1%} букв)")
        im.paste(layer, (px, py), layer)
        donor_line = line_px(donor_path, t["box"], len(t.get("rows") or []) or len(lines)) if donor_path else None
        report["items"].append({
            "text": joined, "font": os.path.basename(path), "by_code": True, "rows": len(lines),
            "box": list(t["box"] or [0.0, 0.8, 1.0, 1.0]), "place": [round(v, 4) for v in place],
            "block_px": [layer.width, layer.height], "box_px": [round(bw), round(bh)],
            "fill_of_box_h": round(core_h / bh, 2) if bh else None,
            # строка — по заглавным без диакритики (с обводкой, как чернила донора)
            "line_h_in_preview_246": round(core_h / max(1, len(lines)) * PREVIEW_SIZE[0] / W, 1),
            # столько же места было у самой донорской надписи: мельче донора мы не делаем
            "box_h_in_preview_246": round(bh * PREVIEW_SIZE[1] / H, 1),
            "donor_line_h_in_preview_246": (round(donor_line * PREVIEW_SIZE[0] / donor_w, 1)
                                            if donor_line and donor_w else None),
            "inside_frame": bool(px >= 0 and py >= 0 and px + layer.width <= W and py + layer.height <= H),
            # влезла ли надпись в СВОЁ место (рамку букв донора), а не просто в кадр:
            # именно из-за этого буквы вылезали за чёрную рамку панели наружу
            # в облаке генератора «влезла ли» решает замер букв по маске облака (off_plate)
            "inside_box": bool(layer.width <= bw + 1 and core_h <= bh + 1) if plate_miss is None else True,
            "ink_delta": glyph_delta(im, layer, (px, py), fill),
            "fill": list(fill), "stroke": list(stroke) if stroke else None,
            "recolored_from": recolored,
            # облако генератора: где оно и сколько букв легло мимо него (замер кодом по маске)
            "plate": plate["bbox"] if plate else None,
            "off_plate": plate_miss,
        })
    tmp = str(out_path) + ".tmp.png"
    im.save(tmp)
    os.replace(tmp, str(out_path))
    report["out"] = str(out_path)
    return report


# ---------------------------------------------------------------- автопроверка кодом
def _luma(path, size=(160, 90)):
    from PIL import Image
    import numpy as np
    with Image.open(path) as im:
        return np.asarray(im.convert("L").resize(size, Image.LANCZOS), dtype=np.float32)


def ssim(path_a, path_b, size=(256, 144)) -> float:
    """Похожесть по строению кадра (SSIM, серое 256×144, окно 7). Грубая мера «не копия»."""
    import numpy as np
    a, b = _luma(path_a, size), _luma(path_b, size)

    def blur(x, k=7):
        pad = k // 2
        p = np.pad(x, pad, mode="reflect")
        c = np.cumsum(np.cumsum(p, axis=0), axis=1)
        c = np.pad(c, ((1, 0), (1, 0)))
        return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)

    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    ma, mb = blur(a), blur(b)
    va, vb, cov = blur(a * a) - ma * ma, blur(b * b) - mb * mb, blur(a * b) - ma * mb
    s = ((2 * ma * mb + c1) * (2 * cov + c2)) / ((ma * ma + mb * mb + c1) * (va + vb + c2))
    return round(float(s.mean()), 3)


def dhash_distance(path_a, path_b) -> int:
    """Сколько битов из 256 различаются в разностном хеше 16×16."""
    a, b = _luma(path_a, (17, 16)), _luma(path_b, (17, 16))
    return int(((a[:, 1:] > a[:, :-1]) != (b[:, 1:] > b[:, :-1])).sum())


def english_problems(texts, iso) -> List[dict]:
    """Английское слово в надписи — замечание владельцу («текст не адаптирован под язык ЦА»)."""
    out = []
    for text in texts or []:
        words = english_words([text], iso)
        if words:
            out.append({"code": "english", "note_ru": f"в надписи «{text}» осталось английское: {', '.join(words)}"})
    return out


def make_grid(path, out_path) -> str:
    """Копия картинки с сеткой 10×10 и подписями долей кадра — по ней модель меряет крупность
    (так судьи цикла 7 и мерили, и расхождения видели)."""
    from PIL import Image, ImageDraw
    with Image.open(path) as im:
        im = im.convert("RGB")
        im = im.resize((1280, max(1, round(1280 * im.height / im.width))), Image.LANCZOS)
    d = ImageDraw.Draw(im)
    W, H = im.size
    font = _label_font(18)
    for k in range(1, 10):
        x, y = round(W * k / 10), round(H * k / 10)
        for xy in (((x, 0), (x, H)), ((0, y), (W, y))):
            d.line(xy, fill=(0, 0, 0), width=3)
            d.line(xy, fill=(255, 0, 255), width=1)
        for pos, text in (((x + 3, 2), f"{k / 10:.1f}"), ((3, y + 2), f"{k / 10:.1f}")):
            d.text(pos, text, fill=(255, 0, 255), font=font, stroke_width=2, stroke_fill=(0, 0, 0))
    tmp = str(out_path) + ".tmp.jpg"
    im.save(tmp, quality=90)
    os.replace(tmp, str(out_path))
    return str(out_path)


def _squash(text: str) -> str:
    """Буквы подряд, без пробелов и переносов: только так сравнение не зависит от того,
    как модель отформатировала JSON."""
    return re.sub(r"\s+", "", norm(text))


def text_faults(verdict, expected: List[str], by_code: List[str] = ()) -> List[dict]:
    """Сверяет ВСЁ прочитанное с набором надписей, буква в букву, в обе стороны.

    Модель возвращает многострочную надпись то одной строкой, то отдельными элементами
    массива, то с \\n внутри, поэтому пробелы и переносы не считаются, а подпись может
    собраться из 2-3 кусков. Но каждая заказанная надпись должна найтись целиком, и после
    вычёркивания всех надписей не должно остаться НИ ОДНОГО символа: повтор слова
    («VREAU SĂ TE VĂD! VĂD!»), кавычки, чужая вывеска — такой же брак, как пропущенная буква
    (живьём c8/critic1: «входит в прочитанное» пропускало оба случая).

    by_code — надписи, которые нарисовал код (румынские Ș/Ț): буквы в них верные заведомо,
    поэтому они вычёркиваются и по близкому прочтению, а браком не бывают."""
    raw = list(verdict.get("texts") or [])
    boxes_in = list(verdict.get("boxes") or [])
    keep = [i for i, x in enumerate(raw) if str(x).strip()]
    read = [re.sub(r"\s+", " ", norm(raw[i])).strip() for i in keep]
    boxes = [_box_of(boxes_in[i]) if i < len(boxes_in) else None for i in keep]
    pool = [_squash(x) for x in read]
    faults = []

    def take(target) -> bool:
        for i, block in enumerate(pool):
            if target in block:
                pool[i] = block.replace(target, "", 1)
                return True
        live = [i for i, b in enumerate(pool) if b]
        for size in (2, 3):
            for combo in itertools.permutations(live, size):
                if "".join(pool[i] for i in combo) == target:
                    for i in combo:
                        pool[i] = ""
                    return True
        return False

    # Сначала вычёркиваем всё, что прочитано верно, и только потом ищем ближайшее к ошибочным:
    # иначе кусок чужой (верной) надписи мог бы уйти в «прочитано» ошибочной.
    missed = [w for w in list(expected) + list(by_code) if _squash(w) and not take(_squash(w))]
    for want in missed:
        target = _squash(want)
        near_i = min((i for i, b in enumerate(pool) if b), key=lambda i: _distance(target, pool[i]), default=None)
        if want in by_code:
            if near_i is not None and _distance(target, pool[near_i]) <= max(2, len(target) // 6):
                pool[near_i] = ""
            continue
        near = read[near_i] if near_i is not None else ""
        if near_i is not None:
            pool[near_i] = ""               # ошибочное прочтение этой надписи — не «лишний текст»
        faults.append({"want": want, "read": near,
                       "letters": sorted(set(norm(want)) - set(near)) if near else sorted(set(norm(want)))})
    for i, block in enumerate(pool):
        if block:
            f = {"want": "", "read": block, "extra": True, "letters": []}
            box = boxes[i]
            if box and (box[2] - box[0]) * (box[3] - box[1]) < SMALL_BLOCK:
                # Мелкая вывеска фона (OPEN, номер дома): openai-image рисует их на ночных улицах
                # часто, новый круг из-за неё — 18 единиц. Не брак, а «посмотрите глазами».
                f["soft"] = f["small"] = True
            faults.append(f)
    faults += spacing_faults(read, list(expected) + list(by_code),
                             {f["want"] for f in faults if f.get("want")})
    return faults


SMALL_BLOCK = 0.015       # доля кадра: лишняя надпись меньше — мелкий текст фона, не брак


def spacing_faults(read: List[str], expected: List[str], skip=()) -> List[dict]:
    """Слова склеены или разорваны: сверка без пробелов этого не видит. Живьём (critic р3,
    FOKHwQSCGTk): поверх «MIA» тёмное пятно, чтец прочитал «LAMIA EX» вместо «LA MIA EX».
    Такой сигнал — «посмотрите глазами», а не брак: пробел мог просто потеряться при чтении."""
    tokens, glued, owner = set(), {}, {}
    for e in expected:
        if e in skip:
            continue
        toks = [_squash(t) for t in norm(e).split() if _squash(t)]
        for t in toks:
            tokens.add(t)
            owner.setdefault(t, e)
        for i in range(len(toks)):
            for j in range(i + 2, len(toks) + 1):
                glued.setdefault("".join(toks[i:j]), e)
    out, seen = [], set()
    for block in read:
        rt = [_squash(t) for t in block.split() if _squash(t)]
        for t in rt:
            if t not in tokens and t in glued and glued[t] not in seen:
                seen.add(glued[t])
                out.append({"want": glued[t], "read": block, "spacing": True, "soft": True, "letters": []})
        for a, b in zip(rt, rt[1:]):
            if a + b in tokens and (a not in tokens or b not in tokens) and owner[a + b] not in seen:
                seen.add(owner[a + b])
                out.append({"want": owner[a + b], "read": block, "spacing": True, "soft": True, "letters": []})
    return out


def _distance(a, b) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# ---------------------------------------------------------------- генерация
def refused_before(path) -> int:
    """Сколько раз генератор уже ОТКАЗАЛ на этом файле (квитанция, статус failed).

    Квитанция не даёт отправить тот же запрос второй раз: попытка с тем же телом, что
    уже провалилась, отдаёт старый отказ бесплатно и нового запроса не делает. Это
    правильно для кадров, но у обложки промпт и опорные картинки при повторе те же, и
    «Повторить незавершённое» воспроизводило отказ вечно (живьём 2 прогона из 6)."""
    receipt = read_json(str(path) + ".generation.json", {}) or {}
    return sum(1 for a in receipt.get("attempts") or [] if isinstance(a, dict) and a.get("status") == "failed")


def retry_prompt(prompt, refused) -> str:
    """Тот же промпт для новой попытки после отказа.

    Зерно openai-image не принимает (422), поэтому новый запрос отличается строкой в
    конце. Строка ничего не придумывает заново: она повторяет ограничения, которые в
    промпте уже есть, — и номер попытки, чтобы тело запроса было новым всегда."""
    if not refused:
        return prompt
    return (prompt + f"\n\nATTEMPT {refused + 1}. Everybody in the picture is a clearly adult 20+ and dressed "
            "no more revealingly than in IMAGE 1; the picture is a non-sexual illustration for a story recap.")


def generate_one(path, prompt, refs, generate=None) -> dict:
    """Одна покупка у FastGen. Квитанция лежит рядом с файлом: повторный запуск не купит снова.

    Отказ генератора — исключение: следующая попытка (в этом же прогоне до TRIES раз
    или по кнопке «Повторить незавершённое») идёт НОВЫМ запросом, см. retry_prompt."""
    generate = generate or IM.generate_many
    ref_list = [IM.Ref(name=name, data_uri=IM.data_uri(src)) for name, src in refs[:IM.MAX_REFS]]

    def job_for():
        return {"key": os.path.basename(str(path)), "path": str(path),
                "prompt": retry_prompt(prompt, refused_before(path)), "refs": ref_list,
                "model": MODEL, "options": dict(OPTIONS), "aspect": ASPECT, "tries": 1}

    result = {}
    for _attempt in range(TRIES):
        before = refused_before(path)
        result = (generate([job_for()], threads=1) or [None])[0] or {}
        if result.get("status") == "ошибка" and "другому запросу" in str(result.get("error") or ""):
            # Рядом лежит картинка прошлого, оборвавшегося прогона, а задание с тех пор
            # изменилось (прямоугольник ремонта каждый раз новый). Без этого шаг падал этой
            # ошибкой ВЕЧНО, и ролик нельзя было довести никаким «Повторить незавершённое».
            log("черновик прошлого прогона не подходит под новое задание — откладываю его и покупаю заново")
            try:
                Path(str(path)).unlink()
            except OSError:
                pass
            # Квитанцию НЕ удаляем, а откладываем под другим именем: это уже оплаченные
            # единицы, и units_spent (glob cover*.generation.json) должна их видеть.
            receipt = Path(str(path) + ".generation.json")
            if receipt.is_file():
                k = 1
                while Path(f"{path}.stale-{k}.generation.json").exists():
                    k += 1
                os.replace(receipt, f"{path}.stale-{k}.generation.json")
            result = (generate([job_for()], threads=1) or [None])[0] or {}
        if result.get("status") in ("готово", "уже есть"):
            break
        if refused_before(path) <= before:
            break          # не отказ генератора (квота, сеть, неясный ответ): вторая покупка его не лечит
        log("генератор отказал — пробую ещё раз новым запросом")
    if result.get("status") not in ("готово", "уже есть"):
        raise CoverError("Генератор не сделал обложку: " + _why(result.get("error"), path))
    if result.get("status") == "готово":
        try:
            IM.clean_watermark(str(path))
        except _STOP:
            raise
        except Exception as exc:   # звёздочка бывает только у nano-banana; отсутствие чистки шаг не валит
            log(f"звёздочку генератора счистить не удалось ({type(exc).__name__}: {str(exc)[:120]}) — "
                "проверьте углы обложки глазами")
    return result


def _why(error, path=None) -> str:
    """Короткая русская причина вместо английского ответа провайдера.

    Полный ответ провайдера кладётся рядом с картинкой файлом: владельцу на странице
    ролика нужна фраза, а не JSON с идентификаторами запроса."""
    text = str(error or "").strip()
    if not text:
        return "причина не названа"
    if path:
        try:
            Path(str(path) + ".error.txt").write_text(text, encoding="utf-8")
        except OSError:
            pass
    low = text.lower()
    for marks, ru in (
            (("moderation", "safety", "policy", "content_filter", "rejected"),
             "генератор отказался рисовать этот кусок: сработала его собственная проверка содержания"),
            (("timeout", "timed out", "deadline"), "генератор не ответил вовремя"),
            (("quota", "insufficient", "429", "rate limit", "rate_limit"),
             "у подписки кончилась квота или превышен предел запросов в минуту"),
            (("unauthorized", "401", "403", "forbidden"), "генератор не принял ключ доступа"),
            (("другому запросу",), "рядом лежит картинка от другого запроса"),
            (("failed", "error", "500", "502", "503"), "генератор ответил отказом и картинку не отдал")):
        if any(mark in low for mark in marks):
            return ru + (f" (полный ответ: {os.path.basename(str(path))}.error.txt)" if path else "")
    return "генератор не отдал картинку" + (f" (полный ответ: {os.path.basename(str(path))}.error.txt)" if path else "")


def units_spent(paths) -> Optional[int]:
    """Единицы квоты по КВИТАНЦИЯМ, а не по картинкам.

    Неудачная покупка оставляет квитанцию без картинки: считая по .png, студия
    показывала владельцу половину настоящей траты. Квитанции оборвавшегося прогона —
    такие же деньги."""
    total, known = 0, False
    for path in paths:
        receipt = read_json(str(path), {}) or {}
        for attempt in receipt.get("attempts", []):
            if attempt.get("status") == "failed":
                # Отказ генератора счётчик FastGen не списывает: живьём заметка писала
                # 32 единицы при настоящих 24 (GET /api/v6/usage 36 -> 60).
                known = known or bool(attempt.get("id"))
                continue
            if attempt.get("id") and type(attempt.get("quota_units")) in (int, float):
                total += attempt["quota_units"]
                known = True
    return total if known else None


def units_refused(paths) -> int:
    """Сколько отказов генератора получили номер задания FastGen (могли быть списаны)."""
    n = 0
    for path in paths:
        receipt = read_json(str(path), {}) or {}
        n += sum(1 for a in receipt.get("attempts", []) if a.get("status") == "failed" and a.get("id"))
    return n


def make_preview(cover_path, out_path) -> str:
    from PIL import Image
    with Image.open(cover_path) as im:
        im.convert("RGB").resize(PREVIEW_SIZE, Image.LANCZOS).save(str(out_path), quality=88)
    return str(out_path)


def _label_font(size=19):
    """Шрифт подписей листа. Встроенный шрифт PIL кириллицу рисует квадратами."""
    from PIL import ImageFont
    for path in (FONTS + ("/System/Library/Fonts/Supplemental/Arial.ttf",
                          "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")):
        if os.path.exists(path) and has_glyphs(path, "ОРИГИНАЛЬНАЯ обложка"):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def fit_cover(path) -> bool:
    """Обложка ровно 1672×941: провайдер иногда отдаёт соседний размер.

    Возвращает True, только если картинку пришлось тянуть ЗАМЕТНО (больше 2% по любой
    стороне): разница в один пиксель пугала владельца заметкой про растяжение рядом со
    словом «готова», хотя глазами такого растяжения нет."""
    from PIL import Image
    with Image.open(path) as im:
        if im.size == COVER_SIZE:
            return False
        notable = max(abs(im.width - COVER_SIZE[0]) / COVER_SIZE[0],
                      abs(im.height - COVER_SIZE[1]) / COVER_SIZE[1]) > 0.02
        im.convert("RGB").resize(COVER_SIZE, Image.LANCZOS).save(str(path) + ".tmp.png")
    os.replace(str(path) + ".tmp.png", str(path))
    return notable



# ---------------------------------------------------------------- лист для глаз
def _fit(path, width):
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        return im.resize((width, max(1, round(width * im.height / im.width))), Image.LANCZOS)


def make_sheet(donor_path, variants, chosen_path, out_path, texts=()) -> str:
    """Лист для глаз: «конкурент | вариант 1 | 2 | 3» мелко с оценками, ниже выбранная
    крупно и как она выглядит в ленте 246×138.

    variants — [(подпись, путь или None)]: у несостоявшегося варианта пустая клетка."""
    from PIL import Image, ImageDraw
    cell, gap, pad, big = 420, 14, 28, 1300
    label = _label_font(18)
    row = [("КОНКУРЕНТ", _fit(donor_path, cell))]
    for title, path in variants:
        row.append((title, _fit(path, cell) if path and Path(path).is_file()
                    else Image.new("RGB", (cell, round(cell * 9 / 16)), (200, 200, 205))))
    chosen = _fit(chosen_path, big)
    with Image.open(chosen_path) as im:
        feed = im.convert("RGB").resize(PREVIEW_SIZE, Image.LANCZOS)
    W = max(pad * 2 + sum(im.width for _t, im in row) + gap * (len(row) - 1), pad * 2 + big + gap + PREVIEW_SIZE[0])
    h_row = max(im.height for _t, im in row)
    H = pad + 26 + h_row + gap + 26 + chosen.height + pad + (30 if texts else 0)
    sheet = Image.new("RGB", (W, H), (245, 245, 247))
    d = ImageDraw.Draw(sheet)
    x, y = pad, pad
    for title, im in row:
        d.text((x, y), title[:44], fill=(20, 20, 20), font=label)
        sheet.paste(im, (x, y + 26))
        x += im.width + gap
    y += 26 + h_row + gap
    d.text((pad, y), "ВЫБРАНА (после чистки)", fill=(20, 20, 20), font=label)
    sheet.paste(chosen, (pad, y + 26))
    d.text((pad + big + gap, y), "в ленте 246×138", fill=(20, 20, 20), font=label)
    sheet.paste(feed, (pad + big + gap, y + 26))
    if texts:
        d.text((pad, H - 30), "Ожидается: " + " | ".join(texts), fill=(20, 20, 20), font=label)
    tmp = str(out_path) + ".tmp.jpg"
    sheet.save(tmp, quality=88)
    os.replace(tmp, str(out_path))
    return str(out_path)


# ---------------------------------------------------------------- чистка шума
def _nlmeans_file(src, dst):
    import cv2
    im = cv2.imread(str(src), cv2.IMREAD_COLOR)
    if im is None:
        raise OSError(f"не читается {src}")
    out = cv2.fastNlMeansDenoisingColored(im, None, 3, 3, 7, 21)
    tmp = str(dst) + ".tmp.png"
    if not cv2.imwrite(tmp, out):
        raise OSError(f"не записан {tmp}")
    os.replace(tmp, str(dst))


def clean_cover(src, dst, env=None) -> dict:
    """Почистить выбранную обложку от ИИ-зерна; src не трогается (это «оригинал»).

    Замер 19.09 (tasks/anime_clean): Real-ESRGAN аниме на обложках — 7,0 против 5,0 «как
    есть». esrgan — через модуль чистки кадров (denoise, выпуск 4): машина с видеокартой по
    ssh, при её недоступности он сам сглаживает на месте. Модуля нет или сбой — сглаживание
    h=3 здесь же. Сбой и его — обложка идёт как есть. Возвращает {method, note}."""
    env = os.environ if env is None else env
    mode = clean_mode(env)
    if mode == "off":
        shutil.copyfile(src, str(dst) + ".tmp.png")
        os.replace(str(dst) + ".tmp.png", str(dst))
        return {"method": "off", "note": ""}
    if mode == "esrgan":
        try:
            import denoise as DN
            cfg = DN.settings({**env, "MANGA_CLEAN": "esrgan"})
            work = Path(str(dst) + ".clean")
            item_dst = str(work / Path(str(dst)).name)
            res = DN.clean_many([dict(src=str(src), input=str(src), dst=item_dst)], cfg, log=log)
            if (res.get("esrgan") or res.get("nlmeans")) and os.path.isfile(item_dst):
                os.replace(item_dst, str(dst))
                shutil.rmtree(work, ignore_errors=True)
                method = "esrgan" if res.get("esrgan") else "nlmeans"
                return {"method": method, "note": "" if method == "esrgan" else
                        f"{cfg.get('label') or 'машина с видеокартой'} недоступна — обложка сглажена на сервере"}
            shutil.rmtree(work, ignore_errors=True)
            log("Real-ESRGAN не отдал обложку — сглаживаю на месте")
        except _STOP:
            raise
        except Exception as exc:   # noqa: BLE001 — чистка довесок: без неё обложка остаётся годной
            log(f"Real-ESRGAN недоступен ({type(exc).__name__}: {str(exc)[:160]}) — сглаживаю на месте")
    try:
        _nlmeans_file(src, dst)
        return {"method": "nlmeans", "note": "" if mode == "nlmeans" else
                "Real-ESRGAN недоступен — обложка сглажена на сервере"}
    except _STOP:
        raise
    except Exception as exc:   # noqa: BLE001
        log(f"чистка не удалась ({type(exc).__name__}: {str(exc)[:160]}) — обложка идёт как есть")
        shutil.copyfile(src, str(dst) + ".tmp.png")
        os.replace(str(dst) + ".tmp.png", str(dst))
        return {"method": "off", "note": "чистка от зерна не удалась — обложка как есть"}


# ---------------------------------------------------------------- выбирающий
JUDGE_PROMPT = """You pick the best of {count} YouTube thumbnails for our manhwa recap channel.

IMAGE 1 is the COMPETITOR's thumbnail (file {donor_name}). {variant_list}
Our viewers are {lang}-speaking teenagers; the video is in {lang}.

What a good thumbnail of ours is (the owner's checklist):
1. NOT A COPY of IMAGE 1: a new drawing — another camera, new poses, a new background, our characters. Code measured the
   similarity to IMAGE 1 (SSIM, above {ssim_stop} means a copy; dHash bits changed, below {dhash_stop} means a copy):
{similarity}
   Among good variants prefer the one whose camera, poses and background are the MOST different from IMAGE 1: the
   same room, the same people in the same places with the same props is a copy of the layout, not a new drawing.
2. THE SAME IDEA AND TRIGGERS as IMAGE 1: {situation} The hook: {hook}
3. THE KEY CHARACTERS AND OBJECTS ARE AS BIG AS IN IMAGE 1 — faces as large in the frame, not zoomed out.
4. OUR HEROES look like this: {heroes}
   Check build, age look, posture and marks (scars, glasses, a nasal cannula...) too, not only the face and hair.
5. THE TEXT: {text_rule}
6. Everybody is clearly an adult (20+). NO school uniform (blazer + tie + pleated or plaid skirt, sailor collar).
7. Fan-service is NOT higher than in IMAGE 1 (no deeper neckline, no bare shoulders or cleavage where IMAGE 1 has
   none, no more skin).
8. It reads in a tiny 246x138 feed preview: big clear faces and emotions, big readable letters. Nothing lies over
   the letters: no dark smudge, stain, hair, hand or object across a caption.
9. No logos, watermarks, channel names, part/episode numbers, no English words.

For EVERY variant (the letters are checked separately by code, you judge the rest):
- "fixes": captions a native {lang} teenager would write differently (spelling, accent marks, grammar):
  [{{"drawn": "<exactly as drawn>", "native": "<correct>"}}]; [] if every caption is correct {lang}.
- "school_uniform", "logo", "underage", "more_revealing": true only if you clearly see it.
- "text_damaged": true if a smudge, stain, shadow, hair or object lies over any letter of a caption.
- "not_our_hero": true if one of OUR HEROES clearly lacks the traits of point 4 (another build, age look or marks).
- "score": 0-10 against the checklist; "why": one short sentence.
Then "best": the number of the best variant, "why_ru": one sentence IN RUSSIAN why it is the best,
"redo_reason": one English sentence for the illustrator what to fix if all variants had to be drawn again.

Answer with ONE JSON object and nothing else:
{{"variants": [{{"n": 1, "fixes": [], "school_uniform": false, "logo": false, "underage": false,
"more_revealing": false, "text_damaged": false, "not_our_hero": false, "score": 7, "why": "..."}}], "best": 1, "why_ru": "...", "redo_reason": ""}}
"""

# Чтение надписей — ОТДЕЛЬНЫМ вызовом, без заказанного текста и без чек-листа. Живьём 19.09
# (р2, c8 TAvIqS9VzK0): выбирающий с идеей кадра в промпте «читал» «БРОСЬ ЕЁ!» там, где
# нарисовано «БРОСЬ Ё!», на всех трёх; этот же промпт отдельно — прочитал верно.
READ_PROMPT = """You are a proofreading scanner. You see {count} images. You do NOT know what any of them is supposed to say.

For EVERY image transcribe ALL writing drawn on it: captions, speech bubbles, signs, numbers, sound effects, logos,
tiny text in the background. One string per separate block of text, top to bottom, left to right.
Copy the drawn glyphs character by character, exactly as drawn:
- every letter, accent mark (Ă Â Î Ș Ț Ą Ę Ł Ó Ś Ź Ż Ё Й ...), punctuation mark and QUOTE MARK as drawn;
- if a word or letter is drawn twice, write it twice; if a letter is missing, leave it missing;
- NEVER fix spelling, never complete a word into a real word, never drop a stray mark. A misspelled word is copied misspelled.
Zoom into every text block before writing it. Also give "spelled": the same block as single characters separated
by spaces (so a missing or doubled letter is visible), and "box": where the block is, [left, top, right, bottom] as
fractions 0-1 of the image width and height.

Answer with ONE JSON object and nothing else:
{{"images": [{{"n": 1, "texts": [{{"text": "...", "spelled": "...", "box": [0.1, 0.05, 0.4, 0.2]}}]}}]}}
"""

# Второй взгляд — каждая надпись вырезана, увеличена и читается ОТДЕЛЬНЫМ вызовом. Замер р2
# (c8 TAvIqS9VzK0, у вариантов 1-2/1-3 нарисовано «БРОСЬ Ё!», у 1-1 «БРОСЬ ЕЁ!»): три картинки
# одним вызовом — верно 3 чтения из 6, модель «выравнивает» одну фразу на всех картинках (то
# всем ЕЁ, то всем Ё); девять вырезок одним вызовом — всем «ЕЁ» 3 из 3; вырезка одна на вызов —
# верно 10 из 10, 8-18 с на вызов.
ZOOM_PROMPT = """You are a proofreading scanner. You see {count} zoomed crops of thumbnails. Each crop is centred on ONE
block of writing; pieces of other writing may be cut by the crop edge — ignore anything cut by the edge.
You do NOT know what the block is supposed to say. Transcribe the centred block glyph by glyph, exactly as drawn:
every letter, accent mark, punctuation mark and QUOTE MARK; a word drawn twice is written twice; a missing letter or
short word stays missing; NEVER write what the phrase "should" say. Also give "spelled": single characters separated
by spaces.

Answer with ONE JSON object and nothing else:
{{"crops": [{{"n": 1, "text": "...", "spelled": "..."}}]}}
"""


def _box_of(value):
    try:
        x0, y0, x1, y1 = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if not (0 <= x0 < x1 <= 1.001 and 0 <= y0 < y1 <= 1.001):
        return None
    return x0, y0, min(x1, 1.0), min(y1, 1.0)


def zoom_crops(images, rows, work_dir) -> List[tuple]:
    """Вырезать каждую прочитанную надпись с запасом и увеличить -> [(номер картинки, номер блока, путь)]."""
    from PIL import Image
    out = []
    for n, blocks in sorted(rows.items()):
        with Image.open(images[n - 1]) as im:
            im = im.convert("RGB")
            W, H = im.size
            for b, block in enumerate(blocks):
                box = _box_of(block.get("box"))
                if not box:
                    continue
                x0, y0, x1, y1 = box
                px, py = max(0.03, 0.4 * (x1 - x0)), max(0.03, 0.4 * (y1 - y0))
                crop = im.crop((int(max(0, x0 - px) * W), int(max(0, y0 - py) * H),
                                int(min(1, x1 + px) * W), int(min(1, y1 + py) * H)))
                k = min(3.0, 1100 / max(crop.size))
                if k > 1:
                    crop = crop.resize((round(crop.width * k), round(crop.height * k)), Image.LANCZOS)
                path = Path(work_dir) / f"{Path(images[n - 1]).stem}_zoom_{b + 1}.jpg"
                crop.save(str(path), quality=92)
                out.append((n, b, str(path)))
    return out


def parse_reading_rows(data, count) -> Dict[int, List[dict]]:
    """Ответ чтеца -> {номер картинки: [{"text", "box"}]}. Нет ни одной картинки — ошибка."""
    got = {}
    for row in (data or {}).get("images") or []:
        if not isinstance(row, dict):
            continue
        try:
            n = int(row.get("n") or 0)
        except (TypeError, ValueError):
            continue
        texts = row.get("texts") or []
        blocks = []
        for x in (texts if isinstance(texts, list) else [texts]):
            text = x.get("text") if isinstance(x, dict) else x
            if text is not None and str(text).strip():
                blocks.append({"text": str(text), "box": x.get("box") if isinstance(x, dict) else None})
        if 1 <= n <= count:
            got[n] = blocks
    if not got:
        raise CoverError("в ответе чтеца нет ни одной картинки")
    return got


# Ответ чтеца «картинки не вижу» или пустые точки вместо надписи — не прочтение (critic р3:
# запасной claude без пути к файлу вернул «No image provided» и «...»).
NO_IMAGE = re.compile(r"no image|image (?:is )?not|not (?:been )?provided|can(?:no|')t (?:see|view|open|read)|"
                      r"unable to (?:see|view|open|read)|don'?t see|no (?:file|picture|attachment)|not attached", re.I)


def junk_block(text) -> bool:
    t = str(text or "").strip()
    return bool(NO_IMAGE.search(t)) or not re.search(r"\w", t)


READ_THREADS = 4          # одновременных вызовов чтеца на обложку (плюс выбирающий)


def read_texts(images, ask, work_dir) -> tuple:
    """Надписи на картинках вслепую, в два взгляда, каждый вызов — ОДНА картинка (см. ZOOM_PROMPT):
    целиком — что и где написано; затем каждая найденная надпись вырезкой крупно.
    ask(prompt, images) -> JSON. Картинку не прочитали — её нет в ответе («не сверено»);
    вырезку не прочитали — остаётся прочтение целиком. -> ({номер: [надписи]}, сводка)."""
    rows, errors, doubt = {}, [], {}

    def first(n):
        try:
            data = ask(READ_PROMPT.format(count=1), [images[n - 1]])
            if isinstance(data, dict) and data.get("error") and not data.get("images"):
                raise CoverError(f"чтец не открыл картинку: {str(data['error'])[:80]}")
            blocks = parse_reading_rows(data, 1).get(1)
            junk = [b for b in blocks or [] if junk_block(b["text"])]
            if blocks and len(junk) == len(blocks) and any(NO_IMAGE.search(b["text"]) for b in junk):
                raise CoverError(f"чтец не видел картинку: «{junk[0]['text'][:60]}»")
            fb = isinstance(data, dict) and data.get("_fallback")
            return n, [b for b in blocks or [] if not junk_block(b["text"])], None, (
                "мусор в ответе чтеца" if junk else ""), fb
        except _STOP:
            raise
        except Exception as exc:   # noqa: BLE001
            return n, None, f"{type(exc).__name__}: {str(exc)[:160]}", "", False
    with ThreadPoolExecutor(max_workers=min(READ_THREADS, len(images)) or 1) as pool:
        for n, blocks, err, junk, fb in pool.map(LLM.CP.carry(first), range(1, len(images) + 1)):
            if blocks is not None:
                rows[n] = blocks
                if junk or fb:
                    doubt[n] = junk or "читал запасной чтец или claude"
            else:
                errors.append(f"картинка {n}: {err}")
    if not rows:
        raise CoverError("чтец не прочитал ни одной картинки: " + "; ".join(errors)[:300])
    crops = zoom_crops(images, rows, work_dir)

    def second(crop):
        n, b, path = crop
        try:
            data = ask(ZOOM_PROMPT.format(count=1), [path])
            got = [str((r or {}).get("text") or "").strip() for r in (data or {}).get("crops") or []
                   if isinstance(r, dict)]
            text = got[0] if got else ""
            if text and junk_block(text):
                return crop, "", f"мусор вместо прочтения: «{text[:40]}»"
            if text and isinstance(data, dict) and data.get("_fallback"):
                doubt.setdefault(n, "читал запасной чтец или claude")
            return crop, text, None
        except _STOP:
            raise
        except Exception as exc:   # noqa: BLE001 — второй взгляд довесок: остаётся первое прочтение
            return crop, "", f"{type(exc).__name__}: {str(exc)[:160]}"
    zoomed = 0
    if crops:
        with ThreadPoolExecutor(max_workers=min(READ_THREADS, len(crops))) as pool:
            for (n, b, _path), text, err in pool.map(LLM.CP.carry(second), crops):
                if text:
                    rows[n][b]["text"] = text
                    zoomed += 1
                elif err:
                    errors.append(f"вырезка {n}-{b + 1}: {err}")
    if errors:
        log("чтец надписей: " + "; ".join(errors)[:300])
    return ({n: [b["text"] for b in blocks] for n, blocks in rows.items()},
            {"zoomed": zoomed, "crops": len(crops), "errors": errors, "doubt": doubt,
             "boxes": {n: [b.get("box") for b in blocks] for n, blocks in rows.items()}})


_CODEX_NOISE = re.compile(r"codex_login|refresh token|refresh_token|token_data", re.I)


def _usage_of(stdout: str) -> Optional[dict]:
    """Расход вызова из событий codex --json (строка с полем usage)."""
    found = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        u = ev.get("usage")
        if not isinstance(u, dict) and isinstance(ev.get("msg"), dict):
            u = ev["msg"].get("usage")
        if isinstance(u, dict):
            found = u
    return found


def _json_of(text: str) -> dict:
    raw = LLM.strip_fence(text or "")
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j <= i:
        raise CoverError(f"Выбирающий ответил не JSON: {raw[:160]}")
    data = json.loads(raw[i:j + 1])
    if not isinstance(data, dict):
        raise CoverError("Выбирающий ответил не объектом JSON")
    return data


def codex_images_json(prompt: str, images: List[str], name: str, effort: str = "medium",
                      timeout: int = 300):
    """Один вызов codex exec с картинками (-i на каждую) -> (JSON, расход).

    Альт-ключ (MANGA_ALT_API_KEY) — прокси флагами -c, как call_codex выпуска 4; без него —
    подписка через CODEX_HOME. Строки «codex_login … refresh token was revoked» — шум старого
    файла входа (разведка 19.09: rc=0, ответ есть), в причину отказа не попадают."""
    key = os.environ.get("MANGA_ALT_API_KEY", "").strip()
    if not key and LLM.CXP.POOL is not None:
        # Пул подписок MANGA_CODEX_HOMES — как у судьи кадров: учётка с запасом, замок обновления
        # входа, строка в журнале. Раньше вызов шёл мимо пула в одну учётку.
        try:
            # wait_reset=False: все учётки упёрлись в лимит (в том числе посреди этого вызова) — пул не ждёт
            # сброса окна (до суток), а у обложки есть запасной выбирающий (MANGA_COVER_JUDGE_FALLBACK).
            # Лимиты читает сам пул под своей защитой — ни одного лишнего чтения (оно может применить подарочный сброс).
            out, rc, err = LLM.codex_exec_pooled(prompt, name, effort, [str(p) for p in images], timeout,
                                                 wait_reset=False)
        except LLM.LimitError as exc:
            raise CoverError(f"codex/{name}: все учётки Codex упёрлись в лимит — выбирает запасной ({exc})") from None
        if not out:
            raise CoverError(f"codex/{name}: пустой ответ (rc={rc}). {str(err or '')[-300:]}")
        return _json_of(out), None
    fd, out_path = tempfile.mkstemp(suffix=".txt", prefix="cover-judge-")
    os.close(fd)
    cmd = ["codex", "exec", "-m", name]
    if key:
        env = {k: v for k, v in os.environ.items() if k not in ("CODEX_HOME", "OPENAI_API_KEY")}
        env["MANGA_ALT_API_KEY"] = key
        base = os.environ.get("MANGA_ALT_OPENAI_BASE_URL", "").strip() or "https://triklz27.ru/v1"
        cmd += ["--ignore-user-config",
                "-c", 'model_provider="manga_alt"',
                "-c", 'model_providers.manga_alt.name="manga_alt"',
                "-c", f'model_providers.manga_alt.base_url="{base}"',
                "-c", 'model_providers.manga_alt.env_key="MANGA_ALT_API_KEY"',
                "-c", 'model_providers.manga_alt.wire_api="responses"']
    else:
        env = {**os.environ, "CODEX_HOME": LLM.CODEX_HOME}
    cmd += ["-c", f'model_reasoning_effort="{effort}"', "--skip-git-repo-check", "--ephemeral",
            "--color", "never", "--json", "-o", out_path]
    for path in images:
        cmd += ["-i", str(path)]
    cmd.append("-")
    slot = getattr(LLM, "_alt_slot", None) if key else None
    try:
        if slot:
            with slot():
                p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, env=env)
        else:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, env=env)
        try:
            out = open(out_path, encoding="utf-8").read().strip()
        except OSError:
            out = ""
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
    if not out:
        err = "\n".join(line for line in (p.stderr or "").replace(prompt, "").splitlines()
                        if re.match(r"\s*(ERROR|error)\b", line) and not _CODEX_NOISE.search(line))
        if key:
            err = err.replace(key, "***")
        raise CoverError(f"codex/{name}: пустой ответ (rc={p.returncode}). {err[-300:]}")
    return _json_of(out), _usage_of(p.stdout)


def ask_judge(prompt: str, images: List[str], model: str):
    """Вызов выбирающего -> (JSON, расход или None). codex — картинки флагами -i; claude —
    тот же путь, что у карточки кадра: CLI сам открывает файлы Read в папке ролика."""
    kind, _, name = model.partition(":")
    if kind == "codex":
        return codex_images_json(prompt, images, name, judge_effort())
    if kind == "claude":
        return vision_json(prompt, images, model), None
    raise CoverError(f"Неизвестная модель выбирающего: {model}")


def _judge_copy(src, dst) -> str:
    """JPEG q88 для выбирающего: 1672×941 ≈ 290 КБ и ≈3,3 тыс. входных токенов."""
    from PIL import Image
    with Image.open(src) as im:
        im.convert("RGB").save(str(dst) + ".tmp.jpg", quality=88)
    os.replace(str(dst) + ".tmp.jpg", str(dst))
    return str(dst)


def _flag(value) -> bool:
    return value is True or str(value).strip().lower() in ("true", "yes", "1")


def parse_verdict(data, count) -> dict:
    """Ответ выбирающего -> {ok, best, why_ru, redo, redo_reason, by_n: {n: {...}}}."""
    by_n = {}
    for row in (data or {}).get("variants") or []:
        if not isinstance(row, dict):
            continue
        try:
            n = int(row.get("n") or row.get("variant") or 0)
        except (TypeError, ValueError):
            continue
        if not 1 <= n <= count:
            continue
        try:
            score = max(0.0, min(10.0, float(row.get("score"))))
        except (TypeError, ValueError):
            score = None
        fixes = []
        for f in row.get("fixes") or [] if isinstance(row.get("fixes"), list) else []:
            if isinstance(f, dict) and str(f.get("native") or "").strip():
                fixes.append({"drawn": str(f.get("drawn") or "").strip()[:200], "native": str(f["native"]).strip()[:200]})
        by_n[n] = {"fixes": fixes,
                   "score": score, "why": str(row.get("why") or "")[:300],
                   "flags": [f for f in ("school_uniform", "logo", "underage", "more_revealing", "text_damaged",
                                            "not_our_hero") if _flag(row.get(f))]}
    try:
        best = int((data or {}).get("best") or 0)
    except (TypeError, ValueError):
        best = 0
    return {"ok": bool(by_n), "best": best if best in by_n else None,
            "why_ru": str((data or {}).get("why_ru") or "")[:400],
            "redo": _flag((data or {}).get("redo")), "redo_reason": str((data or {}).get("redo_reason") or "")[:400],
            "by_n": by_n}


FLAG_RU = {"school_uniform": "школьная форма", "logo": "чужой логотип или название канала",
           "underage": "человек выглядит моложе 20", "more_revealing": "откровеннее донора",
           "text_damaged": "пятно или предмет поверх букв", "not_our_hero": "герой не похож на свой лист"}
BLOCKING_FLAGS = ("school_uniform", "logo", "underage")
# «Посмотрите глазами», но не брак: новый круг из-за них не покупается (critic р3: флаг
# «откровеннее» стоял у всех шести вариантов двух кругов — 36 единиц вместо 18, итог тот же).
SOFT_FLAGS = ("more_revealing", "text_damaged", "not_our_hero")


def native_fixes(fixes, expected_all) -> List[dict]:
    """Правки носителя к надписям, нарисованным ТАК, КАК МЫ ЗАКАЗАЛИ: значит, ошибся перевод,
    а не рисовальщик (c8, M2ORVZbRm0I: заказано и нарисовано «VREAU SĂ TE VAD!», по-румынски
    «VĂD»). Брак рисования ловит сверка букв, сюда он не попадает."""
    ordered = {_squash(e): e for e in expected_all}
    out = []
    for f in fixes or []:
        drawn, native = _squash(f.get("drawn")), _squash(f.get("native"))
        if drawn in ordered and native and native != drawn:
            out.append({"ordered": ordered[drawn], "native": f["native"]})
    return out


# Наш ролик цветной (все стили S0–S4) — обложка обязана быть яркой и цветной, даже когда у
# конкурента чёрно-белая манга. Решение владельца 21.09.2026: «превью, если у нас ролик красочный,
# не должен выглядеть чб — фулл ярким цветным и кликбейтным». Замер тех же дней: цветные обложки
# 0,38–0,53 средней насыщенности, «Тихони», повторившие чб конкурента, — 0,02–0,13, сам конкурент 0,01.
MONO_MAX = 0.25


def colourfulness(path) -> Optional[float]:
    """Средняя насыщенность (HSV, 0–1) без тёмных пикселей — у чёрного насыщенность шумит."""
    try:
        from PIL import Image
        import numpy as np
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((320, 180))
            hsv = np.asarray(im.convert("HSV"), dtype=np.float32) / 255.0
    except (OSError, ValueError):
        return None
    lit = hsv[..., 2] > 0.15
    return round(float(hsv[..., 1][lit].mean()), 3) if lit.any() else 0.0


def blocking_of(variant) -> List[str]:
    """Блокирующий брак варианта по-русски: неверные буквы (сверка КОДОМ) и флаги выбирающего."""
    out = [] if variant.get("letters_doubt") else [
        _fault_ru(f) for f in variant.get("letters") or [] if not f.get("soft")]
    out += [FLAG_RU[f] for f in variant.get("flags") or [] if f in BLOCKING_FLAGS]
    sat = variant.get("saturation")
    if isinstance(sat, (int, float)) and sat < MONO_MAX:
        out.append(f"почти без цвета (насыщенность {sat:.2f} при нужных от {MONO_MAX}) — ролик цветной, "
                   "обложка должна быть яркой")
    return out


def _fault_ru(f) -> str:
    if f.get("spacing"):
        return f'слова склеены или разорваны: ожидалось «{f["want"]}», прочитано «{f["read"]}»'
    if f.get("extra"):
        return (f'мелкая надпись фона «{f["read"]}»' if f.get("small") else f'лишний текст на обложке: «{f["read"]}»')
    return f'буквы: ожидалось «{f["want"]}», прочитано «{f["read"]}»'


def soft_of(variant) -> List[str]:
    """Что у варианта надо посмотреть глазами (не брак, новый круг не покупается)."""
    out = [FLAG_RU[f] for f in variant.get("flags") or [] if f in SOFT_FLAGS]
    doubt = variant.get("letters_doubt")
    for f in variant.get("letters") or []:
        if f.get("soft"):
            out.append(_fault_ru(f))
        elif doubt:
            out.append(f"{_fault_ru(f)} (прочтение ненадёжно: {doubt})")
    return out


def is_copy(variant) -> bool:
    s, b = variant.get("ssim"), variant.get("dhash")
    return (s is not None and s >= COPY_SSIM_STOP) or (b is not None and b <= COPY_DHASH_STOP)


def revealing(variant) -> bool:
    return "more_revealing" in (variant.get("flags") or [])


def decide(variants: List[dict], judge: dict) -> dict:
    """Выбор в одном круге. variants — только состоявшиеся (есть картинка), с полями оценки.

    Судья ответил: берём его лучший, если у того нет блокирующего брака; иначе лучший по
    оценке из годных; годных нет — «переделать». «Посмотрите глазами» (soft_of: откровеннее
    донора, пятно на буквах, герой не с листа, мелкая вывеска) — не брак: у лучшего такое, а
    есть годный без этого с оценкой не ниже чем на 1 балл — берём его; такое у всех годных —
    берём лучшего с пометкой, нового круга НЕТ. При равной оценке — вариант дальше от донора.
    Судья не ответил: годный (без брака чтеца), не копия; брак у всех — «переделать»."""
    if not variants:
        return {"pick": None, "redo": False, "eyes": True, "by": "none"}
    if not (judge or {}).get("ok"):
        order = ([v for v in variants if not v.get("blocking") and not is_copy(v) and not soft_of(v)]
                 + [v for v in variants if not v.get("blocking") and not is_copy(v)]
                 + [v for v in variants if not is_copy(v)] + list(variants))
        return {"pick": order[0]["k"], "redo": all(v.get("blocking") for v in variants), "eyes": True, "by": "code"}
    good = [v for v in variants if not v.get("blocking")]
    clean = [v for v in good if not soft_of(v)]

    def score(v):
        return v.get("score") if v.get("score") is not None else -1

    def rank(v):
        return score(v), v.get("dhash") or 0

    best = next((v for v in good if v["k"] == judge.get("best")), None)
    if best is not None and soft_of(best) and clean and score(max(clean, key=rank)) >= score(best) - 1:
        return {"pick": max(clean, key=rank)["k"], "redo": False, "eyes": False, "by": "judge_score"}
    if best is not None:
        return {"pick": best["k"], "redo": False, "eyes": bool(soft_of(best)), "by": "judge"}
    if clean:
        return {"pick": max(clean, key=rank)["k"], "redo": False, "eyes": False, "by": "judge_score"}
    if good:
        return {"pick": max(good, key=rank)["k"], "redo": False, "eyes": True, "by": "judge_score"}
    worst_first = min(variants, key=lambda v: (len(v.get("blocking") or []), len(soft_of(v)), -score(v)))
    return {"pick": worst_first["k"], "redo": True, "eyes": True, "by": "judge_score"}


# ---------------------------------------------------------------- сам шаг
def spend_line(units, refused) -> str:
    """Строка расхода FastGen для заметки. Отказ генератора с номером задания FastGen то
    списывался, то нет: пока замер не повторён, владелец видит и нижнюю, и верхнюю оценку."""
    if units is None:
        return ""
    if refused:
        return f" Единиц FastGen: {units} (до {units + refused * UNITS_PER_PASS}, если FastGen списал отказ генератора)."
    return f" Единиц FastGen: {units}."


def _rel(root, path) -> str:
    return str(Path(path).relative_to(root)) if path else ""


def _variant_title(v, chosen_key) -> str:
    score = f" — {v['score']:.0f}/10" if v.get("score") is not None else ""
    mark = " ✓" if v["key"] == chosen_key else ""
    return f"круг {v['round']}, вариант {v['k']}{score}{mark}"


def all_variants(state) -> List[dict]:
    return [v for rnd in state.get("rounds") or [] for v in rnd.get("variants") or []]


def build_note(state) -> str:
    """Заметка владельцу: что выбрано, почему, брак, расход."""
    ch = state.get("chosen") or {}
    variants = all_variants(state)
    made = [v for v in variants if v.get("file")]
    pick = next((v for v in variants if v["key"] == ch.get("key")), {})
    note = ("Обложка готова" if state.get("verdict") == "ok" else "Обложка готова — проверьте глазами")
    note += f": вариант {pick.get('k')} из {len(made)}"
    if len(state.get("rounds") or []) > 1:
        note += f" (круг {pick.get('round')} из {len(state['rounds'])})"
    texts = state.get("texts") or []
    note += (", надписи «" + " | ".join(texts) + "»." if texts else ", без надписей.")
    if ch.get("by") == "operator":
        note += " Выбран оператором вручную."
    elif ch.get("by") in ("judge", "judge_score"):
        judge = next((r.get("judge") for r in state.get("rounds") or [] if r.get("n") == pick.get("round")), {}) or {}
        who = judge.get("model") or "выбирающий"
        note += f" Выбрал {who}" + (f" (оценка {pick['score']:.0f}/10)" if pick.get("score") is not None else "")
        if judge.get("why_ru") and ch.get("by") == "judge":
            note += f": {judge['why_ru'].rstrip('.')}."
        else:
            note += "."
        if judge.get("fallback_used"):
            note += f" Основной выбирающий не ответил, выбирал запасной ({judge.get('model')})."
    elif ch.get("by") == "code":
        note += " Выбирающий не ответил — взят первый вариант, который не копия донора по коду."
    if state.get("texts_by_code"):
        note += " Кодом нарисовано: " + "; ".join(state["texts_by_code"]).rstrip(".") + "."
    if pick.get("blocking"):
        note += " Брак у выбранной: " + "; ".join(pick["blocking"]) + "."
    if soft_of(pick):
        note += " Посмотрите глазами: " + "; ".join(soft_of(pick)) + "."
    if pick.get("small_text"):
        note += " Посмотрите глазами: " + "; ".join(pick["small_text"]) + "."
    if pick.get("unread") and state.get("texts"):
        note += " Чтец надписей не ответил — буквы не сверены, посмотрите глазами."
    for f in pick.get("native") or []:
        note += f" Носитель языка пишет «{f['native']}», а в переводе «{f['ordered']}» — проверьте надпись."
    if is_copy(pick):
        note += f" По коду похожа на донора (SSIM {pick.get('ssim')}, dHash {pick.get('dhash')}) — посмотрите глазами."
    for line in state.get("english") or []:
        note += f" {line[:1].upper()}{line[1:]}."
    if state.get("no_sheet"):
        note += " Без листа, только словами: " + ", ".join(state["no_sheet"]) + "."
    failed = [v for v in variants if v.get("error")]
    if failed:
        note += f" Не нарисовано вариантов: {len(failed)} (генератор отказал)."
    clean = state.get("clean") or {}
    if clean.get("method") == "esrgan":
        note += " Зерно убрано Real-ESRGAN."
    elif clean.get("method") == "nlmeans":
        note += " Зерно сглажено (h=3)."
    if clean.get("note"):
        note += f" {clean['note'][0].upper()}{clean['note'][1:]}."
    note += spend_line(state.get("units"), state.get("units_refused") or 0)
    use = state.get("usage") or {}
    if use:
        note += (f" Вызовов Claude: {use.get('claude_calls', 0)}, выбирающего: {use.get('judge_calls', 0)},"
                 f" чтеца надписей: {use.get('reader_calls', 0)};"
                 f" шаг занял {float(use.get('step_seconds') or 0) / 60:.1f} мин.")
    return note


def apply_choice(root, state, key, by, eyes=False) -> dict:
    """Положить вариант key в out/cover.png (с чисткой), превью, лист, cover.json. Ничего не покупает."""
    root = Path(root)
    out_dir = root / "out"
    v = next((x for x in all_variants(state) if x["key"] == key and x.get("file")), None)
    if v is None:
        raise CoverError(f"Варианта {key} нет: его генератор не нарисовал.")
    src = root / v["file"]
    if not src.is_file():
        raise CoverError(f"Файл варианта {key} пропал с диска ({v['file']}).")
    cover_path = out_dir / "cover.png"
    state["clean"] = clean_cover(src, cover_path)
    state["chosen"] = {"key": key, "round": v["round"], "k": v["k"], "by": by, "eyes": bool(eyes),
                       "original": v["file"]}
    state["letter_faults"] = list(v.get("letters") or [])
    state["problems_left"] = list(v.get("blocking") or []) + (
        ["выбирающий не ответил — выбор сделан кодом"] if by == "code" else []) + soft_of(v) + (
        ["надписи не прочитаны — буквы не сверены"] if v.get("unread") and state.get("texts") else []) + [
        f'носитель пишет «{f["native"]}» вместо «{f["ordered"]}»' for f in v.get("native") or []] + list(
        v.get("small_text") or [])
    # Выбор оператора — его глаза: пометка «проверьте глазами» с него снимается, брак — нет.
    eyes = bool(eyes) and by != "operator"
    state["chosen"]["eyes"] = eyes
    # «Откровеннее донора» и правка носителя к переводу — не брак рисования, но и не «готово»:
    # правило владельца и грамотность надписи проверяет человек.
    # 27.09 (F08): подпись кодом мельче FIT_SHARE донорской (RO d4cd8cf9: 8.2 px против 11.7 в
    # ленте 246) — тоже «проверьте глазами». Выбор варианта от этого не меняется (soft_of не трогаем).
    state["verdict"] = "warn" if (eyes or v.get("blocking") or state.get("english") or soft_of(v)
                                  or v.get("native") or (v.get("unread") and state.get("texts"))
                                  or v.get("small_text")) else "ok"
    make_preview(cover_path, out_dir / "cover_preview.jpg")
    make_sheet(root / "source" / "thumbnail.jpg",
               [(_variant_title(x, key), root / x["file"] if x.get("file") else None) for x in all_variants(state)],
               cover_path, out_dir / "cover_sheet.jpg", texts=state.get("texts") or [])
    receipts = sorted(out_dir.glob("cover*.generation.json"))
    state["units"], state["units_refused"] = units_spent(receipts), units_refused(receipts)
    state["files"] = {"cover": "out/cover.png", "preview": "out/cover_preview.jpg", "sheet": "out/cover_sheet.jpg",
                      "original": v["file"]}
    state["note"] = build_note(state)
    write_json(out_dir / "cover.json", state)
    return {"note": state["note"], "verdict": state["verdict"]}


_PICK_LOCK = threading.Lock()


def pick_variant(root, key) -> dict:
    """Кнопка «взять другой»: оператор переключает обложку на другой уже нарисованный
    вариант. Ничего не покупается и выбирающий не зовётся."""
    root = Path(root)
    with _PICK_LOCK:        # два нажатия подряд не пишут cover.png/cover.json одновременно
        state = read_json(root / "out" / "cover.json", {}) or {}
        if not state.get("done"):
            raise CoverError("Обложка ещё делается — переключать можно после шага «Обложка».")
        was = (state.get("chosen") or {}).get("key")
        result = apply_choice(root, state, str(key), "operator")
    log(f"оператор взял вариант {key} вместо {was} — без покупки")
    return result


def remeasure_small(root, state, state_path, thumb, iso) -> None:
    """27.09.2026, I2-02: готовая обложка (done=True) отдавалась из кэша с прежним verdict, а замера
    мелкой подписи кодом (F08, small_captions) у неё ещё не было: RO d4cd8cf9 — «ok» при строке
    8.2 px против донорских 11.7. Перемеряем, как возобновление в finish: оплаченный оригинал ->
    черновик -> раскладка кодом -> замер. Ничего не покупаем, модель не зовём, cover.png и
    _final.png не трогаем. Замер берём, только если черновик пиксель в пиксель равен
    сохранённому _final.png: иначе карточка с тех пор другая и мерили бы не то, что нарисовано."""
    todo = [v for v in all_variants(state) if v.get("file") and v.get("raw") and "small_text" not in v]
    if not state.get("texts_by_code") or not todo:
        return
    from PIL import Image, ImageChops
    root, measured = Path(root), 0
    try:
        card = tidy_card(read_json(root / "source" / CARD_NAME) or {}, [])     # герои надписям не нужны
        paint_from_donor(card["texts"], thumb)
        by_code = code_texts(card, iso)
        if [" ".join(t["lines"][iso]) for t in by_code] != list(state["texts_by_code"]):
            log("обложка: карточка надписей с тех пор другая — мелкую подпись не перемеряю")
            return
        for t in by_code:
            t["font"] = font_for(" ".join(t["lines"][iso]))
        for v in todo:
            raw, final = root / v["raw"], root / v["file"]
            if not raw.is_file() or not final.is_file():
                continue
            probe = raw.with_name(raw.stem + "_probe.tmp.png")
            shutil.copyfile(raw, probe)
            try:
                fit_cover(probe)
                report = draw_texts(probe, probe, by_code, iso, donor_path=str(thumb))
                with Image.open(probe) as a, Image.open(final) as b:
                    same = a.size == b.size and ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getbbox() is None
            finally:
                probe.unlink(missing_ok=True)
            if same:
                v["small_text"] = small_captions(report)
                measured += 1
    except Exception as exc:      # noqa: BLE001 — замер не должен ломать выдачу готовой обложки
        log(f"обложка: мелкую подпись перемерить не вышло ({exc}) — статус прежний")
        return
    if not measured:
        return
    pick = next((v for v in all_variants(state) if v["key"] == (state.get("chosen") or {}).get("key")), {})
    if pick.get("small_text"):
        left = list(state.get("problems_left") or [])
        state["problems_left"] = left + [s for s in pick["small_text"] if s not in left]
        state["verdict"] = "warn"
        state["note"] = build_note(state)
        log("обложка: подпись кодом мельче донорской — «проверьте глазами»")
    write_json(state_path, state)


def finished_note(state, state_path, out_dir) -> dict:
    """Готовая обложка при повторе: ничего не покупаем, расход пересчитываем по квитанциям."""
    receipts = sorted(Path(out_dir).glob("cover*.generation.json"))
    units, refused = units_spent(receipts), units_refused(receipts)
    if units is not None and (units != state.get("units") or refused != (state.get("units_refused") or 0)):
        state = {**state, "units": units, "units_refused": refused}
        state["note"] = build_note(state)
        write_json(state_path, state)
    return {"note": state.get("note") or "Обложка уже готова, ничего не покупалось заново.",
            "verdict": state.get("verdict")}


def run_step(root, data, *, generate=None, vision=None, call=None, judge=None, fetch_thumbnail=None,
             alive=None) -> dict:
    """Выполнить шаг «Обложка». Возвращает {"note": ..., "verdict": ...}.

    Что покупается: три варианта openai-image thinking (по 6 единиц) одновременно. Если у всех
    трёх блокирующий брак — ещё один круг из трёх. Повторный запуск ничего не покупает заново:
    промпт каждого варианта записан в cover.json ДО покупки, рядом с картинкой лежит
    квитанция, и оценка выбирающего тоже сохранена.

    judge — подмена выбирающего в тестах: (prompt, images, model) -> JSON или (JSON, расход).
    alive — проверка «задача ещё моя» перед каждой покупкой."""
    alive = alive or (lambda: None)
    root = Path(root)
    out_dir = root / "out"
    cover_path = out_dir / "cover.png"
    state_path = out_dir / "cover.json"
    if not enabled():
        return {"note": "Шаг выключен настройкой MANGA_COVER=off: обложка не делалась."}
    lang = data.get("language") or LG.DEFAULT
    iso = LG.iso(lang)
    thumb = root / "source" / "thumbnail.jpg"
    ref = str(data.get("source_project") or "")
    ref_root = root.parent / ref if ref else None
    if not thumb.is_file() and ref_root is not None and (ref_root / "source" / "thumbnail.jpg").is_file():
        # Перевод эталона (тот же сюжет на другом языке): своей ссылки нет, донор — тот же,
        # что у эталона. Герои — листы ЭТОГО ролика, надписи — на его языке.
        thumb.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ref_root / "source" / "thumbnail.jpg", thumb)
        log(f"превью донора взято у ролика-эталона {ref[:8]}")
    ref_meta = (SRC.read_meta(ref_root) or {}) if ref_root is not None else {}
    donor_url = data.get("source_url") or ref_meta.get("url") or ""
    if not thumb.is_file() and donor_url:
        # У эталона превью не оказалось (убрано уборкой) — ссылка донора известна из его разбора.
        vid = (data.get("video_id") if data.get("source_url") else ref_meta.get("video_id")) or SRC.video_id(donor_url)
        try:
            (fetch_thumbnail or SRC.fetch_thumbnail)(vid, SRC._proxy(), thumb)
            log("превью донора докачано")
        except _STOP:
            raise
        except Exception as exc:
            raise CoverError(f"Превью ролика-донора не скачано ({exc}). Без него обложку делать не из чего: "
                             "проверьте ссылку и прокси MANGA_SUBS_PROXY, затем повторите незавершённое.") from None
    if not thumb.is_file():
        if ref:
            return {"note": f"Ролик — перевод ролика {ref[:8]}, а тот собран из вставленного текста: "
                            "обложки-донора нет, поэтому шаг пропущен."}
        return {"note": "Ролик собран из вставленного текста: обложки-донора нет, поэтому шаг пропущен. "
                        "Чтобы студия сделала обложку, создайте ролик по ссылке на оригинал."}

    state = read_json(state_path, {}) or {}
    if state.get("done") and state.get("lang") == lang and cover_path.is_file():
        remeasure_small(root, state, state_path, thumb, iso)     # 27.09.2026 I2-02: замер F08 у старой готовой
        return finished_note(state, state_path, out_dir)
    if state.get("method") != METHOD or state.get("lang") != lang:
        state = {}                      # прошлый способ или другой язык: круги начинаются заново
    model = vision_model()
    use = state.get("usage") or {}
    spent = {"claude_calls": int(use.get("claude_calls") or 0), "claude_seconds": float(use.get("claude_seconds") or 0),
             "judge_calls": int(use.get("judge_calls") or 0), "judge_seconds": float(use.get("judge_seconds") or 0),
             "reader_calls": int(use.get("reader_calls") or 0), "reader_seconds": float(use.get("reader_seconds") or 0),
             "step_seconds": float(use.get("step_seconds") or 0)}
    started = time.monotonic()

    tries = [0]
    _TRIES.set(tries)       # счётчик вызовов этой обложки (потоки шага видят его через CP.carry)

    def counted(fn):
        def inner(*a, **k):
            t0, tries0 = time.monotonic(), tries[0]
            try:
                return fn(*a, **k)
            finally:
                spent["claude_calls"] += max(1, tries[0] - tries0)
                spent["claude_seconds"] += time.monotonic() - t0
        return inner

    vision = counted(vision or vision_json)
    call = counted(call or (lambda prompt: ask_json(prompt, model)[0]))
    judge = judge or ask_judge

    def usage():
        return {**{k: round(v, 1) if isinstance(v, float) else v for k, v in spent.items()},
                "step_seconds": round(spent["step_seconds"] + time.monotonic() - started, 1)}

    heroes = heroes_of(root)
    heroes_by_id = {h["id"]: h for h in heroes}
    meta = SRC.read_meta(root) or (SRC.read_meta(ref_root) if ref_root is not None else None) or {}
    donor_title = meta.get("title") or data.get("title") or ""
    card_path = root / "source" / CARD_NAME
    card = read_json(card_path)
    if (card and card.get("version") == CARD_VERSION and not card_problems(card)
            and not hero_problems(card, heroes, strict=False)):
        log("карточка кадра этого ролика уже есть — беру готовую, заново не разбираю")
    else:
        card = build_card(thumb, donor_title, heroes, model=model, vision=vision)
        write_json(card_path, card)
        log(f"карточка кадра: людей {len(card['chars'])}, объектов {len(card.get('key_objects') or [])}, "
            f"надписей {len(card['texts'])}")
    card = tidy_card(card, heroes)
    # Перевод без отметки нынешней вычитки делается заново: c8 взял из карточки прошлой пробы
    # «VREAU SĂ TE VAD!» (без Ă), а с нуля перевод с вычиткой даёт верное «VĂD».
    if (any(not (t.get("lines") or {}).get(iso) for t in card["texts"])
            or (card["texts"] and card.get("proofread_" + iso) != PROOF_VERSION)):
        lines = translate_texts(card, lang, model=model, call=call, title=donor_title)
        for t in card["texts"]:
            if lines.get(t["orig"]):
                t.setdefault("lines", {})[iso] = lines[t["orig"]]
        card["proofread_" + iso] = PROOF_VERSION
        card.pop("shortened_" + iso, None)      # новый перевод — прогноз «влезет ли» заново
        write_json(card_path, card)
    # Надпись, которую нарисует код (румынские Ș/Ț), обязана влезть в облако крупно: прогноз
    # по облаку донора ДО покупки, и если строка мельче 0.8 донорской — один раз просим короче.
    if not card.get("shortened_" + iso):
        for t in code_texts(card, iso):
            said = shorten_for_plate(t, iso, lang, thumb, call)
            if said:
                log(said)
        card["shortened_" + iso] = True
        write_json(card_path, card)
    expected_all = [" ".join(t["lines"][iso]) for t in card["texts"] if (t.get("lines") or {}).get(iso)]
    if expected_all:
        log(f"надписи на языке ролика ({lang}): " + " | ".join(expected_all))
    for note_ru in paint_from_donor(card["texts"], thumb):
        log("цвет надписи замерен у донора — " + note_ru)
    by_code = code_texts(card, iso)
    for t in by_code:
        t["font"] = font_for(" ".join(t["lines"][iso]))     # нет шрифта с нужными буквами — ошибка ДО покупки
    code_expected = [" ".join(t["lines"][iso]) for t in by_code]
    expected = [e for e in expected_all if e not in code_expected]

    plan = size_plan(card, thumb)
    used = []
    for c in sorted(card["chars"], key=_area, reverse=True):
        if c.get("hero") and c["hero"] in heroes_by_id and heroes_by_id[c["hero"]] not in used:
            used.append(heroes_by_id[c["hero"]])
    hero_images = hero_refs(used, root / "source" / HERO_DIR, IM.MAX_REFS - 1)
    no_sheet = [h["name"] for h in used if not any(hid == h["id"] for _n, _p, hid in hero_images)]
    if no_sheet:
        log("без листа (только словами): " + ", ".join(no_sheet))
    refs = [("competitor_reference.jpg", str(thumb))] + [(name, path) for name, path, _hid in hero_images]
    base_prompt = build_prompt(card, lang, heroes_by_id, skip_code=by_code, plan=plan, hero_images=hero_images,
                               video_style=data.get("style"))
    out_dir.mkdir(parents=True, exist_ok=True)
    judge_dir = out_dir / "cover_judge"      # JPEG-копии для выбирающего; живут только на время вызова

    state.update({"method": METHOD, "lang": lang, "language_iso": iso, "done": False,
                  "model": MODEL, "options": OPTIONS,
                  "donor": {"video_id": meta.get("video_id"), "title": meta.get("title"),
                            "url": meta.get("url") or data.get("source_url"), "thumbnail": "source/thumbnail.jpg"},
                  "texts": expected_all, "texts_by_code": code_expected, "size_plan": plan, "no_sheet": no_sheet,
                  "heroes": [{"id": h["id"], "name": h["name"],
                              "sheets": [n for n, _p, hid in hero_images if hid == h["id"]]} for h in used],
                  "english": [p["note_ru"] for p in english_problems(expected_all, iso)]})
    rounds = state.setdefault("rounds", [])

    def keep_state():
        state["usage"] = usage()
        write_json(state_path, state)

    def buy(rnd):
        """Три варианта круга одновременно; уже купленные не покупаются (квитанция).
        Круг уже оценён (процесс упал после оценки) — не докупается ничего: вариант, который
        не удался, выбирающий всё равно не увидел бы."""
        if rnd.get("judge") is not None:
            return
        todo = []
        for v in rnd["variants"]:
            raw = root / v["raw"]
            if raw.is_file() and Path(str(raw) + ".generation.json").is_file():
                continue
            todo.append(v)
        if not todo:
            return
        alive()
        log(f"круг {rnd['n']}: покупаю вариантов {len(todo)} одновременно "
            f"({MODEL} {OPTIONS.get('quality', '')}, по {UNITS_PER_PASS} ед.)")

        # Потоки вариантов наследуют ролик и признак отмены исполнителя (FastGen): иначе ожидание
        # квоты не прерывалось остановкой службы и шло «без имени» до следующего часа.
        owner = IM.FP_POOL.context()

        def one(v):
            IM.FP_POOL.bind(*owner)
            try:
                generate_one(root / v["raw"], rnd["prompts"][v["k"] - 1], refs, generate=generate)
                return v, None
            except CoverError as exc:
                return v, str(exc)
        with ThreadPoolExecutor(max_workers=len(todo)) as pool:
            for v, err in pool.map(LLM.CP.carry(one), todo):
                v["error"] = err
                if err:
                    log(f"вариант {v['k']} круга {rnd['n']}: {err}")
        if owner[1] is not None and owner[1]():
            # Ожидание места FastGen прервали (служба останавливается или ролик забрали): это не
            # отказ генератора, и новый круг покупать нельзя.
            raise IM.FP_POOL.Cancelled("покупка обложки прервана: служба останавливается или ролик забрали")

    def finish(rnd):
        """Картинки круга -> готовые варианты: размер, надписи кодом, похожесть на донора."""
        for v in rnd["variants"]:
            raw = root / v["raw"]
            if not raw.is_file():
                v["file"] = None
                continue
            v["error"] = None
            from PIL import Image
            with Image.open(raw) as im:
                size = im.size
            final = raw
            report = None
            if size != COVER_SIZE or by_code:
                # Ответ генератора не трогаем (он — оплаченный оригинал с квитанцией): размер и
                # надписи кодом ложатся в отдельный файл.
                final = raw.with_name(raw.stem + "_final.png")
                if not final.is_file():
                    tmp = raw.with_name(raw.stem + "_fit.tmp.png")
                    shutil.copyfile(raw, tmp)
                    fit_cover(tmp)
                    if by_code:
                        report = draw_texts(tmp, final, by_code, iso, donor_path=str(thumb))
                        tmp.unlink(missing_ok=True)
                    else:
                        os.replace(tmp, final)
                elif by_code and "small_text" not in v:
                    # 27.09 (F08): _final.png с прошлого запуска, а замер строки тогда не хранился —
                    # повтор после падения отдал бы мелкую подпись как «ok». Раскладка — чистый код
                    # (замер: 9/9 вариантов пиксель в пиксель), кладём в черновик ради замера.
                    probe = raw.with_name(raw.stem + "_probe.tmp.png")
                    shutil.copyfile(raw, probe)
                    try:
                        fit_cover(probe)
                        report = draw_texts(probe, probe, by_code, iso, donor_path=str(thumb))
                    finally:
                        probe.unlink(missing_ok=True)
            if report is not None:
                v["small_text"] = small_captions(report)
            v["file"] = _rel(root, final)
            v["ssim"], v["dhash"] = ssim(final, thumb), dhash_distance(final, thumb)
            v["saturation"] = colourfulness(final)

    def run_judge(rnd):
        try:
            judge_dir.mkdir(exist_ok=True)
            return judge_round(rnd)
        finally:
            shutil.rmtree(judge_dir, ignore_errors=True)

    def judge_round(rnd):
        made = [v for v in rnd["variants"] if v.get("file")]
        donor_jpg = _judge_copy(thumb, judge_dir / "competitor.jpg")
        images = [donor_jpg] + [_judge_copy(root / v["file"], judge_dir / f"variant_{rnd['n']}_{v['k']}.jpg")
                                for v in made]
        hero_words = "; ".join(f"{h['name']} — {h['traits']}" for h in used) or "no heroes of ours on this thumbnail"
        # Какие надписи заказаны, выбирающему НЕ говорим: зная их, sol «читал» то, что должно
        # быть, а не то, что нарисовано (c8: на картинке «БРОСЬ Ё!», прочитано «БРОСЬ ЕЁ!»).
        # Буквы сверяет код с его слепым прочтением.
        text_rule = (f"{len(expected_all)} short native {_lang_name(lang)} caption(s), big and readable in the feed, "
                     "correct spelling with every accent mark; NOTHING else written — no stray or repeated words, "
                     "no quote marks, no signs or background lettering." if expected_all
                     else "NO TEXT at all: nothing written anywhere on the picture.")
        listing = " ".join(f"IMAGE {i} is our VARIANT {i - 1} (file {Path(img).name})."
                           for i, img in enumerate(images[1:], start=2))
        similarity = "\n".join(f"   variant {i}: SSIM {v['ssim']:.2f}, dHash {v['dhash']}"
                               for i, v in enumerate(made, start=1))
        prompt = JUDGE_PROMPT.format(count=len(made), donor_name=Path(donor_jpg).name, variant_list=listing,
                                     lang=_lang_name(lang), ssim_stop=COPY_SSIM_STOP, dhash_stop=COPY_DHASH_STOP,
                                     similarity=similarity, situation=str(card.get("situation") or "").strip(),
                                     hook=str(card.get("contradiction") or "").strip() or "-",
                                     heroes=hero_words, text_rule=text_rule)
        chain = [judge_model()] + ([judge_fallback()] if judge_fallback() else [])

        def ask(what, prompt_, images_, parse):
            """Цепочка «основной -> запасной» для одного вопроса; ответ разобран parse."""
            result = {"ok": False, "model": None, "fallback_used": False}
            started_ask = time.monotonic()
            for i, name in enumerate(chain):
                t0 = time.monotonic()
                try:
                    got = judge(prompt_, images_, name)
                    data, used_tokens = got if isinstance(got, tuple) else (got, None)
                    result = {**parse(data), "ok": True, "model": name, "fallback_used": i > 0, "usage": used_tokens}
                    break
                except _STOP:
                    raise
                except Exception as exc:   # noqa: BLE001 — довесок: без него выбор кодом / «проверьте глазами»
                    result["error"] = f"{name}: {type(exc).__name__}: {str(exc)[:200]}"
                    log(f"{what} {name} не ответил ({str(exc)[:160]})"
                        + (f" — зову запасного {chain[i + 1]}" if i + 1 < len(chain) else ""))
                finally:
                    spent["judge_calls"] += 1
                    spent["judge_seconds"] += time.monotonic() - t0
            result["seconds"] = round(time.monotonic() - started_ask, 1)
            return result

        def read_round():
            """Чтец надписей — тот же основной/запасной на КАЖДЫЙ вызов (картинка или вырезка)."""
            result = {"ok": False, "model": chain[0], "fallback_used": False}
            started_read = time.monotonic()

            # Основной — дважды до запасного: пустой ответ codex через прокси бывает разовым, а
            # запасной claude читает хуже (р2: на одной картинке нашёл 1 надпись из 3).
            read_chain = chain[:1] * 2 + chain[1:]

            def one(prompt_, images_):
                last = None
                for i, name in enumerate(read_chain):
                    t0 = time.monotonic()
                    try:
                        got = judge(prompt_, images_, name)
                        got = got[0] if isinstance(got, tuple) else got
                        if name != chain[0]:
                            result["fallback_used"] = True
                        # claude читает надписи ненадёжно и с картинкой перед глазами (живьём р3,
                        # TAvIqS9VzK0 основным чтецом: «BUSINESS PROPOSAL», «5P», «FROSH F» там, где
                        # нарисовано верно) — его брак букв только «посмотрите глазами».
                        if (name != chain[0] or name.startswith("claude:")) and isinstance(got, dict):
                            got = {**got, "_fallback": True}
                        return got
                    except _STOP:
                        raise
                    except Exception as exc:   # noqa: BLE001
                        last = exc
                    finally:
                        spent["reader_calls"] += 1
                        spent["reader_seconds"] += time.monotonic() - t0
                raise last
            try:
                by_n, info = read_texts(images[1:], one, judge_dir)
                result.update({"ok": True, **info, "by_n": by_n})
            except _STOP:
                raise
            except Exception as exc:   # noqa: BLE001 — без чтения: «буквы не сверены, посмотрите глазами»
                result["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                log(f"чтец надписей не ответил ({str(exc)[:160]})")
            result["seconds"] = round(time.monotonic() - started_read, 1)
            return result

        def parse_judge(data):
            parsed = parse_verdict(data, len(made))
            if not parsed["ok"]:
                raise CoverError("в ответе нет оценок вариантов")
            return parsed

        with ThreadPoolExecutor(max_workers=2) as pool:      # чтец и выбирающий — одновременно
            reading_f = pool.submit(LLM.CP.carry(read_round))
            result = ask("выбирающий", prompt, images, parse_judge)
            reading = reading_f.result()
        if not result.get("ok"):
            log("выбирающий не ответил — выбираю кодом")
        # Номер в ответе — порядковый среди состоявшихся; переводим в номер варианта.
        mapping = {i: v["k"] for i, v in enumerate(made, start=1)}
        read_by_n = reading.pop("by_n", None) or {}
        boxes_by_n = reading.pop("boxes", None) or {}
        doubt_by_n = reading.pop("doubt", None) or {}
        reading["doubt"] = {str(k): v for k, v in doubt_by_n.items()}
        rnd["reading"] = reading
        if result.get("ok"):
            result["best"] = mapping.get(result.get("best"))
            by_n = result.pop("by_n")
            for i, v in enumerate(made, start=1):
                row = by_n.get(i) or {}
                v["score"], v["why"], v["flags"] = row.get("score"), row.get("why") or "", row.get("flags") or []
                v["native"] = native_fixes(row.get("fixes") or [], expected_all) if row else []
        for i, v in enumerate(made, start=1):
            if reading.get("ok") and i in read_by_n:
                v["texts_read"] = read_by_n[i]
                v["letters"] = text_faults({"texts": v["texts_read"], "boxes": boxes_by_n.get(i) or []},
                                           expected, by_code=code_expected)
                v["unread"] = False
                # Прочтение запасным чтецом или с мусором: брак букв по нему — «посмотрите глазами»,
                # а не новый круг (critic р3: хорошую обложку отбраковали по выдумке чтеца).
                v["letters_doubt"] = doubt_by_n.get(i) if any(not f.get("soft") for f in v["letters"]) else None
            else:
                v["texts_read"], v["letters"], v["unread"], v["letters_doubt"] = [], [], True, None
            v["blocking"] = blocking_of(v)
        rnd["judge"] = result

    n = 0
    while True:
        n += 1
        rnd = rounds[n - 1] if len(rounds) >= n else None
        if rnd is None:
            feedback = []
            if rounds and (rounds[-1].get("judge") or {}).get("redo_reason"):
                feedback = [rounds[-1]["judge"]["redo_reason"]]
            if rounds:
                # Поправки нового круга — по браку прошлого, словами генератору.
                bad = {b for v in rounds[-1]["variants"] for b in v.get("blocking") or []}
                for mark, line in (("буквы", "Draw every caption letter for letter exactly as written, with every "
                                             "accent mark, and no other letters."),
                                   ("лишний текст", "Write ONLY the captions given, each once: no repeated words, no "
                                                    "quote marks, no other lettering or signs anywhere."),
                                   ("школьная", "No school uniform on anybody: office, club or university clothes."),
                                   ("логотип", "No logo, watermark or channel name anywhere."),
                                   ("моложе", "Everybody clearly looks an adult of 20+ in face and body."),
                                   ("без цвета", "The previous attempt came out almost black-and-white. Draw it in FULL "
                                                 "VIVID SATURATED COLOUR: coloured skin, hair, clothes, light and "
                                                 "background; nothing greyscale.")):
                    if any(mark in b for b in bad):
                        feedback.append(line)
                if any("not_our_hero" in (v.get("flags") or []) for v in rounds[-1]["variants"] if v.get("file")):
                    feedback.append("Our heroes match their character sheets in build, age look, posture and marks, "
                                    "not only in face and hair.")
                if any(revealing(v) for v in rounds[-1]["variants"] if v.get("file")):
                    feedback.append("Clothes NOT more revealing than in the competitor's thumbnail: the same covered "
                                    "neckline and shoulders as there, no cleavage, no extra bare skin.")
            prompt = build_prompt(card, lang, heroes_by_id, skip_code=by_code, plan=plan, hero_images=hero_images,
                                  feedback=feedback, video_style=data.get("style")) if feedback else base_prompt
            rnd = {"n": n, "feedback": feedback,
                   "prompts": [prompt + "\n\n" + VARIANT_HINTS[k % len(VARIANT_HINTS)] for k in range(VARIANTS)],
                   "variants": [{"key": f"{n}-{k}", "round": n, "k": k,
                                 "raw": f"out/cover_var_{n}_{k}.png"} for k in range(1, VARIANTS + 1)],
                   "judge": None}
            rounds.append(rnd)
            keep_state()                  # промпты записаны ДО покупки: повтор купит ровно их
        buy(rnd)
        finish(rnd)
        keep_state()
        made = [v for v in rnd["variants"] if v.get("file")]
        if not made:
            if n == 1:
                raise CoverError("Генератор не нарисовал ни одного варианта обложки: "
                                 + "; ".join(sorted({v.get("error") or "" for v in rnd["variants"]}))[:500])
            rnd["judge"] = {"ok": False, "error": "круг не нарисован"}
            keep_state()
            break
        if rnd.get("judge") is None:
            run_judge(rnd)
            keep_state()
        d = decide(made, rnd["judge"])
        rnd["decision"] = d
        keep_state()
        if d["redo"] and n <= REDO_ROUNDS:
            why = sorted({b for v in made for b in v.get("blocking") or []})
            log(f"круг {n}: у всех вариантов брак ({'; '.join(why)[:200]}) — ещё один круг из трёх")
            continue
        break

    last = rounds[-1]
    d = last.get("decision") or {}
    if d.get("pick") and not d.get("redo"):
        key, by, eyes = f"{last['n']}-{d['pick']}", d["by"], d["eyes"]
    else:
        # Круг «переделать» тоже с браком (или не нарисован): лучший из всех, пометка «глазами».
        pool = [v for r in rounds for v in r["variants"] if v.get("file")]
        best = min(pool, key=lambda v: (len(v.get("blocking") or []), len(soft_of(v)), -(v.get("score") or -1),
                                        v["round"], v["k"]))
        key, by, eyes = best["key"], (d.get("by") or "judge_score"), True
    log(f"беру вариант {key} ({by})")
    state["usage"] = usage()
    state["done"] = True
    state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return apply_choice(root, state, key, by, eyes=eyes)
