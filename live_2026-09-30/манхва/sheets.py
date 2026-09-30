# -*- coding: utf-8 -*-
"""Листы персонажей: один эталон внешности на весь ролик.

Схема: рисуем лист с несколькими ракурсами на чистом фоне, режем на кропы,
и КАЖДЫЙ кадр получает эти кропы как образцы. Предыдущий кадр образцом
НИКОГДА не становится — цепочка копит искажение, за двадцать кадров лицо уезжает.

Число образцов — регулятор скорости (замер: 10 образцов против 3 это +50%
латентности), поэтому кладём по типу кадра, а не «максимум всегда».
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

import images

# ВАЖНО: слова «character reference sheet» модель понимает как ДИЗАЙН-МАКЕТ и
# рисует его целиком — с именной плашкой, палитрой и рыбным текстом. Проверено:
# в первом прогоне получилось «Elias Thorne, 17 years old» и абзац lorem ipsum.
# Поэтому просим ровно то, что нужно: одну фигуру на белом, без макета.
# ЛИСТ — ЭТО ПОСТОЯННЫЙ ЭТАЛОН, а не кадр из истории. На листах прошлого прогона
# у героя был нарисован СИНЯК — временная травма одной сцены. Дальше этот синяк
# то появлялся, то исчезал по всему ролику (31 замечание из 132 проверенных
# кадров), потому что модель то копировала его с эталона, то нет.
# Всё временное — травмы, грязь, слёзы, снег — приходит из паспорта СЦЕНЫ.
# На листе персонаж всегда чистый, здоровый и в нейтральном состоянии.
SHEET_STYLE = ("a single isolated character standing alone on a plain flat white background, "
               "studio lighting, high detail anime manhwa illustration. "
               "NEUTRAL REFERENCE STATE: clean, unhurt and tidy — no bruises, no scars, "
               "no blood, no bandages, no dirt, no tears, no wet hair, no snow, no torn "
               "clothing. Exactly one hairstyle, the one written above, never an alternative. "
               "ABSOLUTELY NO text, NO letters, NO name plate, NO caption, NO colour palette "
               "swatches, NO multiple panels, NO collage, NO grid, NO frame, NO border, "
               "NO watermark, NO logo. One figure only, nothing else in the image.")

# Стиль для листов ПРЕДМЕТОВ и МЕСТ — без единого слова про лица, глаза, руки,
# фигуры и позы. Общая STYLE_LINE ролика описывает построение анимешного лица
# («large glossy eyes…», «main character occupies 40-60% of the frame height»), и
# генератор честно исполняет её и там, где людей быть не должно. Замер Codex
# 09.09 (manhwa-lab, results/native24_asset_candidates_01): 3 из 3 листов
# предметов получили руки или людей — ручка в руке, телефон в руке, мальчик
# рядом с карандашом — именно потому, что общая строка стиля содержала
# face/eyes/hands/figures. Слова про людей из такой строки надо вырезать, а не
# перебивать запретом: запрет «no people» в PLACE_STYLE уже стоял и не помог.
# По началу слова, а не по подстроке: «face» сидит внутри «surface» и выкидывал
# фразу про cel shading «two tones per surface»; а «eyebrows» при точном слове
# «eye» проскакивал.
_PEOPLE_WORDS = re.compile(
    # Слово целиком с обычными формами, а не «начало слова»: `brow\w*` ловил
    # «brown», `hand\w*` — «hand-drawn», `person\w*` — «personal».
    r"\b(faces?|facial|eyes?|eyebrows?|eyelashes?|brows?|noses?|mouths?|lips?|cheeks?|"
    r"skin|blush|hair|hairstyles?|hairline|characters?|figures?|persons?|people|"
    r"poses?|posed|posing|hands?(?!-)|body|bodies|subjects?|protagonists?|"
    r"expressions?|gaze)\b", re.IGNORECASE)

OBJECT_SHEET_STYLE = ("No people, no characters, no figures, no faces, no hands, no body parts, "
                      "no mannequins, no silhouettes — the object or the place is the only "
                      "thing in the picture. Crisp dark ink outline on every edge, one warm "
                      "key light.")


def plain(value) -> str:
    """Любое поле модели — строкой. Словарь или список не должны ронять ролик.

    20.09.2026 итальянский ролик упал на `'dict' object has no attribute 'strip'`:
    модель вернула описание объектом вместо строки, и вся пачка кадров встала.
    Берём осмысленный текст, а не repr: у словаря — поля text/en/description,
    у списка — элементы через точку с запятой.
    """
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, dict):
        for field in ("text", "en", "value", "description", "look_en", "appearance_en"):
            if isinstance(value.get(field), str) and value[field].strip():
                return value[field]
        return "; ".join(plain(v) for v in value.values() if v)
    if isinstance(value, (list, tuple)):
        return "; ".join(plain(v) for v in value if v)
    return str(value)


def soften_sheet(prompt: str, level: int) -> str:
    """Лестница мягкости ДЛЯ ЛИСТА: смягчаем слова, но не режем блоки.

    Зачем своя. Лестница кадра (frameprompt.soften) на втором шаге выбрасывает блоки,
    которых нет в листе, и от листа остаются первые два абзаца — а лист задаёт внешность
    героя на весь ролик, и урезанный лист хуже отказа.

    Зачем вообще. Без лестницы отказ генератора по содержанию был ОКОНЧАТЕЛЬНЫМ: повтор
    того же текста запрещён квитанцией (чтобы не платить дважды), и один непослушный лист
    останавливал ролик навсегда. 20.09 так стояли два ролика пачки: «Не готовы листы
    героев: zach/base» повторялось часами, а на деле лист просто ни разу не перерисовали.
    """
    if level <= 0:
        return prompt
    import frameprompt as FP
    text = FP._apply(FP._SOFT_1 if level == 1 else FP._SOFT_2, prompt)
    if level >= 2:
        # Внешность из скелета может звучать резко («shirt half unbuttoned», «slim curvy»);
        # на втором шаге заменяем её описание на нейтральное, остальное оставляем.
        text = _SCHOOL_SAFE.sub("modest everyday clothing, fully covered", text)
    if level >= 3:
        text += ("\n\nKeep it calm and fully clothed: no injury detail, no blood, "
                 "no revealing or partially removed clothing, no suggestive posing.")
    return text


# Приметы, из-за которых фильтр генератора чаще всего отбивает лист героя-подростка.
_SCHOOL_SAFE = re.compile(
    r"\b(half[- ]?unbuttoned|unbuttoned|shirtless|bare[- ]?chested|topless|"
    r"curvy|voluptuous|busty|revealing|tight[- ]?fitting|short skirt|lingerie|underwear)\b",
    re.IGNORECASE)


def object_style_line(style_line: str) -> str:
    """Строка стиля ролика без фраз про людей — для листов предметов и мест.

    Режем STYLE_LINE по запятым и выбрасываем каждую фразу, где есть слово про
    лицо, тело, позу или героя; остаток (манера линии, заливка, свет, формат)
    остаётся общим с кадрами, чтобы лист и кадр были в одной манере.
    """
    keep = []
    for clause in (style_line or "").split(", "):
        if not clause.strip() or _PEOPLE_WORDS.search(clause):
            continue
        keep.append(clause.strip())
    return (", ".join(keep) + ". " + OBJECT_SHEET_STYLE) if keep else OBJECT_SHEET_STYLE


# ОДНА генерация на героя, потом нарезка кодом.
#
# Раньше face, front и three_quarter рисовались ТРЕМЯ независимыми вызовами —
# и получались три слегка разных человека: у портрета одна длина волос, у фигуры
# другая, одежда третья. Поэтому подкладывать в кадр фигуру в рост было
# бесполезно: она спорила с портретом. Замер по 156 кадрам подряд: одежда плыла
# в 30% кадров, причёска в 17%, и почти все сбои — там, где лица в кадре нет и
# зацепиться можно только за фигуру.
#
# Приём из боевых soft.py (compose_character_identity_board) и softveo3.py
# (create_character_reference_crops): рисуем ОДИН лист с тремя видами рядом и
# режем его кодом. Виды согласованы по построению — это один рисунок, а не три.
SHEET_LAYOUT = (
    "ONE character reference sheet of ONE person, three views of the SAME person "
    "side by side on a plain flat white background, evenly spaced, all three the same "
    "height on the canvas. "
    "LEFT THIRD: full body, seen from the front, standing straight, arms relaxed, head to toe. "
    "MIDDLE THIRD: full body of the same person, three-quarter turn, head to toe. "
    "RIGHT THIRD: head and shoulders only, the same face, neutral expression, facing the "
    "camera, drawn large and clearly readable. "
    "All three views must be the SAME person with identical face, identical hairstyle and "
    "identical hair length, identical clothes and identical proportions."
)

def split_columns(path: str):
    """Находит колонки-фигуры по вертикальным просветам, а не по долям ширины.

    Долями резать нельзя: модель ставит на лист то три вида, то четыре, и в
    фиксированный кроп попадают ДВЕ фигуры сразу — а кроп с двумя людьми
    заставляет генератор рисовать двоих в кадре.
    """
    from PIL import Image
    import numpy as np
    im = Image.open(path).convert("L")
    a = np.asarray(im, dtype=float)
    h, w = a.shape
    ink = (a < 235).sum(axis=0) / float(h)      # доля «нарисованного» в столбце
    empty = ink < 0.01
    cols, start = [], None
    for x in range(w):
        if not empty[x] and start is None:
            start = x
        elif empty[x] and start is not None:
            if x - start > w * 0.05:            # огрызки уже 5% ширины не колонка
                cols.append((start, x))
            start = None
    if start is not None and w - start > w * 0.05:
        cols.append((start, w))
    # Лист задуман как ТРИ колонки: две фигуры в рост и портрет справа. У GPT Image 2.5 (17.09.2026)
    # меч, плащ или дым часто дотягиваются до соседа, и белого просвета нет: лист Веррена распознался
    # одной колонкой (этап упал), у Пепельного Короля фигура слиплась с портретом, и в эталон «лицо»
    # молча ушли два человека. Тогда самую широкую колонку режем по самому пустому месту в её середине.
    k = max(1, int(w * 0.006))
    smooth = np.convolve(ink, np.ones(k) / k, mode="same")
    while 0 < len(cols) < 3:
        i = max(range(len(cols)), key=lambda j: cols[j][1] - cols[j][0])
        a0, a1 = cols[i]
        if a1 - a0 < w * 0.25:
            break
        lo, hi = a0 + int((a1 - a0) * 0.15), a1 - int((a1 - a0) * 0.15)
        cut = lo + int(np.argmin(smooth[lo:hi]))
        cols[i:i + 1] = [(a0, cut), (cut, a1)]
    return cols, (w, h)

# Порядок важен: лицо первым — оно решает узнаваемость и уходит в кадр первым эталоном.
VIEWS = [
    ("face", "close-up of the head and shoulders only, neutral expression, facing camera"),
    ("front", "seen from the front, head to toe, standing straight, arms relaxed"),
    ("three_quarter", "seen from a three-quarter angle, head to toe, slight turn"),
    ("profile", "seen from the side, head to toe"),
]


@dataclass
class Sheet:
    cid: str
    name: str
    files: Dict[str, str]          # вид -> путь
    board: str = ""                # цельный лист до нарезки
    status: str = "ждёт"
    error: str = ""

    def refs(self, limit: int = 3) -> List[images.Ref]:
        """Образцы для кадра. Лицо идёт первым — оно важнее всего."""
        order = ["face", "front", "three_quarter", "profile"]
        out = []
        for v in order:
            p = self.files.get(v)
            if p and os.path.exists(p) and os.path.getsize(p) > 4096:
                out.append(images.Ref(name=f"{self.cid}_{v}.png",
                                      data_uri=images.data_uri(p)))
            if len(out) >= limit:
                break
        return out


def build(characters: List[dict], out_dir: str, style_line: str,
          views: int = 4, seed_base: int = 7000, locks: Optional[Dict[str, str]] = None,
          log=print) -> Dict[str, Sheet]:
    """Один герой = ОДНА генерация листа, дальше нарезка кодом."""
    os.makedirs(out_dir, exist_ok=True)
    sheets: Dict[str, Sheet] = {}
    jobs = []
    for ci, c in enumerate(characters):
        cid = c.get("id") or f"char{ci}"
        lock = plain((locks or {}).get(cid)).strip()
        look = plain(c.get("appearance_en")).strip()
        if lock:
            look = f"{lock}. {look}"
        sh = Sheet(cid=cid, name=c.get("name", cid), files={})
        sh.board = os.path.join(out_dir, f"{cid}_board.png")
        sheets[cid] = sh
        jobs.append({
            "key": cid, "path": sh.board,
            "prompt": f"{look}\n\n{SHEET_LAYOUT}\n\n{SHEET_STYLE} {style_line}",
            "seed": seed_base + ci, "aspect": "16:9", "soften": soften_sheet,
        })

    log(f"  листов: {len(jobs)} — по ОДНОЙ генерации на героя, ракурсы режутся кодом")
    res = images.generate_many(jobs, threads=8,
                               on_done=lambda r: log(f"    {r['key']}: {r['status']}"))

    from PIL import Image
    for cid, sh in sheets.items():
        if not (sh.board and os.path.exists(sh.board) and os.path.getsize(sh.board) > 4096):
            sh.status, sh.error = "провал", "лист не нарисовался"
            continue
        try:
            im = Image.open(sh.board).convert("RGB")
            w, h = im.size
            cols, _ = split_columns(sh.board)
            if len(cols) < 2:
                raise RuntimeError(f"на листе распозналось {len(cols)} колонок")
            # Портрет — самая ПРАВАЯ колонка: так задано разметкой листа.
            # Фигуры в рост — все остальные, берём первую и вторую.
            face_col = cols[-1]
            body_cols = cols[:-1] or [cols[0]]
            pad = int(w * 0.005)
            def cut(col, name, bottom=1.0):
                x0 = max(0, col[0] - pad); x1 = min(w, col[1] + pad)
                c = im.crop((x0, 0, x1, int(h * bottom)))
                pth = os.path.join(out_dir, f"{cid}_{name}.png")
                c.save(pth)
                sh.files[name] = pth
            cut(face_col, "face", 0.96)
            cut(body_cols[0], "front")
            cut(body_cols[1] if len(body_cols) > 1 else body_cols[0], "three_quarter")
            sh.status = "готово"
            log(f"    {cid}: колонок на листе {len(cols)}, нарезано 3 вида")
        except Exception as e:
            sh.status, sh.error = "провал", f"нарезка не вышла: {type(e).__name__}: {e}"

    # Лист уходит ВО ВСЕ 493 кадра, поэтому испорченный эталон отравляет весь
    # ролик разом. Проверяем теми же зондами, что и кадры: секунды, ноль денег.
    try:
        import qa_clip
        boards = [(cid, sh.board) for cid, sh in sheets.items()
                  if sh.board and os.path.exists(sh.board)]
        if boards:
            flags = qa_clip.content_flags([b for _, b in boards])
            for (cid, _), fl in zip(boards, flags):
                why = []
                if fl["text"]["i"] == 0 and fl["text"]["p"] > 0.6:
                    why.append("похоже на надписи")
                if fl["people"]["i"] >= 3:
                    why.append("на листе больше трёх фигур")
                if why:
                    log(f"    ! лист {cid}: {', '.join(why)} — посмотри глазами")
    except Exception as e:
        log(f"    (проверка листов не отработала: {type(e).__name__}: {e})")

    bad = [r for r in res if r.get("status") == "ошибка"]
    for cid, sh in sheets.items():
        if sh.status == "провал":
            log(f"  ! лист не получился: {sh.name} — {sh.error}")
    if bad:
        log(f"  не нарисовалось: {len(bad)} — {[b['key'] for b in bad][:5]}")
    return sheets


def refs_for_frame(frame, sheets: Dict[str, Sheet], budget: int = 10) -> List[images.Ref]:
    """Сколько образцов дать этому кадру.

    Пусто в кадре — образцы не нужны вовсе (кадр рисуется вдвое быстрее).
    Один герой — три образца. Двое и больше — делим бюджет.
    """
    who = [c for c in (frame.in_frame or []) if c in sheets]
    if not who:
        return []
    if len(who) == 1:
        return sheets[who[0]].refs(limit=3)
    per = max(2, budget // len(who))
    out: List[images.Ref] = []
    focus = getattr(frame, "focus", "") or ""
    order = ([focus] if focus in sheets else []) + [c for c in who if c != focus]
    for cid in order:
        if cid in sheets:
            out.extend(sheets[cid].refs(limit=per))
        if len(out) >= budget:
            break
    return out[:budget]


# --- листы под КАЖДЫЙ костюм ---------------------------------------------------
# Замер: в 4 сценах из 6 костюм из паспорта не был нарисован НИ В ОДНОМ кадре —
# побеждала одежда, нарисованная на листе персонажа. Текст проигрывает картинке
# примерно в двух случаях из трёх, и никакие формулировки этого не переломили:
# «cream cardigan» из паспорта против камель-пальто с листа — пальто выигрывало
# все 26 кадров подряд.
#
# Значит спорить не надо. У каждого костюма должен быть свой лист. Костюмов на
# ролик немного: герой ходит в школьной форме, в зимней куртке и в фартуке —
# три листа вместо одного, и картинка перестаёт спорить с текстом.

def costume_key(text: str) -> str:
    """Короткий устойчивый ключ костюма, чтобы одинаковые не рисовать дважды."""
    import hashlib, re
    t = re.sub(r"[^a-z ]+", " ", (text or "").lower())
    t = " ".join(sorted(set(w for w in t.split() if len(w) > 3)))
    return hashlib.md5(t.encode()).hexdigest()[:8] if t else "base"


def build_variants(characters: List[dict], scenes, out_dir: str, style_line: str,
                   locks: Optional[Dict[str, str]] = None, seed_base: int = 7000,
                   log=print) -> Dict[str, Dict[str, Sheet]]:
    """Возвращает {cid: {ключ костюма: Sheet}}. Один лист на пару герой+костюм."""
    os.makedirs(out_dir, exist_ok=True)
    by_char = {c.get("id"): c for c in characters if c.get("id")}

    wanted = {}          # (cid, key) -> текст костюма
    for sc in (scenes or {}).values():
        for cid, cost in (getattr(sc, "costume", None) or {}).items():
            if cid in by_char and plain(cost).strip():
                wanted[(cid, costume_key(plain(cost)))] = plain(cost).strip()
    for cid in by_char:                       # базовый лист есть у всех
        wanted.setdefault((cid, "base"), "")

    jobs, index = [], {}
    for n, ((cid, key), cost) in enumerate(sorted(wanted.items())):
        c = by_char[cid]
        lock = plain((locks or {}).get(cid)).strip()
        look = plain(c.get("appearance_en")).strip()
        head = f"{lock}. {look}" if lock else look
        if cost:
            head += (f"\n\nWEARING, in every one of the three views, exactly this and "
                     f"nothing else: {cost}")
        sh = Sheet(cid=cid, name=c.get("name", cid), files={})
        sh.board = os.path.join(out_dir, f"{cid}__{key}_board.png")
        index.setdefault(cid, {})[key] = sh
        jobs.append({"key": f"{cid}/{key}", "path": sh.board,
                     "prompt": f"{head}\n\n{SHEET_LAYOUT}\n\n{SHEET_STYLE} {style_line}",
                     "seed": seed_base + n, "aspect": "16:9", "soften": soften_sheet})

    log(f"  листов с костюмами: {len(jobs)} "
        f"({len(index)} героев, в среднем {len(jobs)//max(1,len(index))} костюма на героя)")
    images.generate_many(jobs, threads=8,
                         on_done=lambda r: log(f"    {r['key']}: {r['status']}"))

    from PIL import Image
    for cid, byk in index.items():
        for key, sh in byk.items():
            if not (os.path.exists(sh.board) and os.path.getsize(sh.board) > 4096):
                sh.status, sh.error = "провал", "лист не нарисовался"
                continue
            try:
                im = Image.open(sh.board).convert("RGB")
                w, h = im.size
                cols, _ = split_columns(sh.board)
                if len(cols) < 2:
                    raise RuntimeError(f"колонок распозналось {len(cols)}")
                pad = int(w * 0.005)
                def cut(col, name, bottom=1.0):
                    c = im.crop((max(0, col[0] - pad), 0,
                                 min(w, col[1] + pad), int(h * bottom)))
                    pth = os.path.join(out_dir, f"{cid}__{key}_{name}.png")
                    c.save(pth)
                    sh.files[name] = pth
                cut(cols[-1], "face", 0.96)
                cut(cols[0], "front")
                cut(cols[1] if len(cols) > 2 else cols[0], "three_quarter")
                sh.status = "готово"
            except Exception as e:
                sh.status, sh.error = "провал", f"нарезка: {type(e).__name__}: {e}"
    ok = sum(1 for b in index.values() for x in b.values() if x.status == "готово")
    log(f"  листов с костюмами готово: {ok} из {len(jobs)}")
    return index


def pick_variant(variants: Dict[str, Dict[str, Sheet]], cid: str, scene) -> Optional[Sheet]:
    """Лист под тот костюм, который у героя в ЭТОЙ сцене."""
    byk = (variants or {}).get(cid) or {}
    if not byk:
        return None
    cost = ((getattr(scene, "costume", None) or {}).get(cid) or "")
    sh = byk.get(costume_key(cost)) if cost else None
    if sh and sh.status == "готово":
        return sh
    base = byk.get("base")
    if base and base.status == "готово":
        return base
    for x in byk.values():
        if x.status == "готово":
            return x
    return None
