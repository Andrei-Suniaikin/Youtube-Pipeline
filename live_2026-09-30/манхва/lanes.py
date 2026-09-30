"""Дорожки очереди: сколько роликов одновременно может быть на этапе одного вида.

MANGA_PARALLEL_VIDEOS — сколько роликов идут одновременно (по умолчанию 1, как раньше).
У каждого вида работы своя дорожка со своим пределом:
  «Текст»    — skeleton, expand, scenes, storyboard (вызовы Claude)  MANGA_TEXT_SLOTS,   по умолчанию = число роликов
  «Картинки» — sheets, images, judge, repair, fix (FastGen)           MANGA_IMAGE_SLOTS,  по умолчанию = число роликов
  «Звук»     — voice, align                                         MANGA_VOICE_SLOTS,  по умолчанию = число роликов
  «Сборка»   — render (процессор)                                    MANGA_RENDER_SLOTS, по умолчанию 1
«Исходник» (скачивание субтитров, секунды) дорожки не имеет.
Ролик, ждущий дорожку, держит только своё место в очереди этой дорожки: остальные дорожки свободны.
Очередь дорожки — в порядке прихода. Единицы и потоки FastGen делит отдельно fastgen_pool.
"""
from __future__ import annotations

import os
import threading
from collections import deque
from contextlib import contextmanager

LANES = (
    ("text", "Текст", ("skeleton", "expand", "scenes", "storyboard"), "MANGA_TEXT_SLOTS"),
    ("images", "Картинки", ("sheets", "images", "judge", "repair", "fix"), "MANGA_IMAGE_SLOTS"),
    ("voice", "Звук", ("voice", "align"), "MANGA_VOICE_SLOTS"),
    ("render", "Сборка", ("render",), "MANGA_RENDER_SLOTS"),
)
LANE_OF = {step: lane for lane, _, steps, _ in LANES for step in steps}
LABEL = {lane: label for lane, label, _, _ in LANES}


class Cancelled(Exception):
    """Ожидание дорожки прервано: служба останавливается или ролик забрали."""


def _count(env, name, default):
    raw = str(env.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name}: нужно целое число от 1, получено {raw!r}") from None
    if value < 1:
        raise ValueError(f"{name}: нужно целое число от 1, получено {raw!r}")
    return value


def settings(env=None):
    """Неверное значение останавливает запуск службы, а не ролик."""
    env = os.environ if env is None else env
    parallel = _count(env, "MANGA_PARALLEL_VIDEOS", 1)
    slots = {lane: _count(env, name, 1 if lane == "render" else parallel) for lane, _, _, name in LANES}
    return {"parallel": parallel, "slots": slots}


class Lanes:
    def __init__(self, slots):
        self.slots = dict(slots)
        self.busy = {lane: 0 for lane in self.slots}
        self.peak = {lane: 0 for lane in self.slots}   # для проверки: больше предела не бывало
        self._line = {lane: deque() for lane in self.slots}
        self._cond = threading.Condition()

    @contextmanager
    def hold(self, step, on_wait=None, cancelled=None, poll=0.5):
        """Занять место на дорожке этапа step на время этапа. on_wait(текст) — один раз, если пришлось ждать.
        cancelled() -> True прерывает ожидание исключением Cancelled; место при этом не занимается."""
        lane = LANE_OF.get(step)
        if lane is None or lane not in self.slots:
            yield None
            return
        ticket = object()
        with self._cond:
            line = self._line[lane]
            line.append(ticket)
            told = False
            try:
                # В порядке прихода: место берёт первый в очереди дорожки, когда оно свободно.
                while self.busy[lane] >= self.slots[lane] or line[0] is not ticket:
                    if cancelled is not None and cancelled():
                        raise Cancelled(lane)
                    if not told and on_wait is not None:
                        told = True
                        on_wait(f"ждёт этап «{LABEL[lane]}»: заняты {self.busy[lane]} из {self.slots[lane]}, "
                                f"впереди в очереди {list(line).index(ticket)}")
                    self._cond.wait(poll)
            finally:
                line.remove(ticket)
                self._cond.notify_all()
            self.busy[lane] += 1
            self.peak[lane] = max(self.peak[lane], self.busy[lane])
        try:
            yield lane
        finally:
            with self._cond:
                self.busy[lane] -= 1
                self._cond.notify_all()
