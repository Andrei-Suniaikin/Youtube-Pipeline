# -*- coding: utf-8 -*-
"""Оркестратор: входной файл -> готовый mp4.

Состояние проекта СЧИТАЕТСЯ по файлам в папке, а не хранится полем — приём
из VideoStudio: рассинхронизировать нечего, потому что синхронизировать нечего.
Каждый шаг умеет пропустить уже сделанное, поэтому повторный запуск достраивает,
а не переделывает.
"""
from __future__ import annotations

import json
import functools
import glob
import hashlib
import base64
import secrets
from pathlib import Path
import shutil
import os
import re
import sys
import time
from dataclasses import asdict
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import beats as B
import claude_pool as CP
import state as S
import storyboard as SB
import rewrite as RW
import visuals as V
import sheets as SH
import places as PL
import scene as SC
import frameprompt as FP
import repair as RP
import memory as MEM
import overlay as OV
import images as IM
import voice as VO
import voice_lumean as LU
import languages as LG
import align as AL
import assemble as AS
import denoise as DN
import render_remote as RR
import translate as TR

STYLE_LINE = "modern Korean webtoon anime illustration, clean crisp dark ink lineart of even weight outlining every character, cel shading with exactly two tones per surface - flat base colour plus one hard-edged shadow, no gradient blending on skin or clothing, anime face construction: large glossy eyes with one bright specular highlight and gradient iris, thin sharp eyebrows, nose drawn as one short stroke, small simple mouth, flat skin tone with a single blush tone, hair as solid colour blocks with hard-edged glossy highlight bands, saturated clean local colours, one warm key light plus a rim light separating the character from the background, hard-edged cast shadows, simplified flat-shaded background with reduced detail kept softly behind the subject, main character occupies 40-60% of the frame height, smooth flat digital fills, clean sharp edges, full-bleed horizontal 16:9 composition filling the entire canvas edge to edge"

# по итогам стенда 07.09.2026 (7 моделей, 2 раунда, замеры в bench/report.json)
# Модель текстовых этапов (скелет, текст, паспорта, раскадровка): MANGA_TEXT_MODEL, по умолчанию Codex.
# «claude:<модель>» идёт через claude CLI (токен CLAUDE_CODE_OAUTH_TOKEN в окружении службы).
# 17.09.2026: у старого CLI на VPS (2.1.40) «sonnet» = Sonnet 4.5, поэтому имя модели указывать полностью.
MODEL_TEXT = os.environ.get("MANGA_TEXT_MODEL", "").strip() or "codex:gpt-5.6-sol"
if not re.fullmatch(r"(codex|claude):[A-Za-z0-9._-]+", MODEL_TEXT):
    raise ValueError("MANGA_TEXT_MODEL: укажите «codex:<модель>» или «claude:<модель>», например claude:claude-sonnet-5")
MODEL_SKELETON = MODEL_TEXT    # 746/700 слов, чистая проза, имена верны (замер на codex:gpt-5.6-sol)
MODEL_EXPAND = MODEL_TEXT
MODEL_VISUALS = MODEL_TEXT
# MANGA_TEXT_MODELS — несколько моделей текста через запятую (например
# «codex:gpt-5.6-sol,claude:claude-sonnet-5»): новому ролику достаётся та, которой сейчас
# пишут текст меньше роликов, и она закрепляется за роликом (text/model.json). Claude-модель
# пропускается, если неделя подписки (CLAUDE_CODE_OAUTH_TOKEN) занята больше
# MANGA_CLAUDE_ROUTE_STOP (по умолчанию 0.8) или остаток не прочитался. Не задано — MANGA_TEXT_MODEL.
TEXT_MODELS = [m.strip() for m in os.environ.get("MANGA_TEXT_MODELS", "").split(",") if m.strip()]
for _m in TEXT_MODELS:
    if not re.fullmatch(r"(codex|claude):[A-Za-z0-9._-]+", _m):
        raise ValueError(f"MANGA_TEXT_MODELS: «{_m}» — нужно «codex:<модель>» или «claude:<модель>» через запятую")
try:
    CLAUDE_ROUTE_STOP = float(os.environ.get("MANGA_CLAUDE_ROUTE_STOP", "").strip() or "0.8")
except ValueError:
    raise ValueError("MANGA_CLAUDE_ROUTE_STOP: доля недели от 0 до 1, например 0.8") from None
# Порог пятичасового окна подписки: выше — новые ролики пишет альт, а не Claude.
try:
    CLAUDE_5H_STOP = float(os.environ.get("MANGA_CLAUDE_5H_STOP", "").strip() or "0.85")
except ValueError:
    raise ValueError("MANGA_CLAUDE_5H_STOP: доля окна от 0 до 1, например 0.85") from None
TEXT_PHASE_HOURS = 24   # ролик с model.json моложе суток и без раскадровки считается «пишущим текст»
# Паспорт внешности по картинке листа умеет только Codex (judge._codex с вложениями).
VISION_LOCKS = MODEL_VISUALS.startswith("codex:")
# MANGA_VISION_MODEL — кто пишет паспорт внешности по картинке листа, когда текст пишет claude.
# Пусто — codex:gpt-5.6-sol, если у службы есть дом Codex (CODEX_HOME) или альт-ключ; иначе шаг
# пропускается, как раньше.
# MANGA_LUMEAN_STRICT=on — язык без шаблона Lumean останавливает ролик, а не озвучивает его edge.
LUMEAN_STRICT = os.environ.get("MANGA_LUMEAN_STRICT", "").strip().lower() in {"1", "on", "true", "yes", "да"}
VISION_MODEL = os.environ.get("MANGA_VISION_MODEL", "").strip() or (
    "codex:gpt-5.6-sol" if (os.environ.get("CODEX_HOME") or os.environ.get("MANGA_ALT_API_KEY")) else "")
if VISION_MODEL and not re.fullmatch(r"(codex|claude):[A-Za-z0-9._-]+", VISION_MODEL):
    raise ValueError("MANGA_VISION_MODEL: укажите «codex:<модель>», например codex:gpt-5.6-sol")
# MANGA_QUOTE_BUBBLES=off: реплика не показывается отдельным кадром «размытая панель + облако с текстом»
# (17.09.2026 владельцу не нравится); панель стоит всё своё время. По умолчанию on — как было.
QUOTE_BUBBLES = os.environ.get("MANGA_QUOTE_BUBBLES", "on").strip().lower()
if QUOTE_BUBBLES not in ("on", "off"):
    raise ValueError("MANGA_QUOTE_BUBBLES: on или off")
# MANGA_QUOTE_MODE — как показывать реплику кадра (поле quote):
#   bubble  — отдельный клип «размытая панель + облако, нарисованное кодом» (как было);
#   panel   — облако рисует генератор ВНУТРИ кадра (реплики до 90 знаков);
#   closeup — второй кадр: крупный план говорящего (реплики до 90 знаков, говорящий с листом);
#   mixed   — короткая (до MANGA_QUOTE_PANEL_MAX) → panel, длиннее → closeup, иначе panel до 90;
#   off     — ни облака, ни второго кадра.
# Не задан: MANGA_QUOTE_BUBBLES=off → off, иначе bubble.
QUOTE_MODES = ("bubble", "panel", "closeup", "mixed", "off")
QUOTE_MAX = 90
QUOTE_MODE = os.environ.get("MANGA_QUOTE_MODE", "").strip().lower() or ("off" if QUOTE_BUBBLES == "off" else "bubble")
if QUOTE_MODE not in QUOTE_MODES:
    raise ValueError("MANGA_QUOTE_MODE: bubble, panel, closeup, mixed или off")
_panel_max = os.environ.get("MANGA_QUOTE_PANEL_MAX", "").strip() or "60"
if not _panel_max.isdigit() or not 1 <= int(_panel_max) <= QUOTE_MAX:
    raise ValueError(f"MANGA_QUOTE_PANEL_MAX: число знаков от 1 до {QUOTE_MAX}")
QUOTE_PANEL_MAX = int(_panel_max)
CLOSEUP_MIN_SECONDS = 3.0   # крупный план говорящего идёт в сборку только на кадре не короче

# ВРЕЗКА — вторая картинка внутри кадра хука: сначала общий план, потом лицо крупно или
# деталь. Даёт вдвое больше картинок на первых секундах, не трогая текст и озвучку.
# MANGA_EXTRA_FRAMES=0 — выключить.
EXTRA_FRAMES = int(os.environ.get("MANGA_EXTRA_FRAMES", "14") or 0)
EXTRA_MIN_REAL = 1.8        # короче этого кадр не делим: врезка мелькнёт и раздражает
QUOTE_ROUND = 30   # номер круга пересдачи в frames/.repair для перерисовки кадра с облаком

# MANGA_TEXT_THREADS — сколько текстовых вызовов модели идут одновременно внутри шагов
# «Паспорта сцен» и «Раскадровка» ОДНОГО ролика (паспорта сцен, куски раскадровки,
# дозаполнение, паспорта воспоминаний и их бланк). Рядом с паспортами сцен идут ещё
# два разовых вызова — рост и паспорт внешности, поэтому на шаге «Паспорта сцен» бывает
# до N+2. Не задано — как было: паспорта 8, раскадровка 4, дозаполнение 2, воспоминания
# по одной сцене. Замер 18.09 (ролик 70 мин, 771 кадр, 14 сцен): раскадровка при 4 потоках
# ~67 мин, упирается в число потоков; при 14 — в самую длинную сцену (её куски идут
# друг за другом ради стыка). Каждый вызов Claude — отдельный процесс CLI ~0,2–0,3 ГБ
# памяти; при MANGA_PARALLEL_VIDEOS=2 одновременно до 2×(N+2) процессов.
_tt = os.environ.get("MANGA_TEXT_THREADS", "").strip()
if _tt and not (_tt.isdigit() and 1 <= int(_tt) <= 32):
    raise ValueError(f"MANGA_TEXT_THREADS: целое число от 1 до 32, получено {_tt!r}")
TEXT_THREADS = int(_tt) if _tt else None


def text_threads(default: int) -> int:
    """Потоки текстового шага: MANGA_TEXT_THREADS, а без неё — прежнее число этого места."""
    return TEXT_THREADS or default


def speaker_offscreen(fd: dict) -> bool:
    """Говорящий известен, но его нет среди нарисованных в кадре."""
    by = fd.get("quote_by") or ""
    return bool(by) and by not in (fd.get("in_frame") or [])


def quote_kind(fd: dict, speakers, mode: Optional[str] = None, panel_max: Optional[int] = None) -> str:
    """Как показать реплику этого кадра: "" | "bubble" | "panel" | "closeup".
    speakers — id героев, чей крупный план в этом кадре можно собрать (лист есть,
    герой не отсутствует в воспоминании; см. Project.quote_plan)."""
    mode = QUOTE_MODE if mode is None else mode
    panel_max = QUOTE_PANEL_MAX if panel_max is None else panel_max
    quote = (fd.get("quote") or "").strip()
    if not quote or mode == "off":
        return ""
    if mode == "bubble":
        return "bubble"
    by = fd.get("quote_by") or ""
    # Кадр уже крупный план говорящего: второй такой же — 4 единицы без смены картинки.
    already_close = (fd.get("shot_size") or "") in ("close", "extreme_close") and by in (fd.get("in_frame") or [])
    closeup_ok = by in speakers and not already_close
    if mode == "panel":
        return "panel" if len(quote) <= QUOTE_MAX else ""
    if mode == "closeup":
        return "closeup" if len(quote) <= QUOTE_MAX and closeup_ok else ""
    # mixed
    if len(quote) <= panel_max:
        # говорящий за кадром — лучше показать его самого, чем облако с хвостиком к краю
        return "closeup" if speaker_offscreen(fd) and closeup_ok else "panel"
    if closeup_ok:
        return "closeup"
    return "panel" if len(quote) <= QUOTE_MAX else ""


def _job_idx(key: str) -> str:
    """Номер кадра из ключа задания. Ключ бывает frame_00007, frame_00007_speaker
    и frame_00007_extra — простое int() по последнему куску спотыкалось о слово."""
    import re as _re
    m = _re.search(r"(\d{4,})", key or "")
    return str(int(m.group(1))) if m else (key or "")


def is_derived_file(name: str) -> bool:
    """Производная картинка кадра, а не кадр раскадровки:
    frames/NNNNN_speaker.png — крупный план говорящего, frames/NNNNN_extra.png — врезка."""
    return os.path.splitext(os.path.basename(name))[0].endswith(("_speaker", "_extra"))


# прежнее имя оставлено: им пользуются другие модули
is_speaker_file = is_derived_file


def _preflight_jobs(jobs) -> List[str]:
    """Замечания к заданиям до первой покупки: пустые эталоны, лишние вложения, короткий промпт."""
    pf, miss_refs = [], 0
    for j in jobs:
        for r in (j.get("refs") or []):
            if not r.data_uri or len(r.data_uri) < 5000:
                miss_refs += 1
        if len(j.get("refs") or []) > IM.MAX_REFS:
            pf.append(f"{j['key']}: приложено {len(j['refs'])} картинок, предел {IM.MAX_REFS}")
        if len(j.get("prompt") or "") < 200:
            pf.append(f"{j['key']}: промпт подозрительно короткий")
    if miss_refs:
        pf.append(f"пустых эталонов в заданиях: {miss_refs}")
    return pf


def _stop_on_preflight(pf: List[str]) -> None:
    if pf:
        log("ПРОВЕРКА ПЕРЕД ПРОГОНОМ нашла проблемы:")
        for x in pf[:12]:
            log(f"  ! {x}")
        if not os.environ.get("MANGA_IGNORE_PREFLIGHT"):
            # Замечания идут В ТЕКСТ ошибки: в очереди видно только её, а журнал службы к тому
            # времени уже уехал. 20.09 ролик трижды вставал с «почини перечисленное», и что
            # именно чинить (не было чертежа у шести сцен) пришлось искать в journalctl.
            what = "; ".join(pf[:5]) + (f" и ещё {len(pf) - 5}" if len(pf) > 5 else "")
            raise RuntimeError(
                f"прогон остановлен до первой генерации: {what}. Почини перечисленное "
                "или запусти с MANGA_IGNORE_PREFLIGHT=1, если знаешь, что делаешь")
    log("проверка перед прогоном: замечаний нет")
                                        # ("Not logged in - Please run /login"), codex работает.
                                        # Замер: паспорт сцены 25 с, JSON валидный с первой попытки.


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _file_sha256(path) -> Optional[str]:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


# Код и библиотеки проверок кадров входят во входы отчёта step_qa: другой код
# или другая версия cv2/OCR — другой отчёт. Код читается при загрузке модуля,
# то есть ровно тот, что работает в процессе.
_QA_CODE_SHA256 = {name: _file_sha256(os.path.join(os.path.dirname(os.path.abspath(__file__)), name))
                   for name in ("pipeline.py", "qa.py", "qa_style.py", "qa_clip.py")}


@functools.lru_cache(maxsize=None)
def _qa_libs() -> dict:
    import cv2
    import numpy
    from importlib import metadata
    libs = {"cv2": cv2.__version__, "numpy": numpy.__version__}
    for name in ("rapidocr_onnxruntime", "onnxruntime", "open_clip_torch", "torch", "Pillow"):
        try:
            libs[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            libs[name] = None
    return libs


def collapse_linebreaks(t: str) -> str:
    """Codex часто отдаёт по предложению на строку с markdown-переносом.
    Схлопываем в обычные абзацы: для озвучки и нарезки нужна проза."""
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"(?<![\n.!?…:])\n(?!\n)", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    # предложение-на-строку -> абзацы по 4-6 предложений
    lines = [l.strip() for l in t.split("\n") if l.strip()]
    if len(lines) > 8 and sum(1 for l in lines if l.count(".") <= 1) > len(lines) * 0.7:
        out, buf = [], []
        for l in lines:
            buf.append(l)
            if len(buf) >= 5:
                out.append(" ".join(buf)); buf = []
        if buf: out.append(" ".join(buf))
        t = "\n\n".join(out)
    return re.sub(r"[ ]{2,}", " ", t).strip()



def _scene_bounds(text: str, sk) -> List[int]:
    """Смещения начал сцен в собранном тексте.

    Текст сцен склеивается, а потом переносы схлопываются — поэтому смещения
    из скелета уже не годятся, и границу приходится искать по содержимому.
    Ищем по нормализованному тексту (пробелы схлопнуты), чтобы схлопывание
    переносов не мешало, и переводим найденное обратно в исходные смещения.
    """
    norm_chars, back = [], []
    prev_space = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if prev_space:
                continue
            norm_chars.append(" ")
            back.append(i)
            prev_space = True
        else:
            norm_chars.append(ch)
            back.append(i)
            prev_space = False
    norm = "".join(norm_chars)

    bounds = [0]
    cur = 0
    for sc in sk.scenes[1:]:
        probe = " ".join((sc.text or "").split())[:70]
        pos = norm.find(probe, cur) if len(probe) >= 20 else -1
        if pos < 0 and len(probe) >= 40:            # текст мог быть слегка поправлен
            pos = norm.find(probe[:40], cur)
        if pos < 0:
            # Молчать здесь нельзя: не найденная граница означает, что кадры
            # уедут в чужую декорацию, а по отчёту всё будет выглядеть исправно.
            raise RuntimeError(
                f"границу сцены {sc.num} не нашли в собранном тексте. "
                f"Скорее всего в скелете нет развёрнутого текста сцен — "
                f"он лежит в text/expand_report.json, а не в skeleton.json")
        cur = pos + 1
        bounds.append(back[pos] if pos < len(back) else len(text))
    return bounds


def _blocked_by_filter(path: str) -> bool:
    """Последняя генерация картинки отклонена провайдером (status failed, единицы возвращены) —
    так FastGen отдаёт отказ фильтра содержания. Сеть, таймаут, нет квитанции — не «фильтр»."""
    try:
        attempts = json.load(open(path + ".generation.json", encoding="utf-8")).get("attempts") or []
    except (OSError, ValueError, AttributeError):
        return False
    last = attempts[-1] if attempts else {}
    return last.get("status") == "failed" and bool(last.get("refunded"))


def drop_blocked_variants(variants: dict, bad: list) -> list:
    """Убрать из листов те НАРЯДЫ, что отклонил фильтр генератора, когда у героя есть основной
    лист: кадр возьмёт основной лист, наряд описан словами. Возвращает [(герой, наряд)].
    Основной лист и прочие сбои не трогаем — их чинит повтор."""
    out = []
    for cid, key, sheet in bad:
        if key == "base" or not (variants.get(cid) or {}).get("base"):
            continue
        if _blocked_by_filter(getattr(sheet, "board", "")):
            variants[cid].pop(key, None)
            out.append((cid, key))
    return out


def claude_week_used() -> Optional[float]:
    """Насколько занята подписка Claude студии: большая из долей окон 5 ч и 7 дней, каждая
    по своему порогу (1 токен haiku). None — не прочиталось. Окно 5 ч кончается первым:
    19.09 ночью оно выбралось за ~4 часа при недельном 22%, и все ролики на Claude встали."""
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if not token:
        return None
    try:
        _code, headers = CP._http_probe(token)
    except Exception as e:  # noqa: BLE001 — сеть: считаем «не прочиталось», Claude не берём
        log(f"  остаток подписки Claude не прочитан: {type(e).__name__}")
        return None
    w = CP.windows_from_headers(headers)
    week, hours5 = (w.get("7d") or {}).get("util"), (w.get("5h") or {}).get("util")
    if week is None and hours5 is None:
        return None
    # Доля недели и доля пятичасового окна от своего порога: что ближе к порогу, то и решает.
    parts = [week if week is not None else 0.0,
             (hours5 / CLAUDE_5H_STOP * CLAUDE_ROUTE_STOP) if hours5 is not None else 0.0]
    return max(parts)


def choose_text_model(projects_dir: str, me: str) -> tuple:
    """Модель из MANGA_TEXT_MODELS, которой сейчас пишут текст меньше роликов (равенство — первая
    по списку). Считаются соседние ролики с model.json моложе суток и без готовой раскадровки.
    Под замком: два ролика, стартующие разом, не выберут одно и то же вслепую."""
    import fcntl
    with open(os.path.join(projects_dir, ".text_model.lock"), "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        busy = {m: 0 for m in TEXT_MODELS}
        now = time.time()
        for mp in glob.glob(os.path.join(projects_dir, "*", "text", "model.json")):
            root = os.path.dirname(os.path.dirname(mp))
            if os.path.abspath(root) == os.path.abspath(me) or os.path.exists(os.path.join(root, "text", "storyboard.json")):
                continue
            try:
                d = json.load(open(mp, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if d.get("model") in busy and now - float(d.get("at") or 0) < TEXT_PHASE_HOURS * 3600:
                busy[d["model"]] += 1
        order = sorted(TEXT_MODELS, key=lambda m: (busy[m], TEXT_MODELS.index(m)))
        for m in order:
            if m.startswith("claude:"):
                used = claude_week_used()
                if used is None or used >= CLAUDE_ROUTE_STOP:
                    log(f"  {m} пропущена: неделя подписки "
                        f"{'не прочитана' if used is None else f'занята на {used:.0%}'} (порог {CLAUDE_ROUTE_STOP:.0%})")
                    continue
            return m, f"пишут текст сейчас: " + ", ".join(f"{k} — {v}" for k, v in busy.items())
        return MODEL_TEXT, "все модели списка недоступны — MANGA_TEXT_MODEL"


def _fill_from_neighbours(frames: list, log=print) -> int:
    """Пустым кадрам даём описание ближайшего непустого кадра ТОЙ ЖЕ сцены.

    Соседняя сцена не годится: там другое место и другие герои, кадр уехал бы по смыслу.
    Если во всей сцене нет ни одного описания, кадры остаются пустыми — это честная
    ошибка, а не тихая подмена.
    """
    by_scene = {}
    for fd in frames:
        by_scene.setdefault(fd.get("scene_num"), []).append(fd)
    filled = 0
    for scene, group in by_scene.items():
        donors = [fd for fd in group if (fd.get("prompt_en") or "").strip()]
        if not donors:
            continue
        for fd in group:
            if (fd.get("prompt_en") or "").strip():
                continue
            near = min(donors, key=lambda d: abs(int(d.get("idx", 0)) - int(fd.get("idx", 0))))
            fd["prompt_en"] = near["prompt_en"]
            fd["filled_from"] = near.get("idx")
            if not (fd.get("in_frame") or []):
                fd["in_frame"] = list(near.get("in_frame") or [])
            filled += 1
    return filled


def frames_missing_ok() -> float:
    """Какую долю несданных кадров ролик переживает заплатками (MANGA_FRAMES_MISSING_OK).

    По умолчанию 5%: на ролике в 864 кадра это до 43 штук. Больше — значит сломался сам
    прогон, и ролик честно останавливается, а не собирается из повторов.
    """
    raw = (os.environ.get("MANGA_FRAMES_MISSING_OK") or "").strip()
    try:
        value = float(raw) if raw else 0.05
    except ValueError:
        raise ValueError(f"MANGA_FRAMES_MISSING_OK: нужно число, а не {raw!r}") from None
    if not 0.0 <= value <= 0.5:
        raise ValueError("MANGA_FRAMES_MISSING_OK: от 0 до 0.5")
    return value


def frame_ready(path: str) -> bool:
    return os.path.exists(path) and os.path.getsize(path) > 4096


def patch_missing_frames(frames_dir: str, frames: list, log=print) -> list:
    """Заменить недостающие кадры ближайшим соседом ТОЙ ЖЕ сцены. Возвращает список заплаток.

    Зачем. 21.09 ночью чешский ролик нарисовал 847 кадров из 864 и упал из-за семнадцати
    несданных: два процента брака отправляли ролик в конец очереди, где он ждал часами и
    начинал всё заново. Ролик с двумя процентами повторённых кадров несравнимо лучше, чем
    ролик, которого нет. Заплатки перечисляются в журнале — их видно и можно пересдать.
    """
    import shutil
    by_scene = {}
    for fd in frames:
        by_scene.setdefault(fd.get("scene_num"), []).append(fd)
    patched = []
    for scene, group in by_scene.items():
        ready = [fd for fd in group
                 if frame_ready(os.path.join(frames_dir, f"{int(fd['idx']):05d}.png"))]
        if not ready:
            continue
        for fd in group:
            idx = int(fd["idx"])
            path = os.path.join(frames_dir, f"{idx:05d}.png")
            if frame_ready(path):
                continue
            # В ХУКЕ сосед берётся самый дальний готовый кадр сцены, а не ближайший:
            # ближайший стоит рядом и на первых секундах читается как «картинка застыла»
            # (кадр 2 ролика 27b42e7c был байт в байт равен кадру 1). Если отличить не от
            # чего — заплатки в хуке нет вовсе, кадр добирётся повтором.
            if idx <= EXTRA_FRAMES:
                далёкие = [d for d in ready if abs(int(d["idx"]) - idx) >= 3]
                if not далёкие:
                    log(f"  заплатка кадру {idx} в хуке НЕ ставится: отличить не от чего — "
                        f"кадр добирётся повтором")
                    continue
                near = max(далёкие, key=lambda d: abs(int(d["idx"]) - idx))
            else:
                near = min(ready, key=lambda d: abs(int(d["idx"]) - idx))
            shutil.copyfile(os.path.join(frames_dir, f"{int(near['idx']):05d}.png"), path)
            patched.append((idx, int(near["idx"])))
    if patched:
        log(f"  заплатки: {len(patched)} кадров взяли картинку соседа по сцене "
            f"({', '.join(f'{a}←{b}' for a, b in patched[:8])}"
            + (f" и ещё {len(patched) - 8}" if len(patched) > 8 else "") + ") — проверьте глазами")
    return patched


# Поля плана сцены из скелета: готовая сцена прошлого прогона берётся, только если все они совпали.
_SCENE_PLAN = ("title", "src_from", "src_to", "budget_words", "who", "where", "what", "role")


class Project:
    def __init__(self, root: str, source_path: str, target_minutes: int = 40,
                 wpm: Optional[float] = None, lang: str = "Russian", style: str = "",
                 voice: Optional[str] = None, frame_limit: Optional[int] = None,
                 strict_render: bool = False):
        self.root = os.path.abspath(root)
        self.source_path = source_path
        # Манера рисовки — свойство ПРОГОНА: один раз выбранная, она пишется в
        # text/style.json и подхватывается всеми последующими шагами (пересдача
        # через день должна рисовать тем же стилем, что и основной проход).
        sp = os.path.join(self.root, "text", "style.json")
        if not style and os.path.exists(sp):
            style = json.load(open(sp, encoding="utf-8")).get("style", "")
        self.style = FP.set_style(style or "S0")
        os.makedirs(os.path.join(self.root, "text"), exist_ok=True)
        if not os.path.exists(sp) or json.load(open(sp, encoding="utf-8")).get("style") != self.style:
            json.dump({"style": self.style, "name": FP.STYLE_NAMES.get(self.style, "")},
                      open(sp, "w", encoding="utf-8"), ensure_ascii=False)
        self.target_minutes = target_minutes
        self.lang = lang
        # Голос по умолчанию — первый голос языка (для русского это прежний VO.RU_VOICE),
        # а не русский диктор на польском тексте.
        self.voice = voice or LG.default_voice(lang)
        # Темп: минуты × темп = объём текста. По умолчанию — прежний темп языка (LG.LEGACY_WPM);
        # с MANGA_VOICE_PACE=on — замер самого голоса проекта (LG.VOICE_WPM). Как и стиль, темп —
        # свойство ПРОГОНА: он пишется в text/pace.json при первом запуске, и смена таблицы или
        # настройки не трогает уже начатые ролики (иначе границы блоков озвучки сдвинутся и вся
        # дорожка озвучится заново). Ролик, начатый до этого файла (скелет уже есть), живёт
        # на прежнем темпе языка — под него написан его текст.
        pp = os.path.join(self.root, "text", "pace.json")
        saved = {}
        if os.path.exists(pp):
            try:
                saved = json.load(open(pp, encoding="utf-8"))
                float(saved.get("wpm") or 0)
            except (ValueError, TypeError, AttributeError) as e:
                raise RuntimeError(f"файл темпа ролика {pp} повреждён ({type(e).__name__}): впишите "
                                   f'{{"wpm": …}} с темпом, на котором ролик начат, или удалите файл, '
                                   f"если текст ещё не написан") from None
        if wpm:
            if saved.get("wpm") and float(saved["wpm"]) != float(wpm):
                raise ValueError(f"темп ролика уже закреплён: {saved['wpm']:g} сл/мин (text/pace.json), "
                                 f"а задан {float(wpm):g} — озвучка начатого ролика перерезалась бы")
            self.wpm, origin = float(wpm), "задан явно"
        elif saved.get("wpm"):
            self.wpm, origin = float(saved["wpm"]), saved.get("origin", "")
        elif os.path.exists(os.path.join(self.root, "text", "skeleton.json")):
            LG.get(lang)                                   # неизвестный язык — понятная ошибка
            self.wpm, origin = LG.LEGACY_WPM[lang], "прежний темп языка (ролик начат до таблицы голосов)"
        elif LG.voice_pace():
            self.wpm, origin = LG.wpm(lang, self.voice), f"замер голоса {self.voice}"
        else:
            LG.get(lang)
            self.wpm, origin = LG.LEGACY_WPM[lang], "прежний темп языка (MANGA_VOICE_PACE выключен)"
        if saved.get("wpm") != self.wpm:
            tmp = pp + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"wpm": self.wpm, "voice": self.voice, "origin": origin}, f, ensure_ascii=False)
            os.replace(tmp, pp)
        self.frame_limit = frame_limit
        self.strict_render = strict_render
        for d in ("", "text", "sheets", "frames", "audio", "clips", "out"):
            os.makedirs(os.path.join(self.root, d), exist_ok=True)
        # Модель текста — тоже свойство ПРОГОНА: скелет, текст, паспорта и раскадровку одного
        # ролика пишет одна модель, и пересдача через день идёт той же.
        self._model = self._pin_text_model()

    @property
    def source_project(self) -> str:
        """Ролик-эталон: этот ролик — тот же сюжет на другом языке (text/source_project.json)."""
        path = self.p("text", "source_project.json")
        if not os.path.exists(path):
            return ""
        try:
            return str(json.load(open(path, encoding="utf-8")).get("project") or "")
        except (OSError, ValueError, AttributeError):
            return ""

    def segment(self, text: str):
        """Нарезка текста на доли. У ролика-перевода доли закреплены (text/beats_fixed.json):
        кадры эталона ссылаются на их номера, пересчитывать нарезку нельзя."""
        fixed = self.p("text", "beats_fixed.json")
        if os.path.exists(fixed):
            rows = json.load(open(fixed, encoding="utf-8"))
            beats = [B.Beat(idx=r["idx"], start=r["start"], end=r["end"],
                            words=len(text[r["start"]:r["end"]].split())) for r in rows]
            return B.Report(beats=beats, assembled_ok=True, corridor_pct=100.0,
                            total_words=sum(b.words for b in beats),
                            median_words=(sorted(b.words for b in beats)[len(beats) // 2] if beats else 0))
        return B.segment(text)

    def _pin_text_model(self) -> str:
        mp = self.p("text", "model.json")
        if os.path.exists(mp):
            try:
                m = json.load(open(mp, encoding="utf-8")).get("model", "")
            except (ValueError, AttributeError):
                m = ""
            if not re.fullmatch(r"(codex|claude):[A-Za-z0-9._-]+", str(m)):
                raise RuntimeError(f"файл модели ролика {mp} повреждён: впишите {{\"model\": \"codex:…\"}} "
                                   f"с моделью, которой ролик начат")
            return m
        if not TEXT_MODELS:
            return None                                     # как было: общие MODEL_* студии
        if os.path.exists(self.p("text", "skeleton.json")):
            m, why = MODEL_TEXT, "ролик начат до выбора модели"
        else:
            m, why = choose_text_model(os.path.dirname(self.root), self.root)
        tmp = mp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"model": m, "why": why, "at": time.time()}, f, ensure_ascii=False)
        os.replace(tmp, mp)
        log(f"модель текста ролика: {m} ({why})")
        return m

    # Закреплённая модель ролика или, если её нет (MANGA_TEXT_MODELS не задан), общие MODEL_* студии —
    # читаются в момент вызова, как раньше.
    @property
    def text_model(self) -> str:
        return getattr(self, "_model", None) or MODEL_TEXT

    @property
    def model_skeleton(self) -> str:
        return getattr(self, "_model", None) or MODEL_SKELETON

    @property
    def model_expand(self) -> str:
        return getattr(self, "_model", None) or MODEL_EXPAND

    @property
    def model_visuals(self) -> str:
        return getattr(self, "_model", None) or MODEL_VISUALS

    @property
    def vision_model(self) -> str:
        """Кто смотрит на нарисованный лист и пишет паспорт внешности. Claude картинки в этом
        месте не смотрит, и паспорт остаётся текстовым — 20.09 у «Реванша» от этого скакал цвет
        глаз. Поэтому при модели-не-codex сверку делает Codex, если он доступен."""
        if self.text_model.startswith("codex:"):
            return self.text_model
        return VISION_MODEL

    @property
    def vision_locks(self) -> bool:
        return self.vision_model.startswith("codex:")

    def p(self, *a) -> str:
        return os.path.join(self.root, *a)

    @property
    def target_words(self) -> int:
        return int(self.target_minutes * self.wpm)

    # ---------- шаг 1-2: скелет ----------
    def _youth_safe_locks(self, locks: dict) -> dict:
        """Паспорт внешности несовершеннолетнего — без слов про фигуру («slim-curvy»)."""
        # Пометка minor ставится ДО того, как из паспорта убран возраст (_youth_safe_characters):
        # иначе «16-year-old» превращается в «young», и несовершеннолетний перестаёт им считаться.
        minors = {c["id"] for c in (self.step_skeleton().get("characters") or [])
                  if c.get("id") and (c.get("minor") or MEM.looks_minor(c.get("appearance_en") or ""))}
        return {k: (MEM.youth_safe_text(v) if k in minors and isinstance(v, str) else v)
                for k, v in (locks or {}).items()}

    @staticmethod
    def _youth_safe_characters(data: dict) -> dict:
        """Описание внешности несовершеннолетних — без слов про фигуру: с ними генератор
        блокирует лист героя целиком (19.09 «Реванш», shizuka/base). Файл на диске не меняется."""
        chars = []
        for c in data.get("characters") or []:
            was = c.get("appearance_en") or ""
            safe = MEM.no_age(MEM.no_props(MEM.youth_safe(was)))
            minor = MEM.looks_minor(was)     # по ИСХОДНОМУ паспорту: в очищенном возраста уже нет
            chars.append({**c, "appearance_en": safe, **({"minor": True} if minor else {})}
                         if safe != was or minor else c)
        return {**data, "characters": chars}

    def step_skeleton(self, force=False):
        out = self.p("text", "skeleton.json")
        if os.path.exists(out) and not force:
            log("скелет: уже есть, пропускаю")
            return self._youth_safe_characters(json.load(open(out, encoding="utf-8")))
        src = open(self.source_path, encoding="utf-8").read()
        src = re.sub(r"\s+", " ", src).strip()
        open(self.p("text", "source.txt"), "w", encoding="utf-8").write(src)
        log(f"скелет: источник {len(src.split())} слов, цель {self.target_words} слов")
        sk = RW.build_skeleton(src, self.target_words, self.lang,
                               model=self.model_skeleton, log=log)
        data = {"characters": sk.characters, "locations": sk.locations,
                "scenes": [asdict(s) for s in sk.scenes]}
        tmp = out + ".tmp"
        json.dump(data, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(tmp, out)
        log(f"скелет: {len(sk.scenes)} сцен, {len(sk.characters)} героев, {len(sk.locations)} мест")
        return self._youth_safe_characters(data)

    def _skeleton_obj(self, data) -> RW.Skeleton:
        sk = RW.Skeleton(characters=data["characters"], locations=data["locations"])
        sk.scenes = [RW.Scene(**s) for s in data["scenes"]]
        return sk

    # ---------- шаг 3: разворачивание ----------
    def _translate_quotes(self) -> int:
        """Реплики в облаках и звуковые надписи — на язык этого ролика.

        Облако художник рисует прямо в картинке, а текст берёт из раскадровки. Раскадровка
        копируется с эталона вместе с репликами, и без этого шага чешский ролик показывает
        русскую фразу (замер 27.09: 5940 реплик из 13788 в 36 переводах из 59).
        """
        доска = self.p("text", "storyboard.json")
        if not os.path.exists(доска):
            return 0
        данные = json.load(open(доска, encoding="utf-8"))
        frames = данные.get("frames") if isinstance(данные, dict) else данные
        if not frames:
            return 0
        места = []          # (номер кадра, графа)
        куски = []
        for i, f in enumerate(frames):
            for графа in ("quote", "sfx"):
                v = (f.get(графа) or "").strip()
                if v:
                    места.append((i, графа))
                    куски.append(v)
        if not куски:
            log("реплики в облаках: их в раскадровке нет")
            return 0
        log(f"реплики в облаках: перевожу {len(куски)} надписей на {self.lang}")
        готово = TR.translate_parts(куски, self.lang, self.model_expand,
                                    workers=text_threads(4), log=log,
                                    cache=self.p("text", "translate_quotes.json"))
        сменено = 0
        for (i, графа), новое_ in zip(места, готово):
            новое_ = (новое_ or "").strip()
            if новое_ and новое_ != (frames[i].get(графа) or "").strip():
                frames[i][графа] = новое_
                сменено += 1
        if isinstance(данные, dict):
            # Пометка «переведено»: повтор не отдаёт уже чешский текст на перевод второй раз
            # (кэш ключуется исходной фразой, на переведённой он промахнулся бы). 27.09.
            данные["quotes_lang"] = self.lang
        tmp = доска + ".tmp"
        json.dump(данные, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(tmp, доска)
        log(f"реплики в облаках: переведено {сменено} из {len(куски)}")
        return сменено

    def _quotes_for_ready_text(self) -> None:
        """Реплики перевода, у которого текст был готов раньше, чем появился _translate_quotes.

        27.09: _translate_quotes зовётся только из _translate_from_source, а тот при готовом
        text/final.txt не запускается. Замер: f34401f0 (испанский, картинки ещё не куплены) —
        509 из 510 реплик на языке эталона. Зовётся из готовой раскадровки (step_storyboard),
        а не из step_expand: шаг «Картинки» до step_expand не доходит, а step_refill держит
        свою копию раскадровки и затёр бы перевод.

        Только пока кадры не куплены. Облако нарисовано в картинке со старой репликой: новая
        реплика ≠ quote_bubble квитанции (bubble_drawn), и все кадры с облаком ушли бы на
        перерисовку за деньги. Поэтому при любой квитанции или картинке в frames/ — ничего.

        Второй раз не переводит: пометка quotes_lang; до неё — по содержимому раскадровки
        (все реплики уже равны готовым переводам кэша), а не по наличию файла кэша.
        """
        if not self.source_project:
            return
        from web_queue import read_json, write_json
        доска = self.p("text", "storyboard.json")
        данные = read_json(доска, {})
        if not isinstance(данные, dict) or данные.get("quotes_lang") == self.lang:
            return
        поля = [(f.get(графа) or "").strip() for f in данные.get("frames") or []
                for графа in ("quote", "sfx") if (f.get(графа) or "").strip()]
        if not поля:
            return
        куплено = (len(glob.glob(self.p("frames", "*.generation.json")))
                   + len(glob.glob(self.p("frames", "*.png"))))
        if куплено:
            if not getattr(self, "_quotes_bought_logged", False):
                self._quotes_bought_logged = True
                log(f"реплики в облаках: кадры уже куплены ({куплено} файлов в frames/) — реплики "
                    f"не перевожу: все кадры с облаком ушли бы на перерисовку")
            return
        кэш = read_json(self.p("text", "translate_quotes.json"), {})
        готовые = ({v["out"].strip() for v in кэш.values()
                    if isinstance(v, dict) and isinstance(v.get("out"), str)}
                   if isinstance(кэш, dict) else set())
        if готовые and all(v in готовые for v in поля):
            данные["quotes_lang"] = self.lang      # переведено до пометки — только пометить
            write_json(доска, данные)
            return
        self._translate_quotes()

    def _translate_from_source(self) -> str:
        """Тот же сюжет на другом языке: текст эталона переводится ПО ДОЛЯМ, границы долей
        сохраняются (text/beats_fixed.json), поэтому раскадровка эталона годится как есть.
        Картинки этот ролик рисует заново — своим зерном (см. _image_seed)."""
        src_root = os.path.join(os.path.dirname(self.root), self.source_project)
        src_text_path = os.path.join(src_root, "text", "final.txt")
        if not os.path.exists(src_text_path):
            raise RuntimeError(f"ролик-эталон {self.source_project[:8]} без готового текста — "
                               f"сначала доделайте его")
        src_text = Path(src_text_path).read_text(encoding="utf-8")
        rep = B.segment(src_text)
        if not rep.assembled_ok:
            raise RuntimeError("нарезка эталона не сошлась — перевод по долям невозможен")
        parts = TR.beat_parts(src_text, rep.beats)
        log(f"перевод с эталона {self.source_project[:8]}: {len(rep.beats)} долей на {self.lang}")
        # Готовые доли — в файл сразу после каждого куска: повтор ролика переводит только недостающее.
        done = TR.translate_parts(parts, self.lang, self.model_expand,
                                  workers=text_threads(4), log=log, cache=self.p("text", "translate_parts.json"))
        text = TR.rebuild(done)
        rows = TR.offsets(done, rep.beats)
        if len(rows) != len(rep.beats):
            raise RuntimeError(f"перевод: долей стало {len(rows)} вместо {len(rep.beats)}")
        tmp = self.p("text", "beats_fixed.json") + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
        os.replace(tmp, self.p("text", "beats_fixed.json"))
        tmp = self.p("text", "final.txt") + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, self.p("text", "final.txt"))
        self._translate_quotes()
        from web_queue import write_json
        write_json(self.p("text", "expand_report.json"),
                   {"translated_from": self.source_project, "beats": len(rows),
                    "words": len(text.split()), "scenes": []})
        log(f"перевод готов: {len(text.split())} слов")
        return text

    def step_expand(self, force=False):
        out = self.p("text", "final.txt")
        if os.path.exists(out) and not force:
            from web_queue import read_json
            cached = Path(out).read_text(encoding="utf-8")
            report_path = self.p("text", "expand_report.json")
            report = read_json(report_path, {})
            invalid = not cached.strip() or report.get("failed_scenes") or any(
                not (s.get("text") or "").strip() or s.get("status") == "ошибка"
                for s in report.get("scenes", []))
            if not invalid:
                log("текст: уже есть, пропускаю")
                return cached
            import uuid
            suffix = ".incomplete." + uuid.uuid4().hex + ".bak"
            shutil.copy2(out, out + suffix)
            if os.path.exists(report_path):
                shutil.copy2(report_path, report_path + suffix)
            os.remove(out)
            log("текст: старый неполный результат сохранён в backup; повторяю незавершённое")
        if self.source_project:
            return self._translate_from_source()
        data = self.step_skeleton()
        sk = self._skeleton_obj(data)
        src = open(self.p("text", "source.txt"), encoding="utf-8").read()
        tolerance = RW.length_tolerance()   # неверная настройка — ошибка до платных вызовов
        # «Повторите незавершённое» доплачивает только за пустые и упавшие сцены: написанные
        # прошлым прогоном берутся из отчёта (19–20.09 каждый повтор переписывал все 12–14 сцен,
        # d6275d4c — 283 вызова Codex на 12 сцен, и под 429 прокси повтор снова падал).
        from web_queue import read_json, write_json
        report_path = self.p("text", "expand_report.json")
        prev = {} if force else read_json(report_path, {})
        old = {o.get("num"): o for o in prev.get("scenes") or []} if "failed_scenes" in prev else {}
        kept = 0
        for s in sk.scenes:
            o = old.get(s.num) or {}
            # Сцена из отчёта годится, только если совпал ВЕСЬ её план: по трём числам после
            # пересборки скелета в ролик мог попасть текст сцены старого скелета (бюджеты сцен
            # повторяются — 597/682/512/767/853), проверено тестом скептика 21.09.
            if ((o.get("text") or "").strip() and o.get("status") != "ошибка"
                    and all(o.get(k) == getattr(s, k) for k in _SCENE_PLAN)):
                s.text, s.model, s.seconds = o["text"], o.get("model", ""), o.get("seconds", 0.0)
                s.words = len(s.text.split())
                s.status = RW._volume_status(s)
                kept += 1
        if kept:
            log(f"разворачивание: {kept} из {len(sk.scenes)} сцен уже написаны прошлым прогоном — "
                f"заказываю только остальные")
        import threading
        lock = threading.Lock()

        def checkpoint(_s):
            # после каждой сцены: перезапуск службы посреди шага не теряет написанное
            with lock:
                try:
                    write_json(report_path, {
                        "scenes": [asdict(x) for x in sk.scenes],
                        "failed_scenes": [x.num for x in sk.scenes
                                          if not (x.text or "").strip() or x.status == "ошибка"],
                        "error": "Текст пишется или прерван; финальный текст не сохранён"})
                except OSError as e:     # отчёт — страховка от перезапуска; без него сцены всё равно пишутся
                    log(f"  ! отчёт сцен не сохранён: {e}")
        log(f"разворачивание: {len(sk.scenes)} сцен на {self.model_expand}")
        RW.expand_scenes(sk, src, self.lang, model=self.model_expand, workers=4, log=log,
                         wpm=self.wpm,
                         on_scene=checkpoint)
        failed = [s.num for s in sk.scenes if not (s.text or "").strip() or s.status == "ошибка"]
        if failed or not sk.scenes:
            write_json(report_path,
                       {"scenes": [asdict(s) for s in sk.scenes], "failed_scenes": failed,
                        "error": "Не все сцены написаны; финальный текст не сохранён"})
            raise RuntimeError(f"Текст не завершён: пустые или упавшие сцены {failed}. Повторите незавершённое.")
        fit = None
        if tolerance:
            # Длина по факту: текст ÷ настоящий темп голоса. Вне допуска — один круг правки сцен.
            fit = RW.fit_length(sk, src, self.target_words, self.lang, model=self.model_expand,
                                tolerance=tolerance, workers=4, log=log)
        text = collapse_linebreaks(RW.assemble_text(sk))
        q = RW.quality(text, self.target_words, sk, lang=self.lang)
        if fit is not None and not fit.get("within", True):
            q["problems"].append(f"длина вне допуска ±{tolerance * 100:g}% и после правки: "
                                 f"{fit['after']} слов при цели {self.target_words} — ролик выйдет "
                                 f"{fit['after'] / self.wpm:.1f} мин вместо {self.target_minutes}")
        q.update(wpm=self.wpm, expected_minutes=round(q["words"] / self.wpm, 1),
                 target_minutes=self.target_minutes)
        # Сначала сам текст, потом отчёт (и отчёт тоже атомарно): отчёт без failed_scenes повтор уже
        # не берёт за «написанные сцены», и сбой между двумя записями стоил бы оплаты всех сцен заново.
        tmp = out + ".tmp"
        open(tmp, "w", encoding="utf-8").write(text)
        os.replace(tmp, out)
        from web_queue import write_json as _write_json
        _write_json(self.p("text", "expand_report.json"),
                    {"scenes": [asdict(s) for s in sk.scenes], "quality": q,
                     **({"length_fit": fit} if fit is not None else {})})
        log(f"текст: {q['words']} слов, медиана предложения {q['med_sent']}, "
            f"проблем {len(q['problems'])}")
        off = q["expected_minutes"] / max(1, self.target_minutes) - 1
        log(f"текст: ожидаемая длина {q['expected_minutes']} мин при заказе {self.target_minutes} мин "
            f"({off * 100:+.0f}%, темп голоса {self.voice} {self.wpm:g} сл/мин)"
            + ("" if tolerance or abs(off) <= 0.10 else " — правка длины выключена (MANGA_LENGTH_FIX)"))
        for p in q["problems"]:
            log(f"  ! {p}")
        if q.get("stops_profile") is False:
            log(f"  ! стоп-лист мета-речи для языка {self.lang} не задан (profiles.py) — "
                f"мета-речь в тексте не проверялась")
        return text

    # ---------- шаг 4: раскадровка ----------
    def step_storyboard(self, force=False):
        out = self.p("text", "storyboard.json")
        if os.path.exists(out) and not force:
            log("раскадровка: уже есть, пропускаю")
            self._quotes_for_ready_text()    # перевод с готовым текстом: реплики на язык ролика (27.09)
            if not os.path.exists(self.p("text", "hook_qa.json")):
                self._hook_qa()      # приёмка хука и на готовой раскадровке (перевод)
            return json.load(open(out, encoding="utf-8"))
        text = self.step_expand()
        full = self.p("text", "final.full.txt")
        if self.frame_limit and os.path.exists(full):
            text = Path(full).read_text(encoding="utf-8")
        data = self.step_skeleton()
        sk = self._skeleton_obj(data)

        rep = self.segment(text)
        if not rep.assembled_ok:
            raise RuntimeError("нарезка: склейка не сошлась с текстом — дальше нельзя")
        log(f"нарезка: {len(rep.beats)} действий, в коридоре {rep.corridor_pct:.1f}%, "
            f"склейка сошлась")

        g = S.Glossary()
        for c in sk.characters:
            g.chars[c["id"]] = S.Character(cid=c["id"], canonical=c["name"],
                                           gender=c.get("gender", "?"),
                                           variants=c.get("variants") or [])
        g.locations = [l["name"] for l in sk.locations]
        g.place_variants = {l["name"]: l.get("variants") or [] for l in sk.locations}
        states = S.resolve(text, rep.beats, g, lang=self.lang)
        known_who = sum(1 for s in states if s.in_frame)
        known_loc = sum(1 for s in states if s.location)
        log(f"состояние сцены: кто {known_who}/{len(states)}, где {known_loc}/{len(states)}")

        frames = SB.build(rep.beats, states, words_per_minute=self.wpm)
        srep = SB.report(frames)
        log(f"кадров {srep['frames']}, медиана {srep['sec_median']}s, "
            f"хронометраж {srep['total_minutes']} мин")

        # Какая сцена у кадра. РАНЬШЕ считалось долей от длины текста — и это
        # врало: сцены разной длины, поэтому кадр из квартиры попадал в магазин
        # и получал чужую декорацию (проверено на кадре 222 первого прогона).
        # Теперь границы ищутся по самому тексту сцены: начало каждой сцены
        # находим её же первыми словами. Тот же приём, что в скелете, где
        # модель отдаёт якорь, а смещение считает код.
        # тексты сцен живут в отчёте разворачивания, в скелете они пустые
        _rep = self.p("text", "expand_report.json")
        if os.path.exists(_rep):
            _texts = {int(x.get("num", 0)): (x.get("text") or "")
                      for x in json.load(open(_rep, encoding="utf-8")).get("scenes", [])}
            for _s in sk.scenes:
                if not (_s.text or "") and _texts.get(_s.num):
                    _s.text = _texts[_s.num]
        bounds = _scene_bounds(text, sk)
        log("границы сцен в тексте: " + ", ".join(str(b) for b in bounds))

        import bisect
        def scene_of(f):
            pos = rep.beats[f.beat_from - 1].start
            return max(1, bisect.bisect_right(bounds, pos))

        # номер сцены проставляем ДО описаний: он нужен, чтобы кадр знал своё
        # место даже там, где текст место не называет (а он не называет в половине)
        for f in frames:
            f.scene_num = scene_of(f)
        by_num = {s_.num: s_ for s_ in sk.scenes}
        inherited = 0
        for f in frames:
            if not f.location:
                sc = by_num.get(f.scene_num)
                if sc and sc.where:
                    loc = next((l["name"] for l in sk.locations if l["id"] == sc.where), sc.where)
                    f.location = loc
                    inherited += 1
        if inherited:
            log(f"место унаследовано из сцены у {inherited} кадров "
                f"(текст его там не называет)")
        known = sum(1 for f in frames if f.location)
        log(f"место известно у {known}/{len(frames)} кадров")

        if self.frame_limit and len(frames) > self.frame_limit:
            # A preview is a coherent prefix: both narration and timing use the
            # same selected beats. Production never enters this branch.
            frames = frames[:self.frame_limit]
            last_beat = frames[-1].beat_to
            rep.beats = rep.beats[:last_beat]
            stop_char = rep.beats[-1].end
            if not os.path.exists(full):
                shutil.copy2(self.p("text", "final.txt"), full)
            text = text[:stop_char].rstrip()
            Path(self.p("text", "final.txt")).write_text(text, encoding="utf-8")
            srep = SB.report(frames)

        scenes = self.step_scenes()
        probs = V.build_prompts(frames, rep.beats, text, sk, STYLE_LINE,
                                scenes=scenes, lang=self.lang,
                                model=self.model_visuals, scene_of_frame=scene_of,
                                workers=text_threads(4), log=log,
                                balanced=(TEXT_THREADS or 1) > 1)
        for p in probs:
            log(f"  ! {p}")

        # Линейка состава: сверяет ТЕКСТ ролика с составом кадра. Нужна, чтобы принимать
        # правки про состав, не купив ни одной картинки.
        try:
            ruler = V.cast_ruler(
                frames,
                {f.idx: f.text(text, rep.beats) for f in frames},
                {c["id"]: c.get("name", "") for c in (sk.characters or []) if c.get("id")},
                log=log, варианты=V.cast_names(sk))
        except Exception as e:   # noqa: BLE001 — линейка не должна ронять ролик
            log(f"  ! линейка состава не отработала: {type(e).__name__}: {e}")
            ruler = {}

        payload = {
            "beats": [{"idx": b.idx, "start": b.start, "end": b.end, "words": b.words}
                      for b in rep.beats],
            "cast_ruler": ruler,
            "frames": [{**{k: v for k, v in asdict(f).items()},
                        "text": f.text(text, rep.beats)} for f in frames],
            "segment_report": {"corridor": rep.corridor_pct, "median": rep.median_words,
                               "assembled_ok": rep.assembled_ok},
            "storyboard_report": srep,
            "state": {"who_known": known_who, "loc_known": known_loc, "total": len(states)},
            "visual_problems": probs,
        }
        tmp = out + ".tmp"
        json.dump(payload, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(tmp, out)

        self._hook_qa()
        return payload

    def _hook_qa(self):
        """Приёмка хука: по тексту и раскадровке, то есть ДО покупки картинок.

        Ничего не блокирует — громко называет провалы и кладёт отчёт в text/hook_qa.json.
        Зовётся и на свежей раскадровке, и на готовой: у перевода раскадровка копируется
        с эталона, шаг выходит сразу, а проверить начало всё равно надо."""
        try:
            import hookqa
            hrep = hookqa.run(self.root)
            hookqa.печать(hrep, log=log)
            htmp = self.p("text", "hook_qa.json.tmp")
            json.dump(hrep, open(htmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            os.replace(htmp, self.p("text", "hook_qa.json"))
        except Exception as e:   # noqa: BLE001 — приёмка не должна ронять ролик
            log(f"  ! приёмка хука не отработала: {type(e).__name__}: {e}")


    # ---------- шаг 4б: паспорта сцен и чертежи помещений ----------
    def step_scenes(self, force=False):
        """Паспорт сцены — то, что дословно копируется во все её кадры.

        Ни одной генерации картинок: чертёж рисуется кодом. Стоит 14 текстовых
        вызовов на ролик — столько же, сколько уже тратится на описания.
        """
        out = self.p("text", "scenes.json")
        if os.path.exists(out) and not force:
            log("паспорта сцен: уже есть, пропускаю")
            raw = json.load(open(out, encoding="utf-8"))
            scenes = {int(k): SC.Scene(**v) for k, v in raw.items()}
            # Костюм-«ссылка» в уже собранном scenes.json («same … as previously worn»,
            # run_ceo круг 2, сцена 2: 19 промптов без одежды) — заменяется конкретным
            # нарядом из более ранней сцены. Для чистых паспортов ничего не меняет.
            SC.inherit_costumes(scenes, log=log)
            return scenes
        data = self.step_skeleton()
        sk = self._skeleton_obj(data)
        text = self.step_expand()
        rep_path = self.p("text", "expand_report.json")
        texts = {}
        if os.path.exists(rep_path):
            texts = {int(x.get("num", 0)): (x.get("text") or "")
                     for x in json.load(open(rep_path, encoding="utf-8")).get("scenes", [])}
        # В английский промпт должно уходить английское название места:
        # русское «Квартира семьи Фан Шу» в англоязычном тексте модель читает
        # как неизвестное слово и достраивает помещение по своему усмотрению.
        loc_name = {l["id"]: FP.en_name(l["id"], l.get("name", ""))
                    for l in sk.locations}
        # Состав сцены берём НЕ только из скелета. Скелет называет главных, а в
        # кадры попадают и остальные — врач, мать, малыш. Замер: 51 кадр из 493
        # содержал человека, которому паспорт сцены не давал ни костюма, ни
        # состояния, ни стороны экрана, и такой герой придумывался заново каждый
        # кадр. Берём объединение: скелет плюс все, кто реально в кадрах сцены.
        extra: Dict[int, set] = {}
        sb_path = self.p("text", "storyboard.json")
        if os.path.exists(sb_path):
            for fd in json.load(open(sb_path, encoding="utf-8")).get("frames", []):
                extra.setdefault(int(fd.get("scene_num") or 0), set()).update(
                    fd.get("in_frame") or [])
        payload = []
        pos = 0
        for sc in sk.scenes:
            body = texts.get(sc.num, "") or ""
            who = list(dict.fromkeys(list(sc.who or []) + sorted(extra.get(sc.num, set()))))
            payload.append({"num": sc.num,
                            "where": loc_name.get(sc.where, sc.where or ""),
                            "who": who,
                            "start": pos, "end": pos + len(body)})
            pos += len(body)
        joined = "".join(texts.get(sc.num, "") for sc in sk.scenes)
        log(f"паспорта сцен: {len(payload)} сцен на {self.model_visuals}")
        # names нужны, чтобы вырезать героев из массовки и по имени, не только по id
        # (run_ceo сцена 4: «the company director by the door» в 44 кадрах одиночной сцены)
        side = None
        if TEXT_THREADS is not None and TEXT_THREADS > 1:
            # Рост и паспорт внешности от паспортов сцен не зависят (на входе только
            # герои скелета) — с MANGA_TEXT_THREADS они идут рядом, а не после:
            # замер 18.09, ролик 70 мин — 19 с и 45 с хвоста шага. При =1 — по одному,
            # как просили: иначе шло бы 3 вызова сразу.
            from concurrent.futures import ThreadPoolExecutor
            side = ThreadPoolExecutor(max_workers=2)
            f_h = side.submit(CP.carry(SC.heights), sk.characters, model=self.model_visuals, log=log)
            f_lk = side.submit(CP.carry(SC.appearance_locks), sk.characters,
                               model=self.model_visuals, log=log, lang=self.lang)
        try:
            scenes = SC.build(payload, joined, model=self.model_visuals, workers=text_threads(8), log=log,
                              names={c["id"]: FP.en_name(c["id"], c.get("name", ""))
                                     for c in sk.characters})
            SC.draw_plans(scenes, self.p("plans"), log=log)
            if side is not None:
                h, lk = f_h.result(), f_lk.result()
        finally:
            if side is not None:
                side.shutdown(wait=True)
        if side is None:
            h = SC.heights(sk.characters, model=self.model_visuals, log=log)
        json.dump(h, open(self.p("text", "heights.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        if side is None:
            lk = SC.appearance_locks(sk.characters, model=self.model_visuals, log=log, lang=self.lang)
        json.dump(lk, open(self.p("text", "locks.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        tmp = out + ".tmp"
        json.dump({str(k): asdict(v) for k, v in scenes.items()},
                  open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(tmp, out)
        return scenes


    # ---------- шаг 4в: дозаполнить пустые графы бланка ----------
    def step_refill(self):
        """Заполняет только кадры без описания — тем же путём, что и раскадровка,
        но без пересборки всех 493 (это 30 минут)."""
        out = self.p("text", "storyboard.json")
        sb = json.load(open(out, encoding="utf-8"))
        text = self.step_expand()
        data = self.step_skeleton()
        sk = self._skeleton_obj(data)
        scenes = self.step_scenes()

        # Доли берём СВОИ, а не из скопированной раскадровки: у перевода она помнит позиции
        # символов в тексте эталона, и кадр получал чужой кусок рассказа (2391 кадр в 56
        # переводах, замер 27.09). self.segment отдаёт закреплённые доли из beats_fixed.json.
        beats = self.segment(text).beats
        frames = [SB.Frame.from_dict(fd) for fd in sb["frames"]]
        # Бланк расходится сам с собой (visuals.check_cast): описание и staging называют
        # героев, которых нет в in_frame. Замер 10.09, run_ceo: кадры 131–137 —
        # in_frame=[male_reporter], а описание — рукопожатие Эммы и Эдриана; в генератор
        # ушёл эталон репортёра, 7 из 7 кадров брак. Состав кадра = те, кого описание
        # называет видимыми (плюс недостающие); описание сбрасывается и заполняется заново.
        V.check_cast(frames, V.cast_names(sk))
        fixed_cast = 0
        for f in frames:
            if getattr(f, "form_error", "") == "cast_mismatch":
                seen = set(FP.mentioned((f.prompt_en or "") + " " + (f.staging or ""),
                                        {c["id"]: FP.en_name(c["id"], c.get("name", ""))
                                         for c in sk.characters}))
                f.in_frame = sorted(seen | set(getattr(f, "form_missing", []) or []))
                f.prompt_en = ""
                fixed_cast += 1
        if fixed_cast:
            log(f"дозаполнение: cast_mismatch у {fixed_cast} кадров — состав кадра переписан по описанию, описание сброшено")
        empty = [f for f in frames if not (f.prompt_en or "").strip()]
        if not empty:
            log("дозаполнение: пустых кадров нет")
            return sb
        log(f"дозаполнение: пустых кадров {len(empty)} — {[f.idx for f in empty][:10]}")
        need_scenes = sorted({f.scene_num for f in empty})
        sub = [f for f in frames if f.scene_num in need_scenes]
        probs = V.build_prompts(sub, beats, text, sk, STYLE_LINE, scenes=scenes,
                                lang=self.lang, model=self.model_visuals,
                                scene_of_frame=lambda f: f.scene_num, workers=text_threads(2), log=log,
                                balanced=(TEXT_THREADS or 1) > 1,
                                # С MANGA_TEXT_THREADS — только куски с пустыми кадрами. Без неё, как
                                # было, сцена целиком: ролик 70 мин 18.09 — 11 пустых кадров в двух
                                # сценах, заново 7 кусков подряд (~17 мин шага) вместо 2 сразу (~6 мин).
                                only_empty=TEXT_THREADS is not None)
        for p_ in probs:
            log(f"  ! {p_}")
        by = {f.idx: f for f in sub}
        for fd in sb["frames"]:
            f = by.get(fd["idx"])
            if f is not None:
                fd.update({k: v for k, v in asdict(f).items()})
        still = [fd["idx"] for fd in sb["frames"] if not (fd.get("prompt_en") or "").strip()]
        if still:
            # Один проход дозаполнения не обязан закрыть всё: 20.09 ночью роликам досталось
            # по 600–700 пустых кадров (описания не строились, пока стоял лимит модели), и
            # остаток ронял ролик целиком. Теперь остаток добираем соседом по сцене: кадр
            # получает описание ближайшего непустого кадра той же сцены. Это хуже своего
            # описания, но несравнимо лучше вставшего ролика, и видно в журнале.
            filled = _fill_from_neighbours(sb["frames"], log)
            still = [fd["idx"] for fd in sb["frames"] if not (fd.get("prompt_en") or "").strip()]
            if still:
                raise RuntimeError(f"после дозаполнения всё ещё пустые: {still[:20]}"
                                   + (f" и ещё {len(still) - 20}" if len(still) > 20 else ""))
            log(f"дозаполнение: {filled} кадров взяли описание соседа по сцене — "
                f"проверьте их глазами")
        tmp = out + ".tmp"
        json.dump(sb, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(tmp, out)
        log("дозаполнение: раскадровка обновлена, пустых кадров нет")
        return sb


    # ---------- шаг 4г: паспорта воспоминаний + листы героев-детей ----------
    def step_memory(self, force=False):
        out = self.p("text", "memory.json")
        if os.path.exists(out) and not force:
            return MEM.load(out)
        sb = self.step_storyboard()
        rep_path = self.p("text", "expand_report.json")
        texts = {}
        if os.path.exists(rep_path):
            texts = {int(x.get("num", 0)): (x.get("text") or "")
                     for x in json.load(open(rep_path, encoding="utf-8")).get("scenes", [])}
        log("паспорта воспоминаний")
        sk = self._skeleton_obj(self.step_skeleton())
        mem = MEM.build(sb["frames"], texts, model=self.model_visuals,
                        characters=sk.characters, log=log, workers=text_threads(1))
        MEM.save(mem, out)
        return mem

    def _memory_sheets(self, mem, sk, locks):
        """Листы героев в возрасте воспоминания. Ключ варианта: mem<сцена>."""
        wanted = {}
        for n, m in mem.items():
            for cid in (m.who or {}):
                ct = MEM.child_costume_text(m, cid)
                if ct:
                    wanted[(cid, m.key)] = ct
        if not wanted:
            return
        chars = {c["id"]: c for c in sk.characters}
        fake_scenes = {}
        for (cid, key), ct in wanted.items():
            fake_scenes[key] = SC.Scene(num=0, costume={cid: ct})
        # build_variants ключует по тексту костюма; нам нужен ключ по сцене —
        # поэтому рисуем напрямую тем же кодом, но с нашим ключом
        idx = getattr(self, "_variants", {})
        jobs, index = [], {}
        for n_, ((cid, key), ct) in enumerate(sorted(wanted.items())):
            c = chars.get(cid)
            if not c:
                continue
            lock = (locks or {}).get(cid, "")
            # Возраст воспоминания идёт ПЕРВЫМ. Когда первым шёл взрослый паспорт
            # («young man…»), лист «ребёнка» выходил взрослым в тапках: генератор
            # следует первому описанию. Из паспорта берём только лицо-волосы-кожу.
            face_only = "; ".join(x for x in lock.split(";")[:3]) if lock else ""
            head = (f"{ct}\n\nSame person as this adult description, but at the age "
                    f"stated above — keep only the face, hair colour, eye colour and skin: "
                    f"{face_only}")
            sh = SH.Sheet(cid=cid, name=c.get("name", cid), files={})
            sh.board = self.p("sheets", f"{cid}__{key}_board.png")
            index[(cid, key)] = sh
            jobs.append({"key": f"{cid}/{key}", "path": sh.board,
                         "prompt": f"{head}\n\n{SH.SHEET_LAYOUT}\n\n{SH.SHEET_STYLE} {STYLE_LINE}",
                         "seed": 9000 + n_,
                         # Лист — три вида в ряд, ему нужен ландшафт: с общим 3:4 вид
                         # ужимался и колонки слипались (demon_boss, прогон 09.09).
                         "aspect": "16:9"})
        log(f"  листов для воспоминаний: {len(jobs)}")
        IM.generate_many(jobs, threads=8, on_done=lambda r: log(f"    {r['key']}: {r['status']}"))
        from PIL import Image
        for (cid, key), sh in index.items():
            if not (os.path.exists(sh.board) and os.path.getsize(sh.board) > 4096):
                sh.status = "провал"; continue
            try:
                im = Image.open(sh.board).convert("RGB"); w, h = im.size
                cols, _ = SH.split_columns(sh.board)
                if len(cols) < 2:
                    raise RuntimeError("колонки не распознались")
                pad = int(w * 0.005)
                for col, name, bottom in ((cols[-1], "face", 0.96), (cols[0], "front", 1.0),
                                          (cols[1] if len(cols) > 2 else cols[0], "three_quarter", 1.0)):
                    c_ = im.crop((max(0, col[0] - pad), 0, min(w, col[1] + pad), int(h * bottom)))
                    pth = self.p("sheets", f"{cid}__{key}_{name}.png"); c_.save(pth); sh.files[name] = pth
                sh.status = "готово"
                idx.setdefault(cid, {})[key] = sh
            except Exception as e:
                sh.status = "провал"; log(f"    ! лист {cid}/{key}: {e}")
        self._variants = idx
        failed = [(cid, key) for (cid, key), sheet in index.items() if sheet.status != "готово"]
        # Отказ фильтра генератора (FastGen: status failed, единицы возвращены) повтором того же
        # описания не лечится: 19.09 «Реванш» — девочки 7 и 14 лет в школьной форме в полный рост.
        # Такие листы не валят ролик: кадры этого воспоминания берут обычный лист героя (см.
        # per_scene в step_images), возраст задаёт строка «IN THIS MEMORY». Список — в
        # text/memory_sheets_blocked.json и в журнале, чтобы эти воспоминания проверили глазами.
        blocked = [k for k in failed if _blocked_by_filter(index[k].board)]
        other = [k for k in failed if k not in blocked]
        if blocked:
            names = [f"{cid}/{key}" for cid, key in blocked]
            log(f"  ! листы воспоминаний заблокированы фильтром генератора ({len(names)}): {', '.join(names[:12])} — "
                f"кадры этих воспоминаний берут обычный лист героя; проверьте их глазами")
            with open(self.p("text", "memory_sheets_blocked.json"), "w", encoding="utf-8") as f:
                json.dump({"blocked": names, "at": time.time()}, f, ensure_ascii=False)
        if other:
            raise RuntimeError("Не готовы листы воспоминаний: " + ", ".join(f"{c}/{k}" for c, k in other[:8]))


    # ---------- шаг 4д: бланк воспоминаний — заново, в мире воспоминания ----------
    def step_refill_memory(self):
        """Кадры-воспоминания описывались в мире настоящего: «Шэнь Буюй и врач
        смотрят», хотя в воспоминании их нет. Паспорт воспоминания в промпте
        этого не переламывает — модель рисует по описанию. Значит описание надо
        писать заново: с местом, светом, составом и возрастом из паспорта."""
        out = self.p("text", "storyboard.json")
        sb = json.load(open(out, encoding="utf-8"))
        text = self.step_expand()
        sk = self._skeleton_obj(self.step_skeleton())
        mem = self.step_memory()
        chars = {c["id"]: c for c in sk.characters}
        rep_path = self.p("text", "expand_report.json")
        scene_texts = {}
        if os.path.exists(rep_path):
            scene_texts = {int(x.get("num", 0)): (x.get("text") or "")
                           for x in json.load(open(rep_path, encoding="utf-8")).get("scenes", [])}

        # Доли берём СВОИ, а не из скопированной раскадровки: у перевода она помнит позиции
        # символов в тексте эталона, и кадр получал чужой кусок рассказа (2391 кадр в 56
        # переводах, замер 27.09). self.segment отдаёт закреплённые доли из beats_fixed.json.
        beats = self.segment(text).beats
        frames = [SB.Frame.from_dict(fd) for fd in sb["frames"]]
        # Аудит F01, 27.09: у перевода в expand_report.json "scenes": [] — модель переписывала
        # воспоминания, не видя текста сцены (польский 088d57e0: 0 из 6 кадров). Собираем текст
        # сцены из СВОИХ долей: от первой до последней доли её кадров. Номер сцены кадру дал
        # scene_of эталона, доли перевода совпадают с долями эталона один к одному.
        if not any(v.strip() for v in scene_texts.values()) and os.path.exists(
                self.p("text", "beats_fixed.json")):
            span = {}
            for f in frames:
                a, b = span.get(f.scene_num, (f.beat_from, f.beat_to))
                span[f.scene_num] = (min(a, f.beat_from), max(b, f.beat_to))
            scene_texts = {n: text[beats[a - 1].start:beats[b - 1].end].strip()
                           for n, (a, b) in span.items() if 1 <= a <= b <= len(beats)}
            log(f"тексты сцен для воспоминаний собраны из своих долей: {len(scene_texts)} сцен")

        # Паспорт — на каждый отрезок воспоминания (memory.pick): в одной сцене чек
        # рвут в зале, а убивают в переулке (замер 18.09, сцена 1).
        def one_mem(item):
            _k, m = item
            n = m.scene
            sub = [f for f in frames if f.scene_num == n and (f.layer or "now") != "now"
                   and MEM.pick(mem, n, f.idx) is m]
            if not sub or not (m.who or {}):
                return 0
            # состав воспоминания с возрастом; отсутствующих в списке нет вовсе
            cast = "\n".join(
                f"  - {cid}: {chars.get(cid, {}).get('name', cid)} — "
                f"{(w.get('age') or 'as now')}; {w.get('look', '')}"
                for cid, w in m.who.items() if cid in chars) or "  (нет)"
            for f in sub:
                f.in_frame = [c for c in (f.in_frame or []) if c in m.who] or list(m.who)[:2]

            class MemScene:               # то, что build_prompts читает у сцены
                where = m.where
                sides = {}
                anchors = ["a view of the whole place", "closer in", "from the doorway",
                           "from low, at the child's eye level"]
                def constants_block(self_):
                    return m.constants_block()
            probs = V.build_prompts(sub, beats, text, sk, STYLE_LINE, scenes={n: MemScene()},
                                    lang=self.lang, model=self.model_visuals,
                                    scene_of_frame=lambda f: f.scene_num, workers=2,
                                    log=log, cast_text=cast,
                                    memory_text=scene_texts.get(n, ""),
                                    memory_ids=[c for c in m.who if c in chars])
            for p_ in probs:
                log(f"  ! {p_}")
            return len(sub)

        items = sorted(mem.items(), key=lambda kv: (kv[1].scene, kv[1].part))
        if TEXT_THREADS is not None and len(items) > 1:
            # Отрезки воспоминаний друг от друга не зависят: у каждого свои кадры
            # и свой паспорт. Без MANGA_TEXT_THREADS — по одному, как было.
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=TEXT_THREADS) as ex:
                touched = sum(ex.map(CP.carry(one_mem), items))
        else:
            touched = sum(one_mem(it) for it in items)
        by = {f.idx: f for f in frames}
        for fd in sb["frames"]:
            f = by.get(fd["idx"])
            if f is not None and (f.layer or "now") != "now":
                fd.update({k: v for k, v in asdict(f).items()})
        tmp = out + ".tmp"
        json.dump(sb, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(tmp, out)
        self._ctx = None
        log(f"бланк воспоминаний переписан в их мире: {touched} кадров")
        return sb

    # ---------- шаг 5а: паспорта мест ----------
    def step_places(self, force=False):
        data = self.step_skeleton()
        sk = self._skeleton_obj(data)
        log(f"паспорта мест: {len(sk.locations)} локаций")
        # Листы мест — без слов про лица/глаза/руки: общая STYLE_LINE описывает
        # анимешное лицо, и генератор рисовал людей на пустом интерьере
        # (замер Codex 09.09: 3 из 3 листов предметов получили руки/людей).
        return PL.build(sk.locations, self.p("places"), SH.object_style_line(STYLE_LINE), log=log)

    # ---------- шаг 5: листы персонажей ----------
    def step_sheets(self, force=False):
        data = self.step_skeleton()
        sk = self._skeleton_obj(data)
        log(f"листы персонажей: {len(sk.characters)} героев")
        lp = self.p("text", "locks.json")
        locks = self._youth_safe_locks(json.load(open(lp, encoding="utf-8")) if os.path.exists(lp) else {})
        # Source descriptions for sheet generation are frozen separately from
        # locks.json, which is later derived from the generated sheet itself.
        source_locks = self.p("text", "locks_text.json")
        if os.path.exists(source_locks):
            locks = json.load(open(source_locks, encoding="utf-8"))
        else:
            from web_queue import write_json
            write_json(source_locks, locks)
        # Лист рисуется НА КАЖДЫЙ КОСТЮМ героя. Замер: строка костюма из
        # паспорта сцены проигрывает одежде, нарисованной на листе, примерно в
        # двух случаях из трёх — в 4 сценах из 6 костюм из паспорта не появился
        # ни в одном кадре. Спорить бесполезно, нужен свой лист под каждый наряд.
        scenes = self.step_scenes()
        self._variants = SH.build_variants(sk.characters, scenes, self.p("sheets"),
                                           STYLE_LINE, locks=locks, log=log)
        try:
            self._memory_sheets(self.step_memory(), sk, locks)
        except CP.JobGone:
            raise  # задачу забрали или служба остановлена в ожидании лимита Claude — это не поломка листов
        except Exception as e:
            raise RuntimeError(f"Листы воспоминаний не собрались: {type(e).__name__}: {e}") from e
        from generation_receipts import valid_image
        bad = [(cid, key, sheet) for cid, variants in self._variants.items() for key, sheet in variants.items()
               if sheet.status != "готово" or not valid_image(sheet.board)
               or not sheet.files or any(not valid_image(path) for path in sheet.files.values())]
        # Лист НАРЯДА, отклонённый фильтром генератора, не валит ролик: кадры этой сцены берут
        # основной лист героя, а наряд и так описан словами в каждом кадре. 20.09 «Реванш»:
        # у 14-летней героини прошёл основной лист, а два наряда (кардиган с юбкой, куртка с
        # джинсами) фильтр отклонил — одежда ни при чём, отказ срабатывает через раз.
        blocked = drop_blocked_variants(self._variants, bad)
        if blocked:
            names = [f"{cid}/{key}" for cid, key in blocked]
            log(f"  ! листы нарядов заблокированы фильтром генератора ({len(names)}): {', '.join(names[:12])} — "
                f"эти сцены рисуются с основного листа героя")
            with open(self.p("text", "sheets_blocked.json"), "w", encoding="utf-8") as f:
                json.dump({"blocked": names, "at": time.time()}, f, ensure_ascii=False)
        invalid = [f"{cid}/{key}" for cid, key, _ in bad if (cid, key) not in blocked]
        invalid.extend(c["id"] for c in sk.characters if c.get("id") not in self._variants)

        # Основной лист ВТОРОСТЕПЕННОГО героя, который генератор не отдаёт: отбрасываем.
        # Его кадры рисуются по словесному описанию внешности. Иначе ролик стоит вечно:
        # повтор читает ту же квитанцию отказа и падает снова (замер 26.09: семь роликов
        # группы «месть» простояли сутки из-за трёх героев, дающих 3–5% кадров).
        if invalid:
            from web_queue import read_json
            доля = {}
            try:
                кадры = (read_json(self.p("text", "storyboard.json"), {}) or {}).get("frames") or []
                if кадры:
                    счёт = {}
                    for f_ in кадры:
                        for cid_ in (f_.get("in_frame") or []):
                            счёт[cid_] = счёт.get(cid_, 0) + 1
                    доля = {cid_: n / len(кадры) for cid_, n in счёт.items()}
            except Exception as e:   # noqa: BLE001 — счёт долей не должен ронять шаг
                log(f"  ! доли героев не посчитались: {type(e).__name__}: {e}")
            мелкие = []
            for имя in list(invalid):
                cid_ = имя.split("/")[0]
                if имя.endswith("/base") and доля.get(cid_, 1.0) < 0.10:
                    мелкие.append(имя)
                    invalid.remove(имя)
                    (self._variants.get(cid_) or {}).pop("base", None)
            if мелкие:
                log(f"  ! листы второстепенных героев генератор не отдал ({len(мелкие)}): "
                    f"{', '.join(мелкие)} — их кадры рисуются по словесному описанию внешности; "
                    f"проверьте их глазами")
                путь = self.p("text", "sheets_blocked.json")
                прежде = read_json(путь, {}) or {}
                прежде.setdefault("blocked", [])
                прежде["no_base"] = sorted(set((прежде.get("no_base") or []) + мелкие))
                прежде["at"] = time.time()
                with open(путь, "w", encoding="utf-8") as f_:
                    json.dump(прежде, f_, ensure_ascii=False)

        if invalid:
            raise RuntimeError("Не готовы листы героев: " + ", ".join(invalid[:8]) + ". Повторите незавершённое.")
        sheets = {cid: (SH.pick_variant(self._variants, cid, None)
                        or SH.Sheet(cid=cid, name=cid, files={}))
                  for cid in self._variants}
        # Паспорт внешности — по картинке листа, а не по тексту. Замер run_ceo (разбор B):
        # директор — locks «thinning crown» при густой чёлке на листе → 5 из 7 кадров лысый;
        # фотограф — кепка с листа в промпте не названа → 4 кадра без кепки. Кадры рисуются
        # с листа, значит и NEVER CHANGES должен описывать лист. Текстовый паспорт остаётся
        # запасным путём, его копия — text/locks_text.json. Признак «ещё не пересмотрен по
        # листу»: locks.json не новее scenes.json; после перезаписи повторные вызовы не
        # тратят вызовы модели заново.
        lp = self.p("text", "locks.json")
        locks = self._youth_safe_locks(json.load(open(lp, encoding="utf-8")) if os.path.exists(lp) else {})
        sc_path = self.p("text", "scenes.json")
        if not self.vision_locks:
            log(f"  паспорт внешности по картинке листа пропущен: {self.vision_model or 'нет модели, которая смотрит картинки'}; "
                "кадры рисуются по текстовому паспорту героя")
        elif os.path.exists(sc_path) and (not os.path.exists(lp)
                                          or os.path.getmtime(lp) <= os.path.getmtime(sc_path)):
            locks = SC.locks_from_sheets(sk.characters, sheets, model=self.vision_model,
                                         fallback=locks, log=log)
            tmp = lp + ".tmp"
            json.dump(locks, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            os.replace(tmp, lp)
            self._ctx = None
        bad = [s for s in sheets.values() if s.status == "провал"]
        for s in bad:
            log(f"  ! лист не получился: {s.name} — {s.error}")
        return sheets

    # ---------- шаг 6: картинки кадров ----------
    def step_images(self, limit: Optional[int] = None, threads: int = 20, force=False):
        sb = self.step_storyboard()
        # 27.09.2026 ORCH-F03-R1: врезка сохранена, а куски ролика не сброшены (процесс упал между
        # покупкой и сбросом) — повтор врезку не покупает, она цела, и сборка отдавала старый ролик
        # без неё. Пометка ставится до покупки врезок и снимается после сброса; осталась — сбросить.
        from web_queue import read_json
        отметка = self.p("frames", "_extra_pending.json")
        for idx in read_json(отметка, []) or []:
            if RP._intact(self.extra_path(idx)) is None:
                self.invalidate_frame_render(int(idx))
        if os.path.exists(отметка):
            os.remove(отметка)
        # Время нынешней раскадровки: картинка, скачанная позже, — картинка своего кадра (drawn_after
        # в задании кадра). При force (перерисовать всё) и без файла раскадровки — как раньше, по квитанции.
        board = self.p("text", "storyboard.json")
        board_time = None if force or not os.path.exists(board) else os.path.getmtime(board)
        selected_results = []
        selected_ids = set()
        target_frames = sb["frames"][:limit] if limit else sb["frames"]
        for fd in target_frames:
            path = self.p("frames", f"{fd['idx']:05d}.png")
            selected = RP.ensure_selected(path, IM.generate_many)
            if selected:
                selected_ids.add(fd["idx"])
                if selected.get("restored"):
                    self.invalidate_frame_render(fd["idx"])
                selected_results.append({"key": f"frame_{fd['idx']:05d}", "path": path,
                                         "status": "уже есть", "attempts": 0, "soften_level": 0,
                                         "seconds": 0, "selected_generation_id": selected.get("generation_id")})
        kinds = self.quote_plan(target_frames)
        quote_todo = self.quote_missing(target_frames, kinds)
        # Врезки хука считаются до обоих ранних выходов: иначе ролик с готовыми кадрами
        # их никогда не получал (аудит F03, 27.09: 0 врезок на 92 роликах).
        extra_todo = self.extra_todo(target_frames, kinds)
        if (len(selected_ids) == len(target_frames) and not quote_todo["panel"]
                and not quote_todo["closeup"] and not extra_todo):
            return selected_results
        from generation_receipts import valid_image
        # Все кадры на месте, а режим реплик рисует облака или крупные планы: докупается
        # только недостающее по репликам. Так же и при повторе после сбоя в конце шага,
        # когда докупать уже нечего, — иначе готовый ролик застрял бы на сверке квитанций.
        # Врезки — тоже «недостающее»: при всех готовых кадрах докупаются только они,
        # задания готовых кадров не пересобираются (см. _quote_images_only).
        if (any(kind in ("panel", "closeup") for kind in kinds.values()) or extra_todo) and all(
                fd["idx"] in selected_ids or valid_image(self.p("frames", f"{fd['idx']:05d}.png"))
                for fd in target_frames):
            return self._quote_images_only(target_frames, selected_ids, selected_results, quote_todo, threads,
                                           extra_todo=extra_todo)
        sheets = self.step_sheets()
        places = self.step_places()
        data = self.step_skeleton()
        sk = self._skeleton_obj(data)
        scene_where = {s_.num: s_.where for s_ in sk.scenes}
        frames = sb["frames"]
        if limit:
            frames = frames[:limit]
            log(f"картинки: ограничение {limit} кадров (проверочный прогон)")

        class F:  # лёгкая обёртка над словарём кадра
            def __init__(self, d): self.__dict__.update(d)

        scenes = self.step_scenes()
        # В английский промпт уходит английское имя, а не русское из скелета:
        # иначе в одном тексте «Шэнь Буюй» из легенды и «Shen Buyu» из описания —
        # для модели это два разных человека.
        names = {c["id"]: FP.en_name(c["id"], c.get("name", "")) for c in sk.characters}
        traits = {c["id"]: FP.visible_traits(c.get("appearance_en") or "")
                  for c in sk.characters}
        hpath = self.p("text", "heights.json")
        heights = json.load(open(hpath, encoding="utf-8")) if os.path.exists(hpath) else {}
        memories = self.step_memory()
        lpath = self.p("text", "locks.json")
        locks = json.load(open(lpath, encoding="utf-8")) if os.path.exists(lpath) else {}
        jobs = []
        skipped = 0
        for _i, fd in enumerate(frames):
            if fd["idx"] in selected_ids:
                continue
            f = F(fd)
            if not getattr(f, "prompt_en", ""):
                skipped += 1
                continue
            sc = scenes.get(getattr(f, "scene_num", 0))
            if sc is None:
                sc = SC.Scene(num=getattr(f, "scene_num", 0),
                              where=getattr(f, "location", "") or "")
            prev = frames[_i - 1] if _i > 0 else None
            f._same_camera = bool(prev and prev.get("scene_num") == fd.get("scene_num")
                                  and prev.get("camera_pos") == fd.get("camera_pos"))
            mem = None
            if (getattr(f, "layer", "now") or "now") != "now":
                mem = MEM.pick(memories, getattr(f, "scene_num", 0), f.idx)
            per_scene = {}
            # Рука из-за кадра (contact): её хозяину нужен лист для рукава —
            # frameprompt.refs_for прикладывает его ролью SLEEVE AND SKIN SOURCE.
            for cid in list(f.in_frame or []) + FP.contact_cids(f, names):
                if cid not in sheets:
                    continue
                if mem and cid in (mem.absent or []):
                    continue            # в воспоминании этого человека нет
                v = None
                if mem:
                    v = ((getattr(self, "_variants", {}) or {}).get(cid) or {}).get(mem.key)
                per_scene[cid] = v or SH.pick_variant(getattr(self, "_variants", {}), cid, sc) or sheets.get(cid)
            per_scene = {k: v for k, v in per_scene.items() if v}
            if mem:
                f.in_frame = [c for c in (f.in_frame or []) if c not in (mem.absent or [])]
            refs, used, has_plan, ref_kinds, has_style = FP.refs_for(
                f, per_scene or sheets, sc, budget=4, style_path=self.p("style_anchor.png"))
            frame_path = self.p("frames", f"{f.idx:05d}.png")
            # Облако реплики внутри рисунка. Готовая картинка сверяется с квитанцией
            # по промпту, поэтому для неё облако берётся по признаку на диске, а не
            # по настройке: смена режима не превращает готовый кадр в «чужой запрос».
            # Готовый кадр без облака перерисовывается ниже отдельной пересдачей.
            from generation_receipts import valid_image
            if valid_image(frame_path):
                bubble = self.bubble_drawn(f.idx) or ""
            else:
                bubble = (fd.get("quote") or "").strip() if kinds.get(f.idx) == "panel" else ""
            job = {
                "key": f"frame_{f.idx:05d}",
                "path": frame_path,
                "kind": "frame",      # images.clean_watermark: звёздочку убираем сразу, листы не трогаем
                # Картинка, скачанная после нынешней раскадровки, — картинка этого кадра, даже если
                # промпт с тех пор разошёлся из-за правки сборки промпта (images.generate_many).
                "drawn_after": board_time,
                "prompt": FP.build(f, sc, names, refs, used, has_plan, heights, traits,
                                   locks, ref_kinds, memory=mem, has_style=has_style,
                                   quote_bubble=bubble, speaker_offscreen=speaker_offscreen(fd),
                                   lang=self.lang),
                "refs": refs,
                "seed": self._image_seed(f.idx),
                "aspect": FP.aspect_for(f),
                "soften": FP.soften,
            }
            if bubble:
                job["quote_bubble"] = bubble
            jobs.append(job)
        # Крупный план говорящего (MANGA_QUOTE_MODE=closeup/mixed): производный кадр
        # тем же механизмом, что и кадр (_job_for), в frames/NNNNN_speaker.png.
        speaker_jobs, speaker_failed = self._speaker_jobs(quote_todo["closeup"])
        # Врезка — вторая картинка внутри кадра хука. Бланк шаблонный, вызовов модели нет.
        extra_jobs = self._extra_jobs(self.extra_todo(target_frames, kinds))
        pf = []
        if skipped:
            # Кадр без описания = дыра в готовом ролике (его время отдаётся соседу,
            # и картинка висит дольше положенного). Прогон 09.09 стартовал с семью
            # такими кадрами в самом конце ролика, потому что это была лишь заметка.
            pf.append(f"без описания {skipped} кадров — это дыры в ролике; "
                      f"дозаполни бланк: python3 app/pipeline.py ... --step refill")

        # Preflight: 493 задания уходят в 20 потоков одним махом. Если у героя
        # не нарисовался лист или у сцены нет чертежа, мы узнаем об этом ПОСЛЕ
        # того, как заплатим за весь прогон. Проверка стоит миллисекунды.
        pf += _preflight_jobs(jobs + speaker_jobs + extra_jobs)
        if not locks:
            pf.append("нет паспорта внешности (text/locks.json) — строка «эти приметы "
                      "никогда не меняются» выпадет из ВСЕХ промптов, и причёска "
                      "поедет по всему ролику")
        no_lock = sorted({c for fd in frames for c in (fd.get("in_frame") or [])
                          if c not in locks})
        if no_lock:
            pf.append(f"нет паспорта внешности у героев: {no_lock[:8]}")
        no_sheet = [cid for cid, sh in sheets.items() if not sh.files]
        # Лист, отброшен сознательно на шаге «Листы» (генератор его не отдал, а герой
        # третьестепенный): его кадры рисуются по словесному описанию внешности. Такой
        # герой не должен останавливать покупку — иначе ролик стоит вечно (замер 26.09:
        # семь роликов группы «месть» простояли сутки из-за трёх героев по 3–5% кадров).
        отброшены = set()
        try:
            from web_queue import read_json as _rj
            _b = _rj(self.p("text", "sheets_blocked.json"), {}) or {}
            отброшены = {str(x).split("/")[0] for x in (_b.get("no_base") or [])}
        except Exception as e:   # noqa: BLE001 — список отброшенных не должен ронять шаг
            log(f"  ! список отброшенных листов не прочитался: {type(e).__name__}: {e}")
        если_ждали = [cid for cid in no_sheet if cid not in отброшены]
        if отброшены & set(no_sheet):
            log(f"  листы отброшены сознательно, кадры этих героев рисуются по описанию: "
                f"{sorted(отброшены & set(no_sheet))}")
        if если_ждали:
            pf.append(f"нет листа у героев: {если_ждали}")
        no_plan = sorted({getattr(F(fd), "scene_num", 0) for fd in frames
                          if not (scenes.get(getattr(F(fd), "scene_num", 0))
                                  and scenes[getattr(F(fd), "scene_num", 0)].plan_path)})
        if no_plan:
            # Чертёж — подсказка композиции, а не условие съёмки: без него кадр рисуется,
            # просто без плана помещения. Останавливать из-за него весь ролик нельзя —
            # 20.09 ролик так простоял несколько часов, пока рядом ждала целая пачка.
            log(f"  ! нет чертежа у сцен: {no_plan} — эти кадры рисуются без плана помещения")
        _stop_on_preflight(pf)

        self._save_job_views(jobs)
        q = IM.quota()
        log(f"квота: свободно {q['limit']-q['used']} из {q['limit']} в этом часу, "
            f"потоков {q['threads']}")
        log(f"картинки: заказываю {len(jobs)}, потоков {threads}")
        if speaker_jobs:
            log(f"  крупных планов говорящего: {len(speaker_jobs)}")
        if extra_jobs:
            log(f"  врезок в хуке: {len(extra_jobs)} (вторая картинка внутри кадра)")
        jobs = jobs + speaker_jobs + extra_jobs

        done = {"n": 0, "err": 0, "t0": time.time()}
        def on_done(r):
            done["n"] += 1
            if r["status"] == "ошибка":
                done["err"] += 1
            if done["n"] % 20 == 0 or done["n"] == len(jobs):
                el = time.time() - done["t0"]
                log(f"  {done['n']}/{len(jobs)} готово, ошибок {done['err']}, "
                    f"{el/60:.1f} мин, темп {done['n']/max(1,el)*3600:.0f}/час")

        if extra_jobs:   # 27.09.2026 ORCH-F03-R1: пометка до покупки, снимается после сброса
            from web_queue import write_json as _wj
            _wj(отметка, sorted(int(j["key"].split("_")[1]) for j in extra_jobs))
        res = IM.generate_many(jobs, threads=threads, on_done=on_done) + selected_results + speaker_failed
        for result in res:
            if result.get("status") == "готово":
                self.invalidate_frame_render(int(result["key"].split("_")[1]))
        if extra_jobs and os.path.exists(отметка):
            os.remove(отметка)
        res = self._redraw_with_bubbles(
            {fd["idx"]: (fd.get("quote") or "").strip() for fd in target_frames if fd["idx"] in quote_todo["panel"]},
            res, threads)
        order = {f"frame_{fd['idx']:05d}": i for i, fd in enumerate(target_frames)}
        # frame_NNNNN_speaker идёт сразу за своим кадром
        res.sort(key=lambda r: (order[r["key"][:11]], r["key"]))
        ok = [r for r in res if r["status"] in ("готово", "уже есть")]
        bad = [r for r in res if r["status"] == "ошибка"]
        log(f"картинки: готово {len(ok)}/{len(res)}, ошибок {len(bad)}")
        for b in bad[:5]:
            log(f"  ! {b['key']}: {(b.get('error') or '')[:120]}")
        # attempts/soften_level: сколько кадров дошли до повтора и лестницы мягкости —
        # иначе не видно, чем куплена «готовность» (generate_many отдаёт их всегда).
        json.dump({"ok": len(ok), "bad": len(bad),
                   "retried": sum(1 for r in res if r.get("attempts", 0) > 1),
                   "softened": sum(1 for r in res if r.get("soften_level", 0) > 0),
                   "errors": [{"key": b["key"], "error": b.get("error")} for b in bad],
                   "frames": [{"key": r["key"], "status": r["status"],
                               "attempts": r.get("attempts", 0),
                               "soften_level": r.get("soften_level", 0),
                               "seconds": r.get("seconds")} for r in res]},
                  open(self.p("frames", "_report.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        return res


    # ---------- шаг 6б: приёмка и автоповтор брака ----------
    def _job_ctx(self):
        """Общий контекст для сборки заданий. Кэшируется: без кэша каждая
        сборка одного кадра перечитывала раскадровку, листы и паспорта —
        восемь секунд на кадр, то есть одиннадцать минут на пересдачу
        восьмидесяти кадров ещё до первой генерации."""
        if getattr(self, "_ctx", None) is None:
            sb = self.step_storyboard()
            data = self.step_skeleton()
            sk = self._skeleton_obj(data)
            hp = self.p("text", "heights.json")
            self._ctx = {
                "sb": sb,
                "by_idx": {int(fd.get("idx", 0)): fd for fd in sb["frames"]},
                "sheets": self.step_sheets(),
                "scenes": self.step_scenes(),
                "memories": self.step_memory(),
                "names": {c["id"]: FP.en_name(c["id"], c.get("name", ""))
                          for c in sk.characters},
                "traits": {c["id"]: FP.visible_traits(c.get("appearance_en") or "")
                           for c in sk.characters},
                "heights": json.load(open(hp, encoding="utf-8")) if os.path.exists(hp) else {},
                "locks": (json.load(open(self.p("text", "locks.json"), encoding="utf-8"))
                          if os.path.exists(self.p("text", "locks.json")) else {}),
            }
        return self._ctx

    def _job_for(self, idx: int, seed: int, frame: Optional[dict] = None):
        """Собирает задание на один кадр — тем же кодом, что и основной проход,
        чтобы пересдача шла по тому же промпту, а не по слегка другому.
        frame — производный бланк (крупный план говорящего) вместо кадра раскадровки."""
        ctx = self._job_ctx()
        sb = ctx["sb"]
        sheets = ctx["sheets"]
        scenes = ctx["scenes"]
        names = ctx["names"]
        heights = ctx["heights"]
        traits = ctx["traits"]
        locks = ctx["locks"]

        class F:
            def __init__(self, d): self.__dict__.update(d)
        fd = ctx["by_idx"].get(idx) if frame is None else frame
        if fd is not None:
            f = F(fd)
            if not getattr(f, "prompt_en", ""):
                return None
            sc = scenes.get(getattr(f, "scene_num", 0)) or SC.Scene(
                num=getattr(f, "scene_num", 0), where=getattr(f, "location", "") or "")
            mem = None
            if (getattr(f, "layer", "now") or "now") != "now":
                mem = MEM.pick(ctx["memories"], getattr(f, "scene_num", 0), getattr(f, "idx", None))
            per_scene = {}
            for cid in list(f.in_frame or []) + FP.contact_cids(f, names):
                if cid not in sheets or (mem and cid in (mem.absent or [])):
                    continue
                v = None
                if mem:
                    v = ((getattr(self, "_variants", {}) or {}).get(cid) or {}).get(mem.key)
                per_scene[cid] = v or SH.pick_variant(getattr(self, "_variants", {}), cid, sc) or sheets.get(cid)
            per_scene = {k: v for k, v in per_scene.items() if v}
            if mem:
                f.in_frame = [c for c in (f.in_frame or []) if c not in (mem.absent or [])]
            if frame is not None and not per_scene and (frame.get("in_frame") or []):
                return None     # крупный план без листа говорящего не рисуем
            # у врезки-детали людей нет вовсе — ей листы и не нужны
            refs, used, has_plan, kinds, has_style = FP.refs_for(
                f, per_scene or sheets, sc, budget=4, style_path=self.p("style_anchor.png"))
            path = self.p("frames", f"{idx:05d}.png")
            bubble = ""
            if frame is None and self.quote_plan([fd]).get(int(idx)) == "panel":
                bubble = (fd.get("quote") or "").strip()
            # Файл здесь НЕ удаляем. Раньше удаляли «чтобы кэш не вернул тот же
            # брак» — и опыт со стилевым якорем, собиравший задания через эту
            # функцию, стёр 12 готовых кадров основного прогона. Удаляет тот,
            # кто пересдаёт (step_repair), явно и сознательно.
            job = {"key": f"frame_{idx:05d}", "path": path, "kind": "frame",
                   "prompt": FP.build(f, sc, names, refs, used, has_plan, heights, traits,
                                      locks, kinds, memory=mem, has_style=has_style,
                                      quote_bubble=bubble, speaker_offscreen=speaker_offscreen(fd),
                                      lang=self.lang),
                   "refs": refs, "seed": seed, "soften": FP.soften,
                   "aspect": FP.aspect_for(f)}
            if bubble:
                job["quote_bubble"] = bubble
            return job
        return None

    # ---------- реплика кадра: облако в кадре и крупный план говорящего ----------
    def quote_speakers(self) -> set:
        """Герои скелета, у которых на диске есть лист (лицо или фигура)."""
        from web_queue import read_json
        ids = [c.get("id") for c in (read_json(self.p("text", "skeleton.json"), {}) or {}).get("characters", [])
               if isinstance(c, dict) and c.get("id")]
        try:
            names = os.listdir(self.p("sheets"))
        except OSError:
            names = []
        out = set()
        for cid in ids:
            pat = re.compile(re.escape(cid) + r"(__[0-9A-Za-z]+)?_(face|front)\.png")
            if any(pat.fullmatch(n) and os.path.getsize(self.p("sheets", n)) > 4096 for n in names):
                out.add(cid)
        return out

    def quote_plan(self, frames) -> Dict[int, str]:
        """idx → как показать реплику кадра (quote_kind); кадры без реплики не входят.
        Крупный план считается возможным ровно тогда, когда его соберёт _job_for:
        у говорящего есть лист, и в воспоминании он не отсутствует. Только диск,
        без вызовов моделей: план спрашивает и обработчик очереди."""
        speakers = self.quote_speakers()
        memories = MEM.load(self.p("text", "memory.json")) if any(
            (fd.get("layer") or "now") != "now" for fd in frames) else {}
        plan = {}
        for fd in frames:
            here = speakers
            if (fd.get("layer") or "now") != "now":
                mem = MEM.pick(memories, fd.get("scene_num", 0), fd.get("idx"))
                if mem and (fd.get("quote_by") or "") in (mem.absent or []):
                    here = speakers - {fd.get("quote_by")}
            kind = quote_kind(fd, here)
            if kind:
                plan[int(fd["idx"])] = kind
        return plan

    def bubble_drawn(self, idx: int) -> Optional[str]:
        """Текст реплики, с облаком которой нарисована картинка кадра на диске, или None.
        Признак — поле quote_bubble в квитанции той генерации, что стоит в кадре:
        выбранного кандидата (frames/NNNNN.png.selected.json) или самого кадра."""
        from web_queue import read_json
        frame = self.p("frames", f"{int(idx):05d}.png")
        receipt = frame + ".generation.json"
        selected = read_json(frame + ".selected.json")
        if isinstance(selected, dict) and selected.get("candidate_path"):
            candidate = os.path.normpath(os.path.join(self.root, selected["candidate_path"]))
            if candidate.startswith(self.root + os.sep) and os.path.exists(candidate + ".generation.json"):
                receipt = candidate + ".generation.json"
        data = read_json(receipt, {})
        attempts = data.get("attempts") if isinstance(data, dict) else None
        last = attempts[-1] if attempts else {}
        if not isinstance(last, dict) or last.get("status") != "downloaded":
            return None
        return last.get("quote_bubble") or None

    def speaker_path(self, idx: int) -> str:
        return self.p("frames", f"{int(idx):05d}_speaker.png")

    def quote_missing(self, frames=None, kinds=None) -> Dict[str, List[int]]:
        """Чего не хватает на диске под MANGA_QUOTE_MODE: кадры без облака своей
        реплики (panel) и реплики без крупного плана говорящего (closeup)."""
        if frames is None:
            from web_queue import read_json
            frames = (read_json(self.p("text", "storyboard.json"), {}) or {}).get("frames", [])
        if kinds is None:
            kinds = self.quote_plan(frames)
        by_idx = {int(fd["idx"]): fd for fd in frames}
        panel = [idx for idx, kind in sorted(kinds.items())
                 if kind == "panel" and self.bubble_drawn(idx) != (by_idx[idx].get("quote") or "").strip()]
        # Крупный план в сборку идёт только на кадре от 3 с. Когда тайминг уже есть
        # (готовый ролик), короткие кадры не заказываются — 4 единицы впустую.
        seconds = self.frame_seconds()
        closeup = [idx for idx, kind in sorted(kinds.items())
                   if kind == "closeup" and RP._intact(self.speaker_path(idx))
                   and seconds.get(idx, CLOSEUP_MIN_SECONDS) >= CLOSEUP_MIN_SECONDS]
        return {"panel": panel, "closeup": closeup}

    def frame_seconds(self) -> Dict[int, float]:
        """idx → длительность кадра в сборке по text/timing.json; {} пока тайминга нет."""
        from web_queue import read_json
        timing = read_json(self.p("text", "timing.json"), {}) or {}
        frames = (read_json(self.p("text", "storyboard.json"), {}) or {}).get("frames", [])
        pairs = timing.get("pairs") or []
        if not frames or len(pairs) != len(frames):
            return {}
        try:
            secs = AS.clip_seconds(pairs, timing.get("total"))
        except Exception as e:
            log(f"  ! длительности кадров не посчитаны по timing.json: {type(e).__name__}: {e}")
            return {}
        return {int(fd["idx"]): sec for fd, sec in zip(frames, secs)}

    def extra_path(self, idx: int) -> str:
        return self.p("frames", f"{int(idx):05d}_extra.png")

    def extra_todo(self, frames, kinds) -> List[int]:
        """Кадры хука, которым нужна врезка: номер не больше EXTRA_FRAMES, реплика не
        занимает вторую половину кадра (там уже крупный план или облако), файла ещё нет."""
        if EXTRA_FRAMES <= 0:
            return []
        out = []
        seconds = None       # тайминг читается один раз и только если есть кандидат
        отказ = None         # 27.09.2026 ORCH-F03-R2: решённое «кадр без врезки» (extra_skip)
        for fd in frames:
            idx = int(fd["idx"])
            if idx > EXTRA_FRAMES:
                continue
            if kinds.get(idx) in ("closeup", "bubble"):
                continue          # вторая половина кадра уже занята
            # RP._intact даёт None у ЦЕЛОГО файла и строку-причину у отсутствующего.
            # Прежнее «if _intact(...): continue» пропускало ровно те кадры, которым
            # врезка нужна, и отбор всегда возвращал пусто (нашёл Codex 27.09).
            if RP._intact(self.extra_path(idx)) is None:
                continue          # врезка уже нарисована и цела
            if отказ is None:
                from web_queue import read_json
                отказ = read_json(self.p("frames", "_extra_skipped.json"), {}) or {}
            if str(idx) in отказ:
                continue          # повтор её не нарисует — кадр идёт без врезки (extra_skip)
            # Тайминг уже есть (возобновление готового ролика): кадр короче EXTRA_MIN_REAL
            # сборка не делит, врезка на нём — 4 единицы впустую. Как у крупного плана.
            if seconds is None:
                seconds = self.frame_seconds()
            if seconds.get(idx, EXTRA_MIN_REAL) < EXTRA_MIN_REAL:
                continue
            out.append(idx)
        return out

    def extra_missing(self, frames=None) -> List[int]:
        """Кадры хука без врезки — по диску, без вызовов моделей. Спрашивает обработчик
        очереди: есть такие — закрытый шаг «Картинки» открывается заново (аудит F03, 27.09)."""
        if EXTRA_FRAMES <= 0:
            return []
        if frames is None:
            from web_queue import read_json
            доска = read_json(self.p("text", "storyboard.json"), {}) or {}
            # раскадровка бывает списком, а не словарём (старые проекты) — обработчик
            # очереди зовёт это на каждом ролике, падать здесь нельзя (сверка 27.09)
            frames = доска.get("frames", []) if isinstance(доска, dict) else (доска or [])
        head = [fd for fd in frames if int(fd["idx"]) <= EXTRA_FRAMES]
        return self.extra_todo(head, self.quote_plan(head))

    def extra_skip(self, results) -> set:
        """27.09.2026 ORCH-F03-R2: врезки, которые повтор не нарисует, — явное «кадр без врезки».
        Это отказ по содержанию после лестницы мягкости (в тексте слова цензуры images._СЛОВА_ЦЕНЗУРЫ,
        а оплаченной генерации, которую можно докачать, нет: квитанция не «отправляю/принята/готова» —
        иначе «violation of protocol» обрыва скачивания сошёл бы за цензуру) и покупка, остановленная
        квитанцией: новую студия не делает. Номер и причина — в frames/_extra_skipped.json, extra_todo их больше не
        заказывает. Таймаут, квота, «try again later» сюда не попадают — их чинит повтор.
        Возвращает ключи записанных врезок."""
        from web_queue import read_json, write_json
        стоп = ("номер не получен", "покупка остановлена", "покупка не делается", "запрос не отправлен",
                "другому запросу", "другим запросом")
        new = {}
        for r in results:
            idx, err = int(_job_idx(r["key"])), str(r.get("error") or "")
            чек = read_json(self.extra_path(idx) + ".generation.json", {})
            last = ((чек.get("attempts") if isinstance(чек, dict) else None) or [{}])[-1]
            оплачена = last.get("status") in ("posting", "uncertain", "accepted", "succeeded", "downloaded")
            if RP._intact(self.extra_path(idx)) and (any(m in err for m in стоп) or (
                    not оплачена and any(w in err.lower() for w in IM._СЛОВА_ЦЕНЗУРЫ))):
                new[str(idx)] = err[:300]
        if new:
            path = self.p("frames", "_extra_skipped.json")
            write_json(path, {**(read_json(path, {}) or {}), **new})
        return {f"frame_{int(i):05d}_extra" for i in new}

    def extra_note(self, results) -> str:
        """27.09.2026 ORCH-F03-R2: заметка шага «Картинки» о врезках — при каждом закрытии шага, пока
        есть запись, а не только в прогоне, где решение принято. Ошибка врезки при закрытом шаге —
        это отложенная на повторе врезка: записи нет, следующий запуск ролика её докупит."""
        if EXTRA_FRAMES <= 0:
            return ""
        from web_queue import read_json
        без = sorted(int(k) for k in (read_json(self.p("frames", "_extra_skipped.json"), {}) or {}))
        позже = sorted(int(_job_idx(r["key"])) for r in results if r and r.get("status") == "ошибка"
                       and str(r.get("key") or "").endswith("_extra") and int(_job_idx(r["key"])) not in без)
        note = []
        if без:
            note.append(f"Без врезки кадры {', '.join(map(str, без))}: генератор отказал (фильтр) или покупка "
                        f"остановлена квитанцией, повтор её не нарисует (frames/_extra_skipped.json).")
        if позже:
            note.append(f"Врезки кадров {', '.join(map(str, позже))} не нарисованы и на повторе (сбой генератора) — "
                        f"ролик собран без них; следующий запуск ролика их докупит.")
        return " ".join(note)

    def extra_frame(self, fd: dict, names: Dict[str, str]) -> dict:
        """Бланк врезки: та же сцена, место, слой. Нечётные кадры — лицо крупно,
        чётные — деталь без людей. Чередование, чтобы врезки не были похожи одна на другую."""
        idx = int(fd["idx"])
        лицо = idx % 2 == 1
        cid = (fd.get("focus") or "") or next(iter(fd.get("in_frame") or []), "")
        name = names.get(cid) or FP.en_name(cid) if cid else ""
        costume = fd.get("costume_state") if isinstance(fd.get("costume_state"), dict) else {}
        общее = {
            "idx": idx, "scene_num": fd.get("scene_num", 0), "location": fd.get("location", ""),
            "layer": fd.get("layer") or "now", "quote_by": "", "quote": "",
            "sfx": "", "contact": "", "props_seen": [], "staging": "", "pose": "", "posture": "",
            "distance": "", "camera_pos": "", "angle": "eye", "panel": "",
            # Строку «что изменилось» во врезку не несём: в кадре без лиц она печатается как
            # «CONTINUITY: Флинн теперь склонился над столом» и требует человека, которого нет.
            "changed": "",
        }
        if лицо and cid:
            return {**общее, "in_frame": [cid], "focus": cid, "shot_size": "close",
                    "face_visible": True, "emotion": (fd.get("emotion") or "").strip(),
                    "costume_state": {cid: costume[cid]} if cid in costume else {},
                    "gaze": f"{name} looks slightly off to one side, not at the camera",
                    "prompt_en": (f"Close-up of {name}: the face fills about 70% of the picture height, "
                                  f"the expression carries the moment"
                                  + (f", emotion {(fd.get('emotion') or '').strip()}" if fd.get("emotion") else "")
                                  + "; behind the head only soft blurred colours of the current place, "
                                    "no furniture and no second person.")}
        # деталь: руки, предмет, кусок обстановки — без лиц и без людей целиком
        предмет = (fd.get("prompt_en") or "").strip()
        return {**общее, "in_frame": [], "focus": "", "shot_size": "insert",
                "face_visible": False, "emotion": "", "costume_state": {}, "gaze": "",
                "prompt_en": ("Insert detail from the same moment, no face and no whole person in "
                              "the picture: a close view of the hands, the object or the surface that "
                              "matters most in this shot. The shot it belongs to is: "
                              + предмет[:400] +
                              " Draw ONLY the detail, filling the frame, with the place behind it "
                              "soft and out of focus.")}

    def _extra_jobs(self, idxs):
        """Задания врезок. Бланк шаблонный — ни одного вызова модели."""
        jobs = []
        if not idxs:
            return jobs
        ctx = self._job_ctx()
        доска = self.p("text", "storyboard.json")
        board_time = os.path.getmtime(доска) if os.path.exists(доска) else None
        for idx in idxs:
            fd = ctx["by_idx"].get(int(idx))
            if not fd:
                continue
            job = self._job_for(int(idx), self._seed_base + 300000 + int(idx),
                                frame=self.extra_frame(fd, ctx["names"]))
            if not job:
                # Лицо героя без листа не собирается. Врезка-деталь людей не требует —
                # иначе при возобновлении эта врезка «не хватает» вечно и каждый повтор
                # заново открывал бы «Картинки» и «Сборку» (27.09).
                job = self._job_for(int(idx), self._seed_base + 300000 + int(idx),
                                    frame=self.extra_frame({**fd, "focus": "", "in_frame": []}, ctx["names"]))
            if not job:
                log(f"  ! кадр {idx}: врезка не собралась — пропускаю, ролик это не валит")
                continue
            job["key"] = f"frame_{int(idx):05d}_extra"
            job["path"] = self.extra_path(idx)
            if board_time:
                job["drawn_after"] = board_time
            jobs.append(job)
        return jobs

    def speaker_frame(self, fd: dict, names: Dict[str, str]) -> dict:
        """Бланк крупного плана говорящего: та же сцена, место, костюм и слой,
        в кадре один говорящий, без рук из-за кадра, предметов, звука и чужих людей."""
        cid = fd.get("quote_by") or ""
        name = names.get(cid) or FP.en_name(cid)
        emotion = (fd.get("emotion") or "").strip()
        costume = fd.get("costume_state") if isinstance(fd.get("costume_state"), dict) else {}
        return {
            "idx": fd["idx"], "scene_num": fd.get("scene_num", 0), "location": fd.get("location", ""),
            "layer": fd.get("layer") or "now",
            "costume_state": {cid: costume[cid]} if cid in costume else {},
            "in_frame": [cid], "focus": cid, "quote_by": cid,
            "shot_size": "close", "face_visible": True, "angle": "eye", "panel": "",
            "sfx": "", "contact": "", "props_seen": [], "staging": "", "pose": "", "posture": "",
            "distance": "", "changed": "", "camera_pos": "", "emotion": emotion,
            "gaze": f"{name} looks at the person being spoken to, just past the edge of the picture",
            "prompt_en": (f"Close-up of the speaker {name}: the face takes about 70% of the panel height, "
                          f"the mouth slightly open mid-word, the eyes on the person {name} is talking to"
                          + (f", emotion {emotion}" if emotion else "")
                          + "; the background is soft blurred colours of the current place."),
        }

    def _speaker_jobs(self, idxs):
        """Задания крупных планов и строки «ошибка» для тех, что собрать не удалось.
        Ошибка громкая: иначе шаг «готов», а обработчик на каждом «Повторить»
        снова пересобирал бы ролик без крупного плана."""
        jobs, failed = [], []
        if not idxs:
            return jobs, failed
        ctx = self._job_ctx()
        for idx in idxs:
            fd = ctx["by_idx"].get(int(idx))
            job = self._job_for(int(idx), 200000 + int(idx), frame=self.speaker_frame(fd, ctx["names"])) if fd else None
            if job:
                job["key"] = f"frame_{int(idx):05d}_speaker"
                job["path"] = self.speaker_path(idx)
                jobs.append(job)
                continue
            why = (f"крупный план говорящего {(fd or {}).get('quote_by')} не собран: листы героев "
                   f"не отдали его эталон (лист на диске есть)")
            log(f"  ! кадр {idx}: {why}")
            failed.append({"key": f"frame_{int(idx):05d}_speaker", "path": self.speaker_path(idx),
                           "status": "ошибка", "error": why, "attempts": 0, "soften_level": 0})
        return jobs, failed

    def _quote_images_only(self, target_frames, selected_ids, selected_results, quote_todo, threads,
                           extra_todo=()):
        """Готовый ролик в новом режиме реплик: все кадры на месте, докупаются только
        крупные планы и перерисовки кадров с облаком. Задания остальных кадров НЕ
        пересобираются: листы героев нарезаются заново при каждом проходе, и сверка
        старых квитанций с новыми эталонами объявила бы чужим каждый готовый кадр
        (проверено на копии ролика 1a566e52 17.09.2026: 104 ошибки из 104)."""
        log(f"картинки: все {len(target_frames)} кадров на месте — дорисовываю только реплики: "
            f"облаков в кадре {len(quote_todo['panel'])}, крупных планов говорящего {len(quote_todo['closeup'])}, "
            f"врезок {len(extra_todo)}")
        res = list(selected_results) + [
            {"key": f"frame_{fd['idx']:05d}", "path": self.p("frames", f"{fd['idx']:05d}.png"),
             "status": "уже есть", "attempts": 0, "soften_level": 0, "seconds": 0}
            for fd in target_frames if fd["idx"] not in selected_ids]
        speaker_jobs, speaker_failed = self._speaker_jobs(quote_todo["closeup"])
        res += speaker_failed
        quotes = {fd["idx"]: (fd.get("quote") or "").strip() for fd in target_frames if fd["idx"] in quote_todo["panel"]}
        # Та же проверка эталонов, что перед основным прогоном: задания перерисовок
        # собираются здесь только для проверки, repair.run соберёт их тем же _job_for.
        redo = self._bubble_redo(quotes)
        redraw_jobs = [self._job_for(idx, 0) for idx in redo]
        # Врезки хука (аудит F03, 27.09): при готовых кадрах докупаются только они.
        extra_jobs = self._extra_jobs(extra_todo)
        if speaker_jobs or redraw_jobs or extra_jobs:
            pf = [f"frame_{idx:05d}: задание перерисовки не собрано" for idx, job in zip(redo, redraw_jobs) if not job]
            _stop_on_preflight(pf + _preflight_jobs(speaker_jobs + extra_jobs + [j for j in redraw_jobs if j]))
            q = IM.quota()
            log(f"квота: свободно {q['limit']-q['used']} из {q['limit']} в этом часу, потоков {q['threads']}")
        if speaker_jobs:
            log(f"  крупных планов говорящего: заказываю {len(speaker_jobs)}, потоков {threads}")
            done = IM.generate_many(speaker_jobs, threads=threads)
            for result in done:
                if result.get("status") == "готово":
                    self.invalidate_frame_render(int(result["key"].split("_")[1]))
                elif result.get("status") == "ошибка":
                    log(f"  ! {result['key']}: {(result.get('error') or '')[:120]}")
            res += done
        if extra_jobs:
            log(f"  врезок в хуке: заказываю {len(extra_jobs)} (вторая картинка внутри кадра), потоков {threads}")
            # 27.09.2026 ORCH-F03-R1: пометка до покупки, снимается после сброса (см. начало step_images)
            from web_queue import write_json
            отметка = self.p("frames", "_extra_pending.json")
            write_json(отметка, sorted(int(j["key"].split("_")[1]) for j in extra_jobs))
            done = IM.generate_many(extra_jobs, threads=threads)
            for result in done:
                if result.get("status") == "готово":
                    # врезку не удаляет (см. invalidate_frame_render), убирает клипы и видео
                    self.invalidate_frame_render(int(result["key"].split("_")[1]))
                elif result.get("status") == "ошибка":
                    log(f"  ! {result['key']}: {(result.get('error') or '')[:120]}")
            if os.path.exists(отметка):
                os.remove(отметка)
            res += done
        res = self._redraw_with_bubbles(quotes, res, threads)
        order = {f"frame_{fd['idx']:05d}": i for i, fd in enumerate(target_frames)}
        res.sort(key=lambda r: (order[r["key"][:11]], r["key"]))
        bad = [r for r in res if r.get("status") == "ошибка"]
        log(f"картинки реплик: ошибок {len(bad)}")
        return res

    def _bubble_redo(self, quotes: Dict[int, str]) -> List[int]:
        """Готовые кадры, на диске нарисованные без облака своей реплики.
        Кадр, только что нарисованный основным проходом, уже с облаком — его не трогаем."""
        from generation_receipts import valid_image
        return [idx for idx, quote in sorted(quotes.items())
                if valid_image(self.p("frames", f"{idx:05d}.png")) and self.bubble_drawn(idx) != quote]

    def _redraw_with_bubbles(self, quotes: Dict[int, str], results, threads):
        """Готовые кадры без облака своей реплики перерисовываются как пересдача:
        кандидат в frames/.repair, копия оригинала там же (repair.run), кадр
        подменяется только целым кандидатом. Повторный запуск берёт кандидата по
        квитанции и второй раз не покупает."""
        redo = self._bubble_redo(quotes)
        if not redo:
            return results
        log(f"облака в кадре: перерисовываю {len(redo)} готовых кадров с репликой")
        rr = RP.run([{"idx": idx, "why": "облако реплики в кадре"} for idx in redo], self._job_for,
                    IM.generate_many, round_no=QUOTE_ROUND, threads=threads, log=log)
        by_key = {r["key"]: r for r in results}
        outcome = {row["idx"]: row for row in rr.get("frames", [])}
        for idx in redo:
            key = f"frame_{idx:05d}"
            row = outcome.get(idx)
            if row and row["status"] == "replaced":
                self.invalidate_frame_render(idx)
                by_key[key] = {**by_key.get(key, {}), "key": key, "status": "готово",
                               "path": self.p("frames", f"{idx:05d}.png"), "quote_bubble_redrawn": True}
            else:
                why = (row or {}).get("why") or "задание не собрано"
                by_key[key] = {**by_key.get(key, {}), "key": key, "status": "ошибка",
                               "error": f"облако реплики не дорисовано: {why}"}
        return list(by_key.values())

    def frames_with_bubbles(self, frames) -> List[dict]:
        """Копии бланков; у кадра, нарисованного с облаком своей реплики, поле
        _speech_bubble — текст облака (приёмка и судья не считают его лишней надписью)."""
        out = []
        for fd in frames:
            quote = (fd.get("quote") or "").strip()
            drawn = self.bubble_drawn(int(fd["idx"])) if quote else None
            out.append({**fd, "_speech_bubble": drawn} if drawn and drawn == quote else fd)
        return out

    def _image_seed(self, idx):
        """An explicit retry may replace a known terminal failure, never an uncertain POST."""
        from web_queue import read_json, write_json
        path = self.p("frames", "_seed_overrides.json")
        saved = read_json(path, {})
        previous = saved.get(str(idx), {})
        receipt = read_json(self.p("frames", f"{idx:05d}.png.generation.json"), {})
        attempts = receipt.get("attempts", [])
        last = attempts[-1] if attempts else {}
        retry_id = getattr(self, "image_retry_id", None)
        if retry_id and last.get("status") in ("failed", "rejected") and previous.get("retry_id") != retry_id:
            previous = {"retry_id": retry_id, "seed": secrets.randbelow(2_000_000_000) + 1}
            saved[str(idx)] = previous
            write_json(path, saved)
        return previous.get("seed", self._seed_base + idx)

    @property
    def _seed_base(self) -> int:
        """Начало отсчёта зёрен картинок. У ролика-перевода оно своё, поэтому кадры выходят
        другими, хотя сцены и описания те же — как просил владелец: картинки заново."""
        if not self.source_project:
            return 100000
        salt = hashlib.sha256(os.path.basename(self.root.rstrip("/")).encode()).hexdigest()[:6]
        return 1_000_000 + int(salt, 16) % 900_000_000

    def _save_job_views(self, jobs, publish=True):
        """Persist actual prompts and safe local reference copies for the UI."""
        from web_queue import read_json, write_json
        views = read_json(self.p("frames", "_jobs.json"), {})
        refdir = Path(self.p("web_refs"))
        refdir.mkdir(exist_ok=True)
        for job in jobs:
            refs = []
            for ref in job.get("refs", []):
                rel = None
                try:
                    header, encoded = ref.data_uri.split(",", 1)
                    if not header.startswith("data:image/") or ";base64" not in header:
                        raise ValueError("unsupported reference")
                    data = base64.b64decode(encoded, validate=True)
                    from PIL import Image
                    from io import BytesIO
                    with Image.open(BytesIO(data)) as im:
                        im.verify()
                        ext = ".jpg" if im.format == "JPEG" else ".png"
                    dest = refdir / (hashlib.sha256(data).hexdigest() + ext)
                    if not dest.exists():
                        dest.write_bytes(data)
                    rel = str(dest.relative_to(self.root))
                except (ValueError, OSError):
                    pass
                refs.append({"name": ref.name, "path": rel})
            key = _job_idx(job["key"])
            views[key] = {"prompt": job["prompt"], "references": refs, "seed": job.get("seed")}
        if publish:
            write_json(self.p("frames", "_jobs.json"), views)
        return views

    def invalidate_frame_render(self, idx):
        """Move only this frame's derived images and clips into a retained backup."""
        from web_queue import read_json
        import uuid
        paths = [self.p("frames", ".prepared", f"{idx:05d}.png"),
                 self.p("frames", ".prepared", f"{idx:05d}_speaker.png"),
                 self.p("frames", ".prepared", f"{idx:05d}_extra.png"),
                 self.p("derived", f"{idx:05d}_quote.png"),
                 self.p("derived", f"{idx:05d}_sfx.png")]
        # Чистка (denoise): копии кадра и облачка, собранные из чистой копии.
        paths += [self.p("frames", "clean", f"{idx:05d}{tail}") for tail in
                  (".png", ".png.clean.json", "_speaker.png", "_speaker.png.clean.json",
                   "_extra.png", "_extra.png.clean.json")]
        # Саму врезку НЕ удаляем: она нарисована отдельным заданием и к пересдаче кадра
        # отношения не имеет. Чистую копию врезки убрать надо — её пересоберут с нового кадра.
        paths += sorted(glob.glob(self.p("derived", f"{idx:05d}_quote_c*.png")) +
                        glob.glob(self.p("derived", f"{idx:05d}_sfx_c*.png")))
        mapping = read_json(self.p("out", "clip_map.json"), {})
        clip_ids = mapping.get(str(idx))
        if clip_ids is None:
            # Older projects have no map. Compute positions from their exact
            # timeline; quotes add a second clip without changing other indices.
            sb = self.step_storyboard()
            timing = read_json(self.p("text", "timing.json"), {})
            if timing:
                secs = AS.clip_seconds(timing["pairs"], timing["total"])
                pos = 1
                clip_ids = []
                for fd, duration in zip(sb["frames"], secs):
                    quote = (fd.get("quote") or "").strip()
                    count = 2 if quote and duration >= 3.0 and len(quote) <= 90 else 1
                    if fd["idx"] == idx:
                        clip_ids = list(range(pos, pos + count))
                        break
                    pos += count
            else:
                clip_ids = []
        for clip_idx in clip_ids:
            paths.append(self.p("clips", f"clip_{clip_idx:05d}.mp4"))
        for name in ("video.mp4", "silent.mp4", "acceptance.json"):
            paths.append(self.p("out", name))
        backup = Path(self.p("out", ".stale", uuid.uuid4().hex))
        moved = []
        for value in paths:
            src = Path(value)
            if src.exists():
                rel = src.relative_to(self.root)
                dest = backup / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, dest)
                moved.append(str(rel))
        return moved

    def retry_frame(self, idx: int, attempt_id: str, correction: str = ""):
        """One durable candidate; rejudge before swap and retain every original.

        Replay resumes the saved candidate/judge decision, including interruption
        between swapping the PNG and writing the selected verdict.
        """
        from web_queue import read_json, write_json
        import judge as J
        if not re.fullmatch(r"[a-f0-9]{32}", attempt_id):
            raise ValueError("Неверный номер пересдачи")
        idx = int(idx)
        folder = Path(self.p("retries", attempt_id))
        folder.mkdir(parents=True, exist_ok=True)
        state_path = folder / "state.json"
        state = read_json(state_path, {})
        current = Path(self.p("frames", f"{idx:05d}.png"))
        original = folder / "original.png"
        candidates = folder / "candidates"
        candidates.mkdir(exist_ok=True)
        candidate = candidates / f"{idx:05d}.png"

        def sha(path):
            return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None

        if state and (state["frame_id"] != idx or state["correction"] != correction):
            raise ValueError("Пересдача уже сохранена с другими параметрами")
        if state.get("stage") == "done":
            return state
        if not state:
            if not current.is_file():
                raise RuntimeError("Оригинальный кадр отсутствует: повторите незавершённое")
            old_verdict = read_json(self.p("judge", "verdicts.json"), {}).get(str(idx), {})
            RP.backup_image(current, original)
            state = {"frame_id": idx, "attempt_id": attempt_id, "correction": correction,
                     "seed": secrets.randbelow(2_000_000_000) + 1, "created_at": time.time(),
                     "original_sha256": sha(original), "original_verdict": old_verdict,
                     "stage": "prepared"}
            # This durable seed must precede the first provider POST.
            write_json(state_path, state)
        if state["stage"] == "prepared":
            job = self._job_for(idx, state["seed"])
            if not job:
                raise RuntimeError("У кадра нет заполненного бланка")
            job = dict(job)
            job["path"] = str(candidate)
            # Exactly one new candidate per explicit request; provider durability
            # belongs to generate_many and its accepted-ID receipt.
            job["tries"] = 1
            if correction:
                job["prompt"] += "\n\nCORRECTION (highest priority): " + correction
            state["prompt"] = job["prompt"]
            RP.save_candidate_job(job)
            write_json(state_path, state)
            if RP._intact(str(candidate)):
                results = IM.generate_many([job], threads=1)
                if not results or results[0].get("status") == "ошибка":
                    error = (results[0].get("error") if results else None) or "Кандидат не получен"
                    raise RuntimeError(error)
            issue = RP._intact(str(candidate))
            if issue:
                raise RuntimeError(issue)
            self._save_job_views_for_retry(job, folder)
            state["candidate_sha256"] = sha(candidate)
            state["stage"] = "generated"
            write_json(state_path, state)
        if state["stage"] == "generated":
            ctx = self._job_ctx()
            # Neighbor images support the same style question while only idx is
            # selected for judgment. Hard links avoid copying hundreds of MB.
            for fd in ctx["sb"]["frames"]:
                other = int(fd["idx"])
                src = Path(self.p("frames", f"{other:05d}.png"))
                dst = candidates / src.name
                if other != idx and src.exists() and not dst.exists():
                    os.link(src, dst)
            sk = self._skeleton_obj(self.step_skeleton())
            # Судится кандидат: облако — по его собственной квитанции.
            cand_attempts = read_json(str(candidate) + ".generation.json", {}).get("attempts") or [{}]
            cand_bubble = cand_attempts[-1].get("quote_bubble")
            judge_frames = [({**fd, "_speech_bubble": cand_bubble} if cand_bubble else fd)
                            if int(fd["idx"]) == idx else fd for fd in ctx["sb"]["frames"]]
            judged = J.run(judge_frames, str(candidates), ctx["sheets"], ctx["names"],
                           str(folder / "judge"), only=[idx], workers=1,
                           no_count=J.infants(sk.characters),
                           variants=getattr(self, "_variants", {}) or {}, log=log, lang=self.lang,
                           memories=ctx.get("memories"))
            verdict = (judged.get("verdicts") or {}).get(str(idx), {})
            state["candidate_verdict"] = verdict
            state["outcome"] = "replaced" if verdict.get("verdict") in ("accept", "warn") else "returned_original"
            state["stage"] = "judged"
            write_json(state_path, state)
        if state["stage"] == "judged":
            if sha(current) not in (state["original_sha256"], state["candidate_sha256"]):
                raise RuntimeError("Кадр изменён вне пересдачи: автоматическая подмена остановлена")
            if state["outcome"] == "replaced":
                RP.select_image(current, candidate)
                state["invalidated"] = self.invalidate_frame_render(idx)
                verdicts = read_json(self.p("judge", "verdicts.json"), {})
                selected = dict(state["candidate_verdict"])
                stat = current.stat()
                selected["stamp"] = f"{stat.st_size}:{int(stat.st_mtime)}"
                verdicts[str(idx)] = selected
                write_json(self.p("judge", "verdicts.json"), verdicts)
                views = read_json(self.p("frames", "_jobs.json"), {})
                selected_view = read_json(folder / "job_view.json", {})
                if selected_view:
                    views[str(idx)] = selected_view
                    write_json(self.p("frames", "_jobs.json"), views)
            # The original and its judge verdict are never overwritten by reject
            # or unknown; the failed candidate remains inspectable in history.
            state["stage"] = "done"
            state["finished_at"] = time.time()
            write_json(state_path, state)
        return state

    def _save_job_views_for_retry(self, job, folder):
        """Save candidate view without publishing it as the selected original."""
        from web_queue import read_json, write_json
        views = self._save_job_views([job], publish=False)
        view = views.get(_job_idx(job["key"]), {})
        write_json(folder / "job_view.json", view)

    def _qa_inputs(self):
        """Всё, от чего зависит отчёт step_qa. Кадры — тем же отбором, что в
        qa.run: соседские метрики зависят от каждого кадра и от их порядка."""
        import qa
        folder = self.p("frames")
        names = sorted(f for f in os.listdir(folder) if f.lower().endswith((".png", ".jpg"))
                       and not is_speaker_file(f))
        return {"frames": [[n, _file_sha256(os.path.join(folder, n))] for n in names],
                "storyboard_sha256": _file_sha256(self.p("text", "storyboard.json")),
                "code_sha256": _QA_CODE_SHA256, "limits": qa.LIMITS, "libs": _qa_libs()}

    def step_qa(self, write=True):
        """Приёмка: только читает готовую папку и считает числа.

        Отчёт — функция входов (_qa_inputs). Выпуск 10.09 гонял полный проход
        8 раз, из них 3 раза по тем же кадрам. Поэтому рядом с отчётом лежат его
        входы и SHA (_qa.json.input.json): всё совпало — отдаём тот же отчёт,
        иначе полный пересчёт. Неполный отчёт не запоминается.
        """
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import qa
        from web_queue import read_json, write_json
        report_path = self.p("frames", "_qa.json")
        inputs = self._qa_inputs()
        saved = read_json(report_path + ".input.json")
        try:
            raw = Path(report_path).read_bytes() if isinstance(saved, dict) else None
        except OSError:
            raw = None
        if raw is not None and saved.pop("report_sha256", None) == hashlib.sha256(raw).hexdigest() \
                and saved == inputs:
            log("  проверки кадров: кадры, раскадровка и код проверок не менялись — отчёт прошлого прохода")
            rep = json.loads(raw)
            for k, v in rep["summary"].items():
                log(f"  {k:38} {v}")
            if "manner_outliers" in rep:
                log(f"  выпало по манере рисунка (>2.5σ): {len(rep['manner_outliers'])}")
            if "style" in rep:
                log(f"  выпало из стиля: {len(rep['style']['outliers'])} кадров")
            return rep
        incomplete = []
        sb_path = self.p("text", "storyboard.json")
        rep = qa.run(self.p("frames"), sb_path)
        for k, v in rep["summary"].items():
            log(f"  {k:38} {v}")
        try:
            import qa_style, glob
            _paths = sorted(p_ for p_ in glob.glob(self.p("frames", "*.png")) if not is_speaker_file(p_))
            _st = qa_style.report(_paths)
            # qa_style молча пропускает кадр, который не прочитался (разовый сбой чтения).
            if len(_paths) >= 3 and _st.get("кадров") != len(_paths):
                incomplete.append("манера не по всем кадрам")
            _d = _st.get("_dist") or {}
            import numpy as _np
            _vals = _np.array(list(_d.values())) if _d else _np.array([])
            if len(_vals):
                _thr = float(_vals.mean() + 2.5 * _vals.std())
                rep["manner_outliers"] = [int(n[:5]) for n, v in _d.items() if v > _thr]
                log(f"  выпало по манере рисунка (>2.5σ): {len(rep['manner_outliers'])}")
        except Exception as e:
            log(f"  ! метрика манеры не отработала: {type(e).__name__}: {e}")
            incomplete.append("манера")
        try:
            import qa_clip, glob, numpy as np
            paths = sorted(p_ for p_ in glob.glob(self.p("frames", "*.png")) if not is_speaker_file(p_))
            st = qa_clip.style_report(paths)
            emb = st.pop("emb", None)
            for k, v in st.items():
                if k != "худшие кадры по стилю":
                    log(f"  {k:38} {v}")
            if emb is not None:
                sims = emb @ (emb.mean(0) / np.linalg.norm(emb.mean(0)))
                thr = float(np.percentile(sims, 10)) - 0.05
                out = [int(os.path.basename(paths[i])[:-4])
                       for i in range(len(paths)) if float(sims[i]) < thr]
                rep["style"] = {"outliers": out,
                                "median": st.get("стиль медиана")}
                log(f"  выпало из стиля: {len(out)} кадров")
        except Exception as e:
            log(f"  ! смысловые проверки не отработали: {type(e).__name__}: {e}")
            # Нет модуля (VPS без open_clip) — постоянное свойство среды, его
            # версия во входах; любой другой сбой мог быть разовым.
            if not isinstance(e, ImportError):
                incomplete.append("смысловые проверки")
        if any(r.get("text_unavailable") for r in rep["frames"]):
            incomplete.append("надписи")
        if any(r.get("error") for r in rep["frames"]):
            incomplete.append("кадр не прочитался")
        if write:
            tmp = self.p("frames", "_qa.json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rep, f, ensure_ascii=False)
            report_sha = _file_sha256(tmp)
            os.replace(tmp, report_path)
            if incomplete:
                log(f"  отчёт неполный ({', '.join(incomplete)}) — следующий проход посчитает заново")
            elif self._qa_inputs() != inputs:
                log("  кадры менялись во время проверок — следующий проход посчитает заново")
            else:
                write_json(report_path + ".input.json", {**inputs, "report_sha256": report_sha})
        return rep

    def step_judge(self, only=None, workers: int = 5):
        """Судья кадров моделью (judge.py): кадры риска + флаги приёмки."""
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import judge as J
        ctx = self._job_ctx()
        sk = self._skeleton_obj(self.step_skeleton())
        rep = self.step_qa(write=True)
        flags = {}
        by_idx = ctx["by_idx"]
        bubbles = {int(fd["idx"]): fd for fd in self.frames_with_bubbles(ctx["sb"]["frames"])}
        for row in (rep.get("frames") or []):
            if row.get("idx") is None:
                continue
            idx = int(row["idx"])
            why = []
            if row.get("bars_pct", 0) > 1.0:
                why.append("поля")
            bubble = bubbles.get(idx, {}).get("_speech_bubble")
            if row.get("text") and not (by_idx.get(idx, {}).get("sfx") or "").strip() \
                    and not RP.text_is_quote(row["text"], bubble):
                why.append("надписи")
            if why:
                flags[idx] = ", ".join(why)
        return J.run(list(bubbles.values()), self.p("frames"), ctx["sheets"], ctx["names"],
                     self.p("judge"), qa_flags=flags, only=only, workers=workers,
                     no_count=J.infants(sk.characters),
                     variants=getattr(self, "_variants", {}) or {}, log=log, lang=self.lang,
                     memories=ctx.get("memories"))

    def step_repair(self, rounds: int = 2, threads: int = 12, judge: bool = True):
        """Пересдача брака. Промпт тот же, зерно другое."""
        sb = self.step_storyboard()
        out = []
        for r in range(1, rounds + 1):
            rep = self.step_qa(write=True)
            if judge:
                # вердикты судьи добавляются к машинной приёмке (reject → пересдача)
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                import judge as J
                jr = self.step_judge(workers=min(threads, 5))
                jp = J.picks_for_repair(jr.get("verdicts") or {})
            else:
                jp = []
            picked = RP.pick(self.frames_with_bubbles(sb["frames"]), self.p("frames"), rep,
                             rep.get("style"), log=log)
            # Судья важнее машинных флагов: его reject — реальный брак (калибровка
            # 10.09), а флаги OCR/яркости — подозрения. Сначала судья, потом флаги,
            # общий потолок — 15% ролика, как в repair.pick.
            have = {p_["idx"] for p_ in jp}
            picked = jp + [p_ for p_ in picked if p_["idx"] not in have]
            limit = max(1, int(len(sb["frames"]) * RP.MAX_SHARE))
            if len(picked) > limit:
                log(f"  на пересдачу {len(picked)} кадров — больше {int(RP.MAX_SHARE*100)}% ролика, беру первые {limit} (судья впереди)")
                picked = picked[:limit]
            if not picked:
                log("приёмка: брака не нашлось, пересдавать нечего")
                break
            log(f"приёмка: на пересдачу {len(picked)} кадров")
            for p_ in picked[:8]:
                log(f"    кадр {p_['idx']}: {p_['why']}")
            from web_queue import read_json, write_json
            original_verdicts = read_json(self.p("judge", "verdicts.json"), {})
            rr = RP.run(picked, self._job_for, IM.generate_many,
                        round_no=r, threads=threads, log=log)
            # Кандидаты идут в frames/.repair, оригинал подменяется только целым
            # файлом — поэтому kept_original означает «брак остался, дыры нет».
            for f_ in rr.get("frames", []):
                if f_["status"] != "replaced":
                    log(f"    кадр {f_['idx']}: {f_['status']} — {f_['why'][:120]}")
            # Гейт судьи: кандидат, заменивший оригинал, отвечает на ТЕ ЖЕ вопросы;
            # если он всё ещё reject — возвращаем оригинал (копия лежит в .repair).
            replaced = [f_["idx"] for f_ in rr.get("frames", []) if f_["status"] == "replaced"]
            if replaced and judge:
                jr = self.step_judge(only=replaced, workers=threads)
                back = 0
                for idx in replaced:
                    v = (jr.get("verdicts") or {}).get(str(idx)) or {}
                    if v.get("verdict") == "reject":
                        orig = next((row.get("original_copy") for row in rr.get("frames", []) if row["idx"] == idx), None) or RP.original_copy(self.p("frames"), idx)
                        if orig:
                            RP.restore_image(self.p("frames", f"{idx:05d}.png"), orig)
                            restored = read_json(self.p("judge", "verdicts.json"), {})
                            restored[str(idx)] = next((row["original_verdict"] for row in rr.get("frames", []) if row["idx"] == idx and "original_verdict" in row), original_verdicts.get(str(idx), {}))
                            write_json(self.p("judge", "verdicts.json"), restored)
                            back += 1
                            log(f"    кадр {idx}: кандидат тоже брак ({', '.join(v.get('row', {}).get('notes') or [])[:80]}) — оригинал возвращён")
                rr["returned_original"] = back
            for idx in replaced:
                self.invalidate_frame_render(idx)
            out.append(rr)
            write_json(self.p("frames", "_repair.json"), out)
        json.dump(out, open(self.p("frames", "_repair.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        return out

    # ---------- шаг 7: озвучка ----------
    def step_fix_identity(self, threads: int = 6, max_share: float = 0.10):
        """Круг «не тот герой»: судья не узнал героя по кропу листа → пересдача
        с той же постановкой и строкой CORRECTION (лицо/волосы строго по эталону).
        Кандидат принимается только если судья теперь узнаёт всех названных.

        Почему отдельно от обычной пересдачи: новое зерно с тем же промптом
        подмену героя лечит плохо (замер Codex: 12/77/119 не вылечились даже при
        workers=1), а прямое указание «в прошлый раз лицо было не то» — это
        другой промпт, и его надо проверять тем же судьёй, а не верить на слово.
        """
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import judge as J
        vpath = self.p("judge", "verdicts.json")
        if not os.path.exists(vpath):
            log("исправление героев: вердиктов судьи нет — сначала --step judge")
            return {}
        verdicts = json.load(open(vpath, encoding="utf-8"))
        picks = J.picks_for_fix(verdicts)
        from web_queue import read_json, write_json
        saved_fix = read_json(self.p("frames", "_fix_identity.json"), {})
        pending_fix = saved_fix if saved_fix.get("status") == "judging" else None
        sb = self.step_storyboard()
        limit = max(1, int(len(sb["frames"]) * max_share))
        if len(picks) > limit:
            log(f"исправление героев: {len(picks)} кадров, беру первые {limit}")
            picks = picks[:limit]
        if not picks and not pending_fix:
            log("исправление героев: судья узнал всех, нечего исправлять")
            from web_queue import read_json, write_json
            saved = read_json(self.p("frames", "_fix_identity.json"))
            if saved is not None:
                return saved
            result = {"sent": 0, "fixed": 0, "returned_original": 0, "frames": []}
            write_json(self.p("frames", "_fix_identity.json"), result)
            return result
        ctx = self._job_ctx()
        names = ctx["names"]
        locks = ctx["locks"]
        by_name = {}
        for cid, nm in names.items():
            by_name.setdefault(nm, []).append(cid)
        skipped_reference = []

        def skip_reference(idx, reason):
            skipped_reference.append({"idx": idx, "reason": reason})
            log(f"    кадр {idx}: identity fix пропущен — {reason}; исходник не изменён")
            return None

        def build_job(idx: int, seed: int):
            j = self._job_for(idx, seed)
            if not j:
                return None
            who = next((p_["names"] for p_ in picks if p_["idx"] == idx), [])
            if not who:
                return skip_reference(idx, "не указаны персонажи")
            refs = j.get("refs") or []
            lines = []
            for nm in who:
                cids = by_name.get(nm, [])
                if len(cids) != 1 or not cids[0]:
                    return skip_reference(idx, "имя персонажа отсутствует или неоднозначно")
                cid = cids[0]
                positions = {view: [i + 1 for i, r in enumerate(refs)
                                    if getattr(r, "name", None) == f"{cid}_{view}.png"]
                             for view in ("face", "front")}
                if any(len(found) > 1 for found in positions.values()):
                    return skip_reference(idx, f"эталон {cid} неоднозначен")
                view = next((v for v in ("face", "front") if positions[v]), None)
                if view is None:
                    return skip_reference(idx, f"эталон {cid} отсутствует")
                pos = positions[view][0]
                feature = "face" if view == "face" else "visible appearance"
                role = "face reference" if view == "face" else "body reference"
                lock = locks.get(cid, "")
                lines.append(f"{nm}: in the previous attempt this person did NOT look like the "
                             f"reference. Draw {nm} with exactly the {feature}, hairstyle, hair length and "
                             f"hair colour of IMAGE {pos} ({cid}_{view}.png, {role})" + (f" — {lock}" if lock else "") +
                             ". Nothing about this person may be invented.")
            j["prompt"] = j["prompt"] + "\n\nCORRECTION (highest priority): " + " ".join(lines)
            return j

        rr = pending_fix or RP.run(picks, build_job, IM.generate_many, round_no=9, threads=threads, log=log)
        if not pending_fix:
            rr["skipped_reference"] = skipped_reference
        rr["status"] = "judging"
        write_json(self.p("frames", "_fix_identity.json"), rr)
        replaced = [f_["idx"] for f_ in rr.get("frames", []) if f_["status"] == "replaced"]
        fixed, back = 0, 0
        if replaced:
            jr = self.step_judge(only=replaced, workers=min(threads, 5))
            for idx in replaced:
                v = (jr.get("verdicts") or {}).get(str(idx)) or {}
                ident = ((v.get("row") or {}).get("identity") or {})
                ok = v.get("verdict") in ("accept",) and not any(x is False for x in ident.values())
                if ok:
                    fixed += 1
                else:
                    orig = next((row.get("original_copy") for row in rr.get("frames", []) if row["idx"] == idx), None) or RP.original_copy(self.p("frames"), idx)
                    if orig:
                        RP.restore_image(self.p("frames", f"{idx:05d}.png"), orig)
                        from web_queue import read_json, write_json
                        restored = read_json(vpath, {})
                        restored[str(idx)] = next((row["original_verdict"] for row in rr.get("frames", []) if row["idx"] == idx and "original_verdict" in row), verdicts.get(str(idx), {}))
                        write_json(vpath, restored)
                        back += 1
        for idx in replaced:
            self.invalidate_frame_render(idx)
        log(f"исправление героев: отправлено {rr.get('sent', 0)}, кандидатов {len(replaced)}, "
            f"судья принял {fixed}, возвращено оригиналов {back}")
        rr["fixed"] = fixed
        rr["returned_original"] = back
        rr["status"] = "complete"
        from web_queue import write_json
        write_json(self.p("frames", "_fix_identity.json"), rr)
        return rr

    def invalidate_master(self, timing=False):
        """Discard only derived master/timing; clip signatures retain unchanged clips."""
        import uuid
        backup = Path(self.p("out", ".stale", uuid.uuid4().hex))
        names = [("out", "video.mp4"), ("out", "silent.mp4"), ("out", "acceptance.json")]
        if timing:
            names.append(("text", "timing.json"))
        moved = []
        for parts in names:
            source = Path(self.p(*parts))
            if source.exists():
                destination = backup.joinpath(*parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, destination)
                moved.append("/".join(parts))
        return moved

    def step_voice(self, force=False):
        out = self.p("audio", "voice.mp3")
        text = self.step_expand()
        rep = self.segment(text)
        voice_id = self.voice
        engine = VO.tts_engine()
        # Предполётная проверка платного движка идёт ДО любой покупки: нет ключа или голоса —
        # ролик не встаёт, а озвучивается бесплатным edge, и это видно в журнале.
        if engine in ("voicegen", "lumean", "fastgen"):
            import voice_voicegen as VG
            import voice_fastgen as FGV
            try:
                if engine == "voicegen":
                    VG.preflight(self.lang)
                elif engine == "fastgen":
                    FGV.preflight(self.lang)
                else:
                    _key, template = LU.preflight(self.lang)
            except (VG.VoiceGenConfigError, LU.LumeanConfigError, FGV.FastGenTTSConfigError) as e:
                if LUMEAN_STRICT:
                    raise
                log(f"  озвучка: {e} — этот ролик озвучит edge-tts, голосом {self.voice}")
                engine = "edge"
        if engine == "voicegen":
            import voice_voicegen as VG
            chosen = VG.voice_for(self.lang)
            voice_id = VG.voice_id(chosen)
            blocks = VO.split_blocks(text, rep.beats, block_seconds=VG.BLOCK_SECONDS,
                                     max_chars=VG.MAX_CHARS, wpm=self.wpm)
            log(f"озвучка через VoiceGen: {len(blocks)} блоков по ~{VG.BLOCK_SECONDS}s; голос {chosen}, "
                f"голос проекта {self.voice} не используется")
            VG.synth(blocks, self.p("audio"), lang=self.lang, log=log)
        elif engine == "fastgen":
            import voice_fastgen as FGV
            chosen = FGV.voice_for(self.lang)
            voice_id = FGV.voice_id(chosen)
            blocks = VO.split_blocks(text, rep.beats, block_seconds=FGV.BLOCK_SECONDS,
                                     max_chars=FGV.MAX_CHARS, wpm=self.wpm)
            log(f"озвучка через FastGen: {len(blocks)} блоков по ~{FGV.BLOCK_SECONDS}s; модель {FGV.MODEL}, "
                f"голос {chosen}, голос проекта {self.voice} не используется")
            FGV.synth(blocks, self.p("audio"), lang=self.lang, log=log)
        elif engine == "lumean":
            voice_id = LU.voice_id(template, LU.voice_for(self.lang))
            blocks = VO.split_blocks(text, rep.beats, block_seconds=LU.BLOCK_SECONDS, max_chars=LU.MAX_CHARS,
                                     wpm=self.wpm)
            chosen = LU.voice_for(self.lang)
            log(f"озвучка через Lumean: {len(blocks)} блоков по ~{LU.BLOCK_SECONDS}s; голос "
                + (f"{chosen} (подмена в заказе)" if chosen else f"из шаблона {template}")
                + f", голос проекта {self.voice} не используется")
            LU.synth(blocks, self.p("audio"), lang=self.lang, log=log)
        else:
            blocks = VO.split_blocks(text, rep.beats, wpm=self.wpm)
            log(f"озвучка: {len(blocks)} блоков по ~{VO.BLOCK_SECONDS}s")
            VO.synth(blocks, self.p("audio"), voice=self.voice, log=log)
        from web_queue import read_json, write_json
        # path и word_metadata_path нужны align_native: тайминги по меткам синтезатора
        # Manifest precedes concat. A crash after saving voice.mp3 therefore
        # cannot lose native alignment metadata or trigger another TTS purchase.
        write_json(self.p("audio", "blocks.json"),
                   [{"idx": b.idx, "start_char": b.start_char, "end_char": b.end_char,
                    "seconds": b.seconds, "path": b.path,
                    "word_metadata_path": b.words_path} for b in blocks])
        signature = {"request": VO.voice_fingerprint(text, voice_id, "+0%"),
                     "blocks": [hashlib.sha256(Path(b.path).read_bytes()).hexdigest() for b in blocks]}
        signature_path = self.p("audio", "voice.input.json")
        if not force and read_json(signature_path) == signature and os.path.isfile(out) and VO.duration(out) > 0:
            log("озвучка: целая дорожка и метки уже есть, синтез не повторяется")
            return out, VO.duration(out)
        self.invalidate_master(timing=True)
        total = VO.concat(blocks, out)
        write_json(signature_path, signature)
        log(f"озвучка: дорожка {total/60:.1f} мин при заказе {self.target_minutes} мин "
            f"({(total / 60 / max(1, self.target_minutes) - 1) * 100:+.0f}%)")
        return out, total

    def _frame_texts(self, sb) -> List[str]:
        """Текст каждого кадра НА ЯЗЫКЕ ЭТОГО РОЛИКА.

        У перевода раскадровка скопирована с эталона вместе с графой «text» — там лежит
        текст эталона, на чужом языке. Сверять его с речью бессмысленно: 26.09 из-за этого
        разъехались тайминги у 47 роликов из 80. Свои доли лежат в text/beats_fixed.json
        и указывают в свой final.txt — собираем текст кадра по ним.
        """
        frames = sb["frames"]
        fixed = self.p("text", "beats_fixed.json")
        if not os.path.exists(fixed):
            return [f.get("text") or "" for f in frames]
        rows = json.load(open(fixed, encoding="utf-8"))
        text = Path(self.p("text", "final.txt")).read_text(encoding="utf-8")
        out, взято = [], 0
        for f in frames:
            a, b = int(f.get("beat_from") or 0), int(f.get("beat_to") or 0)
            if 1 <= a <= len(rows) and 1 <= b <= len(rows):
                out.append(text[rows[a - 1]["start"]:rows[b - 1]["end"]])
                взято += 1
            else:
                out.append(f.get("text") or "")
        log(f"тайминги: текст кадров взят из своих долей у {взято} из {len(frames)} кадров")
        return out

    # ---------- шаг 8: тайминги ----------
    def step_align(self, force=False):
        out = self.p("text", "timing.json")
        if os.path.exists(out) and not force:
            log("тайминги: уже есть, пропускаю")
            return json.load(open(out, encoding="utf-8"))
        self.invalidate_master()
        audio, total = self.step_voice()
        sb = self.step_storyboard()
        texts = self._frame_texts(sb)
        # Сначала метки самого синтезатора (WordBoundary): точное время первого слова
        # каждого кадра. Whisper — запасной путь: на 492 кадрах он опаздывал на
        # 0.3–1.8 с в 225 кадрах (не узнаёт имена героев). Причина отката пишется
        # в timing.json полем native_error, а не теряется в логе.
        words, native_error = [], None
        try:
            if AL.blocks_without_word_marks(self.p("audio", "blocks.json")):
                raise AL.NativeAlignmentError("озвучка без меток слов (Lumean)")
            pairs, src_kind = AL.native_pairs(self.p("audio", "blocks.json"),
                                              self.p("text", "storyboard.json"), total,
                                              self.p("text", "final.txt"), audio)
        except AL.NativeAlignmentError as e:
            native_error = str(e)
            language = LG.iso(self.lang)
            log(f"  ! метки синтезатора не годятся: {native_error} — иду через whisper")
            log(f"распознавание {total/60:.1f} мин звука (язык {language}) — это самый долгий шаг")
            words_path = self.p("audio", "words.json")
            words = AL.cached_words(words_path, audio, language)
            if words:
                log(f"распознавание уже сделано раньше: беру {len(words)} слов из {words_path}")
            else:
                # Сначала пробуем машину с видеокартой (MANGA_ALIGN_HOST): там тот же
                # faster-whisper считает 80 минут речи за пять минут вместо получаса.
                import align_remote as AR
                words = None
                if AR.enabled():
                    words = AR.words(audio, language, "base", log=log)
                    if not words and AR.settings()["fallback"] == "none":
                        raise RuntimeError("распознавание на другой машине не удалось "
                                           "(MANGA_ALIGN_FALLBACK=none)")
                if not words:
                    words = AL.whisper_words(audio, language=language, model_size="base")
                AL.save_words(words_path, audio, language, words)
                log(f"распознано слов: {len(words)} (сохранены, повтор не будет считать заново)")
            pairs, src_kind = AL.align(texts, words, total)
            if src_kind != "по словам":
                log(f"  ! ВНИМАНИЕ: тайминги получены {src_kind}, а не по словам речи — "
                    f"картинки будут стоять не точно. Причина: распознавание не дало слов.")
        # Предохранитель: раскладка, где один кадр держится дольше двадцатой части ролика,
        # — это не ролик, а стоп-кадр. Лучше ровная раскладка по длине текста, чем такое.
        if pairs and total > 0 and len(pairs) > 20:
            самый = max(float(b) - float(a) for a, b in pairs)
            if самый > 0.05 * total:
                log(f"  ! раскладка вырожденная: один кадр держится {самый/60:.1f} мин "
                    f"из {total/60:.1f} — беру ровную раскладку по длине текста")
                lens = [max(1, len(t.strip())) for t in texts]
                сумма = sum(lens) or 1
                pairs, акк = [], 0.0
                for L in lens:
                    d = total * L / сумма
                    pairs.append((round(акк, 3), round(акк + d, 3)))
                    акк += d
                src_kind = "пропорционально (раскладка по словам не сошлась)"
        chk = AL.check(pairs, total)
        log(f"тайминги ({src_kind}): медиана кадра {chk['median']}s, "
            f"{'сошлось' if chk['ok'] else 'ПРОБЛЕМЫ: ' + '; '.join(chk['problems'][:3])}")
        payload = {"total": total, "pairs": pairs, "check": chk, "source": src_kind,
                   "native_error": native_error, "words_recognised": len(words)}
        tmp = out + ".tmp"
        json.dump(payload, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(tmp, out)
        return payload

    # ---------- шаг «Чистка» (MANGA_CLEAN, модуль denoise) ----------
    def step_clean(self, force=False, only=None) -> dict:
        """Чистые копии кадров и крупных планов говорящего в frames/clean/. Оригиналы не меняются.
        Уже чистое (отпечаток исходника совпадает) не чистится заново; пересданный кадр — чистится.
        only — номера кадров (пересдача с кнопки): остальные кадры не трогаются вовсе.
        При esrgan кадры из запасного сглаживания пробуются снова, только если машина сейчас отвечает."""
        cfg = DN.settings()
        if cfg["mode"] == "off":
            return {"note": "Пропущено: чистка выключена (MANGA_CLEAN=off).", "total": 0}
        only = None if only is None else {int(i) for i in only}
        items, upgrades, kept = [], [], 0
        for fd in self.step_storyboard()["frames"]:
            if only is not None and int(fd["idx"]) not in only:
                continue
            for src in (self.p("frames", f"{int(fd['idx']):05d}.png"), self.speaker_path(fd["idx"]),
                        self.extra_path(fd["idx"])):
                if RP._intact(src):
                    continue
                sha = DN.sha256(src)
                # пиксели — кадр без звёздочки генератора (как в сборке), отпечаток — сам кадр
                item = lambda: dict(src=src, sha=sha, input=AS.prepare_frame(src), dst=DN.clean_path(src))
                if force or not DN.usable(src, sha):
                    items.append(item())
                elif DN.upgradable(src, cfg["mode"], sha):
                    upgrades.append(item())
                else:
                    kept += 1
        if upgrades:
            if DN.reachable(cfg):
                items += upgrades
            else:
                log(f"  {cfg['label']} не отвечает — {len(upgrades)} сглаженных раньше кадров оставлены как есть")
                kept += len(upgrades)
        log(f"чистка ({cfg['mode']}): к чистке {len(items)}, уже чистых {kept}")
        res = DN.clean_many(items, cfg, log=log)
        res["kept"] = kept
        if not items:
            res["note"] = f"Все кадры уже почищены ({kept})."
        log("  " + res["note"])
        return res

    def render_image(self, src, clean_on):
        """(картинка для сборки, метка для имён облачков): чистая копия, если она из этого кадра, иначе как раньше."""
        if clean_on:
            c = DN.usable(src)
            if c:
                return c, "_c" + DN.sha256(c)[:10]
        return AS.prepare_frame(src), ""

    # ---------- шаг 9: сборка ----------
    def step_render(self, force=False):
        out = self.p("out", "video.mp4")
        if self.strict_render:
            expected = self.step_storyboard()["frames"]
            missing = [fd["idx"] for fd in expected
                       if RP._intact(self.p("frames", f"{fd['idx']:05d}.png"))]
            if missing:
                raise RuntimeError(f"Сборка остановлена: не готовы кадры {missing[:20]}. Повторите незавершённое.")
            if not expected:
                raise RuntimeError("Раскадровка пуста — собирать нечего")
        if os.path.exists(out) and not force:
            log("ролик: уже есть, пропускаю")
            return out
        timing = self.step_align()
        sb = self.step_storyboard()
        audio = self.p("audio", "voice.mp3")
        pairs = timing["pairs"]
        # Длительности — по АБСОЛЮТНЫМ границам, квантованным в кадры (assemble.clip_seconds).
        # Пол max(0.6, …) удлинял кадры короче 0,6 с и сдвигал все стыки к концу ролика
        # (до 0,3–0,5 с, замер 09.09); сумма клипов теперь == total с точностью до кадра.
        secs = AS.clip_seconds(pairs, timing["total"])

        # Язык манхвы, как у конкурентов (замер 09.09 по 90 кадрам): звук — надпись
        # на панели (рисует генератор, код — запасной путь); реплика — отдельный
        # кадр-облачко на часть времени кадра (рисует код по графе quote).
        derived = self.p("derived")
        os.makedirs(derived, exist_ok=True)
        try:
            scenes_for_render = self.step_scenes()
        except Exception:
            scenes_for_render = {}
        fb_path = self.p("text", "sfx_fallback.json")
        sfx_fallback = set(json.load(open(fb_path))) if os.path.exists(fb_path) else set()
        clips, missing, n_sfx, n_quote = [], 0, 0, 0
        n_extra = 0
        n_panel, n_closeup = 0, 0
        clean_on = DN.enabled()
        n_clean = 0
        quote_plan = self.quote_plan(sb["frames"])
        clip_map = {}
        carry = 0.0   # время дыр в самом начале, пока нет ни одной картинки
        for i, fd in enumerate(sb["frames"]):
            img = self.p("frames", f"{fd['idx']:05d}.png")
            if not (os.path.exists(img) and os.path.getsize(img) > 4096):
                missing += 1
                # дыра НЕ сдвигает остальные: продлеваем предыдущую картинку
                if clips:
                    clips[-1].seconds += secs[i]
                else:
                    carry += secs[i]
                continue
            # Звёздочка генератора убирается ДО облачка и надписи (assemble.prepare_frame):
            # bubble_frame пересжимает кадр, и потом пятно уже не там.
            # Чистая копия (шаг «Чистка») сделана уже из кадра без звёздочки.
            img, ctag = self.render_image(img, clean_on)
            n_clean += bool(ctag)
            dur = secs[i] + carry
            carry = 0.0
            prev_mode = clips[-1].mode if clips else -1
            mode = AS.mode_for(fd.get("shot_size", ""), fd.get("gaze", ""), prev_mode,
                               fd.get("panel", ""))
            # Польские/чешские кавычки „…” снимаем, как frameprompt.sfx_word; «» — как раньше.
            sfx = (fd.get("sfx") or "").strip().strip('„“”‚‘’').strip()
            # Слово-звук рисует сам генератор (блок SOUND WORD, 7/7 в тесте).
            # Код дорисовывает его только там, где приёмка (OCR) не нашла на
            # кадре никакой надписи, — список text/sfx_fallback.json пишет QA.
            # Иначе на панели было бы два «БАМ».
            if sfx and len(sfx) <= 8 and fd["idx"] in sfx_fallback:
                out_img = os.path.join(derived, f"{fd['idx']:05d}_sfx{ctag}.png")
                if not os.path.exists(out_img):
                    side = ""
                    sc_ = scenes_for_render.get(fd.get("scene_num")) if scenes_for_render else None
                    if sc_ is not None:
                        side = (getattr(sc_, "sides", {}) or {}).get(fd.get("focus") or "", "")
                    anchor = "right" if side == "left" else "left"
                    OV.sfx_overlay(img, sfx, out_img, anchor=anchor)
                img = out_img
                n_sfx += 1
            clip_map[str(fd["idx"])] = [len(clips) + 1]
            quote = (fd.get("quote") or "").strip()
            kind = quote_plan.get(int(fd["idx"]), "")
            if kind == "panel" and self.bubble_drawn(fd["idx"]) == quote:
                n_panel += 1        # облако нарисовано в самом кадре: панель стоит всё время
            speaker = self.speaker_path(fd["idx"])
            if kind == "closeup" and dur >= CLOSEUP_MIN_SECONDS and not RP._intact(speaker):
                # панель 60% времени, крупный план говорящего 40% — в целых кадрах
                panel_dur = round(dur * 0.6 * AS.FPS) / AS.FPS
                close_mode = AS.mode_for("close", fd.get("gaze", ""), mode, "")
                clips.append(AS.Clip(image=img, seconds=panel_dur, mode=mode))
                clips.append(AS.Clip(image=self.render_image(speaker, clean_on)[0], seconds=dur - panel_dur, mode=close_mode))
                clip_map[str(fd["idx"])].append(len(clips))
                n_closeup += 1
                continue
            if kind == "bubble" and dur >= 3.0 and len(quote) <= QUOTE_MAX:
                # панель 60% времени, облачко 40% — реплика звучит ближе к концу бита
                # делим в целых кадрах, чтобы панель + облачко == dur ровно
                panel_dur = round(dur * 0.6 * AS.FPS) / AS.FPS
                bub = os.path.join(derived, f"{fd['idx']:05d}_quote{ctag}.png")
                if not os.path.exists(bub):
                    # сторона хвостика — у паспорта сцены: там за каждым героем закреплена
                    # своя сторона на всю сцену. Прежний поиск слова «left» в первых 60 знаках
                    # мизансцены брал того, кто случайно упомянут первым (кадр 100: хвостик
                    # ушёл к Джулиану, а говорил Нейтан).
                    _by = (fd.get("quote_by") or "").strip()
                    _sc = scenes_for_render.get(fd.get("scene_num")) if scenes_for_render else None
                    _side = ((getattr(_sc, "sides", None) or {}).get(_by) or "").lower() if _sc else ""
                    if _side in ("left", "right"):
                        tail = "bottom-" + _side
                    else:
                        tail = "bottom-left" if "left" in (fd.get("staging") or "").lower()[:60] else "bottom-right"
                    OV.bubble_frame(quote, img, bub, tail=tail)
                clips.append(AS.Clip(image=img, seconds=panel_dur, mode=mode))
                clips.append(AS.Clip(image=bub, seconds=dur - panel_dur, mode=0))
                clip_map[str(fd["idx"])].append(len(clips))
                n_quote += 1
                continue
            # Врезка: вторая картинка внутри того же кадра. Панель 55% времени, врезка 45% —
            # в целых кадрах, чтобы сумма совпала с dur. Короткий кадр не делим: врезка
            # мелькнёт и будет раздражать.
            врезка = self.extra_path(fd["idx"])
            if dur >= EXTRA_MIN_REAL and not RP._intact(врезка):
                panel_dur = round(dur * 0.55 * AS.FPS) / AS.FPS
                extra_mode = AS.mode_for("insert", "", mode, "")
                clips.append(AS.Clip(image=img, seconds=panel_dur, mode=mode))
                clips.append(AS.Clip(image=self.render_image(врезка, clean_on)[0],
                                     seconds=dur - panel_dur, mode=extra_mode))
                clip_map[str(fd["idx"])].append(len(clips))
                n_extra += 1
                continue
            clips.append(AS.Clip(image=img, seconds=dur, mode=mode))
        log(f"  звуковых надписей: {n_sfx}, кадров-облачков: {n_quote}")
        if n_extra:
            log(f"  врезок в сборке: {n_extra} — на первых секундах картинка меняется вдвое чаще")
        if clean_on:
            log(f"  чистых копий кадров в сборке: {n_clean} из {len(sb['frames'])}")
        log(f"  реплики ({QUOTE_MODE}): облаков в кадре: {n_panel}, крупных планов говорящего: {n_closeup}")
        if missing:
            log(f"  ! картинок не хватает: {missing} — их время отдано соседним кадрам")
        if not clips:
            raise RuntimeError("нет ни одной картинки — собирать нечего")

        from web_queue import write_json
        write_json(self.p("out", "clip_map.json"), clip_map)
        log(f"сборка: {len(clips)} клипов")
        silent = self.p("out", "silent.mp4")
        # Сборка клипов — самый тяжёлый шаг по процессору. MANGA_RENDER_HOST уносит его на
        # машину помощнее (Минск), оставляя серверу только звук. Осечка — собираем у себя.
        if not RR.enabled() or not RR.build_silent(self.root, os.path.basename(self.root.rstrip("/")),
                                                   clips, silent, log=log):
            paths = AS.make_clips(clips, self.p("clips"), workers=4, log=log)
            silent = AS.concat_video(paths, silent)
        log("склейка готова, кладу звук")
        AS.mux(silent, audio, out)
        self.accept_video(out, timing["total"])
        return out

    def accept_video(self, out=None, total=None) -> dict:
        """Машинная приёмка готового видео (длина, звук, чёрные провалы, тишина) в out/acceptance.json.

        Отдельным шагом: приёмка идёт минутами, и перезапуск службы посреди неё оставлял ролик
        со всеми готовыми шагами, но без отметки — «не готов» навсегда (21.09: b39239ea и 324237b5).
        Исполнитель зовёт её, когда видео есть, а отметки нет."""
        # read_json здесь не был импортирован вовсе: ветка «total не передан» никогда
        # не исполнялась, потому что сборка всегда передаёт total. Моя строка про проверку
        # тайминга исполняется всегда — и подняла спящую ошибку (нашёл Codex 27.09).
        from web_queue import read_json
        out = out or self.p("out", "video.mp4")
        if total is None:
            total = (read_json(self.p("text", "timing.json"), {}) or {}).get("total")
        if not total:
            raise RuntimeError("Приёмка: нет тайминга ролика (text/timing.json)")
        acc = AS.acceptance(out, total)
        # Провал раскладки по времени — это картинки, под которыми нет речи: кадр в 0,02
        # секунды зритель видит как вспышку. Прежде приёмка про раскладку не знала вовсе
        # и ставила зелёную отметку поверх (замер 27.09: 5 роликов из 78).
        тайм = (read_json(self.p("text", "timing.json"), {}) or {}).get("check") or {}
        if тайм.get("ok") is False:
            беды = "; ".join(тайм.get("problems", [])[:2])
            acc["ok"] = False
            acc.setdefault("problems", []).append(
                f"раскладка по времени не сошлась: {беды}")
            acc["timing_check"] = тайм
        log(f"приёмка: {'ПРОШЛА' if acc['ok'] else 'ПРОБЛЕМЫ'} — "
            f"длина {acc.get('duration',0)/60:.1f} мин, {acc.get('width')}x{acc.get('height')}")
        for p in acc.get("problems", []):
            log(f"  ! {p}")
        from web_queue import write_json as _write_json
        _write_json(self.p("out", "acceptance.json"), acc)
        if self.strict_render and not acc.get("ok"):
            raise RuntimeError("Ролик не прошёл проверку сборки: " + "; ".join(acc.get("problems", [])))
        return acc


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--minutes", type=int, default=40)
    ap.add_argument("--step", default="all",
                    help="skeleton|expand|scenes|storyboard|refill|refill_memory|places|sheets|images|qa|judge|repair|fix|clean|voice|align|render|all")
    ap.add_argument("--limit", type=int, default=None, help="ограничить число кадров")
    ap.add_argument("--threads", type=int, default=20)
    ap.add_argument("--language", default="Russian", choices=list(LG.LANGS))
    ap.add_argument("--voice", default=None, help="голос edge-tts; по умолчанию первый голос языка")
    ap.add_argument("--style", default="", help="манера рисовки: S0 (по умолчанию) | S1 мягкий | S2 драматичный | S3 чистый плоский | S4 вебтун как у топов")
    a = ap.parse_args()
    pr = Project(a.root, a.source, a.minutes, style=a.style, lang=a.language, voice=a.voice)
    import claude_pool as CP
    CP.use_scope(Path(a.root) / "text" / "llm_usage.jsonl", step=f"терминал:{a.step}")
    log(f"стиль рисовки: {pr.style} — {FP.STYLE_NAMES.get(pr.style, '')}")
    t0 = time.time()
    if a.step in ("skeleton", "all"): pr.step_skeleton()
    if a.step in ("expand", "all"): pr.step_expand()
    if a.step in ("storyboard", "all"): pr.step_storyboard()
    if a.step in ("scenes",): pr.step_scenes()
    if a.step in ("qa",): pr.step_qa()
    if a.step in ("refill",): pr.step_refill()
    if a.step in ("refill_memory",): pr.step_refill_memory()
    if a.step in ("refill_memory", "all") and a.step == "all": pr.step_refill_memory()
    if a.step in ("places", "all"): pr.step_places()
    if a.step in ("sheets", "all"): pr.step_sheets()
    if a.step in ("images", "all"): pr.step_images(limit=a.limit, threads=a.threads)
    # приёмка и пересдача идут ПОСЛЕ картинок (раньше repair стоял до images — дыра)
    if a.step in ("judge",): pr.step_judge()
    if a.step in ("repair", "all"): pr.step_repair()
    if a.step in ("fix", "all"): pr.step_fix_identity()
    if a.step in ("clean", "all"): pr.step_clean()
    if a.step in ("voice", "all"): pr.step_voice()
    if a.step in ("align", "all"): pr.step_align()
    if a.step in ("render", "all"): pr.step_render()
    log(f"готово за {(time.time()-t0)/60:.1f} мин")


if __name__ == "__main__":
    main()
