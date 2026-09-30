#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Расчёт будущих слотов публикации канала.

Слот — это конкретный момент выхода ролика: дата, время и таймзона канала.
Расписание канала задаётся не cron-строкой, а перечнем «дни недели → времена»,
потому что нам нужен не триггер «пора запускать», а список будущих слотов:
из него строится картина покрытия и в него встают ссылки контент-плана.

Отдельно обрабатываются переходы на летнее время: несуществующее локальное
время пропускается, неоднозначное берётся по первому проходу.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Iterable

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover — питон старше 3.9 у нас не используется
    ZoneInfo = None  # type: ignore


DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ANY_DAY = "*"
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

MAX_HORIZON_DAYS = 90


class ScheduleError(ValueError):
    """Расписание задано неверно. Сообщение пригодно для показа в интерфейсе."""


def parse_time(value: str) -> tuple[int, int]:
    """'10:00' → (10, 0). Ошибка — с понятным текстом, а не ValueError изнутри."""
    text = (value or "").strip()
    m = TIME_RE.match(text)
    if not m:
        raise ScheduleError(f"время должно быть в формате ЧЧ:ММ, получено: {value!r}")
    return int(m.group(1)), int(m.group(2))


def normalize_schedule(schedule: dict) -> dict[str, list[tuple[int, int]]]:
    """Привести расписание к {день: [(час, минута), ...]}.

    Принимает как {'*': ['10:00']} (каждый день), так и поимённо
    {'mon': [...], 'sat': [...]}. Времена внутри дня сортируются и дедуплицируются.
    """
    if not isinstance(schedule, dict) or not schedule:
        raise ScheduleError("расписание пустое")

    out: dict[str, list[tuple[int, int]]] = {}
    for raw_day, times in schedule.items():
        day = str(raw_day).strip().lower()[:3]
        if day == ANY_DAY or str(raw_day).strip() == ANY_DAY:
            day = ANY_DAY
        elif day not in DAY_KEYS:
            raise ScheduleError(
                f"неизвестный день {raw_day!r}; допустимы {', '.join(DAY_KEYS)} или '*'"
            )
        if isinstance(times, str):
            times = [times]
        if not isinstance(times, Iterable):
            raise ScheduleError(f"для дня {raw_day!r} ожидался список времён")
        parsed = sorted({parse_time(t) for t in times})
        if not parsed:
            continue
        out.setdefault(day, [])
        out[day] = sorted(set(out[day]) | set(parsed))

    if not out:
        raise ScheduleError("в расписании нет ни одного времени")
    if ANY_DAY in out and len(out) > 1:
        raise ScheduleError("нельзя смешивать '*' с конкретными днями недели")
    return out


def _zone(tz_name: str):
    if ZoneInfo is None:  # pragma: no cover
        raise ScheduleError("в этой сборке Python нет поддержки таймзон (zoneinfo)")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        raise ScheduleError(f"неизвестный часовой пояс: {tz_name!r}")


def _localize(day: dt.date, hh: int, mm: int, zone) -> dt.datetime | None:
    """Собрать момент времени в зоне канала.

    Возвращает None, если такого локального времени не существует — это ночь
    перехода на летнее время, когда стрелки прыгают через целый час.
    """
    naive = dt.datetime(day.year, day.month, day.day, hh, mm)
    aware = naive.replace(tzinfo=zone)
    # Признак несуществующего времени: обратный перевод в локальное не совпал.
    back = aware.astimezone(dt.timezone.utc).astimezone(zone)
    if (back.hour, back.minute) != (hh, mm):
        return None
    return aware


def upcoming_slots(
    schedule: dict,
    tz_name: str,
    horizon_days: int = 3,
    now: dt.datetime | None = None,
) -> list[dt.datetime]:
    """Все будущие слоты канала на горизонт, по возрастанию.

    horizon_days=3 значит «сегодняшний остаток плюс ещё двое суток вперёд».
    Уже прошедшие сегодня времена не возвращаются: занять их нельзя.
    """
    if not isinstance(horizon_days, int) or isinstance(horizon_days, bool):
        raise ScheduleError("горизонт должен быть целым числом дней")
    if horizon_days < 1:
        raise ScheduleError("горизонт должен быть не меньше одного дня")
    if horizon_days > MAX_HORIZON_DAYS:
        raise ScheduleError(f"горизонт больше {MAX_HORIZON_DAYS} дней не поддерживается")

    plan = normalize_schedule(schedule)
    zone = _zone(tz_name)
    moment = (now.astimezone(zone) if now is not None else dt.datetime.now(zone))

    slots: list[dt.datetime] = []
    for offset in range(horizon_days):
        day = (moment + dt.timedelta(days=offset)).date()
        key = ANY_DAY if ANY_DAY in plan else DAY_KEYS[day.weekday()]
        for hh, mm in plan.get(key, []):
            point = _localize(day, hh, mm, zone)
            if point is None:
                continue  # это время в эту дату не существует — ночь перевода часов
            if point > moment:
                slots.append(point)
    return sorted(slots)


def next_free_slot(
    schedule: dict,
    tz_name: str,
    taken: Iterable[dt.datetime],
    horizon_days: int = 3,
    now: dt.datetime | None = None,
) -> dt.datetime | None:
    """Ближайший слот канала, который ещё никем не занят.

    `taken` — уже назначенные моменты этого же канала. Сравнение идёт по
    абсолютному моменту времени, поэтому чужая таймзона в списке не мешает.
    """
    busy = {t.astimezone(dt.timezone.utc) for t in taken if t is not None}
    for point in upcoming_slots(schedule, tz_name, horizon_days, now):
        if point.astimezone(dt.timezone.utc) not in busy:
            return point
    return None


def slots_by_day(
    schedule: dict,
    tz_name: str,
    horizon_days: int = 3,
    now: dt.datetime | None = None,
) -> list[tuple[dt.date, list[dt.datetime]]]:
    """Слоты, разложенные по датам — готовая строка канала для вида «Календарь».

    В списке присутствуют ВСЕ дни горизонта, включая те, где слотов нет:
    пустой день это тоже ответ, а не повод пропустить колонку.
    """
    zone = _zone(tz_name)
    moment = (now.astimezone(zone) if now is not None else dt.datetime.now(zone))
    found = upcoming_slots(schedule, tz_name, horizon_days, now)

    buckets: dict[dt.date, list[dt.datetime]] = {}
    for point in found:
        buckets.setdefault(point.date(), []).append(point)

    days = [(moment + dt.timedelta(days=i)).date() for i in range(horizon_days)]
    return [(day, buckets.get(day, [])) for day in days]
