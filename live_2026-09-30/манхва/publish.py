# -*- coding: utf-8 -*-
"""Шаг «Название»: название, описание и теги ролика на языке канала → text/publish.json.

Шаг идёт после «Текста» и ролик не держит: сбой виден у шага, ролик собирается дальше,
а название можно сделать заново кнопкой на странице ролика.

Что даёт модель (один вызов, при замечаниях — ещё один с их списком):
  название — крючок названия донора (source_meta title) на языке канала, 1–2 слова ЗАГЛАВНЫМИ;
  описание — 2–4 предложения о сюжете нашего пересказа; модели даётся только начало
             скелета (без последних сцен), так что финал она не знает и не выдаст;
  теги     — 3–8 тегов сюжета.
Что проверяет код, а не модель: длина (≤ 100 символов у названия), нет английских слов и
сокращений по списку, нет номеров частей, нет чужого алфавита, 1–2 заглавных слова-крючка.
К описанию добавляется блок канала (поле description в channels.json), к тегам — ключевые
слова канала (поле keywords), вместе не длиннее 500 знаков, как требует YouTube.

Повторный запуск не вызывает модель, если publish.json уже есть: заново — только кнопкой
«Сделать заново» (force=True).

Одна попытка «Названия» на ролик за раз: шаг держит замок text/publish.lock (flock — и между потоками
одной службы, и между процессами). Повтор ролика, пока прошлая попытка ждёт ответа модели, ждёт замка и
берёт её готовый файл без второго вызова. publish.json пишется через свой временный файл и os.replace.

Настройки web.env:
  MANGA_TITLE        on — шаг идёт сам в каждом ролике; off (по умолчанию) — пропускается,
                     кнопка на странице ролика работает всё равно.
  MANGA_TITLE_MODEL  модель; без неё — MANGA_TEXT_MODEL, без обеих — claude:claude-sonnet-5.
"""
from __future__ import annotations

import fcntl
import hashlib
import logging
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

try:
    from . import languages as LG
    from . import llm as LLM
except ImportError:
    import languages as LG
    import llm as LLM

log = logging.getLogger("manga.publish")
FILE = Path("text") / "publish.json"
TITLE_MAX = 100
TAGS_MAX = 500           # YouTube: все теги вместе не длиннее 500 знаков
DESCRIPTION_MAX = 700    # только сюжетная часть; блок канала добавляется отдельно
ATTEMPTS = 2
# Один вызов модели: короткий JSON, без повторов внутри llm.call_json (retries=0). Худший случай шага —
# ATTEMPTS × CALL_TIMEOUT = 10 минут (было до 4 вызовов = 20 минут). 150 с не взято: 18.09 через альт-ключ
# три вызова подряд упёрлись в 150 с. Ожидание лимита Claude сюда не входит — его ждут все текстовые шаги.
CALL_TIMEOUT = 300

# Ранги можно (S-ранг, SSS): это не английское слово, а буква ранга.
RANKS = {"s", "ss", "sss", "a", "b", "c", "d", "e", "f", "ex"}
# Английские слова и сокращения, которых не должно быть в названии и описании на языке канала.
ENGLISH = set("""
the and of with my your his her their who whom after before return returns returned reborn rebirth
regressor regression regressed level leveling levelling system hunter hunters player players villain
villainess duke boss overpowered strongest weakest loser genius king queen prince princess tower
dungeon gate gates love lover wife husband secret secrets revenge contract marriage school girl girls
boy boys bully nerd ranker awakened awakening reincarnated reincarnation part episode chapter season
full recap recaps story summary explained cheat skill skills power powers demon god gods sword master
academy lord emperor empress knight magic mage time life world only first last new every never again
back becomes became become got get gets how what why when all this that was were are be is i'm im
million billion cash rich poor simp second chance ex-wife girlfriend boyfriend ceo mc op ml fl pov npc rpg lvl
webtoon trash transmigrated transmigration strongest-mc
""".split())
# Слова, совпадающие со своими словами языка канала (польское my — «мы», румынское demon — «демон»).
NATIVE = {
    # Проверено 18.09.2026 на 4 308 названиях роликов конкурентов (ниши/манхва_2026-09/данные_длина/ролики.jsonl):
    # польское im — «им», португальское time — «команда», румынские are — «имеет», secret, contract, magic, lord.
    "pl": {"my", "was", "system", "demon", "lord", "boss", "im"},
    "cs": {"my", "boss", "lord"},
    "ro": {"demon", "boss", "are", "secret", "contract", "magic", "lord"},
    "it": {"boss", "are"},
    "es": {"boss"},
    "pt": {"boss", "time"},
    "ru": set(),
    "en": set(),
}
# Английское «manhwa» стало словом в названиях каналов (Randka z Manhwą, Renacer Manhwa) — можно.
ALWAYS_OK = {"manhwa", "manhwy", "manhwą", "manhwę", "manhwie", "manga"}
PART = re.compile(
    r"(?i)(?:\b(?:part|parte|partea|pt|cap|cz|č|część|czesc|częśc|část|cast|díl|dil|часть|ч\.|серия|выпуск|ep|eps|episode|"
    r"episodio|episódio|odcinek|epizod|epizoda|epizodul|vol|volume|tom|tomo|capitolo|capítulo|kapitola|"
    r"rozdział|rozdzial|глава|capitolul)\s*\.?\s*(?:\d+|[ivx]{1,4})\b)"
    r"|\bp\d{1,2}\b|#\s*\d+|\(\s*\d+\s*/\s*\d+\s*\)|\b\d+\s*/\s*\d+\b"
    r"|\b\d+\s*[-‑]?\s*(?:часть|я часть|część|parte|část|partea|díl)\b")
WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?")
LATIN = re.compile(r"[A-Za-z]")
CYRILLIC = re.compile(r"[Ѐ-ӿ]")
SENTENCE = re.compile(r"[^.!?…]+[.!?…]+[\"»”“]?|[^.!?…]+$")


def enabled(env=None) -> bool:
    """MANGA_TITLE: on/off, по умолчанию off. Неверное значение останавливает запуск службы."""
    env = os.environ if env is None else env
    raw = str(env.get("MANGA_TITLE", "") or "").strip().lower()
    if raw in ("", "0", "off"):
        return False
    if raw in ("1", "on"):
        return True
    raise ValueError(f"MANGA_TITLE: нужно on или off, а не {raw!r}")


# Без MANGA_TITLE_MODEL и MANGA_TEXT_MODEL — Claude: недельный лимит Codex Pro выбран (18.09.2026),
# а кнопка «Сделать заново» работает и при MANGA_TITLE=off.
DEFAULT_MODEL = "claude:claude-sonnet-5"


def model(env=None) -> str:
    env = os.environ if env is None else env
    name = (str(env.get("MANGA_TITLE_MODEL", "") or "").strip()
            or str(env.get("MANGA_TEXT_MODEL", "") or "").strip() or DEFAULT_MODEL)
    if not re.fullmatch(r"(codex|claude):[A-Za-z0-9._-]+", name):
        raise ValueError("MANGA_TITLE_MODEL: укажите «codex:<модель>» или «claude:<модель>»")
    return name


def path(root) -> Path:
    return Path(root) / FILE


def read(root):
    """Готовый результат шага или None."""
    try:
        data = json.loads(path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("title") else None


STALE = "текст ролика изменился после того, как сделано название — нажмите «Сделать заново»"


def outline_sha(outline) -> str:
    return hashlib.sha256(outline.encode("utf-8")).hexdigest()[:16]


def stale(root, done=None) -> bool:
    """Готовое название сделано по другому тексту ролика (текст переделали после шага «Название»)."""
    done = read(root) if done is None else done
    if not done or not done.get("outline_sha"):
        return False
    try:
        outline = story_outline(root)[0]
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return bool(outline.strip()) and outline_sha(outline) != done["outline_sha"]


def _read_json(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(root, data):
    """Атомарно: свой временный файл рядом (имя уникально), fsync, os.replace; при сбое временный убирается."""
    p = path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


LOCK_POLL = 0.2   # с: как часто ждущая попытка проверяет замок (и не потеряна ли её задача)


def _signature(root):
    try:
        st = path(root).stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


@contextmanager
def _locked(root, on_wait=None):
    """Замок шага на ролик. Пока его держит другая попытка — on_wait() на каждом круге (может бросить,
    если задачу забрали или служба останавливается). Отдаёт True, если пришлось ждать."""
    lock = Path(root) / "text" / "publish.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+") as f:
        waited = False
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                waited = True
                if on_wait:
                    on_wait()
                time.sleep(LOCK_POLL)
        try:
            yield waited
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# ---------- проверки кода ----------

def _words(text):
    return WORD.findall(str(text or ""))


def _names(skeleton) -> set:
    """Имена героев и мест из скелета: латиницей они законны (Chen An, Su Qingying)."""
    out = set()
    for group in ("characters", "locations"):
        for item in (skeleton or {}).get(group, []) or []:
            if not isinstance(item, dict):
                continue
            for value in [item.get("name"), *(item.get("variants") or [])]:
                out.update(w.casefold() for w in _words(value))
    return out


def english_words(text, lang, names=()) -> list:
    iso = LG.iso(lang)
    allowed = NATIVE.get(iso, set()) | ALWAYS_OK | set(names)
    found = []
    for word in _words(text):
        low = word.casefold().replace("’", "'")
        if low in ENGLISH and low not in allowed and word not in found:
            found.append(word)
    return found


def caps_words(title) -> list:
    """Слова-крючки ЗАГЛАВНЫМИ: от двух букв, не ранг (SSS не считается)."""
    return [w for w in _words(title) if len(w) >= 2 and w.upper() == w and w.lower() != w
            and w.casefold() not in RANKS]


def _foreign_script(text, lang, names=()):
    """Русский — без латиницы, кроме рангов и имён; латинские языки — без кириллицы."""
    if LG.script(lang) == "Cyrillic":
        bad = [w for w in _words(text) if LATIN.search(w) and w.casefold() not in RANKS
               and w.casefold() not in names]
        return f"латиница в тексте на языке {LG.get(lang)['label']}: {', '.join(bad[:5])}" if bad else ""
    bad = [w for w in _words(text) if CYRILLIC.search(w)]
    return f"кириллица в тексте на языке {LG.get(lang)['label']}: {', '.join(bad[:5])}" if bad else ""


def check_title(title, lang, names=()) -> list:
    title = str(title or "").strip()
    problems = []
    if not title:
        return ["название пустое"]
    if len(title) > TITLE_MAX:
        problems.append(f"название длиннее {TITLE_MAX} символов ({len(title)})")
    eng = english_words(title, lang, names)
    if eng:
        problems.append("английские слова или сокращения в названии: " + ", ".join(eng))
    if PART.search(title):
        problems.append("номер части или серии в названии: " + PART.search(title).group(0).strip())
    script = _foreign_script(title, lang, names)
    if script:
        problems.append(script)
    caps = caps_words(title)
    if not caps:
        problems.append("нет ни одного слова-крючка ЗАГЛАВНЫМИ (нужно 1–2)")
    elif len(caps) > 2:
        problems.append(f"слов ЗАГЛАВНЫМИ {len(caps)}: {', '.join(caps)} (нужно 1–2)")
    return problems


def sentences(text) -> list:
    return [s.strip() for s in SENTENCE.findall(str(text or "").strip()) if s.strip()]


def check_description(text, lang, names=()) -> list:
    text = str(text or "").strip()
    if not text:
        return ["описание пустое"]
    problems = []
    n = len(sentences(text))
    if not 2 <= n <= 4:
        problems.append(f"в описании {n} предложений (нужно 2–4)")
    if len(text) > DESCRIPTION_MAX:
        problems.append(f"описание длиннее {DESCRIPTION_MAX} символов ({len(text)})")
    eng = english_words(text, lang, names)
    if eng:
        problems.append("английские слова в описании: " + ", ".join(eng))
    script = _foreign_script(text, lang, names)
    if script:
        problems.append(script)
    return problems


def clean_tags(tags, lang, names=(), dropped=None) -> list:
    """Годные теги; выброшенные (английские, чужой алфавит, длиннее 60) — в список dropped, если он дан."""
    out = []
    for tag in tags if isinstance(tags, list) else str(tags or "").split(","):
        tag = re.sub(r"\s+", " ", str(tag or "").strip().lstrip("#")).strip(" ,")
        if not tag:
            continue
        if len(tag) > 60 or english_words(tag, lang, names) or _foreign_script(tag, lang, names):
            if dropped is not None:
                dropped.append(tag)
            continue
        out.append(tag)
    return out


def join_tags(story_tags, channel_keywords) -> list:
    """Теги сюжета + ключевые слова канала, без повторов, вместе ≤ 500 знаков (с запятыми)."""
    words = list(story_tags) + [k.strip() for k in str(channel_keywords or "").split(",")]
    out, seen, size = [], set(), 0
    for tag in words:
        key = tag.casefold()
        if not tag or key in seen:
            continue
        extra = len(tag) + (2 if out else 0)
        if size + extra > TAGS_MAX:
            continue
        out.append(tag)
        seen.add(key)
        size += extra
    return out


# ---------- вход модели ----------

def story_outline(root, share=0.75, fallback_chars=4000):
    """Начало сюжета без финала: сцены скелета до последней четверти (последняя — никогда)."""
    root = Path(root)
    sk = _read_json(root / "text" / "skeleton.json", {}) or {}
    scenes = [s for s in sk.get("scenes", []) or [] if isinstance(s, dict)]
    if scenes:
        keep = max(1, min(len(scenes) - 1, round(len(scenes) * share))) if len(scenes) > 1 else 1
        lines = [f"- {s.get('title') or ''}: {s.get('what') or ''}".strip() for s in scenes[:keep]]
        heroes = [c.get("name") for c in sk.get("characters", []) or [] if isinstance(c, dict) and c.get("name")]
        return "\n".join(lines), heroes[:6], sk
    # Проверочный прогон (frame_limit) укорачивает final.txt уже после шага «Название», а полную копию
    # кладёт в final.full.txt: берём её, иначе проверка «текст изменился» сработала бы ложно.
    text = ""
    for name in ("final.full.txt", "final.txt"):
        try:
            text = (root / "text" / name).read_text(encoding="utf-8")
            break
        except OSError:
            continue
    # Без скелета — только начало пересказа: конец текста модели не показываем.
    return text[: min(fallback_chars, len(text) // 2 or fallback_chars)], [], sk


def build_prompt(lang, donor_title, outline, heroes, problems=None, previous=None) -> str:
    spec = LG.get(lang)
    donor = (f'Title of the source (donor) video, usually English: "{donor_title}". '
             "Take its HOOK (the most intriguing promise), not a literal translation.\n"
             if donor_title else "There is no donor title: invent the hook from the story.\n")
    ranks = "Rank letters (S, SSS) are allowed. " if spec["script"] == "Latin" else \
        "Rank letters S, SS, SSS are the only Latin letters allowed. "
    prompt = (
        f"You write YouTube metadata for a {lang} manhwa recap channel. Everything you output is in "
        f"natural, lively {lang} ({spec['label']}), the way a {lang}-speaking teenager talks — "
        "no translationese.\n\n"
        + donor +
        "\nSTORY OF OUR RETELLING (only its beginning and middle; do NOT guess or reveal the ending):\n"
        f"{outline}\n"
        + (f"Main characters: {', '.join(heroes)}.\n" if heroes else "") +
        "\nRETURN ONLY JSON: {\"title\": \"...\", \"description\": \"...\", \"tags\": [\"...\"]}\n\n"
        "TITLE rules:\n"
        f"- at most {TITLE_MAX} characters, in {lang} only;\n"
        "- ONE or TWO hook words in CAPITAL LETTERS, like top channels "
        "(e.g. the key twist or the number: 10 MILLION, NOBODY, REVENGE — in the channel's language); "
        "all other words normal case;\n"
        f"- NO English words and NO English abbreviations (MC, OP, CEO, POV, ML, FL). {ranks}"
        "Character names may stay as in the story;\n"
        "- NO part / episode numbers, no \"#2\", no \"1/3\", no channel name, no emoji, no quotes around it;\n"
        "- no words \"recap\", \"summary\", \"full\" in any language — it is a story, not a report.\n\n"
        "DESCRIPTION rules:\n"
        f"- 2 to 4 sentences in {lang} about the plot of our retelling: who the hero is, what happens, "
        "what is at stake; end on intrigue;\n"
        "- never reveal how the story ends; no English words; no links, no hashtags, no calls to subscribe.\n\n"
        f"TAGS: 3 to 8 short search tags in {lang} about this story (genre, trope, hero type); "
        "no English, no '#'.\n"
    )
    if problems:
        prompt += ("\nYOUR PREVIOUS ANSWER WAS REJECTED BY THE CHECKER. Previous answer:\n"
                   f"{json.dumps(previous, ensure_ascii=False)}\nProblems (fix every one, keep the rest):\n- "
                   + "\n- ".join(problems) + "\n")
    return prompt


def _problems(answer, lang, names):
    if not isinstance(answer, dict):
        return ["ответ модели не JSON-объект"]
    return check_title(answer.get("title"), lang, names) + check_description(answer.get("description"), lang, names)


def run_step(root, data, channel=None, *, force=False, call_json=None, now=None, on_wait=None) -> dict:
    """Делает text/publish.json. {"note", "called", "warnings"}; готовый файл без force — без вызова модели.
    Под замком ролика: вторая попытка ждёт первую (on_wait — пока ждёт) и берёт её результат."""
    root = Path(root)
    before = _signature(root)
    with _locked(root, on_wait) as waited:
        if force and waited and read(root) and _signature(root) != before:
            # «Сделать заново» ждал попытку, которая как раз сделала новое название: второй вызов не нужен.
            force = False
        return _run_step(root, data, channel, force=force, call_json=call_json, now=now)


def _run_step(root, data, channel=None, *, force=False, call_json=None, now=None) -> dict:
    done = read(root)
    if done and not force:
        warnings = list(done.get("warnings") or [])
        note = "Название уже готово — модель не вызывалась."
        if stale(root, done):
            warnings.append(STALE)
            note += " Проверьте: " + STALE + "."
        return {"note": note, "called": 0, "warnings": warnings}
    lang = data.get("language") or LG.DEFAULT
    LG.get(lang)
    meta = _read_json(root / "text" / "source_meta.json", {}) or {}
    donor = str(meta.get("title") or "").strip()
    outline, heroes, sk = story_outline(root)
    if not outline.strip():
        raise RuntimeError("Нет текста ролика: название делается после шага «Текст».")
    names = _names(sk)
    name = model()
    call_json = call_json or LLM.call_json
    started = time.monotonic()
    answer, problems, calls = None, None, 0
    for attempt in range(1, ATTEMPTS + 1):
        prompt = build_prompt(lang, donor, outline, heroes, problems, answer)
        calls += 1   # retries=0: одна попытка = ровно один вызов модели
        try:
            got, _meta = call_json(prompt, model=name, timeout=CALL_TIMEOUT, retries=0)
        except (LLM.LimitError, LLM.TokenError):
            raise
        except LLM.LLMError as exc:
            # Битый JSON или таймаут: вторая попытка — последняя, дальше ошибка шага (ролик не ждёт).
            if attempt >= ATTEMPTS:
                raise
            log.warning("Шаг «Название»: попытка %s не дала ответа (%s) — повторяю", attempt, str(exc)[:200])
            problems, answer = None, None
            continue
        answer = got if isinstance(got, dict) else {}
        answer = {"title": re.sub(r"\s+", " ", str(answer.get("title") or "")).strip().strip("\"«»„”“"),
                  "description": re.sub(r"[ \t]+", " ", str(answer.get("description") or "")).strip(),
                  "tags": answer.get("tags") or []}
        problems = _problems(answer, lang, names)
        if not problems:
            break
    if not answer["title"]:
        raise RuntimeError("Модель не дала название: " + "; ".join(problems or []))
    channel = channel or {}
    block = str(channel.get("description") or "").strip()
    dropped = []
    story_tags = clean_tags(answer["tags"], lang, names, dropped)
    tags = join_tags(story_tags, channel.get("keywords"))
    description = answer["description"] + (("\n\n" + block) if block else "")
    warnings = list(problems or [])
    if dropped:
        warnings.append("выброшены теги (английские слова или чужой алфавит): " + ", ".join(dropped[:8]))
    if data.get("channel") and not block:
        warnings.append("у канала в channels.json нет описания (поле description): блок канала не добавлен")
    result = dict(
        title=answer["title"], description=description, story=answer["description"], channel_block=block,
        tags=tags, tags_text=", ".join(tags), language=lang, channel=data.get("channel") or "",
        channel_name=channel.get("name") or data.get("channel_name") or "",
        donor_title=donor, model=name, calls=calls, warnings=warnings,
        made_at=time.time() if now is None else now, seconds=round(time.monotonic() - started, 1),
        outline_sha=outline_sha(outline))
    _write(root, result)
    note = f"Название: {len(result['title'])} символов, тегов {len(tags)}, {result['seconds']:g} с, вызовов модели {calls}"
    note += (". Проверьте вручную: " + "; ".join(warnings)) if warnings else "."
    return {"note": note, "called": calls, "warnings": warnings}
