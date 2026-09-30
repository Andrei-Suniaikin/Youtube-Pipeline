# -*- coding: utf-8 -*-
"""Раскадровка: группировка действий в кадры.

Этого правила не было ни в одном наработанном модуле — оно новое.

Идея: кадр НЕ равен биту механически. Кадр рвётся там, где меняется то,
что видно: состав в кадре или место. Внутри одной сцены соседние короткие
биты склеиваются, пока кадр не набрал свой бюджет секунд.

Границы кадра всегда совпадают с границами битов — бит никогда не режется
пополам, иначе картинка сменится посреди фразы.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import List, Optional

# Наблюдаемая норма ниши: картинка держится 4,5 секунды (шесть замеров:
# 2,8 / 4,0 / 4,4 / 4,7 / 5,0 / 6,4). Коридор — вокруг неё.
TARGET_SECONDS = 4.5
MIN_SECONDS = 2.5
MAX_SECONDS = 8.0

# ОКНО ХУКА. Замер 25.09: у нас первые кадры самые длинные (6,2 / 6,5 / 5,5 с при медиане
# 5,0), в первые 30 секунд зритель видит 6 картинок. У 53 конкурентов — 9–14. Половина
# зрителей уходит в первые 30 секунд. В начале ролика бюджет кадра короче: картинок больше
# примерно на 11 из 490, новых вызовов модели нет.
HOOK_STEPS = ((30.0, 2.4, 1.6), (90.0, 3.2, 2.0))


def _budget(elapsed: float):
    """Цель и минимум длительности кадра на данной секунде ролика."""
    for граница, цель, минимум in HOOK_STEPS:
        if elapsed < граница:
            return цель, минимум
    return TARGET_SECONDS, MIN_SECONDS


@dataclass
class Frame:
    idx: int
    beat_from: int          # номера битов, включительно
    beat_to: int
    words: int
    seconds: float          # расчётная, до озвучки
    in_frame: List[str] = field(default_factory=list)
    location: Optional[str] = None
    scene_num: int = 0        # к какой сцене скелета относится кадр
    reason: str = ""        # почему кадр начался здесь
    prompt_en: str = ""     # описание картинки — заполняется отдельным шагом
    # ВАЖНО: camera и focus обязаны быть ПОЛЯМИ класса. В первом прогоне их
    # вешали на объект атрибутами, и asdict() молча их выбрасывал — в результате
    # во все 493 промпта ушёл один и тот же ракурс medium, хотя модель выбирала
    # разные. Проверено: camera=None во всех 493 кадрах storyboard.json.
    camera: str = ""
    focus: str = ""
    # --- графы бланка кадра, добавлены 08.09.2026 --------------------------
    # Каждая пустая графа = параметр, который исполнитель придумает сам и
    # придумает по-разному в каждом кадре. Их видно ДО генерации.
    shot_size: str = ""     # сверхкрупно | крупно | средне | в рост | общий
    angle: str = ""         # на уровне глаз | снизу | сверху | через плечо
    camera_pos: str = ""    # откуда смотрим — привязка к плану помещения
    staging: str = ""       # кто где относительно мебели и кто слева/справа
    pose: str = ""          # поза и руки
    gaze: str = ""          # куда смотрит каждый, в терминах кадра
    emotion: str = ""       # одно слово из закрытого списка
    face_visible: bool = True   # видно ли лицо. Проверено на кадре 282:
    # «крупный план героя» может быть кадром рук под одеялом — и это ВЕРНО
    # по тексту. Если лица нет, эталон лица прикладывать незачем.
    changed: str = ""       # что изменилось с прошлого кадра, АБСОЛЮТНОЙ фразой
    # --- добавлено 09.09.2026 по разбору всех 493 кадров -------------------
    posture: dict = field(default_factory=dict)   # герой -> сидит|стоит|присел|лежит|опирается
    # Без этой графы «наклонился вперёд» из поля позы отменяло «сидит» из
    # расстановки, и сцена шёпота за партой игралась стоя (8 кадров).
    contact: str = ""       # чья рука откуда входит в кадр и что держит.
    # 37 кадров ссылались на человека ЗА кадром — оттуда и росли «варежки»,
    # обрубки рукавов и вторые кисти.
    distance: str = ""      # насколько далеко герои друг от друга и от камеры.
    # Строка «главный — самый крупный и ближе всех» уходила во все 395
    # многолюдных кадров и играла буквально: герой гигантом, второй фигуркой.
    layer: str = "now"      # now | flashback | dream — слой времени.
    # 16 кадров ролика это воспоминание о детстве, но паспорт у них был от
    # настоящего: ребёнка рисовали взрослым, а в чужом воспоминании стояли
    # посторонние герои.
    props_seen: List[str] = field(default_factory=list)   # какие предметы видны
    costume_change: dict = field(default_factory=dict)    # кто что снял/надел в этом кадре
    # --- язык манхвы, добавлено 09.09.2026 по кадрам конкурентов ---------
    quote: str = ""         # реплика для кадра-облачка, ТОЛЬКО если бит сообщает о речи
    quote_by: str = ""      # кто говорит (cid)
    sfx: str = ""           # звуковая надпись у действия: «БАМ», «ВЖУХ», «ТУК» — или пусто
    panel: str = ""
    form_error: str = ""        # 'cast_mismatch' — описание и in_frame про разных людей (visuals.check_cast)
    form_missing: List[str] = field(default_factory=list)   # кого не хватает в in_frame         # тип панели: establishing|reaction|face|detail|impact|dialogue
    costume_state: dict = field(default_factory=dict)     # накопленное состояние одежды
    t_start: Optional[float] = None   # из выравнивания по озвучке
    t_end: Optional[float] = None
    filled_from: Optional[int] = None   # описание взято у соседа по сцене (pipeline._fill_from_neighbours)

    @classmethod
    def from_dict(cls, fd: dict) -> "Frame":
        """Кадр из строки storyboard.json. Посторонние ключи (text и пометки других шагов) пропускаются.

        21.09.2026: добор соседом писал в строку кадра filled_from, а шаги, которые перечитывают
        бланк, делали Frame(**строка) — и при первом же доборе «Раскадровка» падала TypeError
        навсегда (разбор застрявших роликов). Любая будущая пометка в бланке шаг больше не уронит."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in fd.items() if k in known})

    def text(self, src, beats) -> str:
        a = beats[self.beat_from - 1].start
        b = beats[self.beat_to - 1].end
        return src[a:b]


def _cast_changed(a: List[str], b: List[str]) -> bool:
    """Состав сменился, если сменился ГЛАВНЫЙ или ушёл/пришёл кто-то из первых двух."""
    if not a or not b:
        return a != b
    if a[0] != b[0]:
        return True
    return set(a[:2]) != set(b[:2])


def build(beats, states, words_per_minute: float = 135.0) -> List[Frame]:
    """beats — из beats.segment(), states — из state.resolve(). Длины совпадают."""
    assert len(beats) == len(states), "битов и состояний должно быть поровну"
    wps = words_per_minute / 60.0

    frames: List[Frame] = []
    cur_from = 1
    cur_words = 0
    cur_state = states[0]
    reason = "начало"
    elapsed = 0.0      # сколько секунд ролика уже закрыто кадрами — нужно для окна хука

    def flush(to_idx: int, why_next: str):
        nonlocal cur_from, cur_words, reason, elapsed
        if to_idx < cur_from:
            return
        secs = cur_words / wps
        elapsed += secs
        frames.append(Frame(
            idx=len(frames) + 1, beat_from=cur_from, beat_to=to_idx,
            words=cur_words, seconds=round(secs, 2),
            in_frame=list(cur_state.in_frame), location=cur_state.location,
            reason=reason,
        ))
        cur_from = to_idx + 1
        cur_words = 0
        reason = why_next

    for i, (bt, stt) in enumerate(zip(beats, states)):
        n = i + 1
        if n > cur_from:
            secs_now = cur_words / wps              # кадр без этого бита
            secs_with = (cur_words + bt.words) / wps  # кадр с этим битом

            цель, минимум = _budget(elapsed)   # в начале ролика кадр короче

            hard = None
            if stt.location and stt.location != cur_state.location:
                hard = "сменилось место"
            elif _cast_changed(cur_state.in_frame, stt.in_frame):
                hard = "сменился состав"

            cut = None
            if hard and secs_now >= минимум:
                cut = hard
            elif secs_with > MAX_SECONDS:
                cut = "кадр стал бы слишком длинным"
            elif secs_now >= минимум and abs(secs_with - цель) > abs(secs_now - цель):
                # добавить этот бит — уехать от нормы дальше, чем закрыть кадр сейчас
                cut = "набран бюджет секунд"

            if cut:
                flush(n - 1, cut)
                cur_state = stt
        cur_words += bt.words
        if n == cur_from:
            cur_state = stt

    flush(len(beats), "")
    return frames


def report(frames: List[Frame]) -> dict:
    if not frames:
        return {"frames": 0}
    secs = sorted(f.seconds for f in frames)
    q = lambda p: secs[min(len(secs) - 1, int(len(secs) * p))]
    total = sum(f.seconds for f in frames)
    from collections import Counter
    return {
        "frames": len(frames),
        "total_minutes": round(total / 60, 1),
        "sec_median": round(secs[len(secs) // 2], 2),
        "sec_p10": round(q(0.1), 2), "sec_p90": round(q(0.9), 2),
        "sec_min": round(secs[0], 2), "sec_max": round(secs[-1], 2),
        "over_max": sum(1 for s in secs if s > MAX_SECONDS),
        "under_min": sum(1 for s in secs if s < MIN_SECONDS),
        "beats_per_frame": round(sum(f.beat_to - f.beat_from + 1 for f in frames) / len(frames), 2),
        "reasons": dict(Counter(f.reason for f in frames if f.reason)),
        "no_location": sum(1 for f in frames if not f.location),
    }
