#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка донора ДО постановки в план (19.09.2026).

Дефект. Ролик без субтитров узнавался только на шаге «текст»: ссылка уже вставлена,
слот канала уже занят, оператор уже отработал — и выпуск падает, оставив в расписании
дыру. Спросить то же самое можно в момент вставки, бесплатно и по тем же данным.

Проверяем три вещи, из которых вторая и третья дороже первой:

1. что проверка вообще отвечает на вопросы «есть ли субтитры, на каком языке, сколько
   знаков, сколько идёт ролик, как называется» и отклоняет то, что нельзя брать;
2. что она НИЧЕГО не оплачивает: ни одной единицы квоты YouTube Data API, длительность
   берётся из последней реплики дорожки, а скачанное ложится в общий кэш субтитров —
   второй раз тот же ролик не качается;
3. что мёртвый туннель и исчерпанная квота выхода — это «не смогли проверить», а НЕ
   «донор плохой», и что эти два исхода в ответе РАЗНЫЕ. Сложив их вместе, пульт начнёт
   браковать исправные ссылки ровно в ту минуту, когда лёг туннель, — этой ошибкой уже
   было похоронено сорок с лишним выпусков.

    .venv/bin/python tests/test_donor_precheck.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

TMP = tempfile.mkdtemp(prefix="donor-check-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DB"] = DB
# Сторож живости выходов (A1) в этом тесте не участвует: период 0 — он никуда не ходит.
os.environ["VS_SUBS_PROBE_PERIOD"] = "0"
os.environ["VS_SUBS_PROXIES"] = "direct"          # выход без туннеля, сети всё равно нет
os.environ["VS_SUBS_COOKIES"] = os.path.join(TMP, "нет-такого-файла.txt")
for name in ("VS_DONOR_MIN_MINUTES", "VS_DONOR_MAX_MINUTES", "VS_DONOR_MIN_CHARS",
             "VS_DONOR_CHARS_PER_MINUTE", "VS_DONOR_THIN_RATIO", "VS_DONOR_ESTIMATE_REJECTS",
             "VS_DONOR_CHECK_BUDGET_SEC", "VS_DONOR_CHECK_LIMIT", "VS_DONOR_CHECK_SUBS"):
    os.environ.pop(name, None)

import requests                                                    # noqa: E402

import pub_remote as PR                                            # noqa: E402
import pub_tags as PT                                              # noqa: E402

FAILS = []
URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
VID = "dQw4w9WgXcQ"
OTHER = "https://youtu.be/aBcDeFgHiJk"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


# ─────────────────── подставные субтитры YouTube (сети нет) ───────────────────

class NoTranscriptFound(Exception):
    """Имя класса — это то, по чему `_no_subtitles` опознаёт «дорожки нет»."""


class TooManyRequests(Exception):
    """429: адрес выхода исчерпал квоту YouTube. Лечится временем."""


class YouTubeRequestFailed(Exception):
    """«YouTube ответил не 200» — самая частая обёртка библиотеки над блокировкой адреса.

    Ротация и раньше считала это отказом выхода, а объяснение причины относило к
    мёртвому туннелю — то есть звало владельца чинить исправный туннель.
    """


class Track:
    def __init__(self, code="en", generated=True, parts=None):
        self.language_code = code
        self.is_generated = generated
        self.parts = parts or []

    def fetch(self):
        return list(self.parts)


class FakeApi:
    """Дорожки понарошку: что вернуть, решает сценарий."""

    def __init__(self, plan):
        self.plan = plan

    def list(self, vid):
        self.plan["seen"].append(vid)
        # Медленный выход: так выглядит полумёртвый туннель — порт слушает, ответа нет.
        # Без него проверку бюджета не построить: мгновенный отказ укладывается в любой
        # срок, а дорого обходится именно ожидание.
        if self.plan.get("slow"):
            time.sleep(float(self.plan["slow"]))
        boom = self.plan.get("raise")
        if boom is not None:
            raise boom
        return list(self.plan.get("tracks") or [])


def speech(minutes: float, chars: int) -> list:
    """Дорожка на заданную длину с заданным числом знаков.

    Реплики раскладываются встык до конца ролика: длину проверка считает по последней
    (`start + duration`), а число знаков — по всему тексту, поэтому врозь их задавать и
    надо, иначе сценарий «60 минут, 5 000 знаков» не построить.
    """
    total = max(1, int(minutes * 60))
    count = 20
    step = total / float(count)
    size = max(1, chars // count)
    parts = []
    for number in range(count):
        parts.append({"text": "слово " * (size // 6) + "а" * (size % 6),
                      "start": number * step, "duration": step})
    return parts


class Net:
    """Сеть понарошку. Всё, что не oembed, — провал теста: платного здесь быть не может."""

    def __init__(self):
        self.calls = []
        self.title = "Оригинальное название донора"
        self.status = 200

    def install(self):
        self.saved = requests.Session.request
        net = self

        class Resp:
            def __init__(self, status, payload):
                self.status_code = status
                self._payload = payload

            def json(self):
                return self._payload

        def spy(session, method, url, **kw):
            net.calls.append(url)
            if "googleapis.com" in url:
                FAILS.append("проверка донора сходила в платный YouTube Data API")
            return Resp(net.status, {"title": net.title})

        requests.Session.request = spy

    def remove(self):
        requests.Session.request = self.saved

    def oembed_calls(self):
        return [u for u in self.calls if "oembed" in u]


def install_api(plan):
    """Подставить дорожки в настоящий `pub_remote.donor_subs`, минуя сеть."""
    stub = types.ModuleType("youtube_transcript_api")
    stub.YouTubeTranscriptApi = object
    sys.modules["youtube_transcript_api"] = stub
    PR._transcript_api = lambda proxy: FakeApi(plan)


def fresh_db():
    """Чистый кэш субтитров: каждый сценарий начинается с «ролика не видели»."""
    if os.path.exists(DB):
        os.remove(DB)


def set_exits(names):
    """Подставить список выходов субтитров и стереть память об отдыхе.

    На бою их у владельца больше двух десятков, и все сценарии про «сколько выходов
    сожгла одна ссылка» видны только на нескольких выходах.
    """
    PR.SUBS_PROXIES = list(names)
    PR._subs_rest_until.clear()


class NoAlter:
    """Соединение с базой, которому не дают дописать колонки.

    Так выглядит занятая база (SQLITE_BUSY), файл, открытый только на чтение, и чужая
    длинная транзакция: `CREATE TABLE IF NOT EXISTS` проходит, `ALTER TABLE ADD COLUMN`
    — нет. Подменяем именно соединение, а не саму досыпку: проверять надо то, что
    случится с кэшем ПОСЛЕ неудачной миграции, а не то, как мы её зовём.
    """

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *args):
        if "ALTER TABLE" in sql.upper():
            raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def main() -> int:
    net = Net()
    net.install()
    try:
        # ─────────────── 1. чистая таблица вердиктов, без сети и базы ───────────────
        print("\nВердикт по фактам — чистая функция")
        lim = PT.donor_limits()
        base = {"video_id": VID, "has_subs": True, "chars": 30000, "minutes": 30.0,
                "length_from": "субтитры", "title": "есть"}
        check("нормальный донор — ставим", PT.donor_verdict(base, lim)["verdict"], "ok")
        check("не ссылка — отказ",
              PT.donor_verdict({"video_id": ""}, lim)["reason"], "not_youtube")
        check("и ставить нельзя", PT.donor_verdict({"video_id": ""}, lim)["can_plan"], False)

        no_subs = dict(base, has_subs=False, chars=0)
        check("нет субтитров — отказ", PT.donor_verdict(no_subs, lim)["reason"], "no_subs")
        check("и ставить нельзя", PT.donor_verdict(no_subs, lim)["can_plan"], False)

        thin = dict(base, chars=900)
        check("обрывок дорожки — отказ", PT.donor_verdict(thin, lim)["reason"], "short_text")

        long_real = dict(base, minutes=150.0)
        check("измеренные 150 минут — отказ",
              PT.donor_verdict(long_real, lim)["reason"], "too_long")
        long_guess = dict(base, minutes=150.0, length_from="оценка по знакам")
        check("те же 150 минут ОЦЕНКОЙ — не отказ",
              PT.donor_verdict(long_guess, lim)["verdict"], "warn")
        check("и это сказано причиной",
              PT.donor_verdict(long_guess, lim)["reason"], "long_estimate")
        check("но по настройке отказ возможен",
              PT.donor_verdict(long_guess, dict(lim, estimate_rejects=1))["verdict"], "reject")

        short = dict(base, minutes=10.0, chars=9000)
        check("короче границы владельца — предупреждение",
              PT.donor_verdict(short, lim)["verdict"], "warn")
        check("ставить всё равно можно", PT.donor_verdict(short, lim)["can_plan"], True)
        check("и причина названа", PT.donor_verdict(short, lim)["reason"], "short_video")

        sparse = dict(base, minutes=60.0, chars=5000)
        check("дорожка неполная — предупреждение",
              PT.donor_verdict(sparse, lim)["reason"], "thin_text")
        check("без названия — предупреждение",
              PT.donor_verdict(dict(base, title=""), lim)["reason"], "no_title")

        for reason in ("exits_quota", "exit_dead", "budget", "off", "no_net", "ours"):
            got = PT.donor_verdict(dict(base, fail=reason), lim)
            check(f"«{reason}» — это «не проверили», а не отказ", got["verdict"], "unknown")
            check(f"«{reason}» не мешает поставить", got["can_plan"], True)
        check("нечего сказать — тоже не отказ",
              PT.donor_verdict({"video_id": VID}, lim)["verdict"], "unknown")

        # ─────────────── 2. живой путь через pub_remote: хороший донор ───────────────
        print("\nХороший донор: спрашиваем один раз, помним всё")
        fresh_db()
        plan = {"seen": [], "tracks": [Track("pl", True, speech(35, 34000))]}
        install_api(plan)
        got = PT.check_donor(URL)
        check("вердикт", got["verdict"], "ok")
        check("субтитры есть", got["has_subs"], True)
        check("язык дорожки", got["lang"], "pl")
        check("язык по-русски", got["lang_ru"], "Польский")
        check("знаков посчитано", got["chars"] > 30000, True)
        check("длина взята из дорожки", got["length_from"], "субтитры")
        check("и она похожа на правду", 30 < got["minutes"] < 40, True)
        check("название добыто", got["title"], "Оригинальное название донора")
        check("и именно из сети", got["title_from"], "сеть")
        check("платного не было", got["paid"], False)
        check("в YouTube ходили один раз", len(plan["seen"]), 1)
        check("за названием — тоже один раз", len(net.oembed_calls()), 1)

        print("\nВторая проверка того же ролика бесплатна")
        plan["seen"] = []
        before = len(net.oembed_calls())
        again = PT.check_donor(URL)
        check("вердикт тот же", again["verdict"], "ok")
        check("в YouTube за субтитрами не ходили", plan["seen"], [])
        check("ответ помечен как из кэша", again["cached"], True)
        check("и за названием тоже не ходили", len(net.oembed_calls()), before)
        check("название взято из кэша", again["title_from"], "кэш")
        check("язык помнится", again["lang"], "pl")
        check("длина помнится", round(again["minutes"]) == round(got["minutes"]), True)

        # ─────────────── 3. мусор на входе: сеть не трогаем вовсе ───────────────
        print("\nНе-ссылка на ролик не стоит ни одного запроса")
        plan["seen"] = []
        before = len(net.calls)
        # Ссылка на КАНАЛ — настоящий случай вставки: хост правильный, ролика нет.
        # Мусорная строка тем и слабее как проверка, что за неё и так никто не спросит.
        bad = PT.check_donor("https://www.youtube.com/@donor/videos")
        check("отказ", bad["verdict"], "reject")
        check("причина", bad["reason"], "not_youtube")
        check("в сеть не ходили вообще", len(net.calls), before)
        check("и за субтитрами тоже", plan["seen"], [])
        check("мусор — тоже отказ",
              PT.check_donor("просто текст, а не ссылка")["reason"], "not_youtube")

        # ─────────────── 4. у ролика действительно нет дорожки ───────────────
        print("\nНет субтитров — это приговор ролику")
        fresh_db()
        plan = {"seen": [], "raise": NoTranscriptFound("нет дорожек")}
        install_api(plan)
        none = PT.check_donor(OTHER)
        check("отказ", none["verdict"], "reject")
        check("причина", none["reason"], "no_subs")
        check("ставить нельзя", none["can_plan"], False)
        check("и это именно проверенный ответ", none["checked"], True)

        # ─────────────── 5. мёртвый туннель ≠ исчерпанная квота ───────────────
        print("\nНаша беда не делает донор плохим")
        fresh_db()
        plan = {"seen": [], "raise": requests.exceptions.ConnectTimeout("туннель молчит")}
        install_api(plan)
        dead = PT.check_donor(OTHER)
        check("не проверили", dead["verdict"], "unknown")
        check("виноват выход", dead["reason"], "exit_dead")
        check("ссылку всё равно ставим", dead["can_plan"], True)
        check("и честно говорим, что не проверяли", dead["checked"], False)
        check("про туннель сказано словами", "туннель" in dead["why"], True)

        fresh_db()
        PR._subs_rest_until.clear()
        plan = {"seen": [], "raise": TooManyRequests("429")}
        install_api(plan)
        quota = PT.check_donor(OTHER)
        check("тоже не проверили", quota["verdict"], "unknown")
        check("но причина ДРУГАЯ", quota["reason"], "exits_quota")
        check("две беды не смешаны", quota["reason"] == dead["reason"], False)
        check("про квоту сказано словами", "квот" in quota["why"], True)
        check("ссылку ставим и здесь", quota["can_plan"], True)
        PR._subs_rest_until.clear()

        print("\nВыходы отдыхают — спрашивать некого, но донор ни при чём")
        fresh_db()
        plan = {"seen": [], "tracks": [Track("ru", True, speech(30, 30000))]}
        install_api(plan)
        PR._subs_rest_until[""] = time.time() + 600      # единственный выход на отдыхе
        rested = PT.check_donor(OTHER)
        PR._subs_rest_until.clear()
        check("не проверили", rested["verdict"], "unknown")
        check("причина — квота выходов", rested["reason"], "exits_quota")
        check("в YouTube не ходили", plan["seen"], [])

        # ─────────────── 6. настройки, выключающие шаг субтитров ───────────────
        print("\nВыключенная проверка честно называет себя")
        fresh_db()
        plan["seen"] = []
        off = PT.check_donor(OTHER, limits=dict(PT.donor_limits(), subs=0))
        check("не проверили", off["verdict"], "unknown")
        check("причина", off["reason"], "off")
        check("в YouTube не ходили", plan["seen"], [])

        print("\nОтвет только по кэшу — без сети")
        fresh_db()
        plan["seen"] = []
        offline = PT.check_donor(OTHER, net=False)
        check("не проверили", offline["verdict"], "unknown")
        check("причина", offline["reason"], "no_net")
        check("в YouTube не ходили", plan["seen"], [])

        # ─────────────── 7. бюджет времени на пачку ───────────────
        print("\nПачка ссылок: бюджет и предел")
        fresh_db()
        plan = {"seen": [], "tracks": [Track("en", True, speech(30, 30000))]}
        install_api(plan)
        pack = PT.check_donors([URL, OTHER], limits=dict(PT.donor_limits(), limit=1))
        check("первая проверена", pack["items"][0]["verdict"], "ok")
        check("вторая — нет", pack["items"][1]["verdict"], "unknown")
        check("и причина названа", pack["items"][1]["reason"], "over_limit")
        check("за субтитрами ходили один раз", len(plan["seen"]), 1)
        check("порядок сохранён", [i["url"] for i in pack["items"]], [URL, OTHER])
        check("сводка считается", pack["summary"]["ok"], 1)

        fresh_db()
        plan["seen"] = []
        spent = PT.check_donors([URL, OTHER],
                                limits=dict(PT.donor_limits(), budget_sec=0.000001))
        check("бюджет кончился сразу", spent["items"][0]["reason"], "budget")
        check("и вторая не проверена", spent["items"][1]["reason"], "budget")
        check("в сеть за субтитрами не ходили", plan["seen"], [])
        check("ставить обе всё равно можно",
              [i["can_plan"] for i in spent["items"]], [True, True])

        print("\nКончившийся бюджет не прячет то, что уже известно")
        plan["seen"] = []
        PT.check_donor(URL)                       # положили ролик в кэш
        plan["seen"] = []
        known = PT.check_donors([URL], limits=dict(PT.donor_limits(), budget_sec=0.000001))
        check("ответ по кэшу дан", known["items"][0]["verdict"], "ok")
        check("и он помечен кэшем", known["items"][0]["cached"], True)
        check("в сеть при этом не ходили", plan["seen"], [])

        # ─────────────── 8. пороги живут в настройке ───────────────
        print("\nПороги — настройка, а не правило в коде")
        os.environ["VS_DONOR_MAX_MINUTES"] = "10"
        lim = PT.donor_limits()
        check("окружение услышано", lim["max_minutes"], 10.0)
        check("и подписано откуда", lim["source"]["max_minutes"], "окружение")
        check("ролик на 35 минут теперь длинный",
              PT.donor_verdict(dict(base, minutes=35.0), lim)["reason"], "too_long")
        os.environ["VS_DONOR_MAX_MINUTES"] = "мусор"
        check("мусор в настройке — это «настройки нет»",
              PT.donor_limits()["max_minutes"], 90.0)
        os.environ.pop("VS_DONOR_MAX_MINUTES")

        # ─────────────── 9. длительность без «seconds» в кэше ───────────────
        print("\nСтарая строка кэша: длины не знаем, но и не врём")
        guess = PT.check_donor(
            URL, subs=lambda link, net=True: {"text": "а" * 120000, "chars": 120000,
                                              "lang": "ru", "seconds": 0, "cached": True},
            title_fetch=lambda link: "название")
        print("\nПустой ответ — это наша поломка, а не плохой донор")
        empty = PT.check_donor(URL, subs=lambda link, net=True: {"text": "", "chars": 0},
                               title_fetch=lambda link: "название")
        check("не проверили", empty["verdict"], "unknown")
        check("виноваты мы", empty["reason"], "ours")
        check("ссылку ставим", empty["can_plan"], True)

        check("длина оценена", guess["length_from"], "оценка по знакам")
        check("незнакомый язык не называем русским",
              PT.check_donor(URL, subs=lambda link, net=True: {
                  "text": "а" * 30000, "chars": 30000, "lang": "zz",
                  "seconds": 1800, "cached": True},
                  title_fetch=lambda link: "название")["lang_ru"], "")
        check("и оценка честно велика", guess["minutes"] > 90, True)
        check("но по оценке не отклоняем", guess["verdict"], "warn")
        check("причина названа", guess["reason"], "long_estimate")

        # ────── 10. неудавшаяся досыпка колонок не убивает кэш целиком ──────
        # Дефект: колонки lang/seconds/title/title_at дописывались «всё или ничего», а
        # провал ALTER глотался. После него чтение просило несуществующие колонки и
        # возвращало «не знаем», запись падала в тот же except — кэш умирал молча, и
        # каждый выпуск шёл за субтитрами в сеть заново. Это ровно та нагрузка, из-за
        # которой YouTube и закрывает адрес.
        print("\nКолонки не дописались — кэш всё равно работает")
        fresh_db()
        set_exits([""])
        plan = {"seen": [], "tracks": [Track("ru", True, speech(30, 30000))]}
        install_api(plan)
        saved_db = PR._cabinet_db
        PR._cabinet_db = lambda: NoAlter(sqlite3.connect(DB, timeout=20))
        try:
            first = PT.check_donor(URL)
            check("расшифровка добыта", first["has_subs"], True)
            check("в сеть сходили один раз", len(plan["seen"]), 1)
            look = sqlite3.connect(DB)
            cols = {r[1] for r in look.execute("PRAGMA table_info(subs_cache)")}
            look.close()
            check("новых колонок в таблице и правда нет", "lang" in cols, False)
            again = PT.check_donor(URL)
            check("второй раз отвечаем из кэша", again["cached"], True)
            check("и в сеть за тем же роликом не ходили", len(plan["seen"]), 1)
            check("знаки на месте", again["chars"], first["chars"])
            # Чего в трёх старых колонках нет, того мы не выдумываем: язык и длина
            # честно неизвестны, и длина считается оценкой по знакам.
            check("язык не выдуман", again["lang"], "")
            check("длина названа оценкой", again["length_from"], "оценка по знакам")
            check("и вердикт по ссылке всё равно есть", again["can_plan"], True)
        finally:
            PR._cabinet_db = saved_db

        # ────── 11. бюджет обрывает уже начатый поход, а не только следующий ──────
        print("\nБюджет пачки обрывает перебор выходов")
        fresh_db()
        set_exits(["socks5h://127.0.0.1:%d" % (1080 + n) for n in range(5)])
        plan = {"seen": [], "slow": 0.08,
                "raise": requests.exceptions.ConnectTimeout("туннель молчит")}
        install_api(plan)
        started = time.time()
        pack = PT.check_donors([URL], limits=dict(PT.donor_limits(), budget_sec=0.15,
                                                  subs_exits=0))
        spent = time.time() - started
        check("опрошены не все выходы", len(plan["seen"]) < 5, True)
        check("хотя бы один выход опрошен", len(plan["seen"]) >= 1, True)
        check("уложились примерно в бюджет", spent < 0.5, True)
        check("и это названо «не успели», а не бедой выхода",
              pack["items"][0]["reason"], "budget")
        check("ссылку всё равно ставим", pack["items"][0]["can_plan"], True)
        PR._subs_rest_until.clear()

        # ────── 12. предпросмотр не жжёт все выходы разом ──────
        # Выходы — общий ресурс с ночным шагом «текст», и отказ каждого отправляет его
        # отдыхать для ВСЕЙ системы. Человек, листающий два десятка ссылок в плохой
        # день, не должен уводить на отдых весь список перед ночным прогоном.
        print("\nУ предпросмотра свой потолок по выходам")
        fresh_db()
        set_exits(["socks5h://127.0.0.1:%d" % (1080 + n) for n in range(5)])
        plan = {"seen": [], "raise": TooManyRequests("429")}
        install_api(plan)
        capped = PT.check_donor(OTHER, limits=dict(PT.donor_limits(), subs_exits=2))
        check("опрошено ровно столько выходов, сколько разрешено", len(plan["seen"]), 2)
        check("на отдых ушли только они", len(PR._subs_rest_until), 2)
        check("причина названа квотой выходов", capped["reason"], "exits_quota")
        check("и ссылку это не отклоняет", capped["can_plan"], True)
        PR._subs_rest_until.clear()

        # ────── 13. «YouTube ответил не 200» — это квота, а не мёртвый туннель ──────
        print("\nБлокировка адреса не выдаётся за поломку туннеля")
        fresh_db()
        set_exits([""])
        plan = {"seen": [], "raise": YouTubeRequestFailed("request failed")}
        install_api(plan)
        blocked = PT.check_donor(OTHER)
        check("это отказ выхода", blocked["verdict"], "unknown")
        check("и причина — квота, а не туннель", blocked["reason"], "exits_quota")
        check("владельца чинить туннель не зовут", "туннель" in blocked["why"], False)
        check("сама функция разбора говорит то же",
              PR._exit_fault_kind(YouTubeRequestFailed("x")), PR.DOWN_QUOTA)
        PR._subs_rest_until.clear()

        # ────── 14. пароль прокси не уезжает на экран ──────
        print("\nПароль выхода не попадает в ответ проверки")
        fresh_db()

        def leaky(link, net=True):
            raise PR._subs_down(PR.DOWN_TUNNEL,
                                "socks5h://ads:s3cret@10.0.0.7:1080 не отвечает")

        leak = PT.check_donor(URL, subs=leaky, title_fetch=lambda link: "название")
        check("пароля в ответе нет", "s3cret" in repr(leak), False)
        check("но выход узнаваем", "10.0.0.7:1080" in leak["fail_text"], True)
        check("и причина не потерялась", leak["reason"], "exit_dead")

        # ────── 15. чужой хост с 11 символами в хвосте — не ролик YouTube ──────
        print("\nОдиннадцать символов в конце чужой ссылки — не ролик")
        fresh_db()
        plan = {"seen": [], "tracks": [Track("ru", True, speech(30, 30000))]}
        install_api(plan)
        before = len(net.calls)
        for link in ("https://vimeo.com/12345678901",
                     "https://rutube.ru/video/abcdefghijk"):
            alien = PT.check_donor(link)
            check(f"отказ по {link.split('/')[2]}", alien["reason"], "not_youtube")
            check("и ставить нельзя", alien["can_plan"], False)
        check("в дефицитные выходы не ходили", plan["seen"], [])
        check("и в сеть вообще", len(net.calls), before)
        check("а настоящая ссылка YouTube по-прежнему проверяется",
              PT.check_donor(URL)["verdict"], "ok")

        # ────── 16. ноль в поле бюджета значит «не ждать» ──────
        print("\nНоль бюджета — это «не ждать», а не «без потолка»")
        fresh_db()
        plan["seen"] = []
        before = len(net.calls)
        nowait = PT.check_donor(OTHER, limits=dict(PT.donor_limits(), budget_sec=0))
        check("в сеть за субтитрами не ходили", plan["seen"], [])
        check("и за названием тоже", len(net.calls), before)
        check("причина названа", nowait["reason"], "budget")
        check("ссылку всё равно ставим", nowait["can_plan"], True)

        # ────── 17. срок жизни названия — такая же настройка, как остальные ──────
        print("\nДесятый порог виден на экране настроек, а не только в systemd")
        check("порог есть в списке", "title_ttl_sec" in PT.donor_limits(), True)
        check("и подписан источником",
              PT.donor_limits()["source"]["title_ttl_sec"], "умолчание")
        fresh_db()
        plan["seen"] = []
        PT.check_donor(URL)                        # запомнили название
        before = len(net.oembed_calls())
        check("по умолчанию название берётся из кэша",
              PT.check_donor(URL)["title_from"], "кэш")
        check("и в сеть за ним не ходили", len(net.oembed_calls()), before)
        os.environ["VS_DONOR_TITLE_TTL"] = "0"
        try:
            check("окружение услышано", PT.donor_limits()["title_ttl_sec"], 0.0)
            check("и подписано откуда",
                  PT.donor_limits()["source"]["title_ttl_sec"], "окружение")
            check("с нулевым сроком название спрашивают заново",
                  PT.check_donor(URL)["title_from"], "сеть")
            check("то есть в oembed сходили", len(net.oembed_calls()) > before, True)
        finally:
            os.environ.pop("VS_DONOR_TITLE_TTL", None)

        # ────── 18. короткая дорожка — это отказ по длине, а не «виноваты мы» ──────
        # Проверка зовёт `donor_subs(strict=False)` ради ЧИСЛА знаков: она показывает
        # его человеку и судит по своим порогам. Вернись сюда строгий режим — обрывок
        # приехал бы причиной «сбой на нашей стороне», то есть unknown, и молча уехал
        # бы в план.
        print("\nОбрывок дорожки: число знаков настоящее, вердикт — отказ")
        fresh_db()
        set_exits([""])
        plan = {"seen": [], "tracks": [Track("ru", True, [{"text": "а" * 50,
                                                           "start": 0.0,
                                                           "duration": 3.0}])]}
        install_api(plan)
        thin_real = PT.check_donor(URL)
        check("знаки посчитаны настоящие", thin_real["chars"], 50)
        check("дорожка признана существующей", thin_real["has_subs"], True)
        check("вердикт — отказ", thin_real["verdict"], "reject")
        check("причина — обрывок", thin_real["reason"], "short_text")
        check("и это не «не смогли проверить»", thin_real["fail"], "")
        check("обрывок в кэш не лёг", PR._subs_cache_get(VID), "")

        print("\nПриговоры ролику размечены, а не свалены в «виноваты мы»")
        now_lim = PT.donor_limits()
        thin_kind = PT._donor_subs_failure(PR._subs_failed(PR.FAIL_THIN, "клок"), PR)
        check("обрывок из строгого режима — отказ по длине",
              PT.donor_verdict(dict({"video_id": VID}, **thin_kind), now_lim)["reason"],
              "short_text")
        not_yt = PT._donor_subs_failure(PR._subs_failed(PR.FAIL_NOT_YT, "не ссылка"), PR)
        check("неразобранная ссылка — не ролик",
              PT.donor_verdict(dict({"video_id": VID}, **not_yt), now_lim)["reason"],
              "not_youtube")
        budget_kind = PT._donor_subs_failure(PR._subs_down(PR.DOWN_BUDGET, "не успели"), PR)
        check("кончившийся срок — «не проверили»", budget_kind["fail"], "budget")
    finally:
        net.remove()
        PR._subs_rest_until.clear()

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
