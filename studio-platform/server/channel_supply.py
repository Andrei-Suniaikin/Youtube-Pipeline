#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Запас материала канала: на сколько ближайших выходов подряд есть чем выходить.

Зачем отдельный модуль. Ссылки оператор вставляет пачкой, и до сих пор нигде не было
ответа на простой вопрос «когда вставлять следующую». Он виден только задним числом —
когда выход уже прошёл пустым. Внешних источников владелец подключать не стал
(решение 29.09.2026), поэтому единственная защита от простоя — предупредить заранее.

Что считаем запасом. Не «сколько ссылок лежит», а «сколько ближайших выходов канала
ПОДРЯД обеспечены материалом». Разница существенная: десять ссылок, стоящих через
одну, не спасают от пустого выхода уже завтра — а число «десять» об этом молчит.
Поэтому счёт идёт от ближайшего выхода вперёд и обрывается на первом необеспеченном.

Ссылки без даты. Это те, которым не хватило свободного выхода в горизонте канала;
они уже в плане и ждут. Движок (`pub_engine.reflow_slots`) раздаёт им САМЫЕ РАННИЕ
свободные выходы, поэтому здесь мы раскладываем их точно так же — иначе запас
показывал бы дыру там, где ближайший тик её закроет, и тревога кричала бы впустую.

Ноль и «не знаем» — разные ответы. У канала без расписания запас не ноль: считать
его не от чего, выходов у канала нет вовсе. Такой канал отвечает `known=False` и
`days=None`, и ни в тревогу, ни в «каналов без запаса» не попадает. Ноль возвращается
только тогда, когда выходы есть и ближайший из них пустой.
"""

from __future__ import annotations

import datetime as dt
import json

from channel_slots import MAX_HORIZON_DAYS, ScheduleError, upcoming_slots

# Докуда заглядываем вперёд, считая запас. Горизонт самого канала (по умолчанию трое
# суток) для этого не годится: это горизонт РАССТАНОВКИ, за него ссылки не ставятся,
# и запас всегда упирался бы в него, каким бы большим он ни был. Порог тревоги
# ограничен тридцатью сутками (`pub_alerts.LIMITS`), и окно счёта заведомо шире:
# иначе «запас 30 суток» и «запас 45 суток» выглядели бы одинаково у самого порога.
SCAN_DAYS = 45

# Строки плана, которые действительно обеспечивают выход. `cancelled` выпала из плана,
# и её выход свободен; остальные держат его — тот же перечень, что у уникального
# индекса `idx_pub_plan_slot_busy_v2` и у `pub_engine.reflow_slots`. Разойтись им
# нельзя: запас обязан считать занятым ровно то, что движок считает занятым.
HOLDING = ("planned", "frozen", "running", "done", "error")


def supply_facts(c, tenant_id: str, now: float) -> dict:
    """Сырьё для расчёта запаса по всем каналам разом: {id канала: факты}.

    Отдельный запрос на канал превратил бы список из семидесяти каналов в семьдесят
    запросов — та же причина, по которой одним запросом берутся `plan_by_channel` и
    `work_by_channel` в `pub_tags`.

    `dated` — моменты будущих выходов, за которыми уже закреплена ссылка. `waiting` —
    ссылки без даты; их отбор дословно повторяет отбор в `pub_engine.reflow_slots`,
    потому что ждут выхода ровно они и никто больше.
    """
    out: dict = {}
    rows = c.execute(
        "SELECT channel_id, slot_at, status FROM pub_plan_items "
        f"WHERE tenant_id=? AND status IN ({','.join('?' * len(HOLDING))})",
        (tenant_id,) + HOLDING).fetchall()
    for row in rows:
        cid, slot_at, status = row[0], row[1], row[2]
        rec = out.setdefault(cid, {"dated": set(), "waiting": 0})
        if slot_at is None:
            if status == "planned":
                rec["waiting"] += 1
        elif float(slot_at) > now:
            rec["dated"].add(round(float(slot_at)))
    return out


def channel_supply(row, facts=None, now=None, scan_days: int = SCAN_DAYS) -> dict:
    """Запас одного канала. Ничего не читает из базы — только считает.

    `row` — строка `pub_channels_live` (нужны schedule и tz), `facts` — запись этого
    канала из `supply_facts` или None, если строк плана у него нет вовсе.

    Возвращает: known (знаем ли вообще), items (ссылок в запасе), covered (обеспечено
    выходов подряд), days (на сколько суток вперёд хватает) и until (момент последнего
    обеспеченного выхода). `capped` — запас упёрся в край окна счёта: значит «не
    меньше», а не «ровно столько».
    """
    facts = facts or {}
    dated = set(facts.get("dated") or ())
    waiting = int(facts.get("waiting") or 0)
    moment = (dt.datetime.now(dt.timezone.utc) if now is None
              else dt.datetime.fromtimestamp(float(now), dt.timezone.utc))
    items = len(dated) + waiting

    try:
        schedule = json.loads(row["schedule"] or "{}")
    except (TypeError, ValueError):
        schedule = {}
    if not schedule:
        return _unknown(items, waiting, "расписание не задано — запас считать не от чего")

    scan = max(1, min(int(scan_days), MAX_HORIZON_DAYS))
    try:
        slots = upcoming_slots(schedule, row["tz"], scan, moment)
    except ScheduleError:
        # Кривое расписание — это «не знаем», а не «запаса нет»: тревожить владельца
        # пустым каналом, когда на деле сломана настройка, значит увести его не туда.
        return _unknown(items, waiting, "расписание задано с ошибкой — запас неизвестен")
    if not slots:
        return _unknown(items, waiting,
                        f"в ближайшие {scan} суток канал не выходит — запаса нет смысла")

    stamps = [round(s.timestamp()) for s in slots]
    # Раскладываем ссылки без даты так же, как это сделает ближайший тик движка:
    # по самым ранним свободным выходам. Иначе запас показывал бы дыру, которой
    # через полминуты не будет.
    filled = set(dated)
    left = waiting
    for stamp in stamps:
        if left <= 0:
            break
        if stamp not in filled:
            filled.add(stamp)
            left -= 1

    covered = 0
    for stamp in stamps:
        if stamp not in filled:
            break
        covered += 1

    until = slots[covered - 1] if covered else None
    # Сутки считаем до ПОСЛЕДНЕГО обеспеченного выхода: именно после него канал
    # замолчит. Не до первого пустого — тот наступает позже и обещал бы лишний день.
    days = 0.0 if until is None else max(0.0, (until.timestamp() - moment.timestamp()) / 86400)
    return {"known": True, "items": items, "waiting": waiting, "covered": covered,
            "days": days, "until": until, "capped": covered == len(stamps),
            "text": _text(covered, days, items, covered == len(stamps), scan)}


def _unknown(items: int, waiting: int, why: str) -> dict:
    """Ответ «не знаем». Ноль сюда не подставляется намеренно — см. докстроку модуля."""
    return {"known": False, "items": items, "waiting": waiting, "covered": 0,
            "days": None, "until": None, "capped": False, "text": why}


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _days_text(days: float) -> str:
    """Срок словами. Неполные сутки НЕ округляются вверх: «запас на 1 день» вместо
    «меньше суток» позволил бы отложить вставку ссылок ещё на день — ровно до пустого
    выхода, о котором мы и предупреждаем."""
    if days < 1:
        return "запас меньше суток"
    n = int(days)
    return f"запас на {n} {_plural(n, 'день', 'дня', 'дней')}"


def _text(covered: int, days: float, items: int, capped: bool, scan: int) -> str:
    """Запас человеческой строкой — её же показывает плитка канала."""
    if not covered:
        if items:
            # Материал есть, но стоит дальше: ближайший выход всё равно будет пустым,
            # и молчать об этом нельзя только потому, что ссылки в плане не кончились.
            return "ближайший выход пустой — материал стоит дальше"
        return "материала нет — вставьте ссылки"
    вых = _plural(covered, "выход", "выхода", "выходов")
    if capped:
        return f"запаса больше чем на {scan} суток · {covered} {вых}"
    return f"{_days_text(days)} · {covered} {вых}"
