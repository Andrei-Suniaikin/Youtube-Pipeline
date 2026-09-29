#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тревоги: сообщение, когда конвейер встал (ТЗ §13).

Шесть поводов, все из ТЗ дословно:

  1. очередь не пустая, но за 30 минут ничего не завершилось
  2. провайдер на паузе дольше часа
  3. есть готовые ролики, но за 3 часа ничего не залито
  4. наступило время слота, а ролика нет
  5. на диске меньше 100 ГБ
  6. самая старая задача ждёт дольше 6 часов

Седьмой и восьмой поводы добавлены владельцем 07.09.2026, в ТЗ их нет:

7. задача упала с ошибкой — говорится один раз, без повтора и без «починилось»
8. ключ провайдера исчерпан — отказ про деньги, а не про частоту; говорится сразу

Девятый и десятый — тоже не из ТЗ, добавлены владельцем 16.09.2026, оба читают снимок
`admin_state.fastgen_usage` (`server/main.py:save_fastgen_usage`, кладут его туда и
`fastgen_status`, и `dev/alerts.py` перед каждым тиком — чтобы поводы видели свежие
цифры, даже когда в кабинет никто не заходит):

9. ключ FastGen истекает — состояние, а не событие; повтор и «починилось» по общим
   правилам ниже
10. расход картинок близок к часовому лимиту — 07.09.2026 конвейер уже упёрся именно в
    него (`HTTP 429 … Limit: 6000 successful requests per hour`) и узнали об этом
    постфактум по логу, а не заранее

Оба читают один и тот же снимок, и у обоих одно правило свежести: если `checked` в
снимке старше `VS_ALERT_USAGE_STALE_SEC` (по умолчанию 2 часа), оба молчат — судить о
сроке ключа или о лимите по цифрам многочасовой давности значило бы врать так же, как
выдуманный процент, который запрещает правило 1. Это же правило защищает и от обратной
лжи: если тревога уже прозвучала, а снимок потом протух (сеть недоступна), «починилось»
не отправляется, пока не придут свежие цифры — иначе именно сбой сети выключал бы
тревогу о ключе (см. `FASTGEN_STATE_KEYS` в `tick`).

Одиннадцатый повод добавлен владельцем 21.09.2026 по просьбе заказчика: новый ключ
ставит он сам, и от системы ему нужно ровно одно — вовремя узнать, что ключ кончился
и его пора менять:

11. ключ кончился — либо срок действия уже прошёл, либо провайдер перестал ключ
    принимать (`key_dead:{family}`). До этого повода исчерпание ключа КАРТИНОК и
    ОБЛОЖЕК не доходило до телеграма вовсе: §8 читает `credential_cooldown`, а туда
    пишет только канал промптов, так что оставалось одно §7 «задача упала» — один
    раз, без повтора и без «починилось»

Тринадцатый повод добавлен владельцем 29.09.2026 вместе с воротами выдачи заливки
(`server/pub_upload_api.py`, `server/pub_adspower.py`):

13. задание заливки не выдаётся дольше получаса, потому что его профиль AdsPower открыт
    ПОСТОРОННИМ — другим конвейером на той же машине или человеком руками. Само по себе
    придерживание задания правильно (два браузера в одном профиле рвут куки), но ждать
    молча нельзя: освободить профиль может только человек, а он об этом не знает

Три правила подачи, тоже из ТЗ: одно сообщение на случай, повтор не чаще раза в час,
обязательное сообщение «починилось».

**Здесь нет сети.** Модуль только смотрит в базу и решает, что надо сказать; отправку
делает переданная функция `send(text) -> bool`. Из-за этого весь механизм проверяется
тестами без телеграма, без токена и без интернета, а транспорт можно поменять, не трогая
ни одного условия.

Состояние живёт в `admin_state.alerts_state` — в той же базе, что и всё остальное.
Перезапуск процесса не превращает продолжающуюся аварию в новую и не теряет обязанность
сказать «починилось».
"""
from __future__ import annotations

import json
import sys
import os
import shutil
import time

STATE_KEY = "alerts_state"

# Пороги ТЗ. Через окружение меняются только ради тестов и разбора инцидента —
# значения по умолчанию равны написанному в ТЗ, и менять их «на глаз» не следует.
def _sec(name: str, default: int) -> int:
    try:
        return max(1, int(float(os.environ.get(name) or default)))
    except (TypeError, ValueError):
        return default


def _num(name: str, default: float) -> float:
    """Как `_sec`, но без округления до целых секунд и без минимума в 1 — §9/§10
    оперируют днями и процентами, а не секундами, и `_sec` для них не подходит."""
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return float(default)


STALL_SEC = _sec("VS_ALERT_STALL_SEC", 30 * 60)          # §1
RESTING_SEC = _sec("VS_ALERT_RESTING_SEC", 60 * 60)      # §2
UPLOAD_SEC = _sec("VS_ALERT_UPLOAD_SEC", 3 * 3600)       # §3
OLDEST_SEC = _sec("VS_ALERT_OLDEST_SEC", 6 * 3600)       # §6
# §7 — не из ТЗ, добавлен владельцем 07.09.2026. Окно нужно не как порог, а чтобы
# включение тревог не выкатило разом всю историю падений: говорим только про свежие.
FAILED_SEC = _sec("VS_ALERT_FAILED_SEC", 24 * 3600)      # §7
# Сколько падений называем поимённо. Общий ключ или сдохший провайдер валят задачи
# пачкой — на 45 каналах это десятки сообщений подряд, после которых тревоги перестают
# читать. Про остальные говорим одной строкой: важен факт, а разбирают их на экране.
FAILED_MAX = _sec("VS_ALERT_FAILED_MAX", 5)              # §7
REPEAT_SEC = _sec("VS_ALERT_REPEAT_SEC", 60 * 60)        # повтор не чаще раза в час
# Поводы про ключи повторяются реже остальных (жалоба владельца 28.09.2026: «очень
# много раз долбит прилажка»). Причина не в настройке, а в природе повода: очередь
# встала — это чинится за минуты, и напомнить через час осмысленно. Ключ истекает
# через шесть дней или расход подошёл к лимиту — это не чинится за час НИКАК: нужно
# купить продление или дождаться сброса. Час между напоминаниями об одном и том же
# сроке не добавляет ни одного нового сведения, зато к третьему разу тревоги
# перестают читать — и пропускают ту, которая про очередь.
KEY_REPEAT_SEC = _sec("VS_ALERT_KEY_REPEAT_SEC", 24 * 3600)
DISK_MIN_GB = float(os.environ.get("VS_ALERT_DISK_GB") or 100)
# §9/§10 — не из ТЗ, добавлены владельцем 16.09.2026.
KEY_EXPIRES_DAYS = _num("VS_ALERT_KEY_EXPIRES_DAYS", 7)          # §9: за сколько дней предупредить
USAGE_PCT = _num("VS_ALERT_USAGE_PCT", 90)                       # §10: доля часового лимита, в %
USAGE_STALE_SEC = _sec("VS_ALERT_USAGE_STALE_SEC", 2 * 3600)     # §9/§10: снимок старше — не повод
# §11 — не из ТЗ, добавлен владельцем 21.09.2026. Сколько отказов подряд считаем
# доказательством того, что ключ мёртв. Один отказ доказательством не является:
# статистику мы спрашиваем каждую минуту, и провайдер отвечает 401 в том числе на
# собственном сбое. Та же дисциплина, что в воркере (`googler_worker.
# _raise_if_llm_credits_exhausted`): фатально только со второго подтверждения.
KEY_REFUSE_HITS = _num("VS_ALERT_KEY_REFUSE_HITS", 2)            # §11
# §13 — не из ТЗ, решение владельца 29.09.2026. Задание заливки не выдаётся, пока его
# профиль AdsPower открыт посторонним (чужой конвейер на той же машине или человек
# руками). Ждать при этом можно сколько угодно долго и совершенно молча — поэтому
# полчаса ожидания и есть повод: слот уходит, а сделать с этим никто ничего не может,
# пока не узнает.
PROFILE_BUSY_SEC = _sec("VS_ALERT_PROFILE_BUSY_SEC", 30 * 60)    # §13

# ─────────────── пороги: правятся с экрана, а не только из окружения ───────────────
#
# Решение владельца 24.09.2026: «добавить возможность админу это всё эдитить». До этого
# порог менялся только переменной окружения, то есть по SSH и с перезапуском службы.
#
# Порядок старшинства: правка с экрана → переменная окружения → умолчание в коде.
# Значение из окружения остаётся запасным: снял правку — вернулось то, что на машине.
# Применяются пороги в `detect()`, один раз за проход: его зовут и процесс тревог, и
# экран «Админ», и оба должны видеть одно и то же.
ALERT_LIMITS_KEY = "alert_thresholds"

# имя → как назвать человеку, в чём измеряется, разумные границы.
# «sec» правится в МИНУТАХ: пороги тут получасовые и часовые, секунды человеку незачем.
LIMITS = {
    "STALL_SEC": ("очередь не пустая, но ничего не завершается", "sec", 60, 24 * 3600),
    "RESTING_SEC": ("провайдер на паузе", "sec", 60, 24 * 3600),
    "UPLOAD_SEC": ("есть готовые ролики, но заливки нет", "sec", 60, 48 * 3600),
    "OLDEST_SEC": ("самая старая задача ждёт", "sec", 60, 72 * 3600),
    "REPEAT_SEC": ("повтор одного и того же повода", "sec", 300, 24 * 3600),
    "KEY_REPEAT_SEC": ("повтор поводов про ключи", "sec", 300, 7 * 86400),
    "FAILED_SEC": ("про какие падения говорим: за последние", "sec", 3600, 7 * 86400),
    "DISK_MIN_GB": ("свободного места меньше", "gb", 1, 5000),
    "KEY_EXPIRES_DAYS": ("предупредить о сроке ключа за", "days", 1, 90),
    "USAGE_PCT": ("расход ключа близок к лимиту, в %", "pct", 50, 100),
    "KEY_REFUSE_HITS": ("отказов подряд, чтобы счесть ключ мёртвым", "count", 1, 10),
    "FAILED_MAX": ("сколько падений называть поимённо", "count", 1, 50),
    "PROFILE_BUSY_SEC": ("задание ждёт чужой профиль AdsPower", "sec", 60, 24 * 3600),
}
# Значения «как на этой машине»: до первой правки с экрана они и работают.
_DEFAULTS = {name: (int(globals()[name]) if LIMITS[name][1] == "count"
                    else float(globals()[name])) for name in LIMITS}


def limits_state(c) -> dict:
    """Действующие пороги и чем они заданы — для экрана «Админ»."""
    try:
        saved = _state_row(c, ALERT_LIMITS_KEY) or {}
    except Exception:                                              # noqa: BLE001
        saved = {}
    saved = saved if isinstance(saved, dict) else {}
    out = []
    for name, (title, unit, lo, hi) in LIMITS.items():
        own = saved.get(name)
        out.append({"key": name, "what": title, "unit": unit,
                    "value": float(own) if isinstance(own, (int, float)) else _DEFAULTS[name],
                    "default": _DEFAULTS[name], "min": lo, "max": hi,
                    "own": isinstance(own, (int, float))})
    return {"limits": out}


def load_limits(c) -> None:
    """Применить правки с экрана к модулю. Зовётся в начале каждого прохода `detect`."""
    try:
        saved = _state_row(c, ALERT_LIMITS_KEY) or {}
    except Exception:                                              # noqa: BLE001
        saved = {}
    for name in LIMITS:
        value = saved.get(name) if isinstance(saved, dict) else None
        value = float(value) if isinstance(value, (int, float)) else _DEFAULTS[name]
        # Счётные пороги остаются целыми: FAILED_MAX режет список (`rows[:FAILED_MAX]`),
        # а дробным числом срез не сделать.
        globals()[name] = int(value) if LIMITS[name][1] == "count" else value


def set_limit(c, name: str, value, now=None) -> dict:
    """Поправить один порог. `value=None` — вернуть то, что задано на машине."""
    if name not in LIMITS:
        raise ValueError("такого порога нет")
    saved = _state_row(c, ALERT_LIMITS_KEY) or {}
    saved = dict(saved) if isinstance(saved, dict) else {}
    if value is None:
        saved.pop(name, None)
    else:
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError("порог — это число")
        _title, _unit, lo, hi = LIMITS[name]
        if not (lo <= value <= hi):
            raise ValueError(f"допустимо от {lo:g} до {hi:g}")
        saved[name] = value
    _state_save(c, ALERT_LIMITS_KEY, saved, now)
    load_limits(c)
    return limits_state(c)


GB = 1024 ** 3

# Фазы, после которых выпуск уже не «пропущенный слот»: Studio его приняла.
_SLOT_SAFE = ("scheduled", "published")
# Статусы задач, которые считаются живой работой в очереди.
_ACTIVE = ("queued", "running")


class Alert:
    """Один повод. `key` — то, что делает сообщение «одним на случай».

    Два одинаковых повода про разных провайдеров — разные `key`, и это правильно:
    молчать про второго вставший ключ, потому что уже сказали про первого, значит
    скрыть половину аварии.
    """

    __slots__ = ("key", "title", "text", "since_hint", "money")

    def __init__(self, key, title, text, since_hint=None, money=""):
        self.key = key
        self.title = title
        self.text = text
        # Момент, с которого повод уже был верен, если его видно из данных. Без него
        # отсчёт идёт с первого наблюдения — см. `min_hold` в tick().
        self.since_hint = since_hint
        # Есть ли в этом поводе деньги: правило ТЗ §14 требует отвечать на это прямо.
        self.money = money

    def __repr__(self):
        return f"<Alert {self.key}>"


# ──────────────────────────── тринадцать поводов ─────────────────────────────

# Слова, по которым отказ провайдера означает «ключ кончился», а не «слишком часто».
# Список закрытый и совпадает с тем, по которому воркер останавливает прогон
# (`googler_worker._LLM_EXHAUSTED_PATTERNS`): одна формулировка — одно решение.
EXHAUSTED_WORDS = (
    "insufficient_quota", "credit_balance_exhausted", "no credits remaining",
    "exceeded your current quota", "quota exceeded", "billing_not_active",
    "billing hard limit", "usage limit", "кончился лимит аккаунта",
    "исчерпан", "401", "403", "невалиден", "invalid api key",
)


def check_queue_stalled(c, now, tenant=None) -> list:
    """§1. Очередь не пустая, но за 30 минут ничего не завершилось.

    Одного «ничего не завершилось» мало: сразу после запуска пустой системы истории
    завершений нет вовсе, и это не авария. Поэтому требуется ещё и чтобы сама работа
    ждала дольше порога — тогда молчание очереди действительно означает, что она встала.
    """
    where, args = _tenant_where(tenant, "tenant_id")
    row = c.execute(
        f"SELECT COUNT(*) n, MIN(created) oldest FROM queue_tasks "
        f"WHERE status IN {_in(_ACTIVE)}{where}", args).fetchone()
    active, oldest = (row[0] or 0), (row[1] or 0)
    if not active or not oldest or now - oldest < STALL_SEC:
        return []
    last = c.execute(
        f"SELECT MAX(finished) FROM queue_tasks WHERE finished IS NOT NULL{where}",
        args).fetchone()[0]
    if last and now - last < STALL_SEC:
        return []
    quiet = (f"последняя завершилась {_human(now - last)} назад" if last
             else "ни одна задача ещё не завершалась")
    return [Alert(
        "queue_stalled",
        "Очередь встала",
        f"В очереди {_plural(active, 'задача', 'задачи', 'задач')}, {quiet}.\n"
        f"Самая ранняя из них ждёт {_human(now - oldest)}.",
        since_hint=(last + STALL_SEC) if last else None,
        money="Деньги пока не потрачены: задачи не начаты либо начаты и не закрыты — "
              "проверить в журнале вызовов до перезапуска.")]


def check_providers_resting(c, now, tenant=None) -> list:
    """§2. Провайдер на паузе дольше часа.

    Момент начала паузы кабинет не записывает — в `credential_cooldown` есть только время
    пробуждения. Поэтому «дольше часа» устанавливается двумя способами, и оба честные:
    либо мы сами наблюдаем паузу дольше часа подряд, либо до пробуждения осталось больше
    часа — тогда пауза заведомо длиннее часа, ждать доказательства незачем.
    """
    raw = _state_row(c, "credential_cooldown")
    out = []
    for _sid, rec in sorted((raw or {}).items()):
        if not isinstance(rec, dict):
            continue
        until = _f(rec.get("until"))
        if until <= now:
            continue
        if _is_exhausted(rec.get("why")):
            continue      # про этот ключ скажет §8, и скажет сразу, а не через час
        family = str(rec.get("family") or "?")
        left = until - now
        # Пауза известна вперёд — это и есть доказательство её длины.
        proven = left >= RESTING_SEC
        out.append(Alert(
            f"provider_resting:{family}",
            f"Провайдер {family} на паузе",
            f"Ключ {rec.get('mask') or '—'} отдыхает ещё {_human(left)}.\n"
            f"Причина: {(rec.get('why') or 'не записана')[:180]}"
            + ("\nЗамечено при заказе текста или обложки." if rec.get("shared") else ""),
            since_hint=(now - RESTING_SEC) if proven else None,
            money="Оплаченные вызовы не теряются: задачи ждут в очереди. "
                  "Теряется только время слота."))
    return out


def check_upload_stalled(c, now, tenant=None) -> list:
    """§3. Есть готовые ролики, но за 3 часа ничего не залито."""
    where, args = _tenant_where(tenant, "tenant_id")
    ready = c.execute(
        f"SELECT COUNT(*) n, MIN(created) oldest FROM pub_upload_jobs "
        f"WHERE status IN ('queued','claimed','running'){where}", args).fetchone()
    n, oldest = (ready[0] or 0), (ready[1] or 0)
    if not n or not oldest or now - oldest < UPLOAD_SEC:
        return []
    last = c.execute(
        f"SELECT MAX(finished) FROM pub_upload_jobs WHERE finished IS NOT NULL{where}",
        args).fetchone()[0]
    if last and now - last < UPLOAD_SEC:
        return []
    quiet = (f"последняя завершилась {_human(now - last)} назад" if last
             else "ни одной заливки ещё не завершалось")
    return [Alert(
        "upload_stalled",
        "Заливка встала",
        f"Готовых комплектов: {n}, {quiet}.\n"
        f"Самый ранний ждёт {_human(now - oldest)}.",
        since_hint=(last + UPLOAD_SEC) if last else None,
        money="Деньги уже потрачены — ролики сделаны. Пропущенный слот их обесценивает.")]


def check_missed_slots(c, now, tenant=None) -> list:
    """§4. Наступило время слота, а ролика нет.

    Два случая, и они разные по смыслу. Выпуск, который завёлся и не доехал, — сбой
    производства. Слот в плане, под который выпуска не создалось вовсе, — сбой
    планирования. Одним сообщением их не путаем.
    """
    where, args = _tenant_where(tenant, "r.tenant_id")
    late = c.execute(
        f"SELECT COUNT(*) n, MIN(r.slot_at) first FROM pub_releases r "
        f"WHERE r.slot_at <= ? AND r.phase NOT IN {_in(_SLOT_SAFE)} "
        f"AND r.status NOT IN ('cancelled','done'){where}", (now,) + args).fetchone()
    out = []
    if (late[0] or 0):
        out.append(Alert(
            "slot_missed",
            "Слот прошёл, ролика нет",
            f"С прошедшим временем публикации: "
            f"{_plural(late[0], 'выпуск', 'выпуска', 'выпусков')}.\n"
            f"Самый ранний просрочен на {_human(now - late[1])}.",
            since_hint=late[1],
            money="Деньги на эти выпуски уже потрачены."))
    pw, pargs = _tenant_where(tenant, "p.tenant_id")
    orphan = c.execute(
        f"SELECT COUNT(*) n, MIN(p.slot_at) first FROM pub_plan_items p "
        f"LEFT JOIN pub_releases r ON r.plan_item_id = p.id "
        f"WHERE p.slot_at IS NOT NULL AND p.slot_at <= ? AND r.id IS NULL "
        f"AND p.status IN ('planned','frozen'){pw}", (now,) + pargs).fetchone()
    if (orphan[0] or 0):
        out.append(Alert(
            "slot_empty",
            "Слот прошёл, выпуск не заводился",
            f"Позиций плана с прошедшим слотом и без выпуска: {orphan[0]}.\n"
            f"Самая ранняя просрочена на {_human(now - orphan[1])}.",
            since_hint=orphan[1],
            money="Денег не потрачено — работа не начиналась."))
    return out


def check_disk(c, now, tenant=None, path=None) -> list:
    """§5. На диске меньше 100 ГБ — и говорим, на КАКОЙ машине.

    Раньше смотрели только на свой диск. Рендер и заливка идут на другой машине,
    и «мало места» без указания машины посылает человека проверять не тот компьютер.
    Ноды сообщают своё место при каждом опросе очереди (`pub_calls.note_node_disk`);
    если машина молчит дольше четверти часа, её сведения считаются протухшими и
    тревогу мы по ним не поднимаем — иначе выключенная на ночь нода кричала бы вечно.
    """
    target = path or os.environ.get("VS_TENANTS_DIR") or "/data/tenants"
    # Каталога проектов может ещё не быть — например, на свежей машине или на ноутбуке
    # разработчика. Место всё равно надо мерить: поднимаемся до ближайшего существующего
    # предка, и получаем свободное место того тома, куда проекты лягут.
    probe = os.path.abspath(target)
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        return [Alert("disk_unknown", "Свободное место не читается",
                      f"{probe}: {exc}",
                      money="Неизвестно: пока не знаем, сколько места осталось.")]
    free_gb = usage.free / GB
    out = []
    if free_gb < DISK_MIN_GB:
        out.append(Alert(
            "disk_low",
            "Мало места на сервере",
            f"Свободно {free_gb:.1f} ГБ из {usage.total / GB:.0f} ГБ ({probe}).\n"
            f"Порог тревоги — {DISK_MIN_GB:.0f} ГБ.",
            money="Место кончится — встанет и производство, и заливка. "
                  "Что можно убрать, показывает экран «Диск»."))
    return out + _check_nodes(c, now)


def _check_nodes(c, now) -> list:
    """Место на машинах рендера и заливки — по их же сообщениям."""
    try:
        from pub_calls import node_disk
        nodes = node_disk(c, now)
    except Exception:                                              # noqa: BLE001
        return []
    out = []
    for n in nodes:
        if n["stale"] or n["free_gb"] is None or n["free_gb"] >= DISK_MIN_GB:
            continue
        role = {"render": "рендера", "upload": "заливки"}.get(n["role"], n["role"] or "")
        out.append(Alert(
            f"disk_low_node:{n['host']}",
            f"Мало места на машине {n['host']}",
            f"Машина {role}: свободно {n['free_gb']:.1f} ГБ"
            + (f" из {n['total_gb']:.0f} ГБ" if n["total_gb"] else "")
            + f".\nПорог тревоги — {DISK_MIN_GB:.0f} ГБ.",
            money="Эта машина чистит за собой после каждой задачи. Если место всё "
                  "равно кончается — значит задачи падают, не доходя до вердикта, "
                  "и мусор остаётся. Деньги за них уже потрачены."))
    return out


def check_oldest_task(c, now, tenant=None) -> list:
    """§6. Самая старая задача ждёт дольше 6 часов."""
    where, args = _tenant_where(tenant, "tenant_id")
    row = c.execute(
        f"SELECT job_id, kind, created FROM queue_tasks WHERE status='queued'{where} "
        f"ORDER BY created LIMIT 1", args).fetchone()
    if not row or now - (row[2] or now) < OLDEST_SEC:
        return []
    return [Alert(
        "oldest_task",
        "Задача ждёт слишком долго",
        f"Задача #{row[0]} ({row[1]}) стоит в очереди {_human(now - row[2])}.",
        since_hint=(row[2] or now) + OLDEST_SEC,
        money="Денег не потрачено — задача не начата.")]


def _is_exhausted(why) -> bool:
    low = str(why or "").lower()
    return any(w in low for w in EXHAUSTED_WORDS)


def check_key_exhausted(c, now, tenant=None) -> list:
    """§8. Ключ кончился — не «слишком часто», а «платить нечем».

    Почему отдельно от §2. Пауза провайдера — это троттлинг: он проходит сам, и о нём
    говорят через час, чтобы не дёргать по каждому 429. Исчерпанный ключ сам не
    пройдёт: пока его не пополнят или не заменят, каждая следующая задача упрётся в то
    же. Ждать час, чтобы сказать об этом, — терять час.

    Остатка на ключе система не знает и знать не может: у FastGen в API нет ни баланса,
    ни лимитов аккаунта (22 пути, проверено 08.09). Поэтому здесь не «осталось 15%», а
    факт: провайдер ответил отказом, который означает деньги. Выдуманный процент был бы
    той самой молчаливой подстановкой, которую запрещает правило 1.
    """
    raw = _state_row(c, "credential_cooldown")
    out = []
    for _sid, rec in sorted((raw or {}).items()):
        if not isinstance(rec, dict) or _f(rec.get("until")) <= now:
            continue
        why = str(rec.get("why") or "")
        if not _is_exhausted(why):
            continue
        family = str(rec.get("family") or "?")
        out.append(Alert(
            f"key_exhausted:{family}",
            f"Ключ {family} исчерпан",
            f"Ключ {rec.get('mask') or '—'} отвечает отказом, который означает деньги, "
            f"а не троттлинг.\nПровайдер сказал: {why[:180]}"
            + ("\nЗамечено при заказе текста или обложки." if rec.get("shared") else ""),
            money=("Пока ключ не пополнен или не заменён, каждая следующая задача этого "
                   "провайдера упрётся в тот же отказ. Уже оплаченное не теряется — "
                   "задачи ждут в очереди.")))
    return out


def check_failed_tasks(c, now, tenant=None) -> list:
    """§7. Задача упала с ошибкой.

    Поводов ТЗ шесть, и все они про то, что работа ИДЁТ СЛИШКОМ ДОЛГО: очередь стоит,
    провайдер отдыхает, заливка молчит, задача ждёт. Упавшая задача не подходит ни под
    один: она умирает за секунды, очередь после неё пуста, ждать нечему. Ровно так
    07.09 прогон встал на невалидном ключе — и по всем шести поводам было «спокойно».

    Отсюда две особенности подачи, обе намеренные (см. NO_REPEAT / NO_FIXED):

    • не повторяем. Остальные поводы с каждым часом означают всё большую беду, и
      «ВСЁ ЕЩЁ (3)» несёт новость. У падения новостей нет: оно уже случилось, и
      ежечасное напоминание — шум, из-за которого перестают читать и настоящие тревоги.
    • не говорим «починилось». Задача уходит из повода, когда её повторили, отменили
      или она состарилась за окном. Ни одно из этого не значит, что стало хорошо.
    """
    # Колонка обязательно с префиксом таблицы: `projects` тоже имеет tenant_id, и без
    # него запрос падает на «ambiguous column name» — но только когда тенант задан,
    # то есть на экране тревог, а не в проходе рассылки.
    where, args = _tenant_where(tenant, "q.tenant_id")
    rows = c.execute(
        f"SELECT q.job_id, q.kind, q.error, q.finished, q.project_id, p.name "
        f"FROM queue_tasks q LEFT JOIN projects p ON p.id = q.project_id "
        f"WHERE q.status='error' AND COALESCE(q.finished, q.created) >= ?{where} "
        f"ORDER BY COALESCE(q.finished, q.created) DESC", (now - FAILED_SEC,) + args).fetchall()
    out = []
    for job_id, kind, error, finished, project_id, name in rows[:FAILED_MAX]:
        # Деньги считаем по журналу вызовов: у падения этап известен, а вот потратил
        # ли он что-нибудь до падения — нет. Правило ТЗ §14 требует ответить прямо.
        paid = c.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost), 0) FROM provider_calls "
            "WHERE project_id=? AND kind=? AND status='delivered'",
            (project_id, kind)).fetchone() if project_id else (0, 0)
        n, cost = (paid[0] or 0), (paid[1] or 0)
        out.append(Alert(
            f"failed:{job_id}",
            f"Задача упала: {kind}",
            f"Задача #{job_id} ({kind}) проекта «{name or project_id or '—'}» "
            f"остановилась с ошибкой.\n{(error or 'причина не записана').strip()[:400]}",
            since_hint=finished,
            money=(f"До падения оплачено вызовов: {n}"
                   + (f" на {cost:.0f} кр." if cost else ".")
                   if n else "Оплаченных вызовов по этому этапу не записано.")))
    if len(rows) > FAILED_MAX:
        out.append(Alert(
            "failed:ещё",
            "Упало сразу много задач",
            f"За последние {_human(FAILED_SEC)} упало задач: {len(rows)}. "
            f"Поимённо названы {FAILED_MAX} самых свежих, остальные — на экране очереди.\n"
            f"Столько разом обычно значит одну причину на всех: ключ, провайдер или диск.",
            money="Деньги считаются по каждой задаче отдельно — смотрите её карточку."))
    return out


def _fastgen_usage_snapshot(c, now):
    """Снимок `admin_state.fastgen_usage`, если он есть и не протух.

    Общий вход для §9 и §10: один источник данных — одно правило устаревания. Снимок
    кладёт `main.save_fastgen_usage` (из `fastgen_status` и из `dev/alerts.py`); если
    его ещё не было (свежий стенд, ни разу не спросили статус моделей) — снимка нет и
    оба повода молчат, а не выдумывают цифры.
    """
    snap = _state_row(c, "fastgen_usage")
    if not isinstance(snap, dict):
        return None
    checked = _f(snap.get("checked"))
    if not checked or now - checked > USAGE_STALE_SEC:
        return None
    return snap


def check_key_expiring(c, now, tenant=None) -> list:
    """§9. Ключ FastGen истекает — не из ТЗ, добавлено владельцем 16.09.2026.

    Состояние, а не событие: пока до истечения меньше `KEY_EXPIRES_DAYS`, повод держится
    и повторяется по общему правилу раз в час (`REPEAT_SEC`); продление ключа снимает
    его из `admin_state.fastgen_usage` на следующем снимке, и тогда звучит «починилось»,
    как у любого другого состояния — своего исключения из `NO_FIXED` ему не нужно.

    Повод только про «ещё работает, но скоро кончится». Как только срок прошёл, новость
    становится другой — «конвейер встал, нужен новый ключ», — и её несёт §11
    (`check_key_dead`) со своим `key`. Иначе смерть ключа пришла бы строкой «ВСЁ ЕЩЁ (5)
    · Ключ FastGen истекает», которую читают как «время ещё есть».
    """
    snap = _fastgen_usage_snapshot(c, now)
    if not snap:
        return []
    expires = _f(snap.get("expires"))
    # Верхняя граница — §11. Про уже истёкший ключ говорит он, и говорит другое:
    # «кончился, нужен новый». Без границы об одном событии уходило бы два сообщения
    # с разными заголовками, каждое со своим часовым повтором.
    if not expires or expires <= now or expires - now > KEY_EXPIRES_DAYS * 86400:
        return []
    when = time.strftime("%d.%m.%Y", time.localtime(expires))
    return [Alert(
        "fastgen_key_expiring", "Ключ FastGen истекает",
        f"Действует до {when}, осталось {_human(expires - now)}.\n"
        f"Что сделать: заранее получить у провайдера новый ключ — когда срок выйдет, "
        f"картинки и обложки встанут в тот же час.",
        money="Продлевает ключ владелец у провайдера — система это делать не может.")]


def check_usage_near_limit(c, now, tenant=None) -> list:
    """§10. Расход картинок близок к часовому лимиту — не из ТЗ, добавлено владельцем
    16.09.2026.

    07.09.2026 конвейер уже упёрся именно в этот лимит — `HTTP 429 … Limit: 6000
    successful requests per hour` — и узнали об этом постфактум по логу. Порог задан
    долей от `img_gen_per_hour_limit` (`USAGE_PCT`), а не абсолютным числом: лимит у
    провайдера может измениться, и абсолютный порог тихо перестал бы срабатывать.
    """
    snap = _fastgen_usage_snapshot(c, now)
    if not snap:
        return []
    limits = snap.get("limits") if isinstance(snap.get("limits"), dict) else {}
    hourly = snap.get("hourly") if isinstance(snap.get("hourly"), dict) else {}
    limit = _f(limits.get("img_gen_per_hour_limit"))
    used = hourly.get("image_generation")
    # Провайдер с 21.09.2026 отдаёт расход словарём {current_usage, window_start}; снимок
    # старой записи мог остаться в базе. `_f(dict)` дал бы ноль, и тревога молчала бы
    # даже при исчерпанном лимите (см. main.fastgen_count).
    if isinstance(used, dict):
        used = used.get("current_usage")
    used = _f(used)
    if limit <= 0 or used / limit * 100 < USAGE_PCT:
        return []
    return [Alert(
        "fastgen_usage_near_limit", "Расход картинок близок к часовому лимиту",
        f"Израсходовано {int(used)} из {int(limit)} за час.",
        money="Лимит новый час не переносит остаток — упрёмся в 429, как 07.09.2026.")]


# ─────────── §11: ключ кончился (отказ провайдера или вышедший срок) ───────────

def _family_title(family: str) -> str:
    """Как назвать ключ ЗАКАЗЧИКУ. Он покупает ключ FastGen и слова «googler» не знает.

    §9 говорит «Ключ FastGen», §11 говорил «Ключ googler» — про один и тот же ключ.
    Получив «Ключ googler кончился», человек пойдёт искать несуществующий ключ Google,
    тем более что рядом в совете стоит COVER_FAST_GEN_KEY. Внутреннее имя семейства
    оставляем в скобках: оно нужно тому, кто будет класть ключ в файл.
    """
    return f"FastGen (семейство {family})" if family == FASTGEN_FAMILY else family


# Заявка на отказ: `{семейство: {code, mask, since, seen, last}}`. Пишет её тот, кто
# ходит в сеть (`dev/alerts.py:refresh_fastgen_usage`), но пишет через `note_key_refused`
# ниже — чтобы форма записи лежала рядом с поводом, который её читает, и чтобы сам
# `pub_alerts` остался без сети и проверялся тестами без интернета.
KEY_REFUSED_KEY = "fastgen_key_refused"

# Коды, которыми провайдер говорит именно про КЛЮЧ, а не про себя. 5xx и 429 сюда не
# входят намеренно: первое — беда провайдера, второе на бесплатной статистике означает
# «слишком часто спрашиваете», а не «платить нечем». Ошибиться здесь дорого в обе
# стороны: лишний код — ложная тревога раз в час, недостающий — молчание при мёртвом ключе.
KEY_REFUSED_CODES = (401, 402, 403)

# Снимок `/api/v6/usage` снимается ключом семейства `googler` из общего файла ключей
# (`dev/alerts.py:refresh_fastgen_usage`): им кабинет делает картинки, и им же по
# умолчанию работают обложки — `dev/run.sh` отдаёт в `COVER_FAST_GEN_KEY` значение из
# того же файла. Если на боевой машине обложкам задан ОТДЕЛЬНЫЙ ключ, этот повод про
# него ничего не знает: у Cover Studio своя база, свой хост (api.fast-gen.ai) и своё
# окружение, снимка в `admin_state` они не кладут.
FASTGEN_FAMILY = "googler"

_KEY_DEAD_MONEY = (
    "Проверка ключа бесплатна — это чтение статистики, 0 кредитов; генерацией «на "
    "пробу» ключи здесь не проверяются никогда. Само это не пройдёт: пока ключ не "
    "заменён, каждая следующая задача картинок (и обложек, если они работают этим же "
    "ключом) упрётся в тот же отказ. Задачи при этом НЕ ждут в очереди, а падают с "
    "ошибкой, и после замены ключа их надо поднять кнопкой «Повторить» — иначе выпуск "
    "будет считаться сделанным, а кадров в нём не окажется.")


def _key_dead_fix(family: str) -> str:
    return (f"Что сделать: получить у провайдера новый ключ и положить его в файл "
            f"ключей, семейство {family}; если у обложек задан свой COVER_FAST_GEN_KEY "
            f"— обновить и его.")


def note_key_refused(c, family, code, now, mask="") -> None:
    """Провайдер ОТВЕТИЛ и ключ не принял. Одна запись на семейство.

    Значение ключа сюда не попадает никогда — только маска, код ответа и время.
    `since` хранит начало отказа, а не момент разговора о нём: перезапуск процесса не
    должен обнулять длительность аварии.
    """
    rec = _state_row(c, KEY_REFUSED_KEY)
    rec = rec if isinstance(rec, dict) else {}
    was = rec.get(family) if isinstance(rec.get(family), dict) else {}
    rec[family] = {
        "code": int(_f(code)),
        "mask": str(mask or was.get("mask") or ""),
        "since": _f(was.get("since")) or now,
        "seen": int(_f(was.get("seen"))) + 1,
        "last": now,
    }
    _state_save(c, KEY_REFUSED_KEY, rec, now)


def note_key_accepted(c, family, now) -> None:
    """Провайдер ответил цифрами — ключ живой, заявку снимаем.

    Снимается ровно здесь и только здесь: единственное доказательство того, что ключ
    заменили, — принятый провайдером запрос. По таймеру «само рассосётся» было бы
    обещанием, которого никто не давал.
    """
    rec = _state_row(c, KEY_REFUSED_KEY)
    if not isinstance(rec, dict) or family not in rec:
        return
    rec.pop(family, None)
    _state_save(c, KEY_REFUSED_KEY, rec, now)


def check_key_dead(c, now, tenant=None) -> list:
    """§11. Ключ кончился — нужен новый. Не из ТЗ, добавлено владельцем 21.09.2026.

    Новый ключ ставит заказчик сам; от системы ему нужно ровно одно — узнать, что
    ключ кончился. Поэтому повод отвечает на три вопроса сразу: какой ключ (ИМЯ
    семейства, не значение), что с ним случилось и что сделать.

    Почему не §8. §8 читает `admin_state.credential_cooldown`, а туда пишет только
    канал промптов (`main._PROMPT_CHANNEL_FAMILY`: openai / claude_oauth / codex_home /
    alt_llm). Ключ картинок и обложек в эту запись не попадает вовсе — значит про него
    §8 не сработал ни разу, и исчерпание такого ключа доходило до телеграма только как
    §7 «задача упала»: один раз, без повтора, без «починилось» и без слова «ключ» в
    заголовке.

    Два источника факта, и оба бесплатные — проверять ключ генерацией нельзя, один
    кадр FastGen стоит денег (правило проекта про опасный retry):

      • ОТКАЗ. Провайдер ответил кодом из `KEY_REFUSED_CODES` на бесплатную статистику
        `/api/v6/usage` (`note_key_refused`). Считается со ВТОРОГО отказа подряд —
        одиночный 401 бывает и сбоем провайдера, а спрашиваем мы раз в минуту.
      • СРОК. `expires` из снимка `admin_state.fastgen_usage` уже в прошлом.

    Оба источника про один и тот же ключ, поэтому на семейство выходит один Alert:
    мёртвый ключ, у которого ещё и срок вышел, — одна беда, а не две.

    Повод-состояние: `key` постоянный (`key_dead:{family}`, без даты и без кода внутри
    — иначе state не узнавал бы его и сообщение уходило бы каждую минуту), повтор раз
    в час несёт новость «всё ещё не заменили», а «починилось» означает «новый ключ
    приняли». Поэтому ни в `NO_REPEAT`, ни в `NO_FIXED` его нет.
    """
    out = {}
    raw = _state_row(c, KEY_REFUSED_KEY)
    # Запись читаем защищаясь: `detect` зовут и тик, и экран «Админ», и упасть на
    # испорченном JSON значило бы погасить разом все поводы.
    raw = raw if isinstance(raw, dict) else {}
    for family, rec in sorted(raw.items()):
        if not isinstance(rec, dict) or _f(rec.get("seen")) < KEY_REFUSE_HITS:
            continue
        # Та же свежесть, что у снимка: пока отказ никто не подтверждает (например,
        # мёртвый ключ убрали из файла и спрашивать стало нечем), утверждать «ключ не
        # принимают» мы не вправе. Соврать в обратную сторону не даст `FASTGEN_STATE_KEYS`:
        # «починилось» без свежих цифр тоже не уйдёт.
        if now - _f(rec.get("last")) > USAGE_STALE_SEC:
            continue
        key = f"key_dead:{family}"
        out[key] = Alert(
            key, f"Ключ {_family_title(family)} кончился",
            # 402 Payment Required — ключ ЖИВОЙ, кончились деньги на счету. Совет
            # «получите новый ключ» тут вреден: старый именной ключ будет выброшен зря,
            # а достаточно пополнить баланс.
            (f"Кончились деньги на счету провайдера (HTTP 402) — ключ рабочий, менять "
             f"его не надо, нужно пополнить счёт.\n"
             if int(_f(rec.get("code"))) == 402 else
             f"Нужен новый ключ: провайдер отвечает отказом HTTP "
             f"{int(_f(rec.get('code')))} даже на бесплатный запрос статистики.\n")
            + f"Ключ {rec.get('mask') or '—'}, отказов подряд: "
            f"{int(_f(rec.get('seen')))}.\n" + _key_dead_fix(family),
            since_hint=_f(rec.get("since")) or None,
            money=_KEY_DEAD_MONEY)
    snap = _fastgen_usage_snapshot(c, now)
    expires = _f(snap.get("expires")) if snap else 0
    key = f"key_dead:{FASTGEN_FAMILY}"
    if expires and expires <= now and key not in out:
        when = time.strftime("%d.%m.%Y", time.localtime(expires))
        out[key] = Alert(
            key, f"Ключ {_family_title(FASTGEN_FAMILY)} кончился",
            f"Нужен новый ключ: срок действия истёк {when}, "
            f"{_human(now - expires)} назад.\n" + _key_dead_fix(FASTGEN_FAMILY),
            since_hint=expires,
            money=_KEY_DEAD_MONEY)
    return list(out.values())


def _text_subscriptions():
    """Модуль подписок рерайта. Процесс тревог живёт с `server/` на пути, рерайт — рядом."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "rewrite")
    if path not in sys.path:
        sys.path.append(path)
    try:
        import subscriptions
    except ImportError:
        return None
    return subscriptions


def check_text_subscription_dead(c, now, tenant=None) -> list:
    """§12. Подписка для текста разлогинилась. Добавлено 22.09.2026 вместе с экраном
    подписок: раньше протухший вход Codex или Claude было видно только по тому, что
    задачи текста падали одна за другой.

    Факт берётся из состояния, которое пишет сам рерайт по итогам настоящих вызовов
    (`subscriptions.note`): два отказа входа подряд. Проверок «на пробу» нет. Повод —
    состояние: «починилось» уходит, когда подписку заменили, вернули или удалили.
    """
    subs = _text_subscriptions()
    if subs is None:
        return []
    try:
        items = subs.listing(now)
    except Exception:                                  # noqa: BLE001
        return []
    out = []
    for item in items:
        if item["state"] != "dead":
            continue
        name = "Codex (ChatGPT)" if item["provider"] == "codex" else "Claude"
        alive = sum(1 for i in items if i["provider"] == item["provider"]
                    and i["state"] in ("new", "ok", "limit"))
        how = ("войти командой `codex login` на своём компьютере и вставить файл "
               "~/.codex/auth.json" if item["provider"] == "codex" else
               "получить новый токен командой `claude setup-token` и вставить его")
        out.append(Alert(
            f"text_sub_dead:{item['id']}",
            f"Подписка для текста «{item['label']}» разлогинилась",
            f"{name}, вход {item['tail'] or '—'}: провайдер дважды подряд не принял "
            f"вход. Подписка вынута из раздачи.\n"
            + (f"Текст пишут остальные подписки {name}: рабочих — {alive}.\n" if alive
               else f"Других рабочих подписок {name} нет — задачи текста этим "
                    f"провайдером встанут.\n")
            + f"Что сделать: «Ключи» → «Подписки для текста» → «Заменить»: {how}.",
            since_hint=item.get("since"),
            money="Отказ входа денег не стоит: вызов не состоялся, а подписка — "
                  "фиксированная плата. Задачи текста, упавшие на ней, после замены "
                  "поднять кнопкой «Повторить»."))
    return out


def check_upload_profile_busy(c, now, tenant=None) -> list:
    """§13. Задание заливки стоит из-за чужого профиля AdsPower дольше получаса.

    Считаем только `queued`: выданное задание уже льётся, и его ожидание кончилось.
    `blocked_since` ставит и снимает сама выдача (`pub_upload_api`) — тревога ничего
    не вычисляет заново и поэтому не может разойтись с тем, что видит распределитель.

    Один повод на всё, а не по поводу на каждое задание: профиль обычно держит один и
    тот же посторонний, и десяток одинаковых сообщений подряд приводит к тому, что
    тревоги перестают читать (та же причина, что у `FAILED_MAX` в §7).
    """
    # База, до которой новая схема ещё не доехала. Пустой ответ здесь — не умолчание
    # о беде, а правда: колонку заполняют только ворота выдачи, и если её нет, то нет
    # и кода, который придерживает задания, — ждать из-за чужого профиля нечему. Без
    # этой проверки один отсутствующий столбец ронял бы ВЕСЬ проход тревог: процесс
    # тревог (`dev/alerts.py`) ходит в базу своим таймером и схему не создаёт, то есть
    # запросто может прийти раньше, чем служба кабинета применит миграцию.
    if not any(r[1] == "blocked_since"
               for r in c.execute("PRAGMA table_info(pub_upload_jobs)")):
        return []
    where, args = _tenant_where(tenant, "tenant_id")
    rows = c.execute(
        f"SELECT release_id, blocked_since, blocked_why FROM pub_upload_jobs "
        f"WHERE status='queued' AND blocked_since IS NOT NULL{where} "
        f"ORDER BY blocked_since", args).fetchall()
    stuck = [r for r in rows if now - _f(r[1]) >= PROFILE_BUSY_SEC]
    if not stuck:
        return []
    oldest = _f(stuck[0][1])
    names = "\n".join(f"· {r[0]}: {(r[2] or 'профиль занят посторонним')[:160]}"
                       for r in stuck[:int(FAILED_MAX)])
    more = (f"\nи ещё {len(stuck) - int(FAILED_MAX)} — остальные видно на экране "
            f"«Выпуски»" if len(stuck) > int(FAILED_MAX) else "")
    return [Alert(
        "upload_profile_busy",
        "Заливка ждёт чужой профиль AdsPower",
        f"Заданий ждёт профиля: {len(stuck)}, самое раннее — {_human(now - oldest)}.\n"
        f"{names}{more}\n"
        f"Что сделать: закрыть профиль в AdsPower на машине заливки — тем, кто его "
        f"открыл. Сами мы его не тронем: два браузера в одном профиле рвут куки.",
        since_hint=oldest + PROFILE_BUSY_SEC,
        money="Денег это не стоит: задание ещё не начиналось, оплаченный ролик цел. "
              "Теряется время выхода — слот может уйти.")]


CHECKS = (check_queue_stalled, check_providers_resting, check_upload_stalled,
          check_missed_slots, check_disk, check_oldest_task, check_failed_tasks,
          check_key_exhausted, check_key_expiring, check_usage_near_limit,
          check_key_dead, check_text_subscription_dead, check_upload_profile_busy)

# Сколько повод должен продержаться, прежде чем о нём говорят. Там, где длительность
# и есть суть повода (§2), она проверяется здесь; остальные поводы уже содержат свой
# порог внутри запроса, и держать их дополнительно значило бы удваивать ожидание.
MIN_HOLD = {"provider_resting": RESTING_SEC}

# Поводы-события, а не состояния: сказать один раз и не возвращаться. Повтор и
# «починилось» осмысленны, только когда повод описывает то, что тянется.
NO_REPEAT = {"failed"}
NO_FIXED = {"failed"}

# §9/§10/§11 читают один и тот же снимок FastGen. Пока он протух (или его вовсе не
# было), «пропал из active» значит «нет свежих цифр», а не «починилось» — иначе именно
# сбой сети (единственная причина устаревания снимка) выключал бы уже прозвучавшую
# тревогу о ключе. Разбор инцидента 16.09.2026 (review issue 1): тревога прозвучала,
# снимок перестал обновляться (сеть недоступна), два часа спустя `tick` без этого
# исключения посылал бы «ПОЧИНИЛОСЬ», хотя ключ не продлён и расход не упал.
# Сравнение по префиксу, как у NO_REPEAT/NO_FIXED: у §11 в ключе стоит ещё и семейство
# (`key_dead:googler`), и точное совпадение его бы не поймало.
FASTGEN_STATE_KEYS = {"fastgen_key_expiring", "fastgen_usage_near_limit", "key_dead"}

# Поводы про ключи и подписки: им свой, редкий повтор (`KEY_REPEAT_SEC`). Сравнение
# по префиксу до двоеточия — как у NO_REPEAT: у половины из них в ключе стоит ещё и
# семейство или номер подписки.
#
# Почему именно эти шесть, а не «всё, где в названии есть key». `key_exhausted` и
# `key_dead` — про ключ, который уже не работает: до продления ничего не изменится,
# сколько ни напоминай. `provider_resting` — та же порода: провайдер отдыхает по
# своему расписанию, и час напоминаний его не разбудит. Очередь, заливка и место на
# диске сюда НЕ входят намеренно: они чинятся быстро, и там частое напоминание — это
# и есть польза.
KEY_ALERT_KEYS = {"fastgen_key_expiring", "fastgen_usage_near_limit", "key_dead",
                  "key_exhausted", "text_sub_dead", "provider_resting"}


def repeat_after(key: str) -> float:
    """Через сколько повторять этот повод. Первое сообщение задержки не знает."""
    return KEY_REPEAT_SEC if key.split(":")[0] in KEY_ALERT_KEYS else REPEAT_SEC


def detect(c, now=None, tenant=None) -> dict:
    """Все поводы, верные прямо сейчас: key → Alert. Ничего не пишет и не отправляет."""
    now = now or time.time()
    # Пороги, поправленные с экрана, вступают в силу со следующего прохода — без
    # перезапуска службы и без правки окружения.
    load_limits(c)
    found = {}
    for fn in CHECKS:
        for a in fn(c, now, tenant):
            found[a.key] = a
    return found


# ──────────────────────────── подача: одно сообщение ────────────────────────────

def load_state(c) -> dict:
    return _state_row(c, STATE_KEY) or {}


def _state_save(c, key, value, now=None) -> None:
    """Запись в `admin_state` одной формой на всех: и память тревог, и заявка на отказ
    ключа (§11) ложатся одинаково, иначе две похожие записи расходятся молча."""
    c.execute("INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES(?,?,?)",
              (key, json.dumps(value, ensure_ascii=False), now or time.time()))
    c.commit()


def save_state(c, state, now=None) -> None:
    _state_save(c, STATE_KEY, state, now)


def tick(c, send, now=None, tenant=None) -> list:
    """Один проход: посмотреть, решить, сказать, запомнить.

    Возвращает список отправленного [(key, вид, текст)], где вид — `alarm`, `repeat`
    или `fixed`. Безопасно вызывать хоть каждую минуту: повтор ограничен часом, а
    «починилось» отправляется ровно один раз и только если тревога успела прозвучать.

    Не отправленное (send вернул False) не помечается отправленным — иначе упавший
    телеграм превращал бы аварию в тишину.
    """
    now = now or time.time()
    active = detect(c, now, tenant)
    # Снимок FastGen протух (или его нет) — §9/§10 могли исчезнуть из `active` именно
    # из-за этого, а не потому, что ключ продлили или расход упал. См. FASTGEN_STATE_KEYS.
    fastgen_stale = _fastgen_usage_snapshot(c, now) is None
    state = load_state(c)
    sent = []

    for key, alert in sorted(active.items()):
        st = state.get(key)
        if not isinstance(st, dict):
            # Повод виден впервые. Если из данных известно, что он верен уже давно,
            # берём это время — иначе перезапуск процесса откладывал бы тревогу.
            st = {"since": min(_f(alert.since_hint) or now, now), "sent": 0.0, "n": 0}
            state[key] = st
        st["title"] = alert.title
        held = now - _f(st.get("since"))
        if held < MIN_HOLD.get(key.split(":")[0], 0):
            continue
        last_sent = _f(st.get("sent"))
        if last_sent and (key.split(":")[0] in NO_REPEAT
                          or now - last_sent < repeat_after(key)):
            continue
        kind = "repeat" if last_sent else "alarm"
        text = _format(alert, kind, held, int(st.get("n") or 0) + 1)
        if not send(text):
            continue
        st["sent"] = now
        st["n"] = int(st.get("n") or 0) + 1
        sent.append((key, kind, text))

    # Повод мог не «починиться», а ПЕРЕРАСТИ в худший. У §9 («истекает») и §11
    # («кончился») именно так: в тот тик, когда срок выходит, §9 уходит из active, а §11
    # появляется. Без этой проверки заказчик получал подряд «ТРЕВОГА · Ключ кончился» и
    # «ПОЧИНИЛОСЬ · Ключ истекает» — то есть «починилось» ровно в момент поломки. Оба
    # проверяющих воспроизвели это на живом тике. Пока стоит наследник, предшественник
    # молча забывается: сказать о нём нечего, о беде уже сказано.
    _succeeded_by = {"fastgen_key_expiring": f"key_dead:{FASTGEN_FAMILY}"}
    for key in [k for k in state if k not in active]:
        heir = _succeeded_by.get(key)
        if heir and heir in active:
            state.pop(key, None)
            continue
        if fastgen_stale and key.split(":")[0] in FASTGEN_STATE_KEYS:
            continue        # снимок протух — ждём свежих цифр, а не чиним по тишине
        st = state.pop(key)
        if not isinstance(st, dict) or not _f(st.get("sent")):
            continue        # тревога не звучала — «починилось» было бы про что-то новое
        if key.split(":")[0] in NO_FIXED:
            continue        # событие: уход из повода не значит, что стало хорошо
        text = (f"ПОЧИНИЛОСЬ · {st.get('title') or key}\n"
                f"Длилось {_human(now - _f(st.get('since')))}.")
        if not send(text):
            state[key] = st          # не сказали — обязанность сказать остаётся
    save_state(c, state, now)
    return sent


def _format(alert: Alert, kind: str, held: float, n: int) -> str:
    head = "ТРЕВОГА" if kind == "alarm" else f"ВСЁ ЕЩЁ ({n})"
    parts = [f"{head} · {alert.title}", "", alert.text.strip()]
    if kind != "alarm":
        parts.append(f"\nДлится {_human(held)}.")
    if alert.money:
        parts.append(f"\nДеньги: {alert.money}")
    return "\n".join(parts).strip()


# ─────────────────────────────── мелочи ───────────────────────────────

def _in(values) -> str:
    """Литерал IN из наших собственных констант — снаружи сюда ничего не приходит."""
    return "(" + ",".join("'" + v.replace("'", "") + "'" for v in values) + ")"


def _tenant_where(tenant, column) -> tuple:
    return (f" AND {column}=?", (tenant,)) if tenant else ("", ())


def _state_row(c, key):
    row = c.execute("SELECT v FROM admin_state WHERE k=?", (key,)).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except ValueError:
        return None


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _plural(n: int, one: str, few: str, many: str) -> str:
    """«1 задача», «2 задачи», «5 задач». Сообщение читают в три часа ночи —
    пусть оно будет написано по-русски, а не «4 задач(и)»."""
    n = int(n)
    t = n % 100
    if 11 <= t <= 14:
        form = many
    elif n % 10 == 1:
        form = one
    elif 2 <= n % 10 <= 4:
        form = few
    else:
        form = many
    return f"{n} {form}"


def _human(sec) -> str:
    sec = int(max(0, _f(sec)))
    if sec < 90:
        return f"{sec} с"
    if sec < 5400:
        return f"{sec // 60} мин"
    if sec < 48 * 3600:
        h, m = divmod(sec // 60, 60)
        return f"{h} ч {m:02d} мин" if m else f"{h} ч"
    return f"{sec // 86400} сут"
