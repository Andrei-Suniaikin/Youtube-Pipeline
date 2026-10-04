#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ежедневная сводка в формате заказчика («Общий маршрут · 5 дней»).

Зачем. Формат отчёта заказчик задал, а собирает его сегодня человек — руками, проходя
сотню строк плана и выпусков каждый день. Почти всё, что там спрашивают, студия уже
знает: план, выпуски, заливки, подтверждённые выходы, запас каналов, тревоги. Сводка
их только складывает и показывает одной страницей, которую можно скопировать в отчёт.

Формат воспроизводится по смыслу, четырьмя строками:

  · по каждой группе каналов: в плане __, принято __, блокеры __;
  · сегодня: ожидалось __ выходов, подтверждено __, задержано __;
  · производство: принято новых роликов __, каналов без запаса __;
  · проблема: канал → причина → следующий результат.

**Главное здесь — честность чисел, а не их наличие.** Ниже перечислены все места, где
легко соврать, и что сделано вместо этого.

1. **«Подтверждено» — это подтверждённый выход на YouTube, а не `done` в базе.** `done`
   у нас означает проверенное расписание (Studio показала SCHEDULED в нужное время) —
   закон проекта, и он же его известная ловушка: интерфейс называл это публикацией.
   Подтверждением считается ТОЛЬКО фаза `published`, которую ставит поздняя проверка
   `pub_verify` по machine-readable ответу YouTube Data API.

2. **Нет подтверждения — не значит ноль.** Подтверждать бывает нечем: нет ключа
   YouTube Data API, кончились единицы суточной квоты, ещё не прошёл срок ожидания
   после слота (`pub_verify.GRACE_SEC`), проверка просто не успела пройти. Во всех
   этих случаях выпуск попадает в `unknown` со своей причиной, а НЕ в «задержано» и не
   в ноль подтверждённых. Иначе сводка каждое утро без ключа рапортовала бы «вышло 0»
   — и по ней приняли бы решение, что конвейер встал.

3. **Задержано — только там, где задержку видно.** Два случая, и оба доказуемы без
   сети: выпуск не дошёл до канала (фаза младше `scheduled` — ролика на канале нет
   вовсе) и проверка уже ответила, что ролика не видно дольше терпения
   (`pub_verify.GIVE_UP_SEC`) либо что он доступен только по ссылке. Уже полученный
   ответ не отменяется ни пропавшим ключом, ни кончившейся квотой: это то, что мы
   знаем, а не то, что собираемся спросить.

4. **Канал без тега не прячется.** Группы берутся из тегов (`pub_tags`), а тег ничего
   не решает — он только ищет и фильтрует. Канал без тегов попадает в группу «прочие»;
   канал с двумя тегами считается в обеих группах, поэтому сумма по группам больше
   числа каналов — это написано прямо в отчёте, чтобы никто не складывал строки.

5. **Запас каналов: ноль и «не знаем» — разные ответы.** У канала без расписания запас
   не ноль, его не от чего считать (`channel_supply`, `known=False`). Такой канал идёт
   отдельным числом «запас неизвестен», а не в «каналов без запаса».

**Что означает «принято».** Ролик принят каналом = выпуск дошёл до фазы `scheduled`,
то есть YouTube Studio подтвердила расписание. Это единственное «принято», которое у
нас доказуемо и не совпадает с «вышел» (см. пункт 1). Момент берётся из журнала фаз
(`pub_release_events`, событие `scheduled`), а не из `pub_releases.updated`: последний
переписывается каждым касанием выпуска, и вчерашняя приёмка попала бы в сегодняшние
сутки при любой мелкой правке.

**Рассылка в телеграм по умолчанию ВЫКЛЮЧЕНА.** Владелец 28.09.2026 просил уменьшить
поток сообщений («очень много раз долбит прилажка»), и заводить за него новую суточную
рассылку нельзя. Включается настройкой на экране «Сводка»; пока она выключена,
`daily_tick` не отправляет ничего и говорит об этом словами.

Сети в этом модуле нет вовсе: он только читает базу и складывает строку. Отправку
делает переданная функция `send(text) -> bool` — та же, что у тревог.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time

try:
    from zoneinfo import ZoneInfo
except ImportError:                      # pragma: no cover — питон старше 3.9 не наш
    ZoneInfo = None                      # type: ignore

import pub_alerts
import pub_calls
import pub_upload_census
import pub_verify
from block_reasons import explain as explain_block
from channel_supply import channel_supply, supply_facts
from pub_engine import PHASE_INDEX

# Часовой пояс отчёта. У каждого канала он свой, а сутки в сводке — одни: отчёт читает
# один человек в одном месте. Умолчание совпадает с умолчанием канала (`pub_schema`),
# чтобы на одномашинной установке «сегодня» в сводке и «сегодня» у каналов совпадало.
DIGEST_TZ = (os.environ.get("VS_DIGEST_TZ") or "Europe/Warsaw").strip()

SETTINGS_KEY = "digest_settings"      # что настроено: рассылка и час отправки
SENT_KEY = "digest_sent"              # за какие сутки сводка уже уходила

# Рассылка выключена, пока её не включили руками. См. шапку модуля.
DEFAULT_SETTINGS = {"telegram": False, "hour": 9}

NO_TAG = "прочие"                     # группа канала, у которого тегов нет

# Фаза, начиная с которой ролик принят каналом: Studio подтвердила расписание.
ACCEPTED_PHASE = "scheduled"
ACCEPTED_INDEX = PHASE_INDEX[ACCEPTED_PHASE]
PUBLISHED_PHASE = "published"

# Строки плана, которые вообще считаются живыми. `cancelled` выпала из плана — её
# выход свободен, и ждать по ней нечего. Тот же перечень, что у запаса материала
# (`channel_supply.HOLDING`) и у уникального индекса слотов: разойтись им нельзя.
LIVE_PLAN = ("planned", "frozen", "running", "done", "error")


def _zone(tz_name: str):
    """Часовой пояс по имени. Неизвестное имя — не повод падать: сводка важнее пояса.

    Отчёт собирается по таймеру и в телеграм; уронить его из-за опечатки в переменной
    окружения значило бы не отправить ничего и не сказать почему. Поэтому неизвестный
    пояс молча становится UTC, а имя пояса печатается в самой сводке — по нему видно,
    что настройка не применилась.
    """
    if ZoneInfo is None:
        return dt.timezone.utc
    try:
        return ZoneInfo(tz_name)
    except Exception:                                              # noqa: BLE001
        return dt.timezone.utc


def day_bounds(now: float, tz_name: str = None) -> tuple:
    """Календарные сутки, в которые попадает `now`: (начало, конец) в epoch UTC.

    Именно календарные, а не «последние 24 часа»: заказчик читает строку «сегодня», и
    скользящее окно ответило бы на другой вопрос — в него попадала бы половина вчера.
    """
    zone = _zone(tz_name or DIGEST_TZ)
    local = dt.datetime.fromtimestamp(float(now), zone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    # Конец суток считаем прибавлением суток к дате, а не заменой времени на 23:59:59:
    # в день перехода на летнее время в сутках 23 или 25 часов, и «конец» обязан быть
    # настоящим началом следующего дня, иначе час выходов потеряется или удвоится.
    nxt = (start + dt.timedelta(days=1, hours=12)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp(), nxt.timestamp()


def _where(tenant_id, column: str = "tenant_id") -> tuple:
    """Условие по арендатору или его отсутствие. Пустой `tenant_id` значит «все».

    Проход тревог на боевой машине ходит общим (`dev/alerts.py`, VS_DEV_TENANT может
    быть не задан), и сводка обязана понимать то же самое. Молча отфильтровать всё по
    `tenant_id=NULL` значило бы каждое утро присылать пустой отчёт — и выглядел бы он
    не как поломка настройки, а как остановившийся конвейер.
    """
    return ((f" AND {column}=?", (tenant_id,)) if tenant_id else ("", ()))


def _tags_by_channel(c, tenant_id: str) -> dict:
    """{id канала: [теги]} одним запросом на всех.

    Запрос на канал (`pub_tags.tag_names`) превратил бы семьдесят каналов в семьдесят
    запросов — та же причина, по которой одним запросом берутся `plan_by_channel` и
    `supply_facts`.
    """
    where, args = _where(tenant_id, "t.tenant_id")
    rows = c.execute(
        "SELECT ct.channel_id, t.name FROM pub_channel_tags ct "
        f"JOIN pub_tags t ON t.id=ct.tag_id WHERE 1=1{where} "
        "ORDER BY t.name COLLATE NOCASE", args).fetchall()
    out: dict = {}
    for row in rows:
        out.setdefault(row[0], []).append(row[1])
    return out


def _groups_of(tags: list) -> list:
    """Группы одного канала. Без тегов — «прочие», и это не скрытие, а отдельная группа.

    Канал с двумя тегами попадает в обе группы намеренно: тег у нас не маршрут, а метка,
    и «сколько в плане у группы ww2» — честный вопрос независимо от того, помечен ли тот
    же канал ещё и как «тест». Цена — сумма по группам больше числа каналов; про это
    сказано в самом отчёте.
    """
    return list(tags) if tags else [NO_TAG]


def _last_public_check(c) -> dict:
    """Последний ответ поздней проверки по каждому выпуску: {release_id: payload}.

    Берём строку с наибольшим `seq`: повторы одного и того же ответа схлопываются
    (`pub_engine.log_phase`), поэтому последняя строка — это и есть последнее, что мы
    про ролик знаем.
    """
    rows = c.execute(
        "SELECT e.release_id, e.payload FROM pub_release_events e "
        "JOIN (SELECT release_id, MAX(seq) seq FROM pub_release_events "
        "      WHERE phase='public_check' GROUP BY release_id) last "
        "  ON last.release_id=e.release_id AND last.seq=e.seq").fetchall()
    out: dict = {}
    for row in rows:
        try:
            out[row[0]] = json.loads(row[1] or "{}") or {}
        except (ValueError, TypeError):
            out[row[0]] = {}
    return out


def _accepted_at(c, since: float, until: float) -> set:
    """Выпуски, принятые каналом в окне: {release_id}.

    Событие `scheduled` в журнале фаз, а не `pub_releases.updated` — см. шапку модуля.
    """
    rows = c.execute(
        "SELECT release_id FROM pub_release_events WHERE phase=? "
        "AND created>=? AND created<?", (ACCEPTED_PHASE, since, until)).fetchall()
    return {r[0] for r in rows}


def _confirm_state(rel, checked: dict, now: float, have_key: bool, quota: dict) -> tuple:
    """Что мы знаем про один ожидавшийся выход: («вид», «причина»).

    Виды: `confirmed` — ролик подтверждённо публичный; `by_hand` — выход закрыт
    человеком, который посмотрел канал (план выполнен, но машинного подтверждения нет);
    `delayed` — задержку видно; `unknown` — судить нечем, и это не ноль (см. пункт 2
    шапки модуля).

    Порядок проверок не произвольный: СНАЧАЛА всё, что мы уже знаем, и только потом —
    почему не знаем остального. Знания тут два рода, и ни одному ключ не нужен: выпуск,
    не дошедший до канала (ролика там нет вовсе), и уже полученный ответ поздней
    проверки — он лежит в журнале фаз и от наших настроек не зависит.

    Обратный порядок — сперва «нет ключа», сперва «кончилась квота» — переписывал бы оба
    факта в «не знаем» ровно в те дни, когда ключ убрали или квота вышла, то есть прятал
    бы настоящую беду за отсутствием второстепенной. С сохранённым ответом это выходило
    хуже всего: «ролик доступен только по ссылке» исчезало вместе с каналом из «Проблем»
    целиком, потому что на таком ответе `pub_verify` ставит `blocked_reason`, но НЕ
    `status='blocked'` — в блокеры канал не попадает. Отсутствие ключа — свойство нашей
    настройки, и ответа YouTube оно не отменяет.
    """
    if rel is None:
        return "delayed", "выпуск не заводился: строка плана осталась без работы"
    phase = rel["phase"] or "created"
    if phase == PUBLISHED_PHASE:
        return "confirmed", ""
    # Подтверждение человеком (решение владельца 01.10.2026, п. 6): план выполнен, ролик
    # на канале есть, и ждать по нему больше нечего. Но это НЕ машинное подтверждение, и
    # смешивать их в одно число нельзя: за первым стоит ответ YouTube Data API, за вторым
    # — человек, который посмотрел канал. Поэтому свой вид, а в тексте — «в том числе
    # руками N». Спрашивается раньше ответов проверки: проверка по такому выпуску могла
    # не проходить вовсе, и без этой ветки он до конца суток числился бы «не знаем».
    if "hand_confirmed" in rel.keys() and rel["hand_confirmed"]:
        return "by_hand", ""
    if PHASE_INDEX.get(phase, 0) < ACCEPTED_INDEX:
        return "delayed", (rel["blocked_reason"]
                           or "ролик не дошёл до канала: расписание не подтверждено")
    # Дальше — только выпуски, принятые каналом, но не подтверждённые как вышедшие.
    slot = float(rel["slot_at"] or now)
    answer = checked.get(rel["release_id"])
    state = str((answer or {}).get("state") or "")
    if state == pub_verify.UNLISTED:
        return "delayed", "ролик доступен только по ссылке, а должен быть публичным"
    if state == pub_verify.NOT_PUBLIC and now >= slot + pub_verify.GIVE_UP_SEC:
        # Терпение кончилось: ключ не видит ролика дольше `GIVE_UP_SEC`. До этого срока
        # тот же ответ — ещё ожидание, а не задержка, и смешивать их нельзя: иначе сводка
        # объявит сорванным каждый выход через полчаса после слота.
        if (answer or {}).get("reason"):
            return "delayed", str(answer["reason"])
        return "delayed", ("YouTube не показывает ролик публично: он либо ещё приватный, "
                           "либо удалён")
    # Всё остальное — «не знаем», и каждому виду незнания своя причина.
    if not have_key:
        return "unknown", ("проверка выхода выключена: нет ключа YouTube Data API, "
                           "и проверка по странице ролика отключена (VS_PUBLIC_PAGE_CHECK=0)")
    if quota:
        return "unknown", pub_calls.quota_why(quota)
    if now < slot + pub_verify.GRACE_SEC:
        return "unknown", "рано спрашивать: проверка выхода ходит не сразу после слота"
    if answer is None:
        return "unknown", "проверка выхода ещё не проходила по этому ролику"
    if state == pub_verify.NOT_PUBLIC:
        return "unknown", "проверка ответила «ролика ещё не видно» — ждём"
    return "unknown", (answer.get("error")
                       or "ответа про этот ролик у проверки выхода нет")


def _fmt_moment(stamp: float, tz_name: str) -> str:
    """Момент словами: «30.09 10:00». Год не пишем — сводка живёт один день."""
    return dt.datetime.fromtimestamp(float(stamp), _zone(tz_name)).strftime("%d.%m %H:%M")


def period_bounds(first: str, last: str, tz_name: str = None) -> tuple:
    """Даты «с» и «по» включительно (ГГГГ-ММ-ДД, в поясе сводки) → (начало, конец) epoch.

    04.10.2026, владелец: «не совсем понятно, за какой период — давай календарь, а так
    пусть показывает текущий месяц». Конец — начало дня ПОСЛЕ «по», тем же приёмом, что в
    `day_bounds`: в день перевода часов сутки не 24 часа.
    """
    zone = _zone(tz_name or DIGEST_TZ)
    try:
        a = dt.datetime.strptime(str(first), "%Y-%m-%d").replace(tzinfo=zone)
        b = dt.datetime.strptime(str(last), "%Y-%m-%d").replace(tzinfo=zone)
    except ValueError:
        raise ValueError("даты периода — в виде ГГГГ-ММ-ДД")
    if b < a:
        raise ValueError("конец периода раньше начала")
    if (b - a).days > 400:
        raise ValueError("период длиннее 400 дней — выберите короче")
    end = (b + dt.timedelta(days=1, hours=12)).replace(hour=0, minute=0, second=0,
                                                        microsecond=0)
    return a.timestamp(), end.timestamp()


def month_bounds(now: float, tz_name: str = None) -> tuple:
    """Текущий календарный месяц в поясе сводки: даты «с» и «по» (ГГГГ-ММ-ДД)."""
    zone = _zone(tz_name or DIGEST_TZ)
    local = dt.datetime.fromtimestamp(float(now), zone)
    first = local.replace(day=1)
    nxt = (first + dt.timedelta(days=32)).replace(day=1)
    return first.strftime("%Y-%m-%d"), (nxt - dt.timedelta(days=1)).strftime("%Y-%m-%d")


def collect(c, tenant_id: str, now=None, tz_name: str = None, have_key=None,
            since: float = None, until: float = None) -> dict:
    """Сводка за сутки или за период. Ничего не пишет и ни во что не ходит — только читает.

    `have_key` подменяется в тестах: наличие ключа YouTube Data API решает, будет ли
    «подтверждено» числом или честным «не знаем», и проверять это надо без настоящего
    ключа на машине. `since`/`until` — период экрана (см. `period_bounds`); без них —
    календарные сутки, как у утреннего отчёта в телеграм.
    """
    now = time.time() if now is None else float(now)
    tz_name = tz_name or DIGEST_TZ
    if since is None or until is None:
        since, until = day_bounds(now, tz_name)
    since, until = float(since), float(until)
    pub_alerts.load_limits(c)          # порог запаса — тот же, что у тревоги §14

    where, args = _where(tenant_id)
    channels = c.execute(
        f"SELECT * FROM pub_channels_live WHERE status!='archived'{where} "
        "ORDER BY name COLLATE NOCASE", args).fetchall()
    by_id = {row["id"]: row for row in channels}
    tags = _tags_by_channel(c, tenant_id)

    if have_key is None:
        have_key = pub_verify.can_check(c)
    quota = pub_calls.quota_wait(c, now=now)

    # ── строки плана: что ещё впереди и что ожидалось сегодня ──
    plan_ahead: dict = {}              # {channel_id: сколько живых строк впереди}
    today_items = []                   # строки плана сегодня, чьё время уже наступило
    ahead_today = 0                    # выходы сегодня, до которых время не дошло
    rows = c.execute(
        "SELECT id, channel_id, slot_at, status FROM pub_plan_items "
        f"WHERE status IN ({','.join('?' * len(LIVE_PLAN))}){where}",
        LIVE_PLAN + args).fetchall()
    for row in rows:
        if row["channel_id"] not in by_id:
            continue                   # канал в архиве или чужой — в сводку не идёт
        slot = row["slot_at"]
        if slot is None or float(slot) > now:
            plan_ahead[row["channel_id"]] = plan_ahead.get(row["channel_id"], 0) + 1
        if slot is None:
            continue
        slot = float(slot)
        if since <= slot < until:
            if slot <= now:
                today_items.append(row)
            else:
                ahead_today += 1

    releases = {r["plan_item_id"]: r for r in c.execute(
        f"SELECT * FROM pub_releases WHERE 1=1{where} ORDER BY id", args)}
    checked = _last_public_check(c)

    # ── сегодня: ожидалось, подтверждено, задержано, не знаем ──
    today = {"expected": len(today_items), "ahead": ahead_today, "confirmed": 0,
             "by_hand": 0, "delayed": 0, "unknown": 0, "why": []}
    delayed_by_channel: dict = {}
    # «Проблемы» — про сегодня, даже когда числа экрана за месяц (04.10.2026): иначе каждый
    # пропущенный с первого числа выход делал канал «проблемным», и список тонул в
    # прошлом — включая канал, чей сегодняшний ролик уже стоит в расписании.
    day_since, day_until = day_bounds(now, tz_name)
    for item in today_items:
        rel = releases.get(item["id"])
        kind, why = _confirm_state(rel, checked, now, have_key, quota)
        today[kind] += 1
        if kind == "unknown" and why and why not in today["why"]:
            today["why"].append(why)
        if kind == "delayed" and day_since <= float(item["slot_at"]) < day_until:
            delayed_by_channel.setdefault(item["channel_id"], why)

    # Чем подтверждать нечего — говорим ВСЕГДА, а не только когда осталось «не знаем».
    # После перестановки порядка (сохранённый ответ выше отсутствия ключа) отсутствие
    # ключа перестало попадать в причины, если каждый сегодняшний выход уже закрыт
    # сохранённым ответом. Сводка при этом выглядела бы полной, хотя подтверждать новые
    # выходы нечем: завтрашние числа молча поехали бы.
    # ...но только если подтверждать ЕСТЬ что. 03.10.2026 сводка новой студии написала
    # «ключа нет — подтверждать нечем» при одиннадцати задержанных выходах, из которых
    # ни один до канала не дошёл: все стояли на рерайте и озвучке. Ключ к этому не имел
    # отношения, а читалось как «не можем проверить». Про ключ говорим, когда сегодня
    # есть хотя бы один выпуск, принятый каналом (`scheduled` и дальше); иначе — правду.
    def _принят(item) -> bool:
        rel = releases.get(item["id"])
        return rel is not None and PHASE_INDEX.get(rel["phase"] or "", 0) >= ACCEPTED_INDEX
    есть_что_подтверждать = any(_принят(item) for item in today_items)
    if not have_key and есть_что_подтверждать:
        gap = ("ключа YouTube Data API нет — новые выходы подтверждать нечем, "
               "показаны только уже полученные ответы")
        if gap not in today["why"]:
            today["why"].append(gap)
    elif not have_key and today_items and today["delayed"] == len(today_items):
        gap = ("ни один выпуск, чьё время уже наступило, до канала не дошёл — "
               "подтверждать нечего, ключ тут ни при чём")
        if gap not in today["why"]:
            today["why"].append(gap)
    elif quota:
        gap = pub_calls.quota_why(quota)
        if gap and gap not in today["why"]:
            today["why"].append(gap)

    # ── блокеры и приёмка за сутки ──
    accepted_ids = _accepted_at(c, since, until)
    accepted_by_channel: dict = {}
    for rel in releases.values():
        if rel["release_id"] in accepted_ids and rel["channel_id"] in by_id:
            accepted_by_channel[rel["channel_id"]] = \
                accepted_by_channel.get(rel["channel_id"], 0) + 1
    blocked_by_channel: dict = {}
    for rel in releases.values():
        if rel["status"] == "blocked" and rel["channel_id"] in by_id:
            blocked_by_channel.setdefault(rel["channel_id"], []).append(rel)

    # ── запас материала ──
    # Факты запаса берём по каждому арендатору отдельно: `supply_facts` фильтрует по
    # нему сам, а общий проход видит каналы нескольких арендаторов — чужие строки плана
    # не должны попасть в чужой запас. Приём тот же, что в `pub_alerts.check_channel_supply`.
    facts: dict = {}
    for tid in {row["tenant_id"] for row in channels}:
        facts.update(supply_facts(c, tid, now))
    supply_low, supply_unknown, supply_by_channel = 0, 0, {}
    for row in channels:
        if (row["status"] or "active") != "active":
            continue                   # канал на паузе не выходит — запас ему не нужен
        sup = channel_supply(row, facts.get(row["id"]), now)
        supply_by_channel[row["id"]] = sup
        if not sup["known"]:
            supply_unknown += 1
        elif sup["days"] < pub_alerts.SUPPLY_MIN_DAYS:
            supply_low += 1

    # ── группы ──
    groups: dict = {}
    for row in channels:
        for name in _groups_of(tags.get(row["id"]) or []):
            g = groups.setdefault(name, {"tag": name, "channels": 0, "plan": 0,
                                         "accepted": 0, "blocked": 0})
            g["channels"] += 1
            g["plan"] += plan_ahead.get(row["id"], 0)
            g["accepted"] += accepted_by_channel.get(row["id"], 0)
            g["blocked"] += len(blocked_by_channel.get(row["id"], []))
    # «Прочие» всегда последними: это не тег, а остаток, и в списке тегов ему не место.
    ordered = sorted((g for g in groups.values() if g["tag"] != NO_TAG),
                     key=lambda g: g["tag"].lower())
    ordered += [g for g in groups.values() if g["tag"] == NO_TAG]

    problems = _problems(c, channels, blocked_by_channel, delayed_by_channel,
                         supply_by_channel, now, tz_name)
    blockers = _blockers(blocked_by_channel, by_id)
    alerts, alerts_why = _alerts(c, now, tenant_id)

    return {
        "day": dt.datetime.fromtimestamp(now, _zone(tz_name)).strftime("%d.%m.%Y"),
        "tz": tz_name, "now": now, "since": since, "until": until,
        "groups": ordered,
        "today": today,
        # «Принято» — сумма по каналам сводки, а не размер множества `accepted_ids`:
        # в журнале фаз лежат события и по каналам в архиве, и (при общем проходе) по
        # чужим арендаторам. Сосчитав множество целиком, отчёт приписывал бы себе
        # чужую работу — и число в нём никогда бы не сошлось с суммой по группам.
        "production": {"accepted": sum(accepted_by_channel.values()),
                       "no_supply": supply_low,
                       "supply_unknown": supply_unknown,
                       "threshold_days": float(pub_alerts.SUPPLY_MIN_DAYS)},
        "problems": problems,
        # Срывы заливки за те же сутки, разобранные на «безопасно повторить» и
        # «повторять нельзя». Решение владельца 01.10.2026, п. 7: автоповтор решается
        # по числам, а числа надо собрать. Сводка — то место, куда владелец смотрит
        # каждый день, поэтому замер живёт здесь, а не в отдельном отчёте, который
        # никто не откроет.
        "uploads": pub_upload_census.since(c, since, until),
        "alerts": alerts, "alerts_why": alerts_why,
        # Блокеры — СЕЙЧАС, а не за период: выпуск в разборе стоит, пока его не разберут,
        # и ответ на «что держит» не зависит от того, за какие дни смотреть числа.
        "blockers": blockers,
        "period": {"from": dt.datetime.fromtimestamp(since, _zone(tz_name)).strftime("%Y-%m-%d"),
                   "to": dt.datetime.fromtimestamp(until - 1, _zone(tz_name)).strftime("%Y-%m-%d")},
    }


# Порядок групп блокеров: сначала наша беда, её чинит студия; потом те, что чинят люди
# за каналами, донорами и обложками; незнакомое — последним.
_BLOCKER_ORDER = {"studio": 0, "channel": 1, "cover": 2, "donor": 3, "": 4}


def _blockers(blocked_by_channel: dict, by_id: dict) -> list:
    """Выпуски в разборе, сложенные по смыслу причины: чья беда, что случилось, сколько.

    04.10.2026, владелец: «на сводке 24 блокера — что за блокеры, давай разбираться, что
    наше, что не наше». Причину понимаем тем же переводчиком, что и экран выпуска
    (`block_reasons.explain`); не узнанную — показываем как есть, без выдумки.
    """
    groups: dict = {}
    for cid, rels in blocked_by_channel.items():
        name = by_id[cid]["name"] if cid in by_id else str(cid)
        for rel in rels:
            raw = (rel["blocked_reason"] or "").strip()
            b = explain_block(raw)
            key = (b.get("kind") or "", b.get("title") or raw[:160] or "без причины")
            g = groups.setdefault(key, {
                "kind": b.get("kind") or "", "tag": b.get("tag") or "Прочее",
                "title": key[1], "fix": b.get("fix") or "", "revive": bool(b.get("revive")),
                "count": 0, "channels": []})
            g["count"] += 1
            if name not in g["channels"]:
                g["channels"].append(name)
    return sorted(groups.values(),
                  key=lambda g: (_BLOCKER_ORDER.get(g["kind"], 4), -g["count"], g["title"]))


def _alerts(c, now: float, tenant_id: str) -> tuple:
    """Действующие тревоги — те же, что уходят в телеграм. Возвращает (список, беда).

    Проход тревог читает половину базы (очередь, проекты, провайдеры, ноды), и на
    установке, куда очередная миграция ещё не доехала, он падает на отсутствующей
    таблице. Уронить из-за этого ВСЮ сводку нельзя: остальные её числа посчитаны и
    нужны. Но и показать пустой список тоже нельзя — «тревог нет» и «тревоги прочитать
    не удалось» читаются одинаково, а значат противоположное. Поэтому беда называется
    словами и печатается в самом отчёте.
    """
    try:
        found = pub_alerts.detect(c, now, tenant_id)
    except Exception as exc:                                       # noqa: BLE001
        return [], (f"список тревог сейчас не читается ({exc}) — это не значит, "
                    f"что тревог нет")
    return [{"key": k, "title": a.title} for k, a in sorted(found.items())], ""


def _next_result(c, channel_id: int, now: float, tz_name: str) -> str:
    """«Следующий результат» для строки проблемы: ближайший обеспеченный выход.

    Обеспеченный — значит за ним уже закреплена живая строка плана. Ближайший выход
    БЕЗ ссылки результатом не является: к нему выйдет пустота, а обещать её в отчёте
    заказчику — ровно тот обман, из-за которого сводку и просили считать машиной.
    """
    row = c.execute(
        "SELECT MIN(slot_at) FROM pub_plan_items WHERE channel_id=? AND slot_at>? "
        f"AND status IN ({','.join('?' * len(LIVE_PLAN))})",
        (channel_id, now) + LIVE_PLAN).fetchone()
    if not row or row[0] is None:
        return "не назначен — материала на ближайший выход нет"
    return _fmt_moment(row[0], tz_name)


def _problems(c, channels, blocked, delayed, supply, now: float, tz_name: str) -> list:
    """Строки «канал → причина → следующий результат», по одной на канал.

    Одна строка на канал, а не на выпуск: у канала с общей бедой (кончился ключ,
    встала нода) в разборе висит сразу пачка выпусков, и пачка одинаковых строк в
    отчёте вытесняет из него все остальные каналы.

    Порядок причин — по цене простоя: сначала то, что уже сломано (разбор), потом
    сегодняшний несостоявшийся выход, потом кончившийся материал. Канал, у которого
    ничего из этого нет, в список не попадает вовсе.
    """
    out = []
    for row in channels:
        cid = row["id"]
        stuck = blocked.get(cid) or []
        if stuck:
            newest = max(stuck, key=lambda r: r["updated"] or 0)
            # Чья беда — словами, как на экране (block_reasons): «проблема в доноре: …
            # нужен другой ролик» вместо «у ролика нет субтитров (VideoUnplayable)».
            why = ((explain_block(newest["blocked_reason"]).get("short"))
                   or newest["blocked_reason"] or "нужен разбор")
            if len(stuck) > 1:
                why += f" (и ещё {len(stuck) - 1} в разборе)"
        elif cid in delayed:
            why = delayed[cid]
        else:
            sup = supply.get(cid)
            if not sup or not sup["known"] or sup["covered"]:
                continue
            why = sup["text"]
        out.append({"channel": row["name"], "channel_id": cid, "why": why,
                    "next": _next_result(c, cid, now, tz_name)})
    return out


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return many
    if n % 10 == 1:
        return one
    if 2 <= n % 10 <= 4:
        return few
    return many


def _confirmed_text(today: dict) -> str:
    """«Подтверждено» словами. Здесь и живёт правило «не знаем ≠ ноль».

    Ноль подтверждённых пишется числом ТОЛЬКО тогда, когда про каждый ожидавшийся
    выход у нас есть ответ. Если хоть про один ответа нет — это сказано отдельно, а
    когда ответа нет ни про один, слова «подтверждено 0» не появляется вовсе.
    """
    known = today["expected"] - today["unknown"]
    # Подтверждённое человеком считается выполнением плана, но называется отдельно:
    # иначе отчёт заказчику обещал бы машинную проверку там, где её не было.
    руками = today.get("by_hand") or 0
    хвост = f" (в том числе руками {руками})" if руками else ""
    if today["unknown"] and known <= 0:
        return f"подтверждено: не знаем{хвост}"
    if today["unknown"]:
        return (f"подтверждено {today['confirmed'] + руками}{хвост}, "
                f"про {today['unknown']} не знаем")
    return f"подтверждено {today['confirmed'] + руками}{хвост}"


def render(data: dict) -> str:
    """Сводка человеческим текстом — то, что копируют в отчёт и шлют в телеграм."""
    lines = [f"Сводка за {data['day']} ({data['tz']})", ""]

    lines.append("Группы каналов:")
    if not data["groups"]:
        lines.append("· каналов нет")
    for g in data["groups"]:
        lines.append(f"· {g['tag']}: в плане {g['plan']}, принято {g['accepted']}, "
                     f"блокеры {g['blocked']}")
    if any(g["tag"] != NO_TAG for g in data["groups"]):
        lines.append("  (канал с несколькими тегами считается в каждой своей группе — "
                     "строки не складываются)")

    today = data["today"]
    lines += ["", f"Сегодня: ожидалось {today['expected']} "
                  f"{_plural(today['expected'], 'выход', 'выхода', 'выходов')}, "
                  f"{_confirmed_text(today)}, задержано {today['delayed']}."]
    if today["ahead"]:
        lines.append(f"Ещё впереди сегодня: {today['ahead']}.")
    for why in today["why"]:
        lines.append(f"  не знаем потому, что {why}")

    prod = data["production"]
    порог = prod["threshold_days"]
    порог_txt = (f"{int(порог)} {_plural(int(порог), 'сутки', 'суток', 'суток')}"
                 if float(порог).is_integer() else f"{порог:g} суток")
    lines += ["", f"Производство: принято новых роликов {prod['accepted']}, "
                  f"каналов без запаса {prod['no_supply']} (порог — {порог_txt})."]
    if prod["supply_unknown"]:
        lines.append(f"  ещё про {prod['supply_unknown']} "
                     f"{_plural(prod['supply_unknown'], 'канал', 'канала', 'каналов')} "
                     f"запас неизвестен: расписание не задано — это не ноль.")

    загрузки = data.get("uploads") or {}
    if загрузки.get("всего"):
        # Ноль в отчёт не пишем: строка «срывов нет» каждое утро приучает её не читать,
        # и в день, когда срывы появятся, её не заметят.
        lines += ["", "Заливка: " + pub_upload_census.text(загрузки) + "."]
        if загрузки.get("silent"):
            lines.append("  «нода молчала» — это НЕ ноль и не безопасный повтор: "
                         "она могла продолжать загрузку молча.")

    lines += ["", "Проблемы:"]
    if not data["problems"]:
        lines.append("· нет")
    for p in data["problems"]:
        lines.append(f"· {p['channel']} → {p['why']} → следующий результат: {p['next']}")

    if data.get("alerts_why"):
        lines += ["", f"Тревоги: {data['alerts_why']}"]
    elif data["alerts"]:
        lines += ["", "Действующие тревоги: "
                      + "; ".join(a["title"] for a in data["alerts"])]
    return "\n".join(lines)


# ───────────────────────── настройка и суточная отправка ─────────────────────────

def settings(c) -> dict:
    """Что настроено. Отсутствие записи означает умолчание, а умолчание — «выключено».

    Читается через ту же `admin_state`, что и пороги тревог: одна база, одна форма
    хранения, никакого своего файла настроек.
    """
    saved = pub_alerts._state_row(c, SETTINGS_KEY)                 # noqa: SLF001
    saved = saved if isinstance(saved, dict) else {}
    hour = saved.get("hour")
    try:
        hour = int(hour)
    except (TypeError, ValueError):
        hour = DEFAULT_SETTINGS["hour"]
    return {"telegram": bool(saved.get("telegram")),
            "hour": min(23, max(0, hour)),
            "tz": DIGEST_TZ}


def set_settings(c, telegram=None, hour=None, now=None) -> dict:
    """Поправить настройку рассылки. Возвращает то, что стало действовать."""
    current = settings(c)
    value = {"telegram": current["telegram"], "hour": current["hour"]}
    if telegram is not None:
        value["telegram"] = bool(telegram)
    if hour is not None:
        try:
            hour = int(hour)
        except (TypeError, ValueError):
            raise ValueError("час отправки — целое число от 0 до 23")
        if not 0 <= hour <= 23:
            raise ValueError("час отправки — целое число от 0 до 23")
        value["hour"] = hour
    pub_alerts._state_save(c, SETTINGS_KEY, value, now)            # noqa: SLF001
    return settings(c)


def daily_tick(c, send, tenant_id: str, now=None, tz_name: str = None) -> dict:
    """Отправить сводку, если пора. Безопасно звать хоть каждую минуту.

    Три условия, и все три обязательны:

    * рассылка ВКЛЮЧЕНА настройкой (по умолчанию нет — см. шапку модуля);
    * местный час дошёл до назначенного;
    * за эти календарные сутки сводка ещё не уходила.

    Неотправленное не помечается отправленным: если телеграм молчит, обязанность
    отправить остаётся на следующий проход — ровно так же, как у тревог. Иначе один
    сбой сети превращал бы суточный отчёт в пропуск, и никто бы об этом не узнал.
    """
    now = time.time() if now is None else float(now)
    tz_name = tz_name or DIGEST_TZ
    conf = settings(c)
    if not conf["telegram"]:
        return {"sent": False, "why": "рассылка сводки выключена настройкой"}
    local = dt.datetime.fromtimestamp(now, _zone(tz_name))
    day = local.strftime("%Y-%m-%d")
    if local.hour < conf["hour"]:
        return {"sent": False, "why": f"ещё рано: отправка в {conf['hour']:02d}:00"}
    saved = pub_alerts._state_row(c, SENT_KEY)                     # noqa: SLF001
    saved = saved if isinstance(saved, dict) else {}
    if saved.get("day") == day:
        return {"sent": False, "why": "за эти сутки сводка уже уходила"}
    text = render(collect(c, tenant_id, now=now, tz_name=tz_name))
    if not send(text):
        return {"sent": False, "why": "телеграм не принял сообщение — повторим позже",
                "text": text}
    pub_alerts._state_save(c, SENT_KEY, {"day": day, "at": now}, now)   # noqa: SLF001
    return {"sent": True, "why": "", "text": text}
