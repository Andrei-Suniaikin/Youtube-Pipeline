#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Опись профилей AdsPower, Фаза 2a (task-spec.md, profiles-2026-09-23): сбор без базы.

Проверяется здесь только `server/pub_adspower.py::fetch_host_profiles` — чистая функция
относительно SQLite (слияние в `pub_profiles` делает Фаза 2b отдельными функциями того
же модуля):

  1) успешная опись в одну и в несколько страниц `user/list`, `browser/local-active`
     превращается в множество `active_user_ids`;
  2) пейсинг — не чаще одного запроса в секунду к одной машине (проверено подменой
     `time.sleep`/таймера, без настоящего ожидания), причём состояние пейсинга
     переживает отдельный ВЫЗОВ `fetch_host_profiles` (Issue 1, код-ревью итерации 1) —
     второй вызов описи той же машины ждёт перед своим первым же запросом, а опись
     ДРУГОЙ машины при этом не задерживается;
  3) предохранитель: 50-я полная страница `user/list` — неуспех, опись не считается
     завершённой, 51-й запрос не уходит вообще;
  4) сетевая ошибка и `code != 0` в ответе AdsPower — тоже неуспех (`AdsPowerError`);
  5) мутационная проверка белого списка: `username`/`password`/`group_id`/`ip`/объект
     прокси, подмешанные в фейковый ответ, не попадают ни в возвращаемую структуру,
     ни в текст исключения, которое бросает модуль;
  6) элемент `user/list` без `serial_number` не попадает в результат, но всё равно
     учитывается при решении, полна ли страница (Issue 2, код-ревью итерации 1).

Пейсинг теперь общий на модуль (переживает вызов) — каждый тест, дёргающий
`fetch_host_profiles`, обязан начинаться с `A._reset_pace_state_for_tests()`, иначе
«протёкшее» состояние из предыдущего теста (другой `FakeClock`, снова начинающийся с
нуля) даст лишнее или неверное ожидание.

AdsPower не поднимается — транспорт (`requests.get`) подменён целиком, реальная сеть
не трогается:

    VS_TEST_NO_NET=1 .venv/bin/python tests/test_pub_adspower_fetch.py
"""
from __future__ import annotations

import dataclasses
import os
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_adspower as A                                          # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def check_true(label, cond):
    check(label, bool(cond), True)


# ─────────────────────────── фейковый AdsPower ───────────────────────────

class FakeResponse:
    """Ответ AdsPower: ровно то, что читает модуль — `.json()`."""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeAdsPower:
    """Транспорт под запись: страницы `user/list`, ответ `local-active`, счётчик вызовов."""

    def __init__(self):
        self.calls = []                # (путь, params)
        self.user_list_pages = {}      # page -> list[dict]
        self.user_list_code = 0
        self.user_list_msg = ""
        self.local_active_items = []
        self.local_active_code = 0
        self.local_active_msg = ""
        self.fail_paths = set()        # какие пути «рвут сеть» (RequestException)
        self.bad_json_paths = set()    # какие пути отвечают не-JSON-ом

    def get(self, url, params=None, timeout=None):
        params = dict(params or {})
        if url.endswith(A._USER_LIST_PATH):
            path = "user/list"
        elif url.endswith(A._LOCAL_ACTIVE_PATH):
            path = "local-active"
        else:
            raise AssertionError(f"неожиданный url: {url}")
        self.calls.append((path, params))
        if path in self.fail_paths:
            raise requests.exceptions.ConnectionError("сеть не отвечает (фейк)")
        if path in self.bad_json_paths:
            return _NotJson()
        if path == "user/list":
            items = self.user_list_pages.get(params.get("page"), [])
            return FakeResponse({"code": self.user_list_code, "msg": self.user_list_msg,
                                  "data": {"list": items}})
        return FakeResponse({"code": self.local_active_code, "msg": self.local_active_msg,
                              "data": {"list": self.local_active_items}})


class _NotJson:
    def json(self):
        raise ValueError("не json")


class FakeClock:
    """Время не течёт само — только когда модуль явно засыпает."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def with_transport(fake, fn):
    saved = A.requests.get
    A.requests.get = fake.get
    try:
        return fn()
    finally:
        A.requests.get = saved


def make_item(serial, user_id, name="", group_name="", remark="", last_open_time="",
              **extra):
    item = {"serial_number": serial, "user_id": user_id, "name": name,
            "group_name": group_name, "remark": remark, "last_open_time": last_open_time}
    item.update(extra)
    return item


URL = "http://127.0.0.1:50325"


# ─────────────────────────── успешная опись ───────────────────────────

def test_single_page_success():
    print("\nОдна неполная страница — опись успешна, поля из белого списка на месте")
    fake = FakeAdsPower()
    fake.user_list_pages = {1: [
        make_item("46", "uid-46", name="Профиль 46", group_name="history-pl",
                  remark="осн.", last_open_time="2026-09-22 10:11:03"),
        make_item("47", "uid-47", name="Профиль 47"),
    ]}
    fake.local_active_items = [{"user_id": "uid-46", "debug_port": "9222"}]
    clock = FakeClock()
    A._reset_pace_state_for_tests()

    fetch = with_transport(fake, lambda: A.fetch_host_profiles(
        URL, sleep=clock.sleep, clock=clock.now))

    check("одна страница прочитана", fetch.pages, 1)
    check("оба профиля собраны", len(fetch.profiles), 2)
    p46 = next(p for p in fetch.profiles if p.serial == "46")
    check("serial", p46.serial, "46")
    check("adspower_user_id", p46.adspower_user_id, "uid-46")
    check("name", p46.name, "Профиль 46")
    check("group_name", p46.group_name, "history-pl")
    check("remark", p46.remark, "осн.")
    check("last_open_time", p46.last_open_time, "2026-09-22 10:11:03")
    check("46 сейчас открыт", "uid-46" in fetch.active_user_ids, True)
    check("47 сейчас не открыт", "uid-47" in fetch.active_user_ids, False)
    check("вызовы: одна страница + local-active", [c[0] for c in fake.calls],
          ["user/list", "local-active"])


def test_pagination_multiple_pages():
    print("\nНесколько полных страниц + неполная последняя — все профили собраны")
    fake = FakeAdsPower()
    fake.user_list_pages = {
        1: [make_item(str(i), f"uid-{i}") for i in range(100)],
        2: [make_item(str(100 + i), f"uid-{100 + i}") for i in range(100)],
        3: [make_item(str(200 + i), f"uid-{200 + i}") for i in range(37)],
    }
    clock = FakeClock()
    A._reset_pace_state_for_tests()

    fetch = with_transport(fake, lambda: A.fetch_host_profiles(
        URL, sleep=clock.sleep, clock=clock.now))

    check("три страницы прочитаны", fetch.pages, 3)
    check("все 237 профилей собраны", len(fetch.profiles), 237)
    check("порядок страниц", [c[1].get("page") for c in fake.calls if c[0] == "user/list"],
          [1, 2, 3])
    check("после трёх страниц — ровно один local-active",
          sum(1 for c in fake.calls if c[0] == "local-active"), 1)


# ─────────────────────────── пейсинг ───────────────────────────

def test_pacing_one_request_per_second():
    print("\nПейсинг: не чаще 1 запроса в секунду, включая переход к local-active")
    fake = FakeAdsPower()
    fake.user_list_pages = {
        1: [make_item(str(i), f"uid-{i}") for i in range(100)],
        2: [make_item(str(100 + i), f"uid-{100 + i}") for i in range(5)],
    }
    clock = FakeClock()
    A._reset_pace_state_for_tests()

    fetch = with_transport(fake, lambda: A.fetch_host_profiles(
        URL, sleep=clock.sleep, clock=clock.now))

    total_requests = len([c for c in fake.calls])          # 2 страницы + local-active = 3
    check("всего запросов к машине", total_requests, 3)
    check("первый запрос — без ожидания, дальше — ровно по 1с",
          clock.sleeps, [1.0] * (total_requests - 1))
    check("итоговое смещение таймера — (запросов-1) секунд", clock.t, total_requests - 1)
    check("pages", fetch.pages, 2)


def test_pacing_persists_between_calls_for_same_host():
    print("\nПейсинг переживает вызов: второй fetch этой же машины ждёт перед первым же "
          "запросом (Issue 1, код-ревью итерации 1)")
    fake = FakeAdsPower()
    fake.user_list_pages = {1: [make_item("1", "uid-1")]}
    clock = FakeClock()
    A._reset_pace_state_for_tests()

    with_transport(fake, lambda: A.fetch_host_profiles(
        URL, sleep=clock.sleep, clock=clock.now))
    sleeps_after_first_call = len(clock.sleeps)

    fake.calls = []
    with_transport(fake, lambda: A.fetch_host_profiles(
        URL, sleep=clock.sleep, clock=clock.now))
    sleeps_in_second_call = len(clock.sleeps) - sleeps_after_first_call

    check("второй вызов тоже сделал запросы", len(fake.calls) > 0, True)
    # С фиксом: второй вызов ждёт ПЕРЕД первым запросом (страница 1) И перед local-active
    # — итого две паузы. Без фикса (состояние пейсинга снова стало локальным для вызова)
    # первый запрос второго вызова не ждёт вообще — пауза была бы только одна
    # (перед local-active), и эта проверка ловит именно такой откат.
    check("во втором вызове две паузы (перед первым запросом и перед local-active), "
          "а не одна", sleeps_in_second_call, 2)
    check("между началом последнего запроса первого вызова и первым запросом второго "
          "прошла ровно секунда, а не 0", clock.sleeps[sleeps_after_first_call], 1.0)


def test_pacing_does_not_couple_different_hosts():
    print("\nПейсинг разных машин не связан: опись второй машины не ждёт из-за первой")
    fake = FakeAdsPower()
    fake.user_list_pages = {1: [make_item("1", "uid-1")]}
    clock = FakeClock()
    A._reset_pace_state_for_tests()

    with_transport(fake, lambda: A.fetch_host_profiles(
        URL, sleep=clock.sleep, clock=clock.now))

    other_url = "http://127.0.0.1:50999"
    fake.calls = []
    with_transport(fake, lambda: A.fetch_host_profiles(
        other_url, sleep=clock.sleep, clock=clock.now))

    # Каждый вызов даёт ровно одну паузу — перед local-active (первый запрос user/list
    # каждой машины проходит без ожидания, потому что для НЕЁ это первый запрос вообще).
    # Если бы разные машины делили одно и то же время последнего запроса, второй вызов
    # получил бы лишнюю паузу перед своим первым запросом — итого три паузы, а не две.
    check("суммарно две паузы на обе машины, а не три", len(clock.sleeps), 2)


# ─────────────────────────── предохранитель ───────────────────────────

def test_pagination_safety_limit_full_50th_page():
    print("\n50-я страница ещё полная — опись НЕуспешна, 51-й запрос не уходит")
    fake = FakeAdsPower()
    fake.user_list_pages = {p: [make_item(str(p * 1000 + i), f"uid-{p}-{i}")
                                 for i in range(100)]
                            for p in range(1, 51)}
    clock = FakeClock()
    A._reset_pace_state_for_tests()

    raised = None
    try:
        with_transport(fake, lambda: A.fetch_host_profiles(
            URL, sleep=clock.sleep, clock=clock.now))
    except A.AdsPowerError as exc:
        raised = exc

    check_true("AdsPowerError брошен", raised is not None)
    check_true("текст объясняет предел страниц",
               raised is not None and "50 страниц" in str(raised))
    check_true("текст говорит, что справочник не тронут",
               raised is not None and "не тронут" in str(raised))
    check("прочитано ровно 50 страниц, ни одной сверх", len(fake.calls), 50)
    check("local-active не запрашивался — опись оборвана раньше",
          sum(1 for c in fake.calls if c[0] == "local-active"), 0)


def test_pagination_49_full_50_partial_is_success():
    print("\n49 полных страниц + неполная 50-я — опись всё же успешна")
    fake = FakeAdsPower()
    fake.user_list_pages = {p: [make_item(str(p * 1000 + i), f"uid-{p}-{i}")
                                 for i in range(100)]
                            for p in range(1, 50)}
    fake.user_list_pages[50] = [make_item("last", "uid-last")]
    clock = FakeClock()
    A._reset_pace_state_for_tests()

    fetch = with_transport(fake, lambda: A.fetch_host_profiles(
        URL, sleep=clock.sleep, clock=clock.now))
    check("50 страниц прочитано и это успех", fetch.pages, 50)
    check("профили с последней неполной страницы собраны",
          any(p.serial == "last" for p in fetch.profiles), True)


# ─────────────────────────── профиль без serial_number ───────────────────────────

def test_profile_without_serial_is_dropped_but_still_counts_for_page_fullness():
    print("\nЭлемент без serial_number не попадает в profiles, но страница по числу "
          "элементов всё равно считается полной (Issue 2, код-ревью итерации 1)")
    fake = FakeAdsPower()
    page1 = [make_item(str(i), f"uid-{i}") for i in range(99)]
    page1.append(make_item("", "uid-no-serial"))         # 100-й элемент — без serial
    fake.user_list_pages = {
        1: page1,
        2: [make_item("100", "uid-100")],
    }
    clock = FakeClock()
    A._reset_pace_state_for_tests()

    fetch = with_transport(fake, lambda: A.fetch_host_profiles(
        URL, sleep=clock.sleep, clock=clock.now))

    check("страница 1 (100 сырых элементов) всё же считается полной — ушла страница 2",
          fetch.pages, 2)
    check("профиль без serial_number отсутствует в результате",
          any(p.adspower_user_id == "uid-no-serial" for p in fetch.profiles), False)
    check("остальные профили (99 + 1) собраны, элемент без serial просто пропущен",
          len(fetch.profiles), 100)


# ─────────────────────────── отказы AdsPower ───────────────────────────

def test_network_error_stops_the_whole_fetch():
    print("\nСетевая ошибка на первой же странице — вся опись неуспешна")
    fake = FakeAdsPower()
    fake.fail_paths = {"user/list"}
    clock = FakeClock()
    A._reset_pace_state_for_tests()

    raised = None
    try:
        with_transport(fake, lambda: A.fetch_host_profiles(
            URL, sleep=clock.sleep, clock=clock.now))
    except A.AdsPowerError as exc:
        raised = exc
    check_true("AdsPowerError брошен", raised is not None)
    check_true("причина названа по-русски",
               raised is not None and "не отвечает" in str(raised))


def test_code_nonzero_stops_the_whole_fetch():
    print("\ncode != 0 в ответе user/list — неуспех, текст AdsPower доезжает в исключении")
    fake = FakeAdsPower()
    fake.user_list_pages = {1: [make_item("1", "uid-1")]}
    fake.user_list_code = 1
    fake.user_list_msg = "invalid token"
    clock = FakeClock()
    A._reset_pace_state_for_tests()

    raised = None
    try:
        with_transport(fake, lambda: A.fetch_host_profiles(
            URL, sleep=clock.sleep, clock=clock.now))
    except A.AdsPowerError as exc:
        raised = exc
    check_true("AdsPowerError брошен", raised is not None)
    check_true("текст AdsPower виден", raised is not None and "invalid token" in str(raised))


def test_local_active_code_nonzero_also_fails():
    print("\ncode != 0 на local-active — тоже неуспех всей описи (не только user/list)")
    fake = FakeAdsPower()
    fake.user_list_pages = {1: [make_item("1", "uid-1")]}
    fake.local_active_code = 1
    fake.local_active_msg = "internal error"
    clock = FakeClock()
    A._reset_pace_state_for_tests()

    raised = None
    try:
        with_transport(fake, lambda: A.fetch_host_profiles(
            URL, sleep=clock.sleep, clock=clock.now))
    except A.AdsPowerError as exc:
        raised = exc
    check_true("AdsPowerError брошен", raised is not None)
    check_true("текст AdsPower виден", raised is not None and "internal error" in str(raised))


def test_bad_json_response_fails_cleanly():
    print("\nAdsPower ответил не JSON-ом — понятная ошибка, а не необработанное исключение")
    fake = FakeAdsPower()
    fake.bad_json_paths = {"user/list"}
    clock = FakeClock()
    A._reset_pace_state_for_tests()
    raised = None
    try:
        with_transport(fake, lambda: A.fetch_host_profiles(
            URL, sleep=clock.sleep, clock=clock.now))
    except A.AdsPowerError as exc:
        raised = exc
    check_true("AdsPowerError брошен, а не ValueError наружу", raised is not None)


def test_empty_and_malformed_url():
    print("\nПустой адрес и адрес без http(s):// — понятная ошибка до похода в сеть")
    fake = FakeAdsPower()
    clock = FakeClock()

    raised_empty = None
    try:
        with_transport(fake, lambda: A.fetch_host_profiles(
            "", sleep=clock.sleep, clock=clock.now))
    except A.AdsPowerError as exc:
        raised_empty = exc
    check_true("пустой адрес — ошибка", raised_empty is not None)
    check_true("текст про адрес AdsPower",
               raised_empty is not None and "адрес AdsPower" in str(raised_empty))
    check("сеть не трогалась", fake.calls, [])

    raised_bad = None
    try:
        with_transport(fake, lambda: A.fetch_host_profiles(
            "ftp://127.0.0.1:50325", sleep=clock.sleep, clock=clock.now))
    except A.AdsPowerError as exc:
        raised_bad = exc
    check_true("неверная схема — ошибка", raised_bad is not None)
    check("сеть всё ещё не трогалась", fake.calls, [])


# ─────────────────────────── белый список (мутационная проверка) ───────────────────────────

SECRET_MARKERS = ("s3cr3t-username", "s3cr3t-password", "grp-id-777",
                  "10.0.0.66", "socks5://login:pass@proxyhost:1080")


def test_secret_fields_never_leak_into_result_or_errors():
    print("\nМутация: секретные поля, подмешанные в фейковый ответ, не просачиваются никуда")
    fake = FakeAdsPower()
    fake.user_list_pages = {1: [
        make_item("46", "uid-46", name="Профиль 46",
                  username=SECRET_MARKERS[0], password=SECRET_MARKERS[1],
                  group_id=SECRET_MARKERS[2], ip=SECRET_MARKERS[3],
                  proxy={"host": "proxyhost", "login": "login", "password": "pass",
                        "url": SECRET_MARKERS[4]}),
    ]}
    fake.local_active_items = [
        {"user_id": "uid-46", "debug_port": "9222", "password": SECRET_MARKERS[1]},
    ]
    clock = FakeClock()
    A._reset_pace_state_for_tests()

    fetch = with_transport(fake, lambda: A.fetch_host_profiles(
        URL, sleep=clock.sleep, clock=clock.now))

    field_names = {f.name for f in dataclasses.fields(A.ProfileRecord)}
    check("ProfileRecord не заводит колонок для секретов",
          field_names, {"serial", "adspower_user_id", "name", "group_name",
                        "remark", "last_open_time"})

    profile = fetch.profiles[0]
    dumped = repr(profile) + repr(fetch)
    for marker in SECRET_MARKERS:
        check_true(f"секрет {marker!r} не попал в результат", marker not in dumped)

    # Мутация наоборот: доказать, что маркер действительно был бы виден, если бы его
    # не отсекал белый список — иначе проверка выше ничего не гарантирует.
    check_true("маркер пароля правда был в исходном фейковом ответе (иначе тест пуст)",
               SECRET_MARKERS[1] in repr(fake.user_list_pages))


def test_secret_fields_never_leak_even_when_whitelisted_fields_are_empty():
    print("\nМутация: секреты не всплывают как фолбэк, даже если сами белые поля пустые")
    # Все шесть полей белого списка нарочно пусты ("") — если бы реализация подменяла
    # пустое значение соседним секретным полем (`name or username` и т.п.), эта проверка
    # его бы поймала; предыдущий тест этого не видит, там `name`/`remark` непустые.
    fake = FakeAdsPower()
    fake.user_list_pages = {1: [
        {"serial_number": "", "user_id": "", "name": "", "group_name": "",
         "remark": "", "last_open_time": "",
         "username": SECRET_MARKERS[0], "password": SECRET_MARKERS[1],
         "group_id": SECRET_MARKERS[2], "ip": SECRET_MARKERS[3],
         "proxy": {"url": SECRET_MARKERS[4]}},
    ]}
    clock = FakeClock()
    A._reset_pace_state_for_tests()

    fetch = with_transport(fake, lambda: A.fetch_host_profiles(
        URL, sleep=clock.sleep, clock=clock.now))

    dumped = repr(fetch)
    for marker in SECRET_MARKERS:
        check_true(f"секрет {marker!r} не всплыл вместо пустого белого поля",
                   marker not in dumped)


def test_secret_fields_never_leak_into_exception_text():
    print("\nМутация: секреты не попадают и в текст исключения при отказе AdsPower")
    fake = FakeAdsPower()
    fake.user_list_code = 1
    fake.user_list_msg = "invalid token"
    # Секрет в самом элементе, который никогда не должен читаться при code != 0 —
    # страница вообще не парсится на профили при ошибке конверта.
    fake.user_list_pages = {1: [make_item("46", "uid-46", password=SECRET_MARKERS[1])]}
    clock = FakeClock()
    A._reset_pace_state_for_tests()

    raised = None
    try:
        with_transport(fake, lambda: A.fetch_host_profiles(
            URL, sleep=clock.sleep, clock=clock.now))
    except A.AdsPowerError as exc:
        raised = exc
    check_true("AdsPowerError брошен", raised is not None)
    check_true("секрет не попал в текст исключения",
               raised is not None and SECRET_MARKERS[1] not in str(raised))


def main() -> int:
    test_single_page_success()
    test_pagination_multiple_pages()
    test_pacing_one_request_per_second()
    test_pacing_persists_between_calls_for_same_host()
    test_pacing_does_not_couple_different_hosts()
    test_pagination_safety_limit_full_50th_page()
    test_pagination_49_full_50_partial_is_success()
    test_profile_without_serial_is_dropped_but_still_counts_for_page_fullness()
    test_network_error_stops_the_whole_fetch()
    test_code_nonzero_stops_the_whole_fetch()
    test_local_active_code_nonzero_also_fails()
    test_bad_json_response_fails_cleanly()
    test_empty_and_malformed_url()
    test_secret_fields_never_leak_into_result_or_errors()
    test_secret_fields_never_leak_even_when_whitelisted_fields_are_empty()
    test_secret_fields_never_leak_into_exception_text()

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
