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


STALL_SEC = _sec("VS_ALERT_STALL_SEC", 30 * 60)          # §1
RESTING_SEC = _sec("VS_ALERT_RESTING_SEC", 60 * 60)      # §2
UPLOAD_SEC = _sec("VS_ALERT_UPLOAD_SEC", 3 * 3600)       # §3
OLDEST_SEC = _sec("VS_ALERT_OLDEST_SEC", 6 * 3600)       # §6
REPEAT_SEC = _sec("VS_ALERT_REPEAT_SEC", 60 * 60)        # повтор не чаще раза в час
DISK_MIN_GB = float(os.environ.get("VS_ALERT_DISK_GB") or 100)

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


# ─────────────────────────────── шесть поводов ───────────────────────────────

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
        family = str(rec.get("family") or "?")
        left = until - now
        # Пауза известна вперёд — это и есть доказательство её длины.
        proven = left >= RESTING_SEC
        out.append(Alert(
            f"provider_resting:{family}",
            f"Провайдер {family} на паузе",
            f"Ключ {rec.get('mask') or '—'} отдыхает ещё {_human(left)}.\n"
            f"Причина: {(rec.get('why') or 'не записана')[:180]}"
            + ("\nЗамечено соседним сервисом." if rec.get("shared") else ""),
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


CHECKS = (check_queue_stalled, check_providers_resting, check_upload_stalled,
          check_missed_slots, check_disk, check_oldest_task)

# Сколько повод должен продержаться, прежде чем о нём говорят. Там, где длительность
# и есть суть повода (§2), она проверяется здесь; остальные поводы уже содержат свой
# порог внутри запроса, и держать их дополнительно значило бы удваивать ожидание.
MIN_HOLD = {"provider_resting": RESTING_SEC}


def detect(c, now=None, tenant=None) -> dict:
    """Все поводы, верные прямо сейчас: key → Alert. Ничего не пишет и не отправляет."""
    now = now or time.time()
    found = {}
    for fn in CHECKS:
        for a in fn(c, now, tenant):
            found[a.key] = a
    return found


# ──────────────────────────── подача: одно сообщение ────────────────────────────

def load_state(c) -> dict:
    return _state_row(c, STATE_KEY) or {}


def save_state(c, state, now=None) -> None:
    c.execute("INSERT OR REPLACE INTO admin_state(k,v,updated) VALUES(?,?,?)",
              (STATE_KEY, json.dumps(state, ensure_ascii=False), now or time.time()))
    c.commit()


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
        if last_sent and now - last_sent < REPEAT_SEC:
            continue
        kind = "repeat" if last_sent else "alarm"
        text = _format(alert, kind, held, int(st.get("n") or 0) + 1)
        if not send(text):
            continue
        st["sent"] = now
        st["n"] = int(st.get("n") or 0) + 1
        sent.append((key, kind, text))

    for key in [k for k in state if k not in active]:
        st = state.pop(key)
        if not isinstance(st, dict) or not _f(st.get("sent")):
            continue        # тревога не звучала — «починилось» было бы про что-то новое
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
