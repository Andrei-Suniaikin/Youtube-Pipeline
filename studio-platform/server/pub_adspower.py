#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Опрос AdsPower одной машины заливки (task-spec.md, profiles-2026-09-23, Фаза 2a/2b).

Модуль состоит из двух независимых частей:

  * `fetch_host_profiles` (Фаза 2a) — только сбор, БЕЗ обращения к SQLite вообще;
  * `merge_host_profiles` (Фаза 2b, ниже) — слияние УЖЕ полученного `AdsPowerFetch` в
    `pub_profiles`, обращения к сети не делает вообще. Разделение намеренное: Фаза 3
    (`web/app.py`) вызывает обе функции подряд, но сеть и БД тестируются раздельно и без
    друг друга.

Что делает `fetch_host_profiles`:

  1) читает `GET {adspower_url}/api/v1/user/list` постранично (`page_size=100`), не
     чаще одного запроса в секунду к ЭТОЙ машине — пауза действует и ПЕРЕД первым
     запросом вызова тоже, если к этой же машине недавно (в том числе в предыдущем
     вызове `fetch_host_profiles`) уже был запрос (см. `_PACE_LIMITER` ниже, Issue 1
     код-ревью итерации 1);
  2) из каждого элемента вычитывает РОВНО поля из белого списка (см. `ProfileRecord`) —
     всё остальное (включая `username`/`password`/прокси/`ip`) не читается вообще, то
     есть не может попасть ни в возвращаемую структуру, ни в текст исключения;
  3) элемент без `serial_number` в `profiles` не попадает (профиль без серийного номера
     не опознать парой «машина + serial», Фаза 2b не сможет его слить) — но для решения
     «полная ли страница» по-прежнему считается СЫРОЕ число элементов на странице, а не
     число прошедших в `profiles` (см. Issue 2 код-ревью итерации 1);
  4) останавливается на первой неполной странице (успех) либо на пределе `MAX_PAGES`
     страниц, если 50-я страница всё ещё полная (неуспех — полнота не гарантирована);
  5) один раз читает `GET {adspower_url}/api/v1/browser/local-active` (та же пауза);
  6) любая сетевая ошибка, `code != 0` в ответе AdsPower или сработавший предохранитель
     останавливают всю опись как неуспешную — вызывающая сторона (Фаза 2b) в базу
     ничего не пишет.

# Assumed: точный envelope `browser/local-active` не подтверждён сэмплом (только
# словесное описание в задаче: «отдаёт user_id и debug_port открытых прямо сейчас»).
# Разбираю по аналогии с `user/list` — тот же стиль ответа AdsPower,
# `{"code":0,"msg":"...","data":{"list":[...]}}`. Если боевая машина отвечает другой
# обёрткой — это деталь парсинга (`_call_local_active`), не решение по scope.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

# ─────────────────────────── настройки описи ───────────────────────────

PAGE_SIZE = 100
MAX_PAGES = 50          # предохранитель: 50 * 100 = 5000 профилей за один успешный проход
MIN_INTERVAL = 1.0      # секунда между запросами к одной и той же машине
REQUEST_TIMEOUT = 30.0

_USER_LIST_PATH = "/api/v1/user/list"
_LOCAL_ACTIVE_PATH = "/api/v1/browser/local-active"


class AdsPowerError(RuntimeError):
    """Опись машины не завершилась: сеть, отказ AdsPower, либо сработал предохранитель.

    Текст сообщения безопасен для лога и экрана — белого списка это тоже касается:
    сюда никогда не попадает содержимое элемента профиля целиком, только код/пояснение
    самого AdsPower или наше короткое объяснение причины.
    """


@dataclass(frozen=True)
class ProfileRecord:
    """Ровно то, что разрешено знать про один профиль AdsPower (белый список §6.3).

    Никаких прочих полей ответа `user/list` (в первую очередь — `username`, `password`,
    `group_id`, `ip`, `ip_country`, `domain_name`, `created_time`, `debug_port`,
    настройки прокси) здесь нет и не может появиться: значения читаются точечно по
    имени ключа в `_profile_from_user_list`, объект целиком не проходится и не хранится.
    """

    serial: str
    adspower_user_id: str
    name: str
    group_name: str
    remark: str
    last_open_time: str


@dataclass(frozen=True)
class AdsPowerFetch:
    """Итог одной ПОЛНОСТЬЮ успешной описи машины — то, чем распоряжается Фаза 2b.

    `active_user_ids` — множество `user_id` профилей, открытых прямо сейчас
    (`browser/local-active`). `pages` — сколько страниц `user/list` реально прочитано
    (сырое число страниц, независимо от того, сколько элементов попало в `profiles`).
    `profiles` НЕ включает элементы `user/list`, у которых пуст `serial_number` — такой
    профиль невозможно опознать парой «машина + serial» (см. §6.3, §4 п.2 task-spec.md),
    поэтому Фаза 2b и не должна пытаться с ним ничего сливать. Такие элементы при этом
    по-прежнему учитывались при определении полноты страницы (см. `fetch_host_profiles`).
    """

    profiles: tuple[ProfileRecord, ...]
    active_user_ids: frozenset[str]
    pages: int


class _HostPaceLimiter:
    """Не чаще одного запроса в секунду к одной и той же машине AdsPower.

    Состояние — время НАЧАЛА последнего запроса к каждой машине — хранится на уровне
    МОДУЛЯ (единственный экземпляр `_PACE_LIMITER` ниже), а не создаётся заново внутри
    одного вызова `fetch_host_profiles`. Так и было раньше (`_Pacer` создавался в каждом
    вызове) — из-за этого первый запрос повторного вызова описи той же машины вообще не
    ждал (код-ревью Фазы 2a, итерация 1, Issue 1: два вызова подряд на одну машину дали
    интервал `[1.007, 0.0, 1.005]` между запросами — второй `user/list` ушёл через 0.0с
    после `local-active` первого вызова).

    Ключ — нормализованный `adspower_url` (обрезаны пробелы и завершающий `/`), поэтому
    опись РАЗНЫХ машин друг друга не задерживает, а повторная опись ОДНОЙ и той же — ждёт,
    даже если это отдельный, более поздний вызов `fetch_host_profiles`. Доступ к общему
    словарю защищён `threading.Lock`: административные ручки `web/app.py` могут дёрнуть
    опись из разных потоков одновременно.

    `sleep`/`clock` по-прежнему передаются в `wait(...)` вызывающей стороной — подменяются
    в тестах, чтобы не спать по-настоящему и не зависеть от реального времени.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_started: dict[str, float] = {}

    @staticmethod
    def _key(adspower_url: str) -> str:
        return adspower_url.strip().rstrip("/")

    def wait(self, adspower_url: str, min_interval: float,
              sleep: Callable[[float], None], clock: Callable[[], float]) -> None:
        key = self._key(adspower_url)
        while True:
            with self._lock:
                last = self._last_started.get(key)
                now = clock()
                if last is None or (now - last) >= min_interval:
                    self._last_started[key] = now
                    return
                remaining = min_interval - (now - last)
            # Спим ВНЕ лока — иначе опись одной машины блокировала бы регистрацию
            # запросов к другой машине на время сна, а это не требуется и не нужно.
            sleep(remaining)

    def reset_for_tests(self) -> None:
        with self._lock:
            self._last_started.clear()


_PACE_LIMITER = _HostPaceLimiter()


def _reset_pace_state_for_tests() -> None:
    """Только для тестов: обнулить общее на процесс состояние пейсинга между машинами.

    Состояние теперь намеренно живёт на уровне модуля дольше одного вызова
    `fetch_host_profiles` (см. `_HostPaceLimiter`) — поэтому тесты, использующие один и
    тот же `adspower_url` с новым `FakeClock`, начинающимся заново от нуля, обязаны
    вызвать это перед своим первым `fetch_host_profiles`, иначе увидят «протёкшее» время
    из предыдущего теста и получат лишнее или неверное ожидание.
    """
    _PACE_LIMITER.reset_for_tests()


def _text(value: Any, limit: int = 300) -> str:
    return str(value or "").strip()[:limit]


def _profile_from_user_list(item: dict) -> ProfileRecord:
    """Вычитать РОВНО белый список полей одного элемента `user/list`.

    Ключи читаются по одному явным `.get(...)` — не проходом по `item.items()` и не
    сериализацией объекта целиком, иначе секретные поля попали бы сюда незаметно при
    следующей правке.
    """
    return ProfileRecord(
        serial=_text(item.get("serial_number"), 64),
        adspower_user_id=_text(item.get("user_id"), 128),
        name=_text(item.get("name"), 200),
        group_name=_text(item.get("group_name"), 200),
        remark=_text(item.get("remark"), 500),
        last_open_time=_text(item.get("last_open_time"), 64),
    )


def _envelope_list(payload: Any, *, what: str) -> list:
    """Разобрать общий конверт ответа AdsPower `{code, msg, data:{list:[...]}}`.

    Общая часть для `user/list` и `local-active` — оба в проде отвечают в этом же
    стиле (см. `upload/upload_worker.py:_resolve_user_id`, которым проверен формат
    `user/list`).
    """
    if not isinstance(payload, dict):
        raise AdsPowerError(f"AdsPower ({what}): неожиданный формат ответа")
    code = payload.get("code")
    if code != 0:
        msg = _text(payload.get("msg")) or "неизвестная ошибка"
        raise AdsPowerError(f"AdsPower ({what}): {msg}")
    data = payload.get("data")
    items = (data or {}).get("list") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise AdsPowerError(f"AdsPower ({what}): неожиданный формат ответа")
    return items


def _get_json(adspower_url: str, path: str, *, params: dict | None,
               timeout: float, what: str) -> Any:
    url = adspower_url.rstrip("/") + path
    try:
        resp = requests.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise AdsPowerError(f"AdsPower ({what}) не отвечает: {type(exc).__name__}") from exc
    try:
        return resp.json()
    except ValueError as exc:
        raise AdsPowerError(f"AdsPower ({what}) ответил не JSON-ом") from exc


def _call_user_list_page(adspower_url: str, page: int, *, timeout: float) -> list:
    payload = _get_json(adspower_url, _USER_LIST_PATH,
                        params={"page": page, "page_size": PAGE_SIZE},
                        timeout=timeout, what="user/list")
    return _envelope_list(payload, what="user/list")


def _call_local_active(adspower_url: str, *, timeout: float) -> frozenset[str]:
    payload = _get_json(adspower_url, _LOCAL_ACTIVE_PATH, params=None,
                        timeout=timeout, what="local-active")
    items = _envelope_list(payload, what="local-active")
    active = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        uid = _text(item.get("user_id"), 128)
        if uid:
            active.add(uid)
    return frozenset(active)


def fetch_host_profiles(adspower_url: str, *, timeout: float = REQUEST_TIMEOUT,
                         min_interval: float = MIN_INTERVAL,
                         sleep: Callable[[float], None] = time.sleep,
                         clock: Callable[[], float] = time.monotonic) -> AdsPowerFetch:
    """Полная опись одной машины: все страницы `user/list` + один `local-active`.

    Чистая функция относительно базы данных: ничего не читает и не пишет в SQLite,
    только ходит в сеть и возвращает найденное. Бросает `AdsPowerError`, если опись
    не может считаться полной и достоверной — вызывающая сторона (Фаза 2b) в этом
    случае обязана не трогать `pub_profiles` ни строкой (см. §7.1, §7.2 task-spec.md).
    """
    url = (adspower_url or "").strip()
    if not url:
        raise AdsPowerError("у машины не задан адрес AdsPower")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise AdsPowerError("адрес AdsPower должен начинаться с http:// или https://")

    profiles: list[ProfileRecord] = []
    page = 1
    pages_read = 0
    while True:
        _PACE_LIMITER.wait(url, min_interval, sleep, clock)
        items = _call_user_list_page(url, page, timeout=timeout)
        pages_read += 1
        for item in items:
            if not isinstance(item, dict):
                continue
            record = _profile_from_user_list(item)
            if not record.serial:
                # Без serial_number профиль не опознать парой (машина, serial) — в
                # `profiles` не включаем (см. docstring `AdsPowerFetch`), но страница
                # по-прежнему учтена в `len(items)` ниже при решении о полноте.
                continue
            profiles.append(record)
        if len(items) < PAGE_SIZE:
            break                                   # неполная страница — пагинация окончена
        if page >= MAX_PAGES:
            raise AdsPowerError(
                f"остановлено на пределе {MAX_PAGES} страниц — опись не завершена, "
                "справочник не тронут")
        page += 1

    _PACE_LIMITER.wait(url, min_interval, sleep, clock)
    active_user_ids = _call_local_active(url, timeout=timeout)

    return AdsPowerFetch(profiles=tuple(profiles), active_user_ids=active_user_ids,
                          pages=pages_read)


# ═══════════════════════ Фаза 2b — слияние в справочник ═══════════════════════
#
# Всё ниже не обращается к сети вообще — только к `sqlite3.Connection`, переданному
# вызывающей стороной (Фаза 3, `web/app.py`). Вход — `AdsPowerFetch`, УЖЕ полностью
# и успешно собранный `fetch_host_profiles` (то есть эта функция никогда не видит
# результат прерванной/неполной описи — `fetch_host_profiles` в этом случае сама
# бросает `AdsPowerError`, до слияния дело не доходит; см. §4 п.4, §7.1 шаг 8
# task-spec.md — «пропал с машины» допустимо утверждать только после ПОЛНОЙ описи).


@dataclass(frozen=True)
class MergeSummary:
    """Итог слияния одной успешной описи в `pub_profiles` — тело ответа
    `POST /ui/hosts/{id}/refresh-profiles` (§6.4 task-spec.md), кроме `ok`/`host_id`
    (их добавляет сама HTTP-ручка Фазы 3 — `host_id` она и так знает из маршрута).
    """

    host_id: int
    pages: int
    found: int
    new: int
    reappeared: int
    missing_now: int
    active_now: int
    at: float


def merge_host_profiles(c: sqlite3.Connection, tenant_id: str, host_id: int,
                         fetch: AdsPowerFetch, *,
                         now: Callable[[], float] = time.time) -> MergeSummary:
    """Слить ОДНУ успешно завершённую опись машины `host_id` в `pub_profiles`.

    Реализует §7.1 шаг 8 task-spec.md целиком, одной транзакцией:

      1. Для каждого `ProfileRecord` из `fetch.profiles` — UPSERT по ключу
         `(tenant_id, default_host_id=host_id, serial)`:
           - строки не было → создать (`title` = `name` из AdsPower, если непусто,
             иначе `NULL`; `missing_since=NULL`, `active_now=0` — активность
             проставляется отдельным проходом ниже, шаг 3);
           - строка была → обновить `adspower_user_id`/`group_name`/`remark`/
             `last_open_time`/`last_seen`, снять `missing_since` (→`NULL`), и
             тронуть `title` ТОЛЬКО если старое значение было пустым (не перетирать
             заметку, вписанную руками, — §6.1 task-spec.md).
      2. Строкам ЭТОЙ ЖЕ машины, которые до этого прохода не считались пропавшими
         (`missing_since IS NULL`), но не встретились в `fetch.profiles` — поставить
         `missing_since = now()` (в первый раз, когда они не найдены; уже пропавшие —
         не трогать повторно, их `missing_since` остаётся временем первого пропажи).
      3. Пересчитать `active_now` заново для ВСЕХ профилей этой машины по
         `fetch.active_user_ids` — 1, если `adspower_user_id` профиля в этом множестве,
         иначе 0 (в том числе для только что заведённых и для тех, что не были
         активны раньше, но открыты сейчас).

    Существующие строки не удаляются и не перенумеровываются — только INSERT новых и
    UPDATE существующих по `id` (Правило 3 оркестратора). Профиль с одинаковым `serial`
    на ДРУГОЙ машине — это другая строка (другой `default_host_id`), эта функция её не
    видит и не трогает: запросы всегда фильтруют по `(tenant_id, default_host_id=host_id)`.

    Транзакция: если `c` уже была в транзакции (`c.in_transaction`), слияние продолжает
    её и коммит/rollback оставляет вызывающей стороне (тот же приём, что и в
    `_migrate_profiles_host_serial`, `server/pub_schema.py`) — иначе открывает и
    закрывает свою. При любом исключении внутри — откат, в базе не остаётся частично
    слитых изменений этого вызова.

    ВАЖНО: эта функция сама не решает, «достаточно ли полна» опись — это уже решено
    (успешно) тем, что `fetch` вообще существует и передан сюда: `fetch_host_profiles`
    для неуспешной/неполной описи бросает исключение и не возвращает `AdsPowerFetch`
    вовсе. Вызывающая сторона (Фаза 3) обязана звать `merge_host_profiles` только из
    `try`-ветки успешного `fetch_host_profiles`, никогда — из `except`.
    """
    at = now()
    found = len(fetch.profiles)
    new = 0
    reappeared = 0

    in_tx = c.in_transaction
    if not in_tx:
        c.execute("BEGIN")
    try:
        seen_serials: set[str] = set()
        for p in fetch.profiles:
            seen_serials.add(p.serial)
            row = c.execute(
                "SELECT id, title, missing_since FROM pub_profiles "
                "WHERE tenant_id=? AND default_host_id=? AND serial=?",
                (tenant_id, host_id, p.serial)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO pub_profiles(tenant_id, serial, title, default_host_id, "
                    "status, created, adspower_user_id, group_name, remark, "
                    "last_open_time, active_now, last_seen, missing_since) "
                    "VALUES(?,?,?,?,'active',?,?,?,?,?,0,?,NULL)",
                    (tenant_id, p.serial, p.name or None, host_id, at,
                     p.adspower_user_id or None, p.group_name or None,
                     p.remark or None, p.last_open_time or None, at))
                new += 1
                continue
            pid, old_title, old_missing = row
            if old_missing is not None:
                reappeared += 1
            keep_title = old_title if (old_title and old_title.strip()) else (p.name or None)
            c.execute(
                "UPDATE pub_profiles SET adspower_user_id=?, group_name=?, remark=?, "
                "last_open_time=?, last_seen=?, missing_since=NULL, title=? "
                "WHERE id=?",
                (p.adspower_user_id or None, p.group_name or None, p.remark or None,
                 p.last_open_time or None, at, keep_title, pid))

        missing_now = 0
        still_not_missing = c.execute(
            "SELECT id, serial FROM pub_profiles "
            "WHERE tenant_id=? AND default_host_id=? AND missing_since IS NULL",
            (tenant_id, host_id)).fetchall()
        for pid, serial in still_not_missing:
            if serial not in seen_serials:
                c.execute("UPDATE pub_profiles SET missing_since=? WHERE id=?", (at, pid))
                missing_now += 1

        # Занятость ложится в справочник ровно тем же кодом, что и у ворот выдачи
        # (`save_active` ниже): одно знание — одно место записи, иначе два пути со
        # временем разойдутся и экран начнёт спорить сам с собой.
        active_now_count = save_active(
            c, host_id, HostActivity(True, fetch.active_user_ids, "", at), tenant_id)

        if not in_tx:
            c.commit()
    except Exception:
        if not in_tx:
            c.rollback()
        raise

    return MergeSummary(host_id=host_id, pages=fetch.pages, found=found, new=new,
                         reappeared=reappeared, missing_now=missing_now,
                         active_now=active_now_count, at=at)


# ══════ Ворота выдачи заливки: кто открыт прямо сейчас (решение владельца 29.09.2026) ══════
#
# Требование ТЗ (день 1, п.3): «исключительное владение профилем всеми заливщиками И
# операторским входом». Распределитель заданий (`server/pub_upload_api.py`) до этого
# считал занятыми только те профили, которые занял он сам, и не видел ни старый конвейер
# gen, ни конвейер манхвы, ни человека, открывшего профиль руками. Два браузера в одном
# профиле рвут куки и ломают идущую заливку — а заливщик, увидев уже запущенный профиль,
# ещё и «присоединяется без рестарта» (`upload/upload_worker.py`, ветка `browser/active`),
# то есть садится прямо в чужое окно.
#
# Отвечает на вопрос `browser/local-active` тот же модуль, что и опись, — но ответ живёт
# секунды, а спрашивают его на КАЖДЫЙ опрос ноды (несколько раз в минуту, на каждое
# рабочее место). Поэтому здесь свой кэш поверх общего ограничителя частоты.

# Свежий ответ годен десять секунд: дольше держать нельзя (за это время человек успеет
# открыть профиль руками), чаще спрашивать незачем — нода опрашивает кабинет чаще.
ACTIVE_TTL_OK = 10.0
# Неудачу помним ДОЛЬШЕ удачи, и это не опечатка. Пока AdsPower не отвечает, каждая
# попытка стоит целого таймаута, а ответа всё равно нет; повторять её на каждой выдаче
# значит добавлять пять секунд к каждому заданию ради того же самого «не знаю».
ACTIVE_TTL_BAD = 60.0
# Ждём ответа недолго: это ворота выдачи, а не опись. Молчание AdsPower заливку не
# останавливает (см. `HostActivity.known`), поэтому долгое ожидание не купило бы ничего.
ACTIVE_TIMEOUT = 5.0
# Сколько времени записанная занятость профиля считается свежей на экране «Админ».
# Старше — это уже «не знаем», а не «свободен»: профиль мог открыть кто угодно.
ACTIVE_FRESH_SEC = 15 * 60


@dataclass(frozen=True)
class HostActivity:
    """Что известно про занятость профилей ОДНОЙ машины прямо сейчас.

    `known=False` — это «не знаем», и оно НЕ равно «свободно». Перепутав эти два
    состояния, мы одним обрывом сети остановили бы заливку на всех каналах разом
    (если считать молчание занятостью) либо начали бы лезть в чужие открытые профили
    (если считать молчание свободой). Поэтому решение о том, что делать с незнанием,
    принимает вызывающая сторона, а не этот модуль: здесь только честный ответ.

    `why` — человеческими словами, почему не знаем; пусто, когда знаем.
    `at` — время ответа по стенным часам (`time.time`), чтобы его можно было записать
    в базу и показать на экране; для сроков кэша берётся монотонный счётчик отдельно.
    """

    known: bool
    user_ids: frozenset[str]
    why: str
    at: float


_ACTIVE_LOCK = threading.Lock()
_ACTIVE_CACHE: dict[str, tuple[HostActivity, float]] = {}


def _reset_active_cache_for_tests() -> None:
    """Только для тестов: забыть кэшированные ответы `local-active`."""
    with _ACTIVE_LOCK:
        _ACTIVE_CACHE.clear()


def active_profiles(adspower_url: str, *, timeout: float = ACTIVE_TIMEOUT,
                     ttl_ok: float = ACTIVE_TTL_OK, ttl_bad: float = ACTIVE_TTL_BAD,
                     min_interval: float = MIN_INTERVAL,
                     sleep: Callable[[float], None] = time.sleep,
                     clock: Callable[[], float] = time.monotonic) -> HostActivity:
    """Кто открыт на этой машине прямо сейчас. Не бросает исключений НИКОГДА.

    Отказ AdsPower — это не ошибка вызывающей стороны и не повод останавливать выдачу
    заданий, поэтому он возвращается значением (`known=False`), а не исключением: с
    исключением любой вызывающий рано или поздно завернёт вызов в `except` и молча
    решит, что профиль свободен.

    Сеть трогается не чаще, чем раз в `ttl_ok` секунд на машину (и не чаще раза в
    секунду — этим занимается общий `_PACE_LIMITER`, тот же, что у описи: предел
    AdsPower общий на все наши запросы к ней).
    """
    url = (adspower_url or "").strip().rstrip("/")
    if not url:
        # Машину завели, адрес не вписали — спрашивать некого. Это именно «не знаем»:
        # профиль вполне может быть открыт, просто нам нечем это увидеть.
        return HostActivity(False, frozenset(), "у машины не задан адрес AdsPower", 0.0)
    with _ACTIVE_LOCK:
        hit = _ACTIVE_CACHE.get(url)
        if hit is not None:
            got, at = hit
            if clock() - at < (ttl_ok if got.known else ttl_bad):
                return got
    # Сеть — ВНЕ лока: иначе один не отвечающий AdsPower задерживал бы вопросы про все
    # остальные машины на весь свой таймаут. Два потока могут спросить одну машину
    # одновременно — это не беда, ограничитель частоты разведёт их по секундам.
    _PACE_LIMITER.wait(url, min_interval, sleep, clock)
    try:
        got = HostActivity(True, _call_local_active(url, timeout=timeout), "", time.time())
    except AdsPowerError as exc:
        got = HostActivity(False, frozenset(), str(exc), time.time())
    with _ACTIVE_LOCK:
        _ACTIVE_CACHE[url] = (got, clock())
    return got


def save_active(c: sqlite3.Connection, host_id: int, activity: HostActivity,
                 tenant_id: str | None = None) -> int:
    """Записать занятость профилей машины в справочник и вернуть, сколько занято.

    Пишется только ДОСТОВЕРНОЕ знание: при `known=False` не трогается ни одна строка —
    иначе молчание AdsPower обнуляло бы `active_now` и экран показывал бы «свободен»
    там, где на самом деле «не знаем».

    `active_at` — когда это знание получено. Без него экран не может отличить «сейчас
    свободен» от «был свободен утром»: `active_now=0` выглядит одинаково в обоих
    случаях, а решения по ним разные.
    """
    if not activity.known:
        return 0
    where, args = ("", ())
    if tenant_id is not None:
        where, args = (" AND tenant_id=?", (tenant_id,))
    c.execute(f"UPDATE pub_profiles SET active_now=0, active_at=? "
              f"WHERE default_host_id=?{where}", (activity.at, host_id) + args)
    ids = [uid for uid in activity.user_ids if uid]
    if ids:
        holes = ",".join("?" * len(ids))
        c.execute(f"UPDATE pub_profiles SET active_now=1, active_at=? "
                  f"WHERE default_host_id=?{where} AND adspower_user_id IN ({holes})",
                  (activity.at, host_id) + args + tuple(ids))
    return c.execute(f"SELECT COUNT(*) FROM pub_profiles WHERE default_host_id=?{where} "
                     f"AND active_now=1", (host_id,) + args).fetchone()[0]
